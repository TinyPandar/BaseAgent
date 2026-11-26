"""job_wait / job_settle: wait quietly for one background job, and say what became of its goal.

``job_wait`` blocks inside one tool call and looks only at local state (no model call, no new process):
it returns when the job ends, when this agent gets a mid-turn message, when the wait's own time limit
passes, or when the turn stops. Returning never stops or restarts the job. Three separate actions:
ending a wait (``job_wait`` returns), dropping the goal (``job_settle`` with ``dropped``; the process is
untouched) and stopping the process (``job_kill``).

After a newer user input the agent must first answer and say why the job is still needed. The answer
counts only once a frontend has shown it (``baseagent.agent.interim``); text in the model's output is
not enough. A wait that was cut off (Ctrl+C, process exit) is closed with current facts on resume and
waiting is then held until the next user input; an ordinary interjection never holds anything.

It writes the job's link (confirmation, declared purpose), so it is not ``retry_safe``: never retried,
never run in parallel, never offered to read-only workers.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from . import job_links
from .jobs import LOST, TERMINAL, UNKNOWN
from .result import ErrorCode, ToolFailure, ToolResult

WAIT_MIN, WAIT_DEFAULT, WAIT_MAX = 30, 600, 3600
SLICE = 0.1                 # a chat Ctrl+C reaches this thread within one slice
CANCEL_INTERVAL = 0.5       # the durable cancellation check reads SQLite
REFRESH_INTERVAL = 1.0      # jobs of an earlier process are probed, not waited on
RESULT_BYTES = 3500         # below the default max_tool_context_bytes (4000), so the result is not clipped
SHOWING, WAITING = "showing_answer", "waiting"

FINISHED, ENDED_UNKNOWN = "finished", "ended_unknown"
CANCELLED, STOPPED, UNKNOWN_STATE = "cancelled", "wait_stopped", "unknown_state"
INTERRUPTED, EXPIRED = "interrupted", "wait_expired"
CONFIRM_FIRST, GOAL_DROPPED, HELD, NOT_RESPONSIBLE = "confirm_first", "goal_dropped", "held", "not_responsible"
# Outcomes that tell the model the job's final state.
TOLD = (FINISHED, ENDED_UNKNOWN)
# Outcomes after which waiting is held until the next user input (only reachable by resuming a stopped turn).
HOLDING = (CANCELLED, STOPPED)
SUCCEEDED = {"exited": True, "failed": False, "killed": False}

MESSAGES = {
    FINISHED: "The job ended. Judge the result by its status and exit code, its purpose and the user's latest words.",
    ENDED_UNKNOWN: "The process is gone but its exit status was never recorded. This is neither success nor failure: "
                   "check its effects before deciding anything; do not simply run it again.",
    CANCELLED: "The turn is stopping, so waiting ended. The job itself was not stopped by this.",
    STOPPED: "The turn stopped while this wait was in progress. Waiting does not restart by itself: report the job's "
             "state and end the turn; after the next user message you may wait again if it is still needed.",
    UNKNOWN_STATE: "It cannot be verified that the recorded process is still this job, so there is nothing to wait for. "
                   "Not a failure: do not run it again; tell the user what is unknown.",
    INTERRUPTED: "A message for you arrived; waiting ended and the job keeps running. The message follows this result. "
                 "Answer it first. If the job is still needed, call job_wait again in the same message as your answer, "
                 "with still_needed. If its goal no longer holds, use job_settle (dropped).",
    EXPIRED: "This wait's time limit passed without a result. The job itself did not time out or fail.",
    CONFIRM_FIRST: "Not waiting yet: the user said something new after this job's goal was last confirmed.",
    GOAL_DROPPED: "This job's goal was dropped, so it is not waited for. Its result, if any, is information only.",
    HELD: "Not waiting: an earlier wait in this turn was cut off. Report the job's state and end the turn; waiting is "
          "possible again after the next user message.",
    NOT_RESPONSIBLE: "Another agent is responsible for this job and gets its result; job_output can still read the log.",
}

_on_tick = None   # tests: called with the job id at the start of every look at the local state


@dataclass(frozen=True)
class Facts:
    """Everything one look at the local state can see."""

    terminal: bool = False
    lost: bool = False
    unknown: bool = False
    closing: bool = False
    cancelled: bool = False
    interjected: bool = False
    expired: bool = False


def decide(facts):
    """The one outcome of a look, or None to keep waiting. A known end wins over a message or the clock:
    the message stays in the inbox and is delivered before the next model call anyway."""
    if facts.closing:
        return CANCELLED       # session shutdown kills the job; no further model call should follow
    if facts.terminal:
        return ENDED_UNKNOWN if facts.lost else FINISHED
    if facts.cancelled:
        return CANCELLED
    if facts.unknown:
        return UNKNOWN_STATE
    if facts.interjected:
        return INTERRUPTED
    if facts.expired:
        return EXPIRED
    return None


# -- results ------------------------------------------------------------------------------------------
def _facts_of(info, *, stable=False):
    value = {key: info.get(key) for key in ("job_id", "status", "exit_code", "pid")}
    if not stable:
        value.update({key: info.get(key) for key in ("duration_seconds", "log_bytes", "log_truncated")})
    if info.get("error"):
        value["error"] = job_links.clip(info["error"], 200)
    value["command"] = job_links.clip(" ".join(info["argv"]), 160)
    return value


def result(manager, job_id, outcome, *, waited=None, inputs=(), extra=None, with_log=False):
    """The tool result. A wait that only refuses carries no changing numbers, so an identical refused call
    is recognised as a repeated round by the existing loop guard."""
    info = manager.describe(job_id)
    link = manager.link_of(job_id)
    stable = waited is None and not with_log
    data = {"outcome": outcome, "message": MESSAGES[outcome], "job_succeeded": SUCCEEDED.get(info["status"]),
            "job": _facts_of(info, stable=stable), "brief": job_links.brief(link)}
    if waited is not None:
        data["waited_seconds"] = round(waited, 1)
    newer = _newer(inputs, link)
    if newer:
        data["user_said_since_confirmed"] = [job_links.clip(text, 150) for _, text in newer[-2:]]
    data.update(extra or {})
    if with_log:
        tail = manager.output(job_id, tail=600)["content"] if info["log_bytes"] else ""
        data["log_tail"] = tail
        if info["log_bytes"] and not tail:
            data["log_note"] = "the log file is missing or empty; its content is not available"
        elif info["log_truncated"]:
            data["log_note"] = "the log is truncated: later output was dropped at the size limit"
    return ToolResult(data=_fit(data))


def _fit(data):
    """Keep the result small enough that the model sees it whole; say what was cut."""
    def size():
        return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    if size() > RESULT_BYTES and "log_tail" in data:
        while size() > RESULT_BYTES and len(data["log_tail"]) > 80:
            data["log_tail"] = data["log_tail"][len(data["log_tail"]) // 2:]
        data["log_cut_here"] = True
    if size() > RESULT_BYTES:
        data["brief"].pop("user_words_at_start", None)
    return data


def _newer(inputs, link):
    confirmed = link["goal"]["confirmed_inputs"]
    # A job whose start was never tied to a conversation must be confirmed once.
    return list(inputs)[confirmed:] if confirmed is not None else list(inputs)[-1:]


# -- conversation facts ---------------------------------------------------------------------------------
def told(messages, job_id):
    """Index of the last message that gave this conversation the job's final state, or None."""
    from baseagent.agent.inbox import job_in_notice
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        content = message.get("content")
        if not isinstance(content, str) or job_id not in content:
            continue
        if message.get("role") == "user" and job_in_notice(content, job_id):
            return index
        if message.get("role") == "tool":
            data = _wait_data(content)
            if data and data["outcome"] in TOLD and (data.get("job") or {}).get("job_id") == job_id:
                return index
    return None


def answered(messages, index):
    """True when the model has replied after message ``index``, i.e. it has received it."""
    return any(message.get("role") == "assistant" for message in messages[index + 1:])


def _wait_data(content):
    try:
        data = json.loads(content).get("data")
    except (AttributeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("outcome"), str) else None


def _held(state):
    from baseagent.agent.inbox import direct_inputs
    last_input = max((index for index, _ in direct_inputs(state.messages)), default=-1)
    names = {}
    for index in range(state.turn_start, len(state.messages)):
        message = state.messages[index]
        for call in message.get("tool_calls") or ():
            names[call["id"]] = call["function"]["name"]
        if index > last_input and message.get("role") == "tool" and names.get(message.get("tool_call_id")) == "job_wait":
            data = _wait_data(message["content"])
            if data and data["outcome"] in HOLDING:
                return True
    return False


def _is_worker(state):
    return (getattr(state, "metadata", None) or {}).get("session_mode") == "worker"


def _answers(context, since):
    """Show the agent's replies after the newest user input (idempotent) and return their receipts.

    None when there is nothing that can show them.
    """
    from baseagent.agent.interim import InterimAnswerMiddleware
    layer = next((item for item in (context.middleware or ()) if isinstance(item, InterimAnswerMiddleware)), None)
    if layer is None or not layer.answers.available:
        return None
    return layer.show_since(context.state, since)


def _inboxes(context):
    from baseagent.agent.inbox import InboxMiddleware
    return [layer.inbox for layer in (context.middleware or ()) if isinstance(layer, InboxMiddleware)]


def _interjected(inboxes):
    """A mid-turn message for this agent itself: the user's line, or /tell and message_worker for a worker.
    /queue lines, worker results and job notices never end a wait."""
    from baseagent.agent.inbox import LEAD, STEER, USER
    return any(inbox.pending(mode=STEER, source=source) for inbox in inboxes for source in (USER, LEAD))


# -- the tools ------------------------------------------------------------------------------------------
def job_wait(manager, job_id, timeout_seconds=None, still_needed=None, declared=None, *, context=None):
    from baseagent.agent.inbox import direct_inputs
    job = manager._get(job_id)
    state = context.state if context is not None else None
    agent = getattr(state, "session_id", None)
    if not manager.responsible(job, agent):
        return result(manager, job_id, NOT_RESPONSIBLE, extra={"responsible_agent": job.link["agent_session"]
                                                               or job.owner})
    inputs = direct_inputs(state.messages) if state is not None else []
    manager.describe(job_id)                      # refresh a job of an earlier process before the first look
    if job.status in TERMINAL and not manager.closing:
        outcome = decide(Facts(terminal=True, lost=job.status == LOST))
        return result(manager, job_id, outcome, waited=0.0, inputs=inputs, with_log=True)
    if state is not None and _held(state):
        return result(manager, job_id, HELD)
    link = manager.link_of(job_id)
    newer = _newer(inputs, link)
    reason = (still_needed or "").strip()
    if link["goal"]["standing"] == job_links.DROPPED and not (newer and reason):
        return result(manager, job_id, GOAL_DROPPED, inputs=inputs)
    receipts = []
    if newer:
        missing = [] if reason else ["still_needed: say why this job is still needed after what the user said"]
        if not _is_worker(state):
            # The worker's own reply is not what the user reads; the main agent answers the user.
            receipts = _answers(context, newer[-1][0])
            if receipts is None:
                missing.append("no frontend can show your answer before waiting: finish this turn with the answer instead")
            elif not receipts:
                missing.append("answer the user: put the answer as text in the same message as this call")
        if missing:
            return result(manager, job_id, CONFIRM_FIRST, inputs=inputs, extra={"missing": missing})
    limit = float(WAIT_DEFAULT if timeout_seconds is None else timeout_seconds)
    remaining = context.remaining_seconds() if context is not None and getattr(context, "remaining_seconds", None) else None
    capped = remaining is not None and remaining < limit
    limit = max(0.0, min(limit, remaining) if capped else limit)

    def confirm():
        manager.set_goal(job_id, job_links.ACTIVE, inputs=len(inputs) if state is not None else None,
                         reason=reason or link["goal"]["reason"], declared=declared)

    return _wait(manager, job, agent, context, inputs, receipts or [], limit, capped, confirm)


def _wait(manager, job, agent, context, inputs, receipts, limit, capped, confirm):
    cancellation = getattr(context, "cancellation", None)
    inboxes = _inboxes(context) if context is not None and getattr(context, "middleware", None) else []
    key = f"{agent}:{getattr(context, 'call_id', None)}:{job.id}"
    begin = time.monotonic()
    waiting_since = None
    cancelled, next_cancel, next_refresh = False, 0.0, 0.0
    try:
        manager.begin_wait(key, job.id, agent, SHOWING if receipts else WAITING)
        while True:
            if _on_tick is not None:
                _on_tick(job.id)
            now = time.monotonic()
            if cancellation is not None and now >= next_cancel:
                cancelled, next_cancel = cancellation.cancelled(), now + CANCEL_INTERVAL
            if job.loaded and now >= next_refresh:
                manager._refresh(job)
                next_refresh = now + REFRESH_INTERVAL
            status = job.status
            outcome = decide(Facts(terminal=status in TERMINAL, lost=status == LOST, unknown=status == UNKNOWN,
                                   closing=manager.closing, cancelled=cancelled, interjected=_interjected(inboxes),
                                   expired=now - begin >= limit))
            if outcome is not None:
                break
            if waiting_since is None and all(item.shown for item in receipts):
                # Only now has the user been able to read the answer: the wait itself starts here.
                confirm()
                waiting_since = time.time()
                manager.wait_phase(key, WAITING)
            job.done.wait(min(SLICE, max(0.0, limit - (time.monotonic() - begin))) or 0.001)
    finally:
        manager.end_wait(key)
    if manager.closing and cancellation is not None and hasattr(cancellation, "cancel"):
        cancellation.cancel()      # the session is shutting down: this result is saved, no model call follows
    extra = {}
    if receipts:
        extra["answer_shown"] = all(item.shown for item in receipts)
        if extra["answer_shown"]:
            extra["answer_shown_at"] = round(max(item.shown_at for item in receipts), 3)
        extra["waiting_since"] = round(waiting_since, 3) if waiting_since is not None else None
    if outcome == EXPIRED:
        extra["limit"] = "the turn's remaining time" if capped else f"{limit:g} seconds"
        if receipts and waiting_since is None:
            extra["note"] = "no frontend has shown your answer yet (the page may be closed), so waiting never began"
    return result(manager, job.id, outcome, waited=time.monotonic() - begin, inputs=inputs, extra=extra,
                  with_log=outcome in TOLD)


def close_cut_off_wait(manager, arguments, *, context=None):
    """Result of a job_wait that started but never returned: current facts, nothing is waited for or run."""
    job_id = arguments.get("job_id")
    job = manager._get(job_id)
    manager.describe(job_id)
    if job.status in TERMINAL:
        return result(manager, job_id, ENDED_UNKNOWN if job.status == LOST else FINISHED, waited=0.0, with_log=True,
                      extra={"note": "the turn stopped during this wait; the job has ended since"})
    return result(manager, job_id, STOPPED)


def job_settle(manager, job_id, state, note=None, *, context=None):
    from baseagent.agent.inbox import direct_inputs
    job = manager._get(job_id)
    run = context.state if context is not None else None
    agent, turn = getattr(run, "session_id", None), getattr(run, "turn_id", None)
    if not manager.responsible(job, agent):
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "another agent is responsible for this job")
    info = manager.describe(job_id)
    link = manager.link_of(job_id)
    if not link["delivery"]["settled"]:
        if state == "dropped":
            if not (note or "").strip():
                raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "dropping a goal needs a note that says why")
            manager.set_goal(job_id, job_links.DROPPED, inputs=len(direct_inputs(run.messages)) if run is not None else None,
                             reason=note)
        elif info["status"] not in TERMINAL:
            raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"job {job_id} is {info['status']}; only a finished job can be handled")
        elif not job_links.received_by(link, agent):
            raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"you have not received the result of job {job_id} yet")
        else:
            manager.settle(job_id, job_links.HANDLED, agent=agent, turn=turn, note=note)
    link = manager.link_of(job_id)
    return {"job_id": job_id, "job_status": info["status"], "goal": link["goal"]["standing"],
            "settled": (link["delivery"]["settled"] or {}).get("as"), "process": "not touched"}


def register_wait_tools(registry, manager, params, job_id, declared):
    from .annotations import ToolAnnotations
    # Writes the job's link (confirmation, declared purpose, settlement): not read-only, not idempotent.
    notes = ToolAnnotations(read_only=False, destructive=False, idempotent=False, open_world=False)
    text = {"type": "string", "minLength": 1, "maxLength": job_links.MAX_REASON}

    def wait_tool(job_id, timeout_seconds=None, still_needed=None, purpose=None, expect=None, next_step=None, *,
                  context=None):
        return job_wait(manager(), job_id, timeout_seconds, still_needed,
                        {"purpose": purpose, "expect": expect, "next_step": next_step}, context=context)

    def settle_tool(job_id, state, note=None, *, context=None):
        return job_settle(manager(), job_id, state, note, context=context)

    registry.register("job_wait", "Wait quietly for one background job you are responsible for; returns when it ends, "
                      "when a message for you arrives, or when timeout_seconds passes. Never stops or restarts the job. "
                      "After a message: answer it, and if the job is still needed call job_wait again in the same "
                      "message as your answer, with still_needed. Do not poll with job_list/job_output instead. "
                      "purpose/expect/next_step fill in what was never stated for the job; stated ones never change",
                      params({"job_id": job_id, "timeout_seconds": {"type": "integer", "minimum": WAIT_MIN,
                                                                    "maximum": WAIT_MAX},
                              "still_needed": text, **declared}, ["job_id"]),
                      wait_tool, contextual=True, annotations=notes,
                      on_interrupted=lambda arguments, context=None: close_cut_off_wait(manager(), arguments,
                                                                                         context=context))
    registry.register("job_settle", "Record what became of a background job: handled (you received its result and did, "
                      "or no longer need, its follow-up) or dropped (its goal no longer holds; say why in note). Never "
                      "stops the process: job_kill does that",
                      params({"job_id": job_id, "state": {"type": "string", "enum": ["handled", "dropped"]},
                              "note": text}, ["job_id", "state"]),
                      settle_tool, contextual=True, annotations=notes)


__all__ = ["Facts", "decide", "job_settle", "job_wait", "register_wait_tools", "told"]
