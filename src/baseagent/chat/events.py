"""In-memory chat events for chat frontends (CLI, TUI, Web).

These are *not* the persistent metadata events in SQLite (``agent/events.py``,
``baseagent --events``). Those are durable, fixed-shape records with no prompt,
argument or result text. Chat events are display data for a live frontend:
they carry previews (user text, the model's reply, argument and result
previews, worker summaries), live only in a bounded memory buffer and are lost
when the process exits. After a restart a frontend rebuilds its view from the
session state (a ``resync`` snapshot), never from old chat events.

Every event has a per-buffer id that only grows. ``after(after_id)`` returns
the events after an id; when that id has already been pushed out of the buffer
(or belongs to an earlier process), it returns ``None`` and the caller sends a
``resync`` snapshot instead of silently skipping events.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from uuid import uuid4

DEFAULT_CAPACITY = 1000
PREVIEW_CHARS = 300

# kind -> (data fields, when it is emitted). docs/ui.md renders the same table.
KINDS = {
    "user_message": ("text", "a chat line was accepted as a new main-agent turn"),
    "assistant_message": ("text, status", "a turn ended with a reply from the main agent (or single agent)"),
    "tool_call": ("worker, call_id, name, arguments (preview)", "a tool call is dispatched (worker null = main agent)"),
    "tool_result": ("worker, call_id, name, ok, preview", "that tool call returned; preview only, never the body"),
    "worker_started": ("worker, task, allow_writes, session_id", "delegate_task started a background worker"),
    "worker_progress": ("worker, line", "a worker trace line (model step, tool name, result preview)"),
    "worker_done": ("worker, summary, citations, paths, text", "a worker completed; text is the CLI push"),
    "worker_failed": ("worker, detail, error, text", "a worker stopped without completing"),
    "worker_cancelled": ("worker, detail, text", "a worker was cancelled (/cancel, exit, Ctrl+C)"),
    "approval_requested": ("worker, call_id, digest, name, arguments, task, session_id",
                           "a call waits for allow/deny (worker null = main agent / single agent)"),
    "approval_decided": ("worker, call_id, digest, decision, text", "an allow/deny was recorded"),
    "status": ("session_id, busy, budget, tokens, workers, pending_approvals", "after turns, worker changes, refresh"),
    "notice": ("stream, text", "an explanation line the CLI prints (limits, cancel results, usage hints)"),
    "error": ("message", "a command was rejected or a session operation failed"),
    "resync": ("snapshot", "synthetic: the requested id is no longer buffered; replace the view"),
}


@dataclass(frozen=True)
class ChatEvent:
    id: int
    kind: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "ts": self.ts, "data": self.data}


class EventBuffer:
    """Thread-safe bounded event log with monotonically increasing ids."""

    def __init__(self, capacity=DEFAULT_CAPACITY):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("event buffer capacity must be a positive integer")
        self.capacity = capacity
        # Distinguishes this process's ids from another process serving the same session.
        self.epoch = uuid4().hex[:8]
        self._events = deque(maxlen=capacity)
        self._last = 0
        self._changed = threading.Condition()
        self._listeners = []

    @property
    def last_id(self):
        with self._changed:
            return self._last

    def publish(self, kind, **data):
        if kind not in KINDS or kind == "resync":
            raise ValueError(f"unknown chat event kind: {kind}")
        with self._changed:
            self._last += 1
            item = ChatEvent(self._last, kind, data)
            self._events.append(item)
            listeners = list(self._listeners)
            self._changed.notify_all()
        for listener in listeners:
            listener(item)
        return item

    def subscribe(self, listener):
        """Call listener(event) in the publishing thread; returns an unsubscribe function."""
        with self._changed:
            self._listeners.append(listener)

        def unsubscribe():
            with self._changed:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def after(self, after_id):
        """Events with id > after_id, or None when some of them are no longer buffered."""
        with self._changed:
            if after_id is None or after_id < 0 or after_id > self._last:
                return None
            first = self._events[0].id if self._events else self._last + 1
            if after_id < first - 1:
                return None
            return [item for item in self._events if item.id > after_id]

    def wait(self, after_id, timeout):
        """Block until an event newer than after_id exists (or timeout). Returns True if one does."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while self._last <= after_id:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
            return True

    def parse_cursor(self, value):
        """Parse an SSE Last-Event-ID ("<epoch>:<id>" or "<id>"). Another epoch means resync (None)."""
        if value is None or value == "":
            return None
        text = str(value).strip()
        epoch, _, number = text.rpartition(":")
        if epoch and epoch != self.epoch:
            return None
        try:
            return int(number)
        except ValueError:
            return None

    def cursor(self, event_id):
        return f"{self.epoch}:{event_id}"


def preview(value, limit=PREVIEW_CHARS):
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."
