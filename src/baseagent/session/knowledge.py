"""Versioned workspace references; explicit publication, never automatic trust."""

import json
import os
from pathlib import Path
import re

from baseagent.agent.events import event
from baseagent.agent.state import State
from baseagent.tools.memory import history_digest
from baseagent.tools.result import ErrorCode, ToolFailure


def identity(root):
    return os.path.normcase(str(Path(root).resolve()))


def _parameters(key, revision):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) or type(revision) is not int or revision < 0:
        raise ValueError("shared memory requires a valid key and nonnegative revision")


def _session(connection, state, root, store):
    row = connection.execute("SELECT state_json FROM sessions WHERE id=?", (state.session_id,)).fetchone()
    if not row:
        raise ValueError("publishing session does not exist")
    saved = State.from_dict(json.loads(row[0]))
    task_id = None
    if saved.turn_id != state.turn_id:
        node = connection.execute("SELECT * FROM task_nodes WHERE session_id=? AND node_turn_id=?", (state.session_id, state.turn_id)).fetchone()
        if node is None or node["root_turn_id"] != saved.turn_id or getattr(store, "task_id", None) != node["task_id"]:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "node publication requires its current root turn and node store")
        from .tasks import _locked
        _locked(store.owner, store.root_state)
        saved = State.from_dict(json.loads(node["state_json"]))
        task_id = node["task_id"]
    if not saved.workspace_root or identity(saved.workspace_root) != root or saved.turn_id != state.turn_id:
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "session does not belong to this workspace/turn")
    return saved, task_id


def publish(store, state, workspace_root, key, expected_revision):
    _parameters(key, expected_revision)
    root = identity(workspace_root)
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        saved, task_id = _session(connection, state, root, store)
        note = saved.memory_notes.get(key)
        if not note or note != state.memory_notes.get(key):
            raise ToolFailure(ErrorCode.CONFLICT, "session note changed or does not exist")
        try:
            sources = [saved.messages[index] for index in note["source_messages"]]
        except (IndexError, KeyError):
            raise ToolFailure(ErrorCode.CONFLICT, "note source is unavailable")
        if history_digest(sources) != note["source_digest"]:
            raise ToolFailure(ErrorCode.CONFLICT, "note source digest changed")
        row = connection.execute("SELECT revision FROM shared_memories WHERE workspace_root=? AND key=?", (root, key)).fetchone()
        current = row[0] if row else 0
        if current != expected_revision:
            raise ToolFailure(ErrorCode.CONFLICT, "shared memory revision changed; search before publishing")
        count, size = connection.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN key!=? AND withdrawn=0 THEN LENGTH(CAST(content AS BLOB)) ELSE 0 END), 0) FROM shared_memories WHERE workspace_root=?", (key, root)).fetchone()
        if (row is None and count >= 100) or size + len(note["content"].encode("utf-8")) > 64000:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "workspace references exceed 100 keys or 64 KB live text")
        revision = current + 1
        published_note = dict(note)
        if task_id is not None:
            published_note["source_task_id"] = task_id
        connection.execute("""INSERT INTO shared_memories VALUES(?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(workspace_root,key) DO UPDATE SET revision=excluded.revision, content=excluded.content,
            source_session_id=excluded.source_session_id, source_note_json=excluded.source_note_json, withdrawn=0""",
                           (root, key, revision, note["content"], state.session_id, json.dumps(published_note, ensure_ascii=False, allow_nan=False)))
        store._event(connection, state, event("memory_published", revision=revision))
        return {"key": key, "revision": revision, "source_session_id": state.session_id, "source_task_id": task_id}


def withdraw(store, state, workspace_root, key, expected_revision):
    _parameters(key, expected_revision)
    root = identity(workspace_root)
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _session(connection, state, root, store)
        row = connection.execute("SELECT revision, withdrawn FROM shared_memories WHERE workspace_root=? AND key=?", (root, key)).fetchone()
        if not row:
            raise ToolFailure(ErrorCode.NOT_FOUND, "shared memory does not exist")
        if row[0] != expected_revision:
            raise ToolFailure(ErrorCode.CONFLICT, "shared memory revision changed")
        if row[1]:
            raise ToolFailure(ErrorCode.CONFLICT, "shared memory is already withdrawn")
        revision = expected_revision + 1
        connection.execute("UPDATE shared_memories SET revision=?, content=NULL, source_note_json='{}', withdrawn=1 WHERE workspace_root=? AND key=?", (revision, root, key))
        store._event(connection, state, event("memory_withdrawn", revision=revision))
        return {"key": key, "revision": revision, "withdrawn": True}


def search(store, workspace_root, *, query="", after_key="", limit=10):
    if not isinstance(query, str) or len(query) > 200 or not isinstance(after_key, str) or len(after_key) > 64 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("invalid shared memory query, cursor, or limit")
    root = identity(workspace_root)
    words = query.casefold().split()
    values = []
    with store._connection() as connection:
        connection.execute("BEGIN")
        rows = connection.execute("SELECT * FROM shared_memories WHERE workspace_root=? AND key>? ORDER BY key", (root, after_key)).fetchall()
        for row in rows:
            if not all(word in (row["key"] + " " + (row["content"] or "")).casefold() for word in words):
                continue
            note = json.loads(row["source_note_json"])
            source_status = "withdrawn" if row["withdrawn"] else "source_missing"
            if not row["withdrawn"]:
                if note.get("source_task_id") is not None:
                    source = connection.execute("SELECT state_json FROM task_nodes WHERE session_id=? AND task_id=? AND node_turn_id=?", (row["source_session_id"], note["source_task_id"], note.get("turn_id"))).fetchone()
                else:
                    source = connection.execute("SELECT state_json FROM sessions WHERE id=?", (row["source_session_id"],)).fetchone()
                if source:
                    state = State.from_dict(json.loads(source[0]))
                    current_note = state.memory_notes.get(row["key"])
                    if current_note is None:
                        source_status = "source_note_missing"
                    elif current_note != {key: value for key, value in note.items() if key != "source_task_id"}:
                        source_status = "source_note_changed"
                    elif not state.workspace_root or identity(state.workspace_root) != root:
                        source_status = "source_changed"
                    else:
                        try:
                            digest = history_digest([state.messages[index] for index in note["source_messages"]])
                            source_status = "source_matches" if digest == note["source_digest"] else "source_changed"
                        except (IndexError, KeyError):
                            source_status = "source_changed"
            values.append({"key": row["key"], "revision": row["revision"], "content": row["content"],
                           "withdrawn": bool(row["withdrawn"]), "source_session_id": row["source_session_id"],
                           "source_messages": note.get("source_messages", []), "source_digest": note.get("source_digest"),
                           "source_turn_id": note.get("turn_id"),
                           "source_task_id": note.get("source_task_id"),
                           "source_status": source_status})
            if len(values) > limit:
                break
    return {"notes": values[:limit], "next_after_key": values[limit - 1]["key"] if len(values) > limit else None}
