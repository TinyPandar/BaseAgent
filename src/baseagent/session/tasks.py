"""Task-node checkpoints under the root session's lock and transaction.

This is persistence infrastructure, not a delegation runtime or budget controller.
Each node uses a distinct turn ID in the existing tool ledger. Root and ancestor
snapshots commit with node changes; external side effects remain nontransactional.
"""

from copy import deepcopy
from contextlib import contextmanager
from hashlib import sha256
import json
from uuid import uuid4

from baseagent.agent.events import event
from baseagent.agent.state import RunStatus, State
from .store import SessionStore, _json


def _locked(store, root):
    if not isinstance(root, State) or root.session_id not in getattr(store._lock_owners, "sessions", set()):
        raise ValueError("task mutation requires the root session lock on this store and thread")


def _root(connection, root):
    row = connection.execute("SELECT state_json FROM sessions WHERE id=?", (root.session_id,)).fetchone()
    if row is None or json.loads(row[0])["turn_id"] != root.turn_id:
        raise ValueError("task belongs to a missing or different root turn")


def _record(row):
    value = dict(row)
    value["state"] = json.loads(value.pop("state_json"))
    return value


def inspect_tree(store, session_id, root_turn_id=None):
    """Read node snapshots and tool ledgers from one consistent SQLite snapshot."""
    with store._connection() as connection:
        connection.execute("BEGIN")
        root = connection.execute("SELECT state_json FROM sessions WHERE id=?", (session_id,)).fetchone()
        if root is None:
            raise ValueError("session does not exist")
        root_turn_id = root_turn_id or json.loads(root[0])["turn_id"]
        rows = connection.execute("SELECT * FROM task_nodes WHERE session_id=? AND root_turn_id=? ORDER BY depth,rowid", (session_id, root_turn_id)).fetchall()
        nodes = []
        for row in rows:
            node = _record(row)
            node["tool_calls"] = [dict(call) for call in connection.execute("SELECT * FROM tool_calls WHERE session_id=? AND turn_id=? ORDER BY rowid", (session_id, row["node_turn_id"]))]
            nodes.append(node)
    return {"session_id": session_id, "root_turn_id": root_turn_id, "current_root_state": json.loads(root[0]), "nodes": nodes}


def create(store, root, *, parent_task_id, call_id, name, state, max_depth, max_nodes):
    _locked(store, root)
    if any(type(value) is not int or value < 1 for value in (max_depth, max_nodes)):
        raise ValueError("task depth and quantity limits must be positive integers")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in (parent_task_id, call_id, name)):
        raise ValueError("task parent, call and name must be 1-128 nonblank characters")
    if not isinstance(state, State) or state.session_id != root.session_id or state.workspace_root != root.workspace_root or not state.turn_id or state.turn_id == root.turn_id:
        raise ValueError("task state requires the same session/workspace and a distinct turn ID")
    # Stable initial request identity omits generated IDs, counters and timestamps.
    identity = {"name": name, "messages": state.messages, "runtime_contract": state.metadata.get("runtime_contract"),
                "limits": {key: getattr(state, key) for key in ("max_steps", "max_model_calls", "max_tool_calls", "max_total_tokens", "max_duration_seconds", "max_context_bytes", "max_tool_context_bytes", "preauthorize_model")}}
    digest = sha256(_json(identity).encode("utf-8")).hexdigest()
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _root(connection, root)
        parent_turn, depth = root.turn_id, 1
        if parent_task_id != "root":
            parent = connection.execute("SELECT * FROM task_nodes WHERE session_id=? AND root_turn_id=? AND task_id=?", (root.session_id, root.turn_id, parent_task_id)).fetchone()
            if parent is None:
                raise ValueError("parent task does not exist in this root turn")
            parent_turn, depth = parent["node_turn_id"], parent["depth"] + 1
        caller = connection.execute("SELECT status FROM tool_calls WHERE session_id=? AND turn_id=? AND call_id=?", (root.session_id, parent_turn, call_id)).fetchone()
        if caller is None or caller[0] != "running":
            raise ValueError("task creation requires a running parent tool call")
        existing = connection.execute("SELECT * FROM task_nodes WHERE session_id=? AND root_turn_id=? AND parent_task_id=? AND spawn_call_id=?", (root.session_id, root.turn_id, parent_task_id, call_id)).fetchone()
        if existing:
            if existing["request_digest"] != digest:
                raise ValueError("existing task request changed; recovery cannot create a replacement")
            return _record(existing)
        if root.status == RunStatus.COMPLETED or state.status != RunStatus.RUNNING:
            raise ValueError("new task requires active root and node states")
        if depth > max_depth or connection.execute("SELECT count(*) FROM task_nodes WHERE session_id=? AND root_turn_id=?", (root.session_id, root.turn_id)).fetchone()[0] >= max_nodes:
            raise ValueError("task depth or quantity limit exceeded")
        task_id = uuid4().hex
        store._save(connection, root)
        connection.execute("INSERT INTO task_nodes VALUES(?,?,?,?,?,?,?,?,?,?)", (root.session_id, root.turn_id, task_id, parent_task_id, call_id, name, depth, digest, state.turn_id, _json(state.to_dict())))
        store._event(connection, root, event("task_created", task_id=task_id, parent_task_id=parent_task_id, depth=depth))
        row = connection.execute("SELECT * FROM task_nodes WHERE session_id=? AND root_turn_id=? AND task_id=?", (root.session_id, root.turn_id, task_id)).fetchone()
        return _record(row)


class TaskNodeStore(SessionStore):
    """Internal node view for the core loop; root lock must already be held.

    Inherited checkpoint/ledger writes call this view's `_save` and `_event`,
    retaining the same transaction as the root and all ancestor snapshots.
    Public session maintenance and recovery must use the owning root store.
    """

    def __init__(self, store, root, record, parent):
        self.owner, self.root_state, self.parent = store, root, parent
        self.task_id, self.parent_task_id = record["task_id"], record["parent_task_id"]
        self.state = State.from_dict(record["state"])
        self._committed_json = _json(record["state"])
        self._pending = None
        self.path, self.lock_dir = store.path, store.lock_dir
        self.durable = store.durable

    @contextmanager
    def _connection(self):
        pending = []
        previous = self._pending
        self._pending = pending
        try:
            with self.owner._connection() as connection:
                yield connection
            # Update optimistic snapshots only after the transaction commits.
            for view, snapshot, state in pending:
                view._committed_json, view.state = snapshot, state
        finally:
            self._pending = previous

    @classmethod
    def open(cls, store, root, task_id):
        _locked(store, root)
        tree = inspect_tree(store, root.session_id, root.turn_id)
        records = {node["task_id"]: node for node in tree["nodes"]}
        chain, seen, current = [], set(), task_id
        while current != "root":
            if current in seen or current not in records:
                raise ValueError("missing task or invalid parent chain")
            seen.add(current)
            chain.append(records[current])
            current = records[current]["parent_task_id"]
        parent = None
        for record in reversed(chain):
            parent = cls(store, root, record, parent)
        if parent is None:
            raise ValueError("root is not a child task")
        return parent

    def exclusive(self, session_id):
        raise ValueError("node execution must use the already-held root lock")

    def load(self, session_id):
        if session_id != self.root_state.session_id:
            raise ValueError("node belongs to another session")
        with self._connection() as connection:
            row = connection.execute("SELECT state_json FROM task_nodes WHERE session_id=? AND root_turn_id=? AND task_id=?", (session_id, self.root_state.turn_id, self.task_id)).fetchone()
        return State.from_dict(json.loads(row[0])) if row else None

    def cancellation(self, session_id, turn_id):
        if session_id != self.state.session_id or turn_id != self.state.turn_id:
            raise ValueError("cancellation lookup must target this node")
        turns, parent = [self.state.turn_id, self.root_state.turn_id], self.parent
        while parent is not None:
            turns.append(parent.state.turn_id)
            parent = parent.parent
        with self.owner._connection() as connection:
            row = connection.execute("SELECT request_id FROM cancellation_requests WHERE session_id=? AND turn_id IN (" + ",".join("?" for _ in turns) + ") LIMIT 1", (session_id, *turns)).fetchone()
        return row[0] if row else None

    def execution_scope(self, *, cancellation=None):
        from baseagent.agent.scope import ExecutionScope
        ancestors, parent = [], self.parent
        while parent is not None:
            ancestors.append(parent.state)
            parent = parent.parent
        ancestors.append(self.root_state)
        return ExecutionScope(self.state, ancestors, cancellation=cancellation)

    def _save(self, connection, state, *, pending=None):
        _locked(self.owner, self.root_state)
        _root(connection, self.root_state)
        if state.session_id != self.root_state.session_id or state.turn_id != self.state.turn_id or state.workspace_root != self.root_state.workspace_root:
            raise ValueError("cannot change task session, turn or workspace")
        pending = self._pending if pending is None else pending
        if pending is None:
            raise ValueError("node checkpoint requires its transaction context")
        if self.parent:
            self.parent._save(connection, self.parent.state, pending=pending)
        else:
            self.owner._save(connection, self.root_state)
        snapshot = _json(state.to_dict())
        changed = connection.execute("UPDATE task_nodes SET state_json=? WHERE session_id=? AND root_turn_id=? AND task_id=? AND node_turn_id=? AND state_json=?", (snapshot, state.session_id, self.root_state.turn_id, self.task_id, state.turn_id, self._committed_json)).rowcount
        if changed != 1:
            raise ValueError("task checkpoint changed or disappeared; reopen the node view")
        pending.append((self, snapshot, state))

    def _event(self, connection, state, value):
        value = deepcopy(value)
        value["task_id"], value["parent_task_id"] = self.task_id, self.parent_task_id
        self.owner._event(connection, self.root_state, value)
