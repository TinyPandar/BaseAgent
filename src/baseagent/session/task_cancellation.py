"""Current-turn node cancellation, independent of the executor's root lock."""

import json
from uuid import uuid4

from baseagent.agent.events import event
from baseagent.agent.state import RunStatus, State


def _target(connection, session_id, task_id):
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 128:
        raise ValueError("task_id must be 1-128 nonblank characters")
    row = connection.execute("SELECT state_json FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise ValueError("session does not exist")
    root = State.from_dict(json.loads(row[0]))
    node = connection.execute("SELECT * FROM task_nodes WHERE session_id=? AND root_turn_id=? AND task_id=?", (session_id, root.turn_id, task_id)).fetchone()
    if node is None:
        raise ValueError("task does not exist in the current root turn")
    return root, node, State.from_dict(json.loads(node["state_json"]))


def _event(store, connection, root, node, kind, identifier):
    value = event(kind, request_id=identifier)
    value.update(task_id=node["task_id"], parent_task_id=node["parent_task_id"])
    store._event(connection, root, value)


def request(store, session_id, task_id):
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        root, node, state = _target(connection, session_id, task_id)
        if root.status == RunStatus.COMPLETED or state.status == RunStatus.COMPLETED:
            raise ValueError("completed task has no active turn to cancel")
        row = connection.execute("SELECT request_id FROM cancellation_requests WHERE session_id=? AND turn_id=?", (session_id, state.turn_id)).fetchone()
        if row:
            return row[0]
        identifier = uuid4().hex
        connection.execute("INSERT INTO cancellation_requests VALUES(?, ?, ?)", (session_id, state.turn_id, identifier))
        _event(store, connection, root, node, "cancellation_requested", identifier)
        return identifier


def clear(store, session_id, task_id, request_id):
    with store.exclusive(session_id), store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        root, node, state = _target(connection, session_id, task_id)
        changed = connection.execute("DELETE FROM cancellation_requests WHERE session_id=? AND turn_id=? AND request_id=?", (session_id, state.turn_id, request_id)).rowcount
        if changed != 1:
            raise ValueError("cancellation request changed or does not exist")
        _event(store, connection, root, node, "cancellation_cleared", request_id)
