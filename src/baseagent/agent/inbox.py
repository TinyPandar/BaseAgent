"""One inbox for everything that reaches an agent from outside its own loop.

Four kinds of external input used to take four separate paths: a chat line from
the user, a worker result for the main agent, a ``/tell``/``message_worker``
requirement for a worker, and a finished background job. They now all become
``InboxItem`` objects in an ``Inbox``:

``source``  user | worker | job | lead (the main agent / the user via /tell, for a worker)
``mode``    steer (deliver inside the running turn) | queue (start a turn after this one ends)

Delivery happens at exactly one place, ``InboxMiddleware.before_model``: right
before a model call, once every tool call of the last assistant batch has its
reply. All pending *steer* items are merged into ONE user message, each part
under the header of its source, so the model can always tell the user's own
mid-turn words from system-added results. Delivery never makes a model call of
its own: an item waits for the next call the agent makes anyway (or for the
next turn). *Queue* items are never delivered here; the chat session starts a
new turn with them after the current one ends (``baseagent.chat.session``).

Undelivered items are mirrored into ``state.metadata["inbox"]`` and saved by the
loop's ordinary checkpoints, so a crash or ``--resume`` does not lose them;
the in-memory inbox remembers delivered ids, so an item is delivered once.
This module is harness-level (it is not part of ``baseagent.kernel``).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from uuid import uuid4

from baseagent.middleware import AgentMiddleware

USER, WORKER, JOB, LEAD = "user", "worker", "job", "lead"
SOURCES = (USER, WORKER, JOB, LEAD)
STEER, QUEUE = "steer", "queue"
MODES = (STEER, QUEUE)
METADATA_KEY = "inbox"

# Headers keep their earlier wording: chat/session.py and frontends parse them for display.
PUSH_HEADER = "[worker 推送：系统自动附加的结果，不是用户输入]"
USER_HEADER = "[用户消息]"
INBOX_HEADER = "[主 Agent 转达的补充要求]"
STEER_HEADER = "[用户在本轮进行中补充的消息（用户本人输入）]"
JOB_FOOTER = "Use job_output to read the log."
# Order of the parts inside one delivered message: system-added parts first, the user's words last.
ORDER = (LEAD, WORKER, JOB, USER)


def _notice_header():
    from baseagent.tools.jobs import NOTICE_HEADER
    return NOTICE_HEADER


@dataclass
class InboxItem:
    source: str
    text: str
    mode: str = STEER
    id: str = field(default_factory=lambda: "m" + uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError(f"unknown inbox source: {self.source}")
        if self.mode not in MODES:
            raise ValueError(f"unknown inbox mode: {self.mode}")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("inbox text must not be blank")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(source=value["source"], text=value["text"], mode=value.get("mode", STEER),
                   id=value["id"], created_at=value.get("created_at", 0.0))


class Inbox:
    """Thread-safe pending items of one agent. Producers call ``put`` from any thread.

    ``listeners`` get ``(kind, items, **data)`` with kind "put" or "delivered"; they only
    observe (the chat session publishes them as events) and never change delivery.
    """

    def __init__(self):
        self._items = []
        self._delivered = set()
        self._lock = threading.RLock()
        self.listeners = []

    # ----- producers ----------------------------------------------------------------
    def put(self, source, text, mode=STEER, *, item_id=None):
        item = InboxItem(source, text, mode, **({"id": item_id} if item_id else {}))
        with self._lock:
            self._items.append(item)
        self._notify("put", [item])
        return item

    def restore(self, values):
        """Add items saved in session metadata that this inbox does not hold and never delivered."""
        added = []
        with self._lock:
            known = {item.id for item in self._items} | self._delivered
            for value in values or ():
                try:
                    item = InboxItem.from_dict(value)
                except (KeyError, TypeError, ValueError):
                    continue
                if item.id not in known:
                    known.add(item.id)
                    self._items.append(item)
                    added.append(item)
            self._items.sort(key=lambda item: item.created_at)
        return added

    # ----- consumers ----------------------------------------------------------------
    def pending(self, *, mode=None, source=None):
        with self._lock:
            return [item for item in self._items
                    if (mode is None or item.mode == mode) and (source is None or item.source == source)]

    def take(self, *, mode=STEER, source=None, limit=None):
        """Remove and return matching items, oldest first. Callers report them with ``delivered``."""
        with self._lock:
            chosen = [item for item in self._items
                      if item.mode == mode and (source is None or item.source in _as_set(source))]
            if limit is not None:
                chosen = chosen[:limit]
            ids = {item.id for item in chosen}
            self._items = [item for item in self._items if item.id not in ids]
            self._delivered |= ids
            return chosen

    def delivered(self, items, how):
        """Report items that left the inbox (``how``: model_call or new_turn)."""
        if items:
            self._notify("delivered", list(items), how=how)

    def snapshot(self):
        with self._lock:
            return [item.to_dict() for item in self._items]

    def __len__(self):
        with self._lock:
            return len(self._items)

    def _notify(self, kind, items, **data):
        for listener in list(self.listeners):
            try:
                listener(kind, items, **data)
            except Exception:  # noqa: BLE001 -- an observer must never break delivery.
                pass

    # ----- persistence --------------------------------------------------------------
    def sync(self, state):
        """Mirror pending items into the state; the next checkpoint saves them. No key when empty."""
        metadata = getattr(state, "metadata", None)
        if not isinstance(metadata, dict):
            return
        items = self.snapshot()
        if items:
            metadata[METADATA_KEY] = items
        else:
            metadata.pop(METADATA_KEY, None)


def _as_set(value):
    return {value} if isinstance(value, str) else set(value)


def compose(items):
    """One user message for ``items``: one part per source, each under its own header."""
    groups = {}
    for item in items:
        groups.setdefault(item.source, []).append(item.text)
    parts = []
    for source in ORDER:
        texts = groups.get(source)
        if not texts:
            continue
        if source == LEAD:
            parts.append(INBOX_HEADER + "\n" + "\n\n".join(texts))
        elif source == WORKER:
            parts.append(PUSH_HEADER + "\n" + "\n".join(texts))
        elif source == JOB:
            parts.append(_notice_header() + "\n" + "\n".join(texts) + "\n" + JOB_FOOTER)
        else:
            parts.append(STEER_HEADER + "\n" + "\n\n".join(texts))
    return "\n\n".join(parts)


def is_inbox_message(content):
    """True for a user-role message the inbox delivered inside a turn (not a turn of its own).

    A turn may start with worker pushes attached in front of the user's text
    (``PUSH_HEADER`` ... ``USER_HEADER`` text): that message *is* a turn. Push lines are
    single-line JSON, so the line after them tells the two shapes apart.
    """
    if not isinstance(content, str):
        return False
    if content.startswith((INBOX_HEADER, STEER_HEADER, _notice_header())):
        return True
    if not content.startswith(PUSH_HEADER):
        return False
    for line in content[len(PUSH_HEADER):].split("\n")[1:]:
        if line.startswith("{"):
            continue
        return line != USER_HEADER
    return True


def user_parts(content):
    """The user's own mid-turn words inside a delivered inbox message ('' when there are none)."""
    if not isinstance(content, str) or STEER_HEADER not in content:
        return ""
    return content.split(STEER_HEADER + "\n", 1)[-1].strip()


def direct_inputs(messages):
    """[(message index, text)] of what was said to this agent itself, oldest first: the prompt of each turn,
    the user's mid-turn lines and, for a worker, /tell or message_worker. Worker results and job notices are
    not inputs. Read from the stored conversation, which compaction never rewrites."""
    found = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, str):
            continue
        if not is_inbox_message(content):
            if content.startswith(PUSH_HEADER) and f"\n{USER_HEADER}\n" in content:
                content = content.split(f"\n{USER_HEADER}\n", 1)[1]
            found.append((index, content))
            continue
        words = user_parts(content)
        if not words and content.startswith(INBOX_HEADER):
            words = content[len(INBOX_HEADER):].split("\n" + PUSH_HEADER)[0].split("\n" + _notice_header())[0].strip()
        if words:
            found.append((index, words))
    return found


def job_in_notice(content, job_id):
    """True when a delivered inbox message carries the completion notice of ``job_id``."""
    if _notice_header() not in content:
        return False
    body = content.split(_notice_header() + "\n", 1)[-1].split(STEER_HEADER + "\n", 1)[0]
    return any(line.startswith(f"- {job_id} ") for line in body.splitlines())


def notice_lines(notice):
    """The lines of one finished background job (as the job notice has always shown them)."""
    lines = [f"- {notice['job_id']} {notice['status']} (exit code {notice['exit_code']}, "
             f"{notice['log_bytes']} log bytes): {' '.join(notice['argv'])[:200]}"]
    if notice.get("tail"):
        lines.append("  last output:\n" + "\n".join("    " + line for line in notice["tail"].splitlines()))
    lines.extend(_association_lines(notice))
    return "\n".join(lines)


def _association_lines(notice):
    """Why the job was started and whether that still holds; nothing is made up when it was never stated."""
    link = notice.get("link")
    if not link or not (link["agent_session"] or any(link[key] for key in ("purpose", "expect", "next_step"))):
        return []
    lines = ["  " + "; ".join(f"{label}: {link[key] or '(not stated)'}" for label, key in
                             (("purpose", "purpose"), ("expected", "expect"), ("next step", "next_step")))]
    if link["purpose"] is None and (link["origin"] or {}).get("request"):
        lines.append("  user's words when it started: " + json.dumps(link["origin"]["request"], ensure_ascii=False))
    if link["goal"]["standing"] == "dropped":
        lines.append("  its goal was dropped: information only, do not carry out the next step")
    if notice.get("newer_inputs"):
        lines.append("  the user said more after this goal was last confirmed; check that it still holds: "
                     + json.dumps(notice["newer_inputs"], ensure_ascii=False))
    if notice.get("handed_over_by"):
        lines.append(f"  started by agent session {notice['handed_over_by']}, which has ended; you are responsible now")
    received = [item["session"] for item in link["delivery"]["received"]]
    if received:
        lines.append(f"  already received by {', '.join(str(item) for item in received)} but not settled: its follow-up "
                     "may have been done; check before doing it")
    if notice["status"] == "lost":
        lines.append("  its exit status is unknown: neither success nor failure; check before running anything again")
    return lines


def _batch_open(messages):
    from .agent import _tool_batch_open
    return _tool_batch_open(messages)


class JobOwnerMiddleware(AgentMiddleware):
    """A session without ``--session`` gets its id when it starts: its jobs belong to that id from then on."""

    def __init__(self, jobs):
        self.jobs = jobs

    def before_agent(self, state):
        if self.jobs.owner is None:
            self.jobs.owner = getattr(state, "session_id", None)


class InboxMiddleware(AgentMiddleware):
    """Deliver pending steer items before the next model call, at a safe boundary only.

    ``jobs`` (a JobManager) feeds the finished background jobs this agent is responsible for into the same
    inbox, so a job notice and a user's mid-turn line arrive in one message. Three steps are recorded
    separately: the notice was taken, the model received it (it replied after it), the job was settled.
    What was taken but never received is found again from the records, not from a summary.

    ``active(session_id)`` (main agent): is that worker still able to handle its own jobs? ``settles``: this
    agent has ``job_settle``, so a stated next step stays open until it says so; without it, a completed
    turn after the receipt settles the job as reported (a worker's report, the main agent's reply).
    """

    def __init__(self, inbox=None, *, jobs=None, active=None, settles=False):
        self.inbox = inbox if inbox is not None else Inbox()
        self.jobs = jobs
        self.active = active
        self.settles = settles

    def before_agent(self, state):
        metadata = getattr(state, "metadata", None)
        if isinstance(metadata, dict):
            self.inbox.restore(metadata.get(METADATA_KEY))
            self.inbox.sync(state)

    def before_model(self, state):
        if self.jobs is not None:
            self._collect(state)
        # Never between an assistant tool call and its reply (the kernel would refuse the request).
        if not _batch_open(state.messages):
            items = self.inbox.take(mode=STEER)
            if items:
                state.messages.append({"role": "user", "content": compose(items)})
                self.inbox.delivered(items, "model_call")
        self.inbox.sync(state)

    def after_model(self, state, message):
        self.inbox.sync(state)
        self._mark_received(state)

    def after_agent(self, state):
        self.inbox.sync(state)
        self._settle_reported(state)

    # ----- background jobs --------------------------------------------------------------
    def _tracks(self):
        return self.jobs is not None and hasattr(self.jobs, "unreceived")

    def _unreceived(self, state):
        return self.jobs.unreceived(getattr(state, "session_id", None), active=self.active)

    def _collect(self, state):
        if not self._tracks():
            for notice in self.jobs.take_notices():
                self.inbox.put(JOB, notice_lines(notice))
            return
        from baseagent.tools.job_wait import told
        inputs = direct_inputs(state.messages)
        for notice in self._unreceived(state):
            job_id = notice["job_id"]
            if told(state.messages, job_id) is not None:
                continue          # in the conversation already (job_wait or an earlier notice), never announced twice
            if not any(item.source == JOB and item.text.startswith(f"- {job_id} ") for item in self.inbox.pending()):
                confirmed = notice["link"]["goal"]["confirmed_inputs"]
                newer = inputs[confirmed:] if confirmed is not None else []
                self.inbox.put(JOB, notice_lines({**notice, "newer_inputs": [text[:150] for _, text in newer[-2:]]}),
                               item_id=f"job-{job_id}")
            self.jobs.mark_taken(job_id)

    def _mark_received(self, state):
        if not self._tracks():
            return
        from baseagent.tools.job_wait import answered, told
        for notice in self._unreceived(state):
            index = told(state.messages, notice["job_id"])
            if index is not None and answered(state.messages, index):
                self.jobs.mark_received(notice["job_id"], getattr(state, "session_id", None),
                                        getattr(state, "turn_id", None))

    def _settle_reported(self, state):
        from .state import RunStatus
        if not self._tracks() or getattr(state, "status", None) != RunStatus.COMPLETED:
            return
        from baseagent.tools.job_links import DROPPED, REPORTED, received_by
        self._mark_received(state)
        agent = getattr(state, "session_id", None)
        for job in self.jobs.unfinished(agent, active=self.active):
            link = job.link
            if not received_by(link, agent):
                continue
            if self.settles and link["next_step"] and link["goal"]["standing"] != DROPPED:
                continue          # the stated follow-up stays open until job_settle
            self.jobs.settle(job.id, REPORTED, agent=agent, turn=getattr(state, "turn_id", None))

    def reference(self, state):
        """The open jobs of this agent as reference text, or '' (also what a side conversation would freeze).

        Computed for each request and never stored in the conversation. Reading it takes no notice and
        changes no delivery state.
        """
        if not self._tracks():
            return ""
        from baseagent.tools.job_links import open_jobs_reference
        agent = getattr(state, "session_id", None)
        return open_jobs_reference(self.jobs.unfinished(agent, active=self.active), direct_inputs(state.messages),
                                   {item["job_id"]: item["phase"] for item in self.jobs.waits()}, agent)

    def wrap_model_call(self, request, handler):
        text = self.reference(request.state)
        if not text:
            return handler(request)
        # Same role and labelling as other system-added data (worker pushes, job notices): never a system
        # instruction, because declared purposes and quotes are not trusted instructions.
        messages = list(request.messages)
        at = next((index for index, message in enumerate(messages) if message["role"] not in ("system", "developer")),
                  len(messages))
        messages.insert(at, {"role": "user", "content": text})
        return handler(replace(request, messages=messages))


__all__ = ["Inbox", "InboxItem", "InboxMiddleware", "JobOwnerMiddleware", "INBOX_HEADER", "JOB", "LEAD", "METADATA_KEY", "MODES",
           "PUSH_HEADER", "QUEUE", "SOURCES", "STEER", "STEER_HEADER", "USER", "USER_HEADER", "WORKER", "compose",
           "direct_inputs", "is_inbox_message", "job_in_notice", "notice_lines", "user_parts"]
