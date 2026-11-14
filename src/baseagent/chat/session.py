"""Frontend-independent chat session: turns, worker pushes, approvals and chat commands.

``ChatSession`` owns the chat rules that used to live in ``cli_chat.run_chat``.
A frontend calls its commands and shows its events; it adds no rules of its
own. The same object drives the CLI (synchronously, with ``printer`` set so the
exact CLI lines are printed), the Textual TUI and the Web server (both
asynchronously: ``send`` runs the turn in a background thread).

The agent loop is not changed: a turn is still ``run_turn(text)`` from the CLI
composition in ``main.py``, approvals still go through ``SessionStore`` /
``WorkerManager.decide`` with the exact call id and request digest, and worker
limits, the single writer, the shared budget and persistence stay in
``WorkerManager``.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from baseagent.agent.state import RunStatus
from baseagent.agent.workers import ACTIVE, AWAITING, INBOX_HEADER, PUSH_HEADER, USER_HEADER, preview
from baseagent.agent.workers import worker_label as duplex_worker_label
from baseagent.session import SessionBusy
from baseagent.session.checkpoints import CheckpointError, parse_target
from baseagent.session.checkpoints import describe as describe_checkpoints
from baseagent.session.checkpoints import summary as checkpoint_summary

from baseagent.timestamps import iso_utc

from .events import EventBuffer
from .text import (arguments_preview, call_request_json, node_arguments_preview, patch_approval_text, pending_worker, status_lines, stop_explanation,
                   worker_explanation, worker_nodes)

ERRORS = (ValueError, SessionBusy, OSError, sqlite3.Error)
EXIT_COMMANDS = frozenset({"/exit", "/quit"})
CHECKPOINT_COMMANDS = frozenset({"/checkpoints", "/undo", "/rewind"})
CONTINUE_COMMAND = "/continue"
# Defaults of /continue: the interactive per-turn step limit and the per-turn tool-call limit.
CONTINUE_STEPS = 16
CONTINUE_TOOL_CALLS = 32
MAX_CONTINUE = 1000
ANY = object()
STATUS_INTERVAL = 1.0
HISTORY_LIMIT = 200


class ApprovalMismatch(ValueError):
    """The call id / digest does not name a currently pending approval."""


def exit_question(running):
    return f"还有 {len(running)} 个 worker 在运行（{', '.join(record.id for record in running)}）。"


class ChatSession:
    """One chat session for one frontend process.

    ``run_turn(text)`` starts a turn (``None`` continues the current one) and
    returns the state. ``workers`` is the ``WorkerManager`` of a main-agent
    chat (None for single-agent chat). ``printer(stream, text)`` receives the
    lines the CLI prints ("out"/"err"); every such line is also an event.
    """

    def __init__(self, *, run_turn, store, workspace, session_id=None, workers=None, main_agent=False,
                 events=None, printer=None, status_events=True, continue_steps=CONTINUE_STEPS,
                 continue_tool_calls=CONTINUE_TOOL_CALLS):
        self.run_turn = run_turn
        # /continue: how much more the current turn gets by default (the chat's per-turn allowance).
        self.continue_steps, self.continue_tool_calls = continue_steps, continue_tool_calls
        self.store, self.workspace, self.workers = store, workspace, workers
        self.main_agent = bool(main_agent or workers is not None)
        self.requested_session_id = session_id
        self.session_id = session_id
        self.events = events if events is not None else EventBuffer()
        self.printer = printer
        self.state = None
        self.closed = False
        self.started = False
        self.opened = False
        self._turn = threading.Lock()
        self._busy = False
        self._status_at = 0.0
        # Compute and publish each status under one lock: a snapshot taken earlier is never published later.
        self._status_lock = threading.Lock()
        # Live status events feed the TUI/Web panels; the line-based CLI prints /status on demand instead.
        self.status_events = status_events
        if workers is not None:
            workers.observer = self._on_worker

    # ----- output -----------------------------------------------------------------------
    def _say(self, stream, text, kind="notice", **data):
        """One line the CLI prints; GUIs get it as an event. Blank lines are terminal layout only."""
        if text:
            if kind == "notice":
                self.events.publish("notice", stream=stream, text=text)
            elif kind == "error":
                self.events.publish("error", message=text)
            else:
                self.events.publish(kind, text=text, **data)
        if self.printer is not None:
            self.printer(stream, text)

    def _fail(self, text):
        self._say("err", text, kind="error")

    # ----- lifecycle --------------------------------------------------------------------
    def start(self):
        """Load an existing session and its worker registry (parked approvals are queued again)."""
        state = None
        if self.session_id:
            try:
                state = self.store.load(self.session_id)
            except (OSError, sqlite3.Error):
                state = None
        self.state = state
        self.started = True
        if self.workers is not None:
            self.workers.restore(state)
        return state

    def open(self):
        """Start for the TUI/Web: load the session and show worker output only as events.

        The CLI prints worker pushes from a display queue between prompts; a GUI gets the same
        pushes as worker_* events, so the queue is not filled.
        """
        if not self.started:
            self.start()
        self.opened = True
        if self.workers is not None:
            self.workers.on_notice = _swallow
        self.publish_status(force=True)
        return self.state

    def resolve_pending(self, ask):
        """A session reopened at a root approval: ask it first (CLI start-up behaviour)."""
        state = self.state
        if state is not None and state.status == RunStatus.AWAITING_APPROVAL:
            state = self._approvals(state, ask)
            if state is not None:
                self.report(state)
            self.state = state
        return state

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.workers is not None:
            # Nobody drains the display queue of a TUI/Web session, so keep swallowing pushes
            # from workers that are still finishing; the CLI prints its queue before closing.
            self.workers.on_notice = _swallow if self.opened else None
            self.workers.observer = None
        self.persist()

    # ----- turns (shared by every frontend) ---------------------------------------------
    def turn(self, text, ask):
        """One chat line. ``ask(view)`` answers a main-agent approval: (decision or None, stop)."""
        self.events.publish("user_message", text=text)
        self.state = self._turn_from(self.state, text, ask)
        self.persist()
        return self.state

    def _turn_from(self, state, text, ask):
        # The kernel only appends a prompt to a completed turn. If the previous
        # turn was interrupted/failed, resume it first so the conversation remains
        # usable after Ctrl+C instead of exiting the process.
        if state is not None and state.status != RunStatus.COMPLETED:
            resumed = self._invoke(None)
            if resumed is None:
                return state
            state = resumed
            if state.status == RunStatus.AWAITING_APPROVAL:
                state = self._approvals(state, ask)
                if state is None:
                    return None
            self.report(state)
            if state.status != RunStatus.COMPLETED:
                self._say("err", "The previous turn is still unfinished; your new message was not sent. Type "
                                 "/continue to give it more steps and finish it here, or inspect or resume it with "
                                 f"--session {state.session_id or '<id>'} --resume.")
                return state
        compose = self.workers.compose if self.workers is not None else None
        state = self._invoke(compose(text) if compose else text)
        if state is None:
            return None
        state = self._approvals(state, ask)
        if state is not None:
            self.report(state)
        return state

    def continue_turn(self, ask, limits=None):
        """Continue the current turn after a main-agent approval decision, or with raised ``limits`` (/continue)."""
        state = self._invoke(None, limits=limits)
        if state is not None:
            state = self._approvals(state, ask)
            if state is not None:
                self.report(state)
        self.state = state
        self.persist()
        return state

    def _invoke(self, user_input, limits=None):
        try:
            # Older run_turn callables take one argument; limits are passed only by /continue.
            state = self.run_turn(user_input, limits=limits) if limits is not None else self.run_turn(user_input)
        except KeyboardInterrupt:
            self._say("out", "")
            self._say("err", "Turn cancelled. Type another message to continue.")
            return None
        except ERRORS as exc:
            self._fail(f"Session error: {exc}")
            return None
        if state is not None and state.session_id:
            self.session_id = state.session_id
        return state

    def _approvals(self, state, ask):
        """Ask allow/deny for the exact pending main-agent call, then resume in-process."""
        while state is not None and state.status == RunStatus.AWAITING_APPROVAL:
            view = self.root_approval(state)
            if view is None:
                self._say("err", "Approval state is missing its exact request; inspect the session before continuing.")
                return state
            self._publish_approval(view)
            decision, stop = ask(view)
            if decision is None:
                return state
            try:
                self.decide(view["call_id"], view["digest"], decision, worker=None)
            except ERRORS as exc:
                self._fail(f"Could not record the approval decision: {exc}")
                return state
            if stop:
                return state
            state = self._invoke(None)
        return state

    def report(self, state):
        if state.final_answer:
            self._say("out", state.final_answer, kind="assistant_message", status=str(state.status))
        if state.status == RunStatus.AWAITING_APPROVAL:
            return
        nodes = worker_nodes(self.store, state)
        if state.status == RunStatus.COMPLETED:
            for node in nodes:
                if node["state"]["metadata"].get("closed_by_parent"):
                    self._say("err", worker_explanation(node, paused=False))
            return
        active = state.metadata.get("active_subtask")
        node = next((node for node in nodes if node["task_id"] == active), None)
        if node is not None and node["state"]["status"] != RunStatus.COMPLETED:
            self._say("err", worker_explanation(node, paused=True, session_id=state.session_id))
            return
        if state.status == RunStatus.INTERRUPTED:
            self._say("err", "Turn cancelled. Type another message to continue.")
            return
        self._say("err", stop_explanation(state, interactive=True))
        if state.blocked_tool_calls:
            self._say("err", "Unresolved tool calls: " + ", ".join(state.blocked_tool_calls)
                      + ". Inspect with --inspect-session and resolve them before resuming.")

    # ----- approvals --------------------------------------------------------------------
    def root_approval(self, state, *, previews=True):
        """The pending main-agent (or legacy subtask node) call, or None when the request is incomplete."""
        approval = state.metadata.get("approval_request") or {}
        # A worker's pending call lives in its node; the root only records the pause.
        node = None if approval else pending_worker(self.store, state)
        if node is not None:
            approval = node["state"]["metadata"]["approval_request"]
        call_id, digest = approval.get("call_id"), approval.get("request_digest")
        if not call_id or not digest:
            return None
        arguments = (node_arguments_preview(node, call_id) if node is not None
                     else arguments_preview(self.store, state, call_id))
        view = {"worker": None, "node": node["task_id"] if node is not None else None, "call_id": call_id,
                "digest": digest, "name": approval.get("name", "tool"), "arguments": arguments,
                "session_id": state.session_id, "task": None}
        if not previews:
            return view
        return self._with_patch(view, call_request_json(self.store, state, call_id, node))

    def _with_patch(self, view, request_json):
        """apply_patch approvals also carry ``patch``: file count and whole-batch diff (other tools: unchanged)."""
        if view.get("name") == "apply_patch" and request_json:
            text = patch_approval_text(self.workspace, request_json)
            if text:
                view["patch"] = text
        return view

    def _publish_approval(self, view):
        if view.get("patch"):
            # TUI/Web render notices as text: the diff appears beside the approval without a frontend branch.
            prefix = f"[{view['worker']}] " if view.get("worker") else ""
            self.events.publish("notice", stream="out", text=prefix + view["patch"])
        self.events.publish("approval_requested", **view)

    def approval_view(self, record, *, previews=True):
        """What the user must see before answering a worker's call (CLI prints exactly these fields)."""
        approval = record.approval or {}
        try:
            worker_state = self.store.load(record.session_id)
        except (OSError, sqlite3.Error):
            worker_state = None
        arguments = arguments_preview(self.store, worker_state, approval["call_id"]) if worker_state is not None else ""
        with self.workers._lock:
            waiting = sum(1 for other in self.workers.records.values() if other.status == AWAITING) - 1
        view = {"worker": record.id, "node": None, "call_id": approval.get("call_id"),
                "digest": approval.get("request_digest"), "name": approval.get("name", "tool"),
                "arguments": arguments, "session_id": record.session_id, "task": preview(record.task, 120),
                "pending": max(waiting, 0)}
        if previews and view["name"] == "apply_patch" and worker_state is not None:
            view = self._with_patch(view, call_request_json(self.store, worker_state, approval["call_id"]))
        return view

    def next_worker_approval(self):
        """The next parked worker approval in arrival order (view), or None."""
        if self.workers is None:
            return None
        record = self.workers.next_approval()
        return None if record is None else self.approval_view(record)

    def pending_approvals(self, *, previews=True):
        """Every pending approval, oldest first: parked workers, then the main agent's own call.

        ``previews=False`` skips the apply_patch diff preview (status counts only need the views).
        """
        views = []
        if self.workers is not None:
            with self.workers._lock:
                order = list(self.workers._approvals)
                records = [self.workers.records[worker_id] for worker_id in order if worker_id in self.workers.records]
                records += [record for record in self.workers.records.values()
                            if record.status == AWAITING and record.id not in order]
            views += [self.approval_view(record, previews=previews) for record in records
                      if record.status == AWAITING and record.approval]
        state = self._stored_state()
        if state is not None and state.status == RunStatus.AWAITING_APPROVAL:
            view = self.root_approval(state, previews=previews)
            if view is not None:
                views.append(view)
        return views

    def _find(self, call_id, digest, worker):
        """Match the exact pending call. A wrong call id or digest is refused, as in the CLI."""
        if self.workers is not None and worker is not None:
            with self.workers._lock:
                candidates = [record for record in self.workers.records.values()
                              if record.status == AWAITING and record.approval
                              and record.approval.get("call_id") == call_id
                              and (worker is ANY or record.id == worker)]
            if candidates:
                exact = [record for record in candidates if record.approval.get("request_digest") == digest]
                if not exact:
                    raise ApprovalMismatch(f"request digest does not match the pending call {call_id} "
                                           f"(worker {candidates[0].id})")
                return exact[0], None
        if worker is None or worker is ANY:
            state = self._stored_state()
            if state is not None and state.status == RunStatus.AWAITING_APPROVAL:
                view = self.root_approval(state, previews=False)
                if view is not None and view["call_id"] == call_id:
                    if view["digest"] != digest:
                        raise ApprovalMismatch(f"request digest does not match the pending call {call_id}")
                    return None, view
        raise ApprovalMismatch(f"no pending approval for call {call_id}")

    def decide(self, call_id, digest, decision, *, worker=ANY):
        """Record allow/deny for exactly one displayed call. Raises on a mismatch or store error.

        ``worker`` narrows the match: a worker id, None for the main agent, ANY for either.
        A worker resumes in the background; a main-agent call is resumed by the caller.
        """
        if decision not in {"allow", "deny"}:
            raise ValueError("decision must be allow or deny")
        record, view = self._find(call_id, digest, worker)
        word = "allowed" if decision == "allow" else "denied"
        if record is not None:
            self.workers.decide(record, decision)
            self._say("out", f"Tool call {word}. Worker {record.id} continues in the background.",
                      kind="approval_decided", worker=record.id, call_id=call_id, digest=digest, decision=decision)
        else:
            if view["node"] is not None:
                self.store.decide_task_tool(view["session_id"], view["node"], call_id, decision, digest)
            else:
                self.store.decide_tool(view["session_id"], call_id, decision, digest)
            self._say("out", f"Tool call {word}.", kind="approval_decided", worker=None, call_id=call_id,
                      digest=digest, decision=decision)
        self.publish_status(force=True)
        return record

    def skip(self, view):
        """Leave a worker's call pending (for example on EOF); it is asked again after a restart."""
        record = self.workers.get(view["worker"]) if self.workers is not None else None
        if record is not None:
            self.workers.skip_approval(record)

    # ----- commands ---------------------------------------------------------------------
    def command(self, text, *, blocking=True, ask=None):
        """/status, /checkpoints, /undo, /rewind, /continue always; /wait, /cancel, /tell in main-agent chat.

        ``ask`` answers a main-agent approval reached while /continue runs (CLI); GUIs leave it pending.

        False means: an ordinary chat line.
        """
        if text == "/status":
            self._say("out", "\n".join(self.status_lines()))
            self.publish_status(force=True)
            return True
        command, _, rest = text.partition(" ")
        rest = rest.strip()
        if command in CHECKPOINT_COMMANDS:
            self.checkpoint_command(command, rest)
            return True
        if command == CONTINUE_COMMAND:
            self.continue_command(rest, blocking=blocking, ask=ask)
            return True
        if self.workers is None:
            return False
        if command == "/wait":
            if not self.workers.running():
                self._say("out", "No worker is running.")
                return True
            if not blocking:
                self._say("out", "Workers keep running in the background; their results appear here automatically.")
                return True
            self._say("out", "Waiting for running workers (Ctrl+C stops waiting; workers keep running).")
            try:
                self.workers.wait()
            except KeyboardInterrupt:
                self._say("out", "")
                self._say("out", "Stopped waiting; workers keep running.")
            return True
        if command == "/cancel":
            if not rest:
                self._fail("Usage: /cancel <id> or /cancel all")
            else:
                self.cancel(rest)
            return True
        if command == "/tell":
            worker_id, _, note = rest.partition(" ")
            if not worker_id or not note.strip():
                self._fail("Usage: /tell <id> <text>")
                return True
            self.tell(worker_id, note.strip())
            return True
        return False

    # ----- /continue: raise this turn's limits and keep going -----------------------------
    def continue_limits(self, state, extra=None):
        """(max_steps, max_model_calls, max_tool_calls) for /continue: N more steps and model calls, one more
        tool allowance. Used counts stay; a limit is never lowered."""
        steps = self.continue_steps if extra is None else extra
        return (max(state.max_steps, state.step + steps),
                max(state.max_model_calls, state.model_calls + steps),
                max(state.max_tool_calls, state.tool_calls + self.continue_tool_calls))

    def continue_command(self, rest, *, blocking=True, ask=None):
        """``/continue [N]``. Returns the new limits, or None when nothing was continued."""
        extra = None
        if rest:
            if not rest.isdigit() or not 1 <= int(rest) <= MAX_CONTINUE:
                self._fail(f"Usage: /continue [N]  (N = 1-{MAX_CONTINUE} more steps for the current turn; "
                           f"default {self.continue_steps})")
                return None
            extra = int(rest)
        state = self._stored_state()
        if state is None or state.status == RunStatus.COMPLETED:
            self._say("out", "Nothing to continue: the last turn finished. Type a message to start a new turn.")
            return None
        if state.status == RunStatus.AWAITING_APPROVAL:
            self._fail("The current turn is waiting for an approval; answer it first (/continue does not approve).")
            return None
        if state.blocked_tool_calls or state.status == RunStatus.NEEDS_RECOVERY:
            self._fail("The current turn has tool calls with unknown outcomes; raising limits does not resolve them. "
                       f"Inspect with baseagent --session {state.session_id} --inspect-session.")
            return None
        limits = self.continue_limits(state, extra)
        if not blocking:
            if not self._turn.acquire(blocking=False):
                self._fail("上一条消息还在处理中：等主 Agent 回复后再 /continue。")
                return None
            self._announce_continue(state, limits)
            self._background(lambda: self.continue_turn(self._defer, limits))
            return limits
        self._announce_continue(state, limits)
        self.continue_turn(ask or self._defer, limits)
        return limits

    def _announce_continue(self, state, limits):
        self._say("out", f"Continuing this turn with raised limits: steps {state.step}/{limits[0]}, model calls "
                         f"{state.model_calls}/{limits[1]}, tool calls {state.tool_calls}/{limits[2]} (used counts kept; "
                         "finished tool calls are not repeated).")

    # ----- checkpoints (/checkpoints, /undo, /rewind) -------------------------------------
    def checkpoint_command(self, command, rest):
        words = rest.split()
        force = "--force" in words
        words = [word for word in words if word != "--force"]
        if command == "/checkpoints":
            if words or force:
                self._fail("Usage: /checkpoints")
                return None
            return self.checkpoints()
        if command == "/undo":
            if words:
                self._fail("Usage: /undo [--force]")
                return None
            return self.undo(force=force)
        if len(words) != 1:
            self._fail("Usage: /rewind <N> [--force]")
            return None
        try:
            target = parse_target(words[0])
        except ValueError:
            self._fail("Usage: /rewind <N> [--force]（N 是 /checkpoints 里的轮次号）")
            return None
        return self.undo(target, force=force)

    def checkpoints(self):
        """List this session's file checkpoints (also the /checkpoints text)."""
        if not self.session_id or self._stored_state() is None:
            self._say("out", "还没有检查点：这个对话还没有开始第一轮。")
            return {"session_id": self.session_id, "checkpoints": []}
        try:
            listing = self.store.checkpoints(self.session_id)
        except ERRORS as exc:
            self._fail(f"Could not read checkpoints: {exc}")
            return None
        self._say("out", describe_checkpoints(listing))
        return listing

    def undo_blockers(self):
        """Why an undo must wait now (None when it may run): a running turn or an active writer."""
        if self._busy:
            return "撤销被拒绝：主 Agent 还在回复这一轮；等这一轮结束后再撤销。"
        if self.workers is not None:
            with self.workers._lock:
                writers = [record for record in self.workers.records.values()
                           if record.allow_writes and record.status in ACTIVE]
            if writers:
                names = "、".join(f"{record.id}（{'等待审批' if record.status == AWAITING else '运行中'}）"
                                 for record in writers)
                return f"撤销被拒绝：可写的 worker {names}还没结束。等它结束或 /cancel 后再撤销。"
        return None

    def undo(self, target="last", *, force=False):
        """/undo and /rewind: restore the files of one or more turns. Returns the result or None."""
        if not self.session_id or self._stored_state() is None:
            self._fail("没有可撤销的检查点：这个对话还没有开始第一轮。")
            return None
        reason = self.undo_blockers()
        if reason is not None:
            self._fail(reason)
            return None
        try:
            result = self.store.rewind(self.workspace, self.session_id, target, force=force)
        except ERRORS as exc:
            self._fail(str(exc) if isinstance(exc, CheckpointError) else f"Undo failed: {exc}")
            return None
        self.state = self._stored_state() or self.state
        self._say("out", checkpoint_summary(result))
        self.publish_status(force=True)
        return result

    def cancel(self, target):
        """Cancel one worker or "all". Returns [(ok, message)]."""
        if self.workers is None:
            self._fail("This chat has no background workers.")
            return [(False, "no workers")]
        if target == "all":
            messages = self.workers.cancel_all()
            self._say("out", "\n".join(messages) if messages else "No active worker.")
            results = [(True, message) for message in messages]
        else:
            ok, message = self.workers.cancel(target)
            if ok:
                self._say("out", message)
            else:
                self._fail(message)
            results = [(ok, message)]
        self.publish_status(force=True)
        return results

    def cancel_all(self, why):
        """Exit / Ctrl+C: every active worker is recorded as cancelled, nothing is lost silently."""
        if self.workers is None:
            return []
        messages = self.workers.cancel_all(reason=f"cancelled at {why}")
        for message in messages:
            self._say("out", message)
        return messages

    def tell(self, worker_id, text):
        if self.workers is None:
            self._fail("This chat has no background workers.")
            return False, "no workers"
        ok, message = self.workers.message(worker_id, text)
        if ok:
            self._say("out", message)
        else:
            self._fail(message)
        return ok, message

    def running(self):
        return self.workers.running() if self.workers is not None else []

    def wait(self, timeout=None):
        return True if self.workers is None else self.workers.wait(timeout)

    def exit(self, *, force=False, why="exit"):
        """Leave the chat. Running workers need confirmation (force=True cancels them), as in the CLI."""
        running = self.running()
        if (running or self._busy) and not force:
            question = exit_question(running) if running else ""
            if self._busy:
                question += "主 Agent 还在回复这一轮；现在退出，这一轮会在下次用同一 --session 进入时继续。"
            return {"exited": False, "running": [record.id for record in running], "busy": self._busy,
                    "question": question}
        if running:
            self.cancel_all(why)
        if self._busy:
            # The turn thread still holds the session; it is resumed next time like an interrupted turn.
            self.closed = True
            return {"exited": True, "running": []}
        self.close()
        return {"exited": True, "running": []}

    def take_display(self):
        return self.workers.take_display() if self.workers is not None else []

    def flush_display(self):
        """Print queued worker pushes and trace lines (terminal output only; GUIs got them as events)."""
        for stream, text in self.take_display():
            if self.printer is not None:
                self.printer(stream, text)

    def finish_workers(self):
        """No more input: wait for running workers, print their pushes, keep parked approvals for later."""
        workers = self.workers
        if workers is None:
            return
        if workers.running():
            self._say("err", "输入已结束：等待运行中的 worker 完成后退出（Ctrl+C 取消它们）。")
            try:
                workers.wait()
            except KeyboardInterrupt:
                self._say("out", "")
                for message in workers.cancel_all(reason="cancelled at Ctrl+C"):
                    self._say("out", message)
        self.flush_display()
        parked = [record.id for record in workers.records.values() if record.status == AWAITING]
        if parked:
            self._say("err", f"{', '.join(parked)} 仍在等待审批；用同一 --session 再进入对话会逐条询问。")
        self.persist()

    def persist(self):
        if self.workers is None:
            return
        try:
            self.workers.persist()
        except ERRORS as exc:
            self._fail(f"Could not save worker status: {exc}")

    # ----- non-blocking commands for the TUI and the Web server -------------------------
    @property
    def busy(self):
        return self._busy

    def submit(self, text):
        """A typed line from a GUI: "exit", "command", "sent", "rejected" or "empty". May block on /cancel."""
        text = (text or "").strip()
        if not text:
            return "empty"
        if text in EXIT_COMMANDS:
            return "exit"
        if self.command(text, blocking=False):
            return "command"
        return "sent" if self.send(text) else "rejected"

    def send(self, text):
        """Start a turn in the background. Returns False (and an error event) when one is still running."""
        text = (text or "").strip()
        if not text:
            return False
        if not self._turn.acquire(blocking=False):
            self._fail("上一条消息还在处理中：等主 Agent 回复后再发。")
            return False
        self._background(lambda: self.turn(text, self._defer))
        return True

    def approve(self, call_id, digest, *, worker=ANY):
        return self._answer(call_id, digest, "allow", worker)

    def deny(self, call_id, digest, *, worker=ANY):
        return self._answer(call_id, digest, "deny", worker)

    def _answer(self, call_id, digest, decision, worker):
        """Non-blocking allow/deny. A main-agent call needs the turn slot to resume."""
        try:
            record, view = self._find(call_id, digest, worker)
        except ApprovalMismatch as exc:
            self._fail(f"Approval refused: {exc}")
            return {"ok": False, "message": str(exc)}
        if record is None and not self._turn.acquire(blocking=False):
            self._fail("上一条消息还在处理中：等主 Agent 回复后再审批。")
            return {"ok": False, "message": "busy"}
        try:
            self.decide(call_id, digest, decision, worker=record.id if record is not None else None)
        except ERRORS as exc:
            if record is None:
                self._turn.release()
            who = f" for {record.id}" if record is not None else ""
            self._fail(f"Could not record the approval decision{who}: {exc}")
            return {"ok": False, "message": str(exc)}
        if record is None:
            self._background(lambda: self.continue_turn(self._defer))
        else:
            self.persist_idle()
        return {"ok": True, "message": f"{decision} recorded"}

    def persist_idle(self):
        """Save the worker registry unless a turn holds the session (that turn saves it when it ends)."""
        if self._turn.acquire(blocking=False):
            try:
                self.persist()
            finally:
                self._turn.release()

    @staticmethod
    def _defer(view):
        """GUIs answer approvals asynchronously: leave the call pending (its event is already out)."""
        return None, True

    def _background(self, work):
        """Run one turn step in a thread; the caller already holds the turn slot."""
        def run():
            self._busy = True
            self.publish_status(force=True)
            try:
                work()
            except Exception as exc:  # noqa: BLE001 -- a turn failure must not kill the frontend.
                self._fail(f"Session error: {type(exc).__name__}: {exc}")
            finally:
                self._busy = False
                self._turn.release()
                self.publish_status(force=True)

        thread = threading.Thread(target=run, name="chat-turn", daemon=True)
        thread.start()
        return thread

    # ----- worker events ----------------------------------------------------------------
    def _on_worker(self, kind, record, **data):
        if kind == "worker_started":
            self.events.publish(kind, worker=record.id, task=preview(record.task, 200),
                                allow_writes=record.allow_writes, session_id=record.session_id)
        elif kind == "worker_progress":
            self.events.publish(kind, worker=record.id, line=data.get("line", ""))
            self.publish_status()
            return
        elif kind == "worker_done":
            report = record.report or {}
            self.events.publish(kind, worker=record.id, summary=preview(report.get("summary") or "", 600),
                                citations=list(report.get("citations") or [])[:10],
                                paths=list(report.get("paths") or [])[:10], text=data.get("text", ""))
        elif kind == "worker_failed":
            self.events.publish(kind, worker=record.id, detail=record.detail, error=record.error,
                                text=data.get("text", ""))
        elif kind == "worker_cancelled":
            self.events.publish(kind, worker=record.id, detail=record.detail, text=data.get("text", ""))
        elif kind == "approval_requested" and record.approval:
            self._publish_approval(self.approval_view(record))
        self.publish_status(force=True)

    # ----- status and snapshots ---------------------------------------------------------
    def status_lines(self):
        """The CLI's /status block (same words in every frontend)."""
        return status_lines(self.store, self.state, self.requested_session_id, self.workspace, self.workers)

    def _stored_state(self):
        if not self.session_id:
            return None
        try:
            return self.store.load(self.session_id)
        except (OSError, sqlite3.Error):
            return None

    def status(self):
        """Structured status: session, busy flag, budget used/limit, tokens and every worker."""
        state = self._stored_state()
        budget = None
        if self.workers is not None:
            pool = self.workers.budget
            if pool is None:
                try:
                    pool = self.workers.budget_factory()  # the limits the next turn will get
                except Exception:  # noqa: BLE001 -- display only.
                    pool = None
            budget = pool.snapshot() if pool is not None else None
        elif state is not None:
            budget = {"model_calls": state.model_calls, "max_model_calls": state.max_model_calls,
                      "steps": state.step, "max_steps": state.max_steps}
        workers = []
        if self.workers is not None:
            with self.workers._lock:
                records = list(self.workers.records.values())
            for record in records:
                usage = self.workers.live_usage(record)
                workers.append({
                    "id": record.id, "status": record.status, "label": duplex_worker_label(record),
                    "elapsed": round(record.elapsed(), 1), "started_at": record.started_at,
                    "finished_at": record.finished_at, "started_at_utc": iso_utc(record.started_at),
                    "finished_at_utc": iso_utc(record.finished_at), "allow_writes": record.allow_writes,
                    "task": preview(record.task, 80), "session_id": record.session_id,
                    "model_calls": usage.get("model_calls", 0), "max_model_calls": usage.get("max_model_calls"),
                    "steps": usage.get("steps", 0), "max_steps": usage.get("max_steps"),
                    "tool_calls": usage.get("tool_calls", 0), "max_tool_calls": usage.get("max_tool_calls"),
                    "summary": preview((record.report or {}).get("summary") or "", 300)})
        return {
            "session_id": self.session_id, "workspace": str(self.workspace.root),
            "mode": "main-agent" if self.main_agent else "single-agent", "busy": self._busy,
            "turn_status": str(state.status) if state is not None else None,
            "turn_started_at_utc": iso_utc(state.turn_started_at) if state is not None else None,
            "tokens": state.total_tokens if state is not None else 0,
            "max_total_tokens": state.max_total_tokens if state is not None else None,
            "budget": budget, "workers": workers,
            "max_workers": self.workers.max_workers if self.workers is not None else 0,
            "pending_approvals": len(self.pending_approvals(previews=False)),
        }

    def publish_status(self, *, force=False):
        """Status event; progress lines refresh it at most once per second."""
        if not self.status_events:
            return None
        with self._status_lock:
            now = time.monotonic()
            if not force and now - self._status_at < STATUS_INTERVAL:
                return None
            self._status_at = now
            try:
                value = self.status()
            except Exception:  # noqa: BLE001 -- status display must not fail a worker or a turn.
                return None
            return self.events.publish("status", **value)

    def history(self, limit=HISTORY_LIMIT):
        """The stored conversation: user lines (without attached pushes) and main-agent replies."""
        state = self._stored_state()
        if state is None:
            return []
        items = []
        for message in state.messages:
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if message.get("role") == "user":
                items.append({"role": "user", "text": strip_attachments(content)})
            elif message.get("role") == "assistant":
                items.append({"role": "assistant", "text": content})
        return items[-limit:]

    def snapshot(self):
        """Everything a frontend needs to rebuild its view; events after ``last_id`` follow."""
        last_id = self.events.last_id
        return {"last_id": last_id, "session_id": self.session_id, "workspace": str(self.workspace.root),
                "mode": "main-agent" if self.main_agent else "single-agent", "history": self.history(),
                "status": self.status(), "approvals": self.pending_approvals()}

    def events_after(self, after_id):
        """Buffered events after an id, or one synthetic ``resync`` event carrying a snapshot."""
        items = self.events.after(after_id)
        if items is not None:
            return [item.to_dict() for item in items]
        snapshot = self.snapshot()
        return [{"id": snapshot["last_id"], "kind": "resync", "ts": time.time(), "data": {"snapshot": snapshot}}]


def _swallow(stream, text):
    """on_notice hook of TUI/Web sessions: pushes arrive as worker_* events, never in the display queue."""
    return True


def strip_attachments(content):
    """Show what the user typed: drop worker pushes the chat attached to the message."""
    if content.startswith(PUSH_HEADER) and f"\n{USER_HEADER}\n" in content:
        return content.split(f"\n{USER_HEADER}\n", 1)[1]
    if content.startswith(INBOX_HEADER):
        return content[len(INBOX_HEADER):].lstrip("\n")
    return content


__all__ = ["ANY", "ApprovalMismatch", "ChatSession", "exit_question", "strip_attachments"]
