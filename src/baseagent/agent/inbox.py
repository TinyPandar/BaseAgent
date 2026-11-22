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

import threading
import time
from dataclasses import asdict, dataclass, field
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
    def put(self, source, text, mode=STEER):
        item = InboxItem(source, text, mode)
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


def notice_lines(notice):
    """The lines of one finished background job (as the job notice has always shown them)."""
    lines = [f"- {notice['job_id']} {notice['status']} (exit code {notice['exit_code']}, "
             f"{notice['log_bytes']} log bytes): {' '.join(notice['argv'])[:200]}"]
    if notice.get("tail"):
        lines.append("  last output:\n" + "\n".join("    " + line for line in notice["tail"].splitlines()))
    return "\n".join(lines)


def _batch_open(messages):
    from .agent import _tool_batch_open
    return _tool_batch_open(messages)


class InboxMiddleware(AgentMiddleware):
    """Deliver pending steer items before the next model call, at a safe boundary only.

    ``jobs`` (a JobManager) feeds finished background jobs of the owning session into the
    same inbox, so a job notice and a user's mid-turn line arrive in one message.
    """

    def __init__(self, inbox=None, *, jobs=None):
        self.inbox = inbox if inbox is not None else Inbox()
        self.jobs = jobs

    def before_agent(self, state):
        metadata = getattr(state, "metadata", None)
        if isinstance(metadata, dict):
            self.inbox.restore(metadata.get(METADATA_KEY))
            self.inbox.sync(state)

    def before_model(self, state):
        if self.jobs is not None:
            for notice in self.jobs.take_notices():
                self.inbox.put(JOB, notice_lines(notice))
        # Never between an assistant tool call and its reply (the kernel would refuse the request).
        if not _batch_open(state.messages):
            items = self.inbox.take(mode=STEER)
            if items:
                state.messages.append({"role": "user", "content": compose(items)})
                self.inbox.delivered(items, "model_call")
        self.inbox.sync(state)

    def after_model(self, state, message):
        self.inbox.sync(state)

    def after_agent(self, state):
        self.inbox.sync(state)


__all__ = ["Inbox", "InboxItem", "InboxMiddleware", "INBOX_HEADER", "JOB", "LEAD", "METADATA_KEY", "MODES",
           "PUSH_HEADER", "QUEUE", "SOURCES", "STEER", "STEER_HEADER", "USER", "USER_HEADER", "WORKER", "compose",
           "is_inbox_message", "notice_lines", "user_parts"]
