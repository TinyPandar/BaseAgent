"""Machine-readable one-shot output: ``--output-format json|jsonl`` (schema: docs/output.md).

``json`` prints exactly one object on stdout when the run ends. ``jsonl`` prints one
event per line while the turn runs, using the metadata envelope of
``agent/events.py`` (``{type, timestamp, data}``; no prompts, arguments or result
bodies), and ends with a ``result`` line whose ``data`` is the same object ``json``
prints. Everything meant for humans (``Session:``, trace lines, warnings, worker
output) goes to stderr in both formats.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from decimal import Decimal

from baseagent.agent.state import RunStatus

SCHEMA_VERSION = 1
FORMATS = ("text", "json", "jsonl")
POLL_SECONDS = 0.1
# CNY per million tokens, used only when the configured model has a known public price.
# qwen-plus non-thinking: the rates the evaluation metering and --budget-cny already use.
KNOWN_RATES = {"qwen-plus": (Decimal("0.8"), Decimal(2))}


def exit_code(status):
    if status == RunStatus.COMPLETED:
        return 0
    return 130 if status == RunStatus.INTERRUPTED else 1


def usage(state, model_name=None):
    if state is None:
        value = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "unknown_usage_calls": 0,
                 "model_calls": 0, "tool_calls": 0, "steps": 0}
    else:
        value = {"prompt_tokens": state.prompt_tokens, "completion_tokens": state.completion_tokens,
                 "total_tokens": state.total_tokens, "unknown_usage_calls": state.unknown_usage_calls,
                 "model_calls": state.model_calls, "tool_calls": state.tool_calls, "steps": state.step}
    rates = KNOWN_RATES.get((model_name or "").strip().lower())
    if state is not None and rates and state.unknown_usage_calls == 0:
        cost = (Decimal(state.prompt_tokens) * rates[0] + Decimal(state.completion_tokens) * rates[1]) / Decimal(1_000_000)
        value["cny"] = float(cost.quantize(Decimal("0.000001")))
    return value


def pending_approval(state, store=None):
    """The exact call a human must approve or deny (root call, or a named-subtask node call)."""
    if state is None or state.status != RunStatus.AWAITING_APPROVAL:
        return None
    request = state.metadata.get("approval_request") or {}
    task_id = None
    if not request and store is not None:
        from baseagent.chat.text import pending_worker
        node = pending_worker(store, state)
        if node is not None:
            request, task_id = node["state"]["metadata"].get("approval_request") or {}, node["task_id"]
    if not request.get("call_id"):
        return None
    value = {"call_id": request["call_id"], "digest": request.get("request_digest"), "name": request.get("name")}
    if task_id is not None:
        value["task_id"] = task_id
    return value


def result(state=None, *, session_id=None, error=None, status=None, code=None, model_name=None, store=None):
    """The ``json`` object (and the ``data`` of the final ``jsonl`` line)."""
    if state is not None:
        status = RunStatus(state.status).value if status is None else status
        code = exit_code(state.status) if code is None else code
        session_id = state.session_id or session_id
    return {"schema_version": SCHEMA_VERSION, "status": status, "final_answer": state.final_answer if state else None,
            "error": error if error is not None else (state.error if state else None), "session_id": session_id,
            "usage": usage(state, model_name), "exit_code": code, "pending_approval": pending_approval(state, store)}


def envelope(item):
    """A stored metadata event as one jsonl line: ``{type, timestamp, data}`` only."""
    data = dict(item.get("data") or {})
    if item.get("type") == "model_delta" and isinstance(data.get("delta"), str):
        # Streamed text is message content, not metadata: keep only its size.
        data["chars"] = len(data.pop("delta"))
    for key in ("task_id", "parent_task_id"):
        if key in item and key not in data:
            data[key] = item[key]
    return {"type": item.get("type"), "timestamp": item.get("timestamp"), "data": data}


class MachineOutput:
    """Writes the chosen machine format to stdout; text format writes nothing here."""

    def __init__(self, fmt, *, stream=None):
        if fmt not in FORMATS:
            raise ValueError(f"unknown output format: {fmt}")
        self.format = fmt
        self.stream = stream or sys.stdout
        self._lock = threading.Lock()
        self._started = False
        self._follower = None

    @property
    def enabled(self):
        return self.format != "text"

    def _write(self, value):
        with self._lock:
            self.stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str) + "\n")
            self.stream.flush()

    def start(self, session_id):
        """jsonl: the first line names the schema version and the session."""
        if self.format != "jsonl" or self._started:
            return
        self._started = True
        self._write({"type": "output_started", "timestamp": time.time(),
                     "data": {"schema_version": SCHEMA_VERSION, "format": "jsonl", "session_id": session_id}})

    def follow(self, store, session_id):
        """jsonl: stream this session's new metadata events while the turn runs."""
        if self.format != "jsonl":
            return
        self.start(session_id)
        self._follower = _Follower(store, session_id, lambda item: self._write(envelope(item)))
        self._follower.begin()

    def stop(self):
        if self._follower is not None:
            follower, self._follower = self._follower, None
            follower.close()

    def finish(self, value):
        self.stop()
        if self.format == "json":
            self._write(value)
        elif self.format == "jsonl":
            self.start(value.get("session_id"))
            self._write({"type": "result", "timestamp": time.time(), "data": value})


class _Follower:
    def __init__(self, store, session_id, emit):
        self.store, self.session_id, self.emit = store, session_id, emit
        with store._connection() as connection:
            row = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM events WHERE session_id=?",
                                     (session_id,)).fetchone()
        self.cursor = row[0]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jsonl-events", daemon=True)
        self._drain_lock = threading.Lock()

    def begin(self):
        self._thread.start()

    def _run(self):
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.drain()
            except Exception:  # noqa: BLE001, S112 -- a busy database is retried; the final drain reports the rest.
                continue

    def drain(self):
        with self._drain_lock:
            while True:
                items = self.store.events(self.session_id, after=self.cursor, limit=1000)
                for item in items:
                    self.cursor = item["sequence"]
                    self.emit(item)
                if len(items) < 1000:
                    return

    def close(self):
        self._stop.set()
        self._thread.join(5)
        self.drain()
