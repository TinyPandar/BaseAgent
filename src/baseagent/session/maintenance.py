"""Session operations with SQLite snapshots and non-overwriting artifacts."""

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from baseagent.agent.state import RunStatus, State
from .store import SessionBusy


def _page(after_id, limit):
    if after_id is not None and not isinstance(after_id, str):
        raise ValueError("session cursor must be a string")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("page size must be 1-1000")


def list_sessions(store, *, after_id=None, limit=100):
    _page(after_id, limit)
    with store._connection() as connection:
        rows = connection.execute("""
            SELECT id, updated_at, json_extract(state_json, '$.status') AS status,
                json_extract(state_json, '$.workspace_root') AS workspace_root,
                json_extract(state_json, '$.turn_id') AS turn_id,
                json_extract(state_json, '$.step') AS steps,
                json_extract(state_json, '$.total_tokens') AS total_tokens
            FROM sessions WHERE id > ? ORDER BY id LIMIT ?
        """, (after_id or "", limit + 1)).fetchall()
    return {"sessions": [dict(row) for row in rows[:limit]], "next_after": rows[limit - 1]["id"] if len(rows) > limit else None}


def inspect_session(store, session_id):
    with store._connection() as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT state_json FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError("session does not exist")
        state = State.from_dict(json.loads(row[0]))
        rows = connection.execute("SELECT * FROM tool_calls WHERE session_id=? AND turn_id=? ORDER BY rowid", (session_id, state.turn_id)).fetchall()
    return state, [dict(row) for row in rows]


@contextmanager
def _artifact(store, target):
    target = Path(target).expanduser().absolute()
    resolved = target.resolve()
    protected = ([store.path, Path(str(store.path) + "-wal"), Path(str(store.path) + "-shm"), Path(str(store.path) + "-journal"), store.lock_dir]
                 if store.durable else [])
    if any(resolved == item or resolved.is_relative_to(item) for item in protected):
        raise ValueError("artifact cannot replace session storage or lock files")
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"artifact already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="." + target.name + "-", suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        yield temporary
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        # Publishing a hard link is atomic and fails if a concurrent writer created
        # the destination. Never overwrite a user artifact via os.replace().
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def export_session(store, session_id, target):
    """Stream an exact database snapshot; JSON is an archive, not an import format."""
    with _artifact(store, target) as temporary:
        with store._connection() as connection:
            connection.execute("BEGIN")
            row = connection.execute("SELECT state_json, updated_at FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise ValueError("session does not exist")
            schema = connection.execute("PRAGMA user_version").fetchone()[0]
            with temporary.open("w", encoding="utf-8", newline="\n") as output:
                output.write('{"format":"baseagent.session","version":1,"schema_version":')
                output.write(str(schema))
                output.write(',"session":')
                output.write(row["state_json"])
                output.write(',"updated_at":')
                json.dump(row["updated_at"], output)
                output.write(',"tool_calls":[')
                for index, record in enumerate(connection.execute("SELECT * FROM tool_calls WHERE session_id=? ORDER BY rowid", (session_id,))):
                    if index:
                        output.write(",")
                    value = dict(record)
                    for key in ["request_json", "terminal_request_json", "terminal_result_json", "result_json"]:
                        value[key.removesuffix("_json")] = json.loads(value.pop(key)) if record[key] is not None else None
                    json.dump(value, output, ensure_ascii=False, allow_nan=False)
                output.write('],"events":[')
                for index, record in enumerate(connection.execute("SELECT * FROM events WHERE session_id=? ORDER BY sequence", (session_id,))):
                    if index:
                        output.write(",")
                    value = {"sequence": record["sequence"], "turn_id": record["turn_id"], **json.loads(record["event_json"])}
                    json.dump(value, output, ensure_ascii=False, allow_nan=False)
                output.write('],"cancellation_requests":[')
                for index, record in enumerate(connection.execute("SELECT turn_id, request_id FROM cancellation_requests WHERE session_id=? ORDER BY turn_id", (session_id,))):
                    if index:
                        output.write(",")
                    json.dump(dict(record), output)
                output.write('],"event_retention":')
                retention = connection.execute("SELECT through_sequence, deleted_count FROM event_retention WHERE session_id=?", (session_id,)).fetchone()
                json.dump(dict(retention) if retention else None, output)
                output.write(',"published_references":[')
                for index, record in enumerate(connection.execute("SELECT * FROM shared_memories WHERE source_session_id=? ORDER BY workspace_root,key", (session_id,))):
                    if index:
                        output.write(",")
                    value = dict(record)
                    value["source_note"] = json.loads(value.pop("source_note_json"))
                    json.dump(value, output, ensure_ascii=False, allow_nan=False)
                output.write('],"task_nodes":[')
                for index, record in enumerate(connection.execute("SELECT * FROM task_nodes WHERE session_id=? ORDER BY root_turn_id,depth,rowid", (session_id,))):
                    if index:
                        output.write(",")
                    value = dict(record)
                    value["state"] = json.loads(value.pop("state_json"))
                    json.dump(value, output, ensure_ascii=False, allow_nan=False)
                output.write("]")
                output.write("}\n")
                output.flush()
                os.fsync(output.fileno())
    return str(Path(target).absolute())


def backup(store, target, *, timeout=30.0):
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("backup timeout must be positive and finite")
    started = time.monotonic()

    def progress(status, remaining, total):
        if time.monotonic() - started > timeout:
            raise TimeoutError("backup timed out; no destination was published")

    with _artifact(store, target) as temporary:
        with store._connection() as source, closing(sqlite3.connect(temporary)) as destination:
            source.backup(destination, pages=128, progress=progress, sleep=0.05)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or destination.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("backup failed integrity validation")
    return str(Path(target).absolute())


def _delete_locked(store, session_id, *, discard_unfinished=False, cutoff=None):
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT state_json, updated_at FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return "not_found"
        state = State.from_dict(json.loads(row["state_json"]))
        unresolved = connection.execute("SELECT 1 FROM tool_calls WHERE session_id=? AND status != 'completed' LIMIT 1", (session_id,)).fetchone()
        unresolved = unresolved or connection.execute("SELECT 1 FROM task_nodes WHERE session_id=? AND json_extract(state_json, '$.status') != 'completed' LIMIT 1", (session_id,)).fetchone()
        if (state.status != RunStatus.COMPLETED or unresolved) and not discard_unfinished:
            return "unfinished"
        if cutoff is not None and (row["updated_at"] >= cutoff or state.status != RunStatus.COMPLETED or unresolved):
            return "changed"
        connection.execute("DELETE FROM events WHERE session_id=?", (session_id,))
        connection.execute("DELETE FROM tool_calls WHERE session_id=?", (session_id,))
        connection.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    # Keep lock files: unlinking a locked inode could allow a second lock owner.
    return "deleted"


def delete_session(store, session_id, *, discard_unfinished=False):
    if type(discard_unfinished) is not bool:
        raise ValueError("discard_unfinished must be boolean")
    with store.exclusive(session_id):
        result = _delete_locked(store, session_id, discard_unfinished=discard_unfinished)
    if result != "deleted":
        raise ValueError("session does not exist" if result == "not_found" else "unfinished session requires explicit discard")


def cleanup(store, older_than_seconds, *, dry_run=True, after_id=None, limit=100):
    _page(after_id, limit)
    if type(older_than_seconds) not in (int, float) or not math.isfinite(older_than_seconds) or older_than_seconds <= 0:
        raise ValueError("cleanup age must be positive and finite")
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be boolean")
    cutoff = datetime.fromtimestamp(time.time() - older_than_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with store._connection() as connection:
        rows = connection.execute("""
            SELECT id FROM sessions WHERE id > ? AND updated_at < ?
            AND json_extract(state_json, '$.status')='completed'
            AND NOT EXISTS (SELECT 1 FROM tool_calls WHERE tool_calls.session_id=sessions.id AND status != 'completed')
            AND NOT EXISTS (SELECT 1 FROM task_nodes WHERE task_nodes.session_id=sessions.id AND json_extract(state_json, '$.status') != 'completed')
            ORDER BY id LIMIT ?
        """, (after_id or "", cutoff, limit + 1)).fetchall()
    ids = [row["id"] for row in rows[:limit]]
    result = {"dry_run": dry_run, "cutoff": cutoff, "eligible": ids, "deleted": [], "skipped": [],
              "next_after": ids[-1] if len(rows) > limit else None}
    if not dry_run:
        for session_id in ids:
            try:
                with store.exclusive(session_id):
                    outcome = _delete_locked(store, session_id, cutoff=cutoff)
                if outcome == "deleted":
                    result["deleted"].append(session_id)
                else:
                    result["skipped"].append({"id": session_id, "reason": outcome})
            except SessionBusy:
                result["skipped"].append({"id": session_id, "reason": "busy"})
    return result
