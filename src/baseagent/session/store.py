"""SQLite checkpoints and tool ledger. External side effects cannot join a DB transaction."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator

from baseagent.agent.state import RunStatus, State
from baseagent.tools.result import ToolResult
from baseagent.agent.events import event
from baseagent.llm.response import TokenUsage


class SessionBusy(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class SessionStore:
    durable = True

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.path.parent / (self.path.name + ".locks")
        self.lock_dir.mkdir(exist_ok=True)
        self._lock_owners = threading.local()
        self._initialize()

    def _initialize(self):
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6):
                raise ValueError(f"unsupported session database version: {version}")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    turn_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed')),
                    request_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    terminal_request_json TEXT,
                    terminal_result_json TEXT,
                    result_json TEXT,
                    PRIMARY KEY(session_id, turn_id, call_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    turn_id TEXT NOT NULL,
                    event_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_session_sequence ON events(session_id, sequence);
                CREATE TABLE IF NOT EXISTS cancellation_requests (
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    turn_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    PRIMARY KEY(session_id, turn_id)
                );
                CREATE TABLE IF NOT EXISTS event_retention (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    through_sequence INTEGER NOT NULL,
                    deleted_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_memories (
                    workspace_root TEXT NOT NULL,
                    key TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    content TEXT,
                    source_session_id TEXT NOT NULL,
                    source_note_json TEXT NOT NULL,
                    withdrawn INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(workspace_root, key)
                );
                CREATE TABLE IF NOT EXISTS task_nodes (
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    root_turn_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    parent_task_id TEXT NOT NULL,
                    spawn_call_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    depth INTEGER NOT NULL CHECK(depth >= 1),
                    request_digest TEXT NOT NULL,
                    node_turn_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    PRIMARY KEY(session_id, root_turn_id, task_id),
                    UNIQUE(session_id, root_turn_id, parent_task_id, spawn_call_id),
                    UNIQUE(session_id, node_turn_id)
                );
                PRAGMA user_version=6;
                COMMIT;
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level="IMMEDIATE")
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def exclusive(self, session_id: str) -> Iterator[None]:
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 128:
            raise ValueError("session_id must be 1-128 nonblank characters")
        path = self.lock_dir / (sha256(session_id.encode()).hexdigest() + ".lock")
        with path.open("a+b") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SessionBusy(f"session is already running: {session_id}") from exc
            try:
                owned = getattr(self._lock_owners, "sessions", set())
                self._lock_owners.sessions = owned | {session_id}
                yield
            finally:
                self._lock_owners.sessions = owned
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self, session_id: str) -> State | None:
        with self._connection() as connection:
            row = connection.execute("SELECT state_json FROM sessions WHERE id=?", (session_id,)).fetchone()
        return State.from_dict(json.loads(row[0])) if row else None

    def create_task(self, root_state, *, parent_task_id="root", call_id, name, state, max_depth=4, max_nodes=32):
        from .tasks import create
        return create(self, root_state, parent_task_id=parent_task_id, call_id=call_id, name=name, state=state, max_depth=max_depth, max_nodes=max_nodes)

    def task_tree(self, session_id, root_turn_id=None):
        from .tasks import inspect_tree
        return inspect_tree(self, session_id, root_turn_id)

    def task_node(self, root_state, task_id):
        from .tasks import TaskNodeStore
        return TaskNodeStore.open(self, root_state, task_id)

    def decide_task_tool(self, session_id, task_id, call_id, decision, expected_digest):
        from .task_recovery import decide
        return decide(self, session_id, task_id, call_id, decision, expected_digest)

    def resolve_task_call(self, session_id, task_id, call_id, result):
        from .task_recovery import resolve_call
        return resolve_call(self, session_id, task_id, call_id, result)

    def resolve_task_usage(self, session_id, task_id, usage, unknown_calls):
        from .task_recovery import resolve_usage
        return resolve_usage(self, session_id, task_id, usage, unknown_calls)

    def acknowledge_task_bound(self, session_id, task_id):
        from .task_recovery import acknowledge_bound
        return acknowledge_bound(self, session_id, task_id)

    def cancellation(self, session_id, turn_id):
        with self._connection() as connection:
            row = connection.execute("SELECT request_id FROM cancellation_requests WHERE session_id=? AND turn_id=?", (session_id, turn_id)).fetchone()
        return row[0] if row else None

    def request_cancellation(self, session_id, *, task_id=None):
        """May run while the executor holds its session lock; never edits its state."""
        if task_id is not None:
            from .task_cancellation import request
            return request(self, session_id, task_id)
        from uuid import uuid4
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state_json FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise ValueError("session does not exist")
            state = State.from_dict(json.loads(row[0]))
            if state.status == RunStatus.COMPLETED:
                raise ValueError("completed session has no active turn to cancel")
            existing = connection.execute("SELECT request_id FROM cancellation_requests WHERE session_id=? AND turn_id=?", (session_id, state.turn_id)).fetchone()
            if existing:
                return existing[0]
            request_id = uuid4().hex
            connection.execute("INSERT INTO cancellation_requests VALUES(?, ?, ?)", (session_id, state.turn_id, request_id))
            self._event(connection, state, event("cancellation_requested", request_id=request_id))
            return request_id

    def clear_cancellation(self, session_id, request_id, *, task_id=None):
        """Acknowledge one request before resume; never clear a newer request."""
        if task_id is not None:
            from .task_cancellation import clear
            return clear(self, session_id, task_id, request_id)
        with self.exclusive(session_id), self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state_json FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise ValueError("session does not exist")
            state = State.from_dict(json.loads(row[0]))
            changed = connection.execute("DELETE FROM cancellation_requests WHERE session_id=? AND turn_id=? AND request_id=?",
                                         (session_id, state.turn_id, request_id)).rowcount
            if changed != 1:
                raise ValueError("cancellation request changed or does not exist")
            self._event(connection, state, event("cancellation_cleared", request_id=request_id))

    def decide_tool(self, session_id: str, call_id: str, decision: str, expected_digest: str) -> State:
        """Record a human decision for the currently paused, unexecuted call."""
        from baseagent.tools.policy import request_digest
        if decision not in {"allow", "deny"}:
            raise ValueError("approval decision must be allow or deny")
        with self.exclusive(session_id), self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state_json FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise ValueError("session does not exist")
            state = State.from_dict(json.loads(row[0]))
            approval = state.metadata.get("approval_request")
            if state.status != RunStatus.AWAITING_APPROVAL or not approval or approval["call_id"] != call_id or approval["turn_id"] != state.turn_id:
                raise ValueError("call is not awaiting approval")
            call = connection.execute("SELECT status, request_json FROM tool_calls WHERE session_id=? AND turn_id=? AND call_id=?",
                                      (session_id, state.turn_id, call_id)).fetchone()
            if not call or call["status"] != "pending":
                raise ValueError("approval requires an unexecuted pending call")
            function = json.loads(call["request_json"])["function"]
            actual = request_digest(function["name"], function["arguments"])
            if actual != expected_digest or actual != approval["request_digest"]:
                raise ValueError("approval request changed; inspect the exact pending request again")
            state.metadata.setdefault("tool_approvals", {})[call_id] = {**approval, "decision": decision}
            # Keep the paused state: deciding never executes the tool or resets budgets.
            self._save(connection, state)
            self._event(connection, state, event("approval_decided", call_id=call_id, decision=decision))
            return state

    def list_sessions(self, *, after_id=None, limit=100):
        from .maintenance import list_sessions
        return list_sessions(self, after_id=after_id, limit=limit)

    def export_session(self, session_id, target):
        from .maintenance import export_session
        return export_session(self, session_id, target)

    def inspect_session(self, session_id):
        from .maintenance import inspect_session
        return inspect_session(self, session_id)

    def backup(self, target, *, timeout=30.0):
        from .maintenance import backup
        return backup(self, target, timeout=timeout)

    def delete_session(self, session_id, *, discard_unfinished=False):
        from .maintenance import delete_session
        return delete_session(self, session_id, discard_unfinished=discard_unfinished)

    def cleanup(self, older_than_seconds, *, dry_run=True, after_id=None, limit=100):
        from .maintenance import cleanup
        return cleanup(self, older_than_seconds, dry_run=dry_run, after_id=after_id, limit=limit)

    @staticmethod
    def _save(connection: sqlite3.Connection, state: State) -> None:
        if not state.session_id:
            raise ValueError("persistent state requires a session_id")
        connection.execute("""
            INSERT INTO sessions(id, state_json) VALUES(?, ?)
            ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json, updated_at=CURRENT_TIMESTAMP
        """, (state.session_id, _json(state.to_dict())))

    @staticmethod
    def _event(connection, state: State, value: dict) -> None:
        connection.execute("INSERT INTO events(session_id, turn_id, event_json) VALUES(?, ?, ?)", (state.session_id, state.turn_id, _json(value)))

    def events(self, session_id: str, *, after: int = 0, limit: int = 100) -> list[dict]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("event cursor must be nonnegative and limit 1-1000")
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM events WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?", (session_id, after, limit)).fetchall()
        return [{"sequence": row["sequence"], "session_id": row["session_id"], "turn_id": row["turn_id"], **json.loads(row["event_json"])} for row in rows]

    def event_page(self, session_id, **kwargs):
        from .eventlog import page
        return page(self, session_id, **kwargs)

    def follow_events(self, session_id, **kwargs):
        from .eventlog import follow
        return follow(self, session_id, **kwargs)

    def prune_events(self, session_id, **kwargs):
        from .eventlog import prune
        return prune(self, session_id, **kwargs)

    def publish_memory(self, state, workspace_root, key, expected_revision):
        from .knowledge import publish
        return publish(self, state, workspace_root, key, expected_revision)

    def search_shared_memory(self, workspace_root, **kwargs):
        from .knowledge import search
        return search(self, workspace_root, **kwargs)

    def withdraw_memory(self, state, workspace_root, key, expected_revision):
        from .knowledge import withdraw
        return withdraw(self, state, workspace_root, key, expected_revision)

    def save(self, state: State, calls: list[dict[str, Any]] = (), *, event: dict | None = None) -> None:
        """Checkpoint the assistant message and all its pending calls atomically."""
        with self._connection() as connection:
            self._save(connection, state)
            for call in calls:
                connection.execute("""
                    INSERT INTO tool_calls(session_id, turn_id, call_id, status, request_json)
                    VALUES(?, ?, ?, 'pending', ?)
                """, (state.session_id, state.turn_id, call["id"], _json(call)))
            if event is not None:
                self._event(connection, state, event)

    def calls(self, session_id: str, turn_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM tool_calls WHERE session_id=? AND turn_id=? ORDER BY rowid", (session_id, turn_id)).fetchall()
        return [dict(row) for row in rows]

    def call(self, state: State, call_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM tool_calls WHERE session_id=? AND turn_id=? AND call_id=?", (state.session_id, state.turn_id, call_id)).fetchone()
        if row is None:
            raise ValueError(f"missing tool ledger entry: {call_id}")
        return dict(row)

    def start_call(self, state: State, call_id: str) -> None:
        with self._connection() as connection:
            changed = connection.execute("""
                UPDATE tool_calls SET status='running'
                WHERE session_id=? AND turn_id=? AND call_id=? AND status='pending'
            """, (state.session_id, state.turn_id, call_id)).rowcount
            if changed != 1:
                raise ValueError("only pending tool calls can start")
            self._save(connection, state)
            self._event(connection, state, event("tool_started", call_id=call_id))

    def record_attempt(self, state: State, call_id: str, request: dict[str, Any]) -> None:
        with self._connection() as connection:
            changed = connection.execute("""
                UPDATE tool_calls SET attempts=attempts+1, terminal_request_json=?, terminal_result_json=NULL
                WHERE session_id=? AND turn_id=? AND call_id=? AND status='running'
            """, (_json(request), state.session_id, state.turn_id, call_id)).rowcount
            if changed != 1:
                raise ValueError("tool attempt requires a running ledger entry")
            self._save(connection, state)
            self._event(connection, state, event("tool_attempt", call_id=call_id, name=request["name"], tool_calls=state.tool_calls))

    def record_terminal_result(self, state: State, call_id: str, result: ToolResult, *, duration_seconds: float | None = None) -> None:
        with self._connection() as connection:
            changed = connection.execute("""
                UPDATE tool_calls SET terminal_result_json=?
                WHERE session_id=? AND turn_id=? AND call_id=? AND status='running'
            """, (_json(result.to_dict()), state.session_id, state.turn_id, call_id)).rowcount
            if changed != 1:
                raise ValueError("tool result requires a running ledger entry")
            self._save(connection, state)
            self._event(connection, state, event("tool_returned", call_id=call_id, ok=result.ok, error_code=result.error.code if result.error else None, duration_seconds=duration_seconds))

    def complete_call(self, state: State, call_id: str, result: ToolResult) -> None:
        """Commit the result and its transcript entry in a single transaction."""
        with self._connection() as connection:
            changed = connection.execute("""
                UPDATE tool_calls SET status='completed', result_json=?
                WHERE session_id=? AND turn_id=? AND call_id=? AND status='running'
            """, (_json(result.to_dict()), state.session_id, state.turn_id, call_id)).rowcount
            if changed != 1:
                raise ValueError("only running calls can complete")
            self._save(connection, state)
            self._event(connection, state, event("tool_completed", call_id=call_id, ok=result.ok, error_code=result.error.code if result.error else None))

    def resolve_usage(self, session_id: str, usage: TokenUsage, unknown_calls: int) -> State:
        """Record a user-verified aggregate for all unresolved model attempts."""
        if not isinstance(usage, TokenUsage):
            raise ValueError("verified usage must be TokenUsage")
        with self.exclusive(session_id):
            state = self.load(session_id)
            if state is None:
                raise ValueError("session does not exist")
            if type(unknown_calls) is not int or unknown_calls < 1 or unknown_calls != state.unknown_usage_calls:
                raise ValueError("unknown_calls must match all unresolved model attempts")
            if any(node["state"]["unknown_usage_calls"] for node in self.task_tree(session_id, state.turn_id)["nodes"]):
                raise ValueError("child model usage requires node-targeted reconciliation; root aggregate cannot clear it")
            state.prompt_tokens += usage.prompt_tokens
            state.completion_tokens += usage.completion_tokens
            state.total_tokens += usage.total_tokens
            state.unknown_usage_calls = 0
            if "direct_usage" in state.metadata:
                direct = state.metadata["direct_usage"]
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    direct[key] += getattr(usage, key)
                direct["unknown_usage_calls"] -= unknown_calls
            estimated = state.metadata.get("estimated_reservations", {})
            if state.model_reservations and not estimated and usage.total_tokens > sum(state.model_reservations.values()):
                state.metadata["request_bound_violation"] = {"attempt": "reconciled", "reserved_tokens": sum(state.model_reservations.values()), "reported_tokens": usage.total_tokens}
            state.model_reservations = {}
            state.metadata.pop("estimated_reservations", None)
            state.error = None
            if state.status != RunStatus.COMPLETED:
                state.status = RunStatus.INTERRUPTED
            self.save(state, event=event("usage_reconciled", unknown_calls=unknown_calls, total_tokens=usage.total_tokens))
            return state

    def resolve_call(self, session_id: str, call_id: str, result: ToolResult) -> State:
        """User supplies a checked outcome; this method never executes the tool."""
        encoded = _json(result.to_dict())
        if len(encoded.encode("utf-8")) > 256_000:
            raise ValueError("recovery result exceeds 256 KB")
        with self.exclusive(session_id):
            state = self.load(session_id)
            if state is None:
                raise ValueError("session does not exist")
            record = self.call(state, call_id)
            if record["status"] != "running":
                raise ValueError("only unresolved running calls can be reconciled")
            state.messages.append({"role": "tool", "tool_call_id": call_id, "content": encoded})
            state.blocked_tool_calls = [item for item in state.blocked_tool_calls if item != call_id]
            state.status = RunStatus.INTERRUPTED
            state.error = None
            self.complete_call(state, call_id, result)
            return state
