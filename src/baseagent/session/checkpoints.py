"""Per-turn file checkpoints and undo (file snapshots, not a shadow git).

Only files written through ``Workspace._write_file`` (``write_file`` / ``edit_file`` /
``apply_patch``) or deleted through ``Workspace._delete_file`` (``apply_patch``) are
captured. The first write of a file in a user turn stores its original bytes
(or ``missing`` for a new file); later writes of the same file in that turn only
update the expected after-hash (``missing`` after a delete). Contents are addressed by SHA-256 and stored once
in the session database. Nothing scans the repository, and command side effects
(``run_command``) are not captured. See docs/checkpoints.md.

Ownership: a write belongs to ``(session_id, turn)``. Single-agent and one-shot
sessions use their own turn number. A main-agent worker (session
``<main>-wN``) writes into the main session's checkpoint for the main-agent turn
that dispatched it, and the file records its ``worker_id``.

Undo restores files only when every file still has the hash the agent left
(or already has the target hash). Any other content is a conflict: nothing is
written unless ``force`` is set.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from baseagent.agent.events import event
from baseagent.agent.state import RunStatus
from baseagent.backends.docker import DockerError
from baseagent.tools.result import ErrorCode, ToolFailure

MAX_BLOB_BYTES = 200_000          # same as the read/write limits of the file tools
MAX_FILES_PER_TURN = 500          # same as the observed-file limit per turn
EVENT_FILE_LIMIT = 50             # files listed in one checkpoint_restored event
SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn INTEGER NOT NULL CHECK(turn >= 1),
    created_at REAL NOT NULL,
    restored_at REAL,
    PRIMARY KEY(session_id, turn)
);
CREATE TABLE IF NOT EXISTS checkpoint_files (
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    path TEXT NOT NULL,
    before_sha256 TEXT NOT NULL,
    after_sha256 TEXT NOT NULL,
    worker_id TEXT,
    writer_session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(session_id, turn, path),
    FOREIGN KEY(session_id, turn) REFERENCES checkpoints(session_id, turn) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS checkpoint_files_before ON checkpoint_files(before_sha256);
"""


class CheckpointError(ValueError):
    """An undo was refused (busy, unfinished, conflict, nothing to undo). Nothing was written."""

    def __init__(self, message, *, reason, conflicts=(), restored=()):
        super().__init__(message)
        self.reason = reason
        self.conflicts = list(conflicts)
        self.restored = list(restored)


# ----- turn numbers ------------------------------------------------------------------
def turn_number(state):
    """The current user turn, numbered from 1.

    Every new turn appends exactly one user message and the transcript is append-only
    (compaction stores a summary beside it), so the turn number is the count of user
    messages. Requirements a worker receives through its inbox are not turns.
    """
    from baseagent.agent.workers import INBOX_HEADER
    count = sum(1 for message in state.messages if message.get("role") == "user"
                and not str(message.get("content") or "").startswith(INBOX_HEADER))
    return max(1, count)


def take_notices(state):
    """One system line for turns the user undid since the last turn (shown to the agent once)."""
    turns = state.metadata.pop("checkpoint_notices", None) or []
    turns = sorted({turn for turn in turns if type(turn) is int})
    if not turns:
        return None
    named = "、".join(str(turn) for turn in turns)
    return (f"第 {named} 轮的文件改动已撤回（用户执行了撤销），这些改动已不在工作区；"
            "需要时请重新读取相关文件，不要假设它们仍然存在。")


# ----- capture (called by Workspace._write_file) ----------------------------------------
def _owner(context):
    """(root store, checkpoint session, turn, worker id, writer session) or None without a store."""
    store = getattr(context, "store", None) if context is not None else None
    state = getattr(context, "state", None) if context is not None else None
    if store is None or state is None or not getattr(state, "session_id", None):
        return None
    root_state = getattr(store, "root_state", None)
    base = getattr(store, "owner", store)       # TaskNodeStore views write through their root store
    if not hasattr(base, "_connection"):
        return None
    if root_state is not None:
        state = root_state                      # named subtasks belong to the root turn
    metadata = state.metadata
    if metadata.get("session_mode") == "worker" and metadata.get("main_session") and metadata.get("worker_id"):
        turn = metadata.get("main_turn")
        if type(turn) is not int or turn < 1:
            # A worker record from before turn numbers: use the main session's current turn.
            main = base.load(metadata["main_session"])
            turn = turn_number(main) if main is not None else 1
        return base, metadata["main_session"], turn, metadata["worker_id"], state.session_id
    return base, state.session_id, turn_number(state), None, state.session_id


class Capture:
    """The record of one in-flight write. ``commit`` after the write, ``abort`` when it failed."""

    def __init__(self, store, workspace, path, session_id, turn, *, relative, before, after, new_entry,
                 new_checkpoint, previous_after):
        self.store, self.workspace, self.path = store, workspace, path
        self.session_id, self.turn, self.relative = session_id, turn, relative
        self.before, self.after = before, after
        self.new_entry, self.new_checkpoint, self.previous_after = new_entry, new_checkpoint, previous_after

    def commit(self, result):
        actual = result.get("sha256") if isinstance(result, dict) else None
        if actual and actual != self.after:
            self._update(actual)

    def _update(self, value):
        with self.store._connection() as connection:
            connection.execute("UPDATE checkpoint_files SET after_sha256=?, updated_at=? WHERE session_id=? AND turn=? AND path=?",
                               (value, time.time(), self.session_id, self.turn, self.relative))

    def abort(self):
        """The write did not land (error, policy, post_edit_check rollback): leave no "changed" record."""
        try:
            current = self.workspace.backend.file_hash(self.path)
        except Exception:  # noqa: BLE001 -- unreadable: assume the write did not land.
            current = None
        try:
            if current == self.after and current != (self.previous_after or self.before):
                return  # it did land (for example an interrupt after the replace): keep the record
            with self.store._connection() as connection:
                if self.new_entry:
                    connection.execute("DELETE FROM checkpoint_files WHERE session_id=? AND turn=? AND path=?",
                                       (self.session_id, self.turn, self.relative))
                    if self.new_checkpoint:
                        connection.execute("""DELETE FROM checkpoints WHERE session_id=? AND turn=? AND NOT EXISTS
                                              (SELECT 1 FROM checkpoint_files WHERE session_id=? AND turn=?)""",
                                           (self.session_id, self.turn, self.session_id, self.turn))
                    if self.before != "missing":
                        _collect_blobs(connection, [self.before])
                else:
                    connection.execute("UPDATE checkpoint_files SET after_sha256=? WHERE session_id=? AND turn=? AND path=?",
                                       (self.previous_after, self.session_id, self.turn, self.relative))
        except sqlite3.Error:
            pass  # the tool error is reported; a stale record is caught by the hash check at undo


def capture(workspace, path, content, context):
    """Save a file's original before its first write in this turn. Raises ToolFailure (fail closed).

    ``content`` None means the file is about to be deleted (after-hash ``missing``).
    """
    owner = _owner(context)
    if owner is None:
        return None
    store, session_id, turn, worker_id, writer = owner
    relative = workspace._path(path).relative_to(workspace.root).as_posix()
    after = "missing" if content is None else sha256(content.encode("utf-8")).hexdigest()
    try:
        current = workspace.backend.file_hash(path)
        original = None if current == "missing" else workspace.backend.read_bytes(path, MAX_BLOB_BYTES)
    except ToolFailure as exc:
        raise ToolFailure(exc.code, f"checkpoint could not save the original file, so it was not written: {exc}") from None
    except OSError as exc:
        raise ToolFailure(ErrorCode.EXECUTION_FAILED, "checkpoint could not read the original file "
                          f"({type(exc).__name__}), so it was not written") from None
    before = "missing" if original is None else sha256(original).hexdigest()
    now = time.time()
    try:
        with store._connection() as connection:
            row = connection.execute("SELECT restored_at FROM checkpoints WHERE session_id=? AND turn=?",
                                     (session_id, turn)).fetchone()
            if row is not None and row["restored_at"] is not None:
                raise ToolFailure(ErrorCode.CONFLICT, f"turn {turn} was already undone; its checkpoint is closed, "
                                  "so the file was not written")
            if row is None:
                connection.execute("INSERT INTO checkpoints(session_id, turn, created_at) VALUES(?, ?, ?)",
                                   (session_id, turn, now))
            entry = connection.execute("SELECT after_sha256 FROM checkpoint_files WHERE session_id=? AND turn=? AND path=?",
                                       (session_id, turn, relative)).fetchone()
            if entry is not None:
                connection.execute("UPDATE checkpoint_files SET after_sha256=?, updated_at=? WHERE session_id=? AND turn=? AND path=?",
                                   (after, now, session_id, turn, relative))
                return Capture(store, workspace, path, session_id, turn, relative=relative, before=before, after=after,
                               new_entry=False, new_checkpoint=False, previous_after=entry["after_sha256"])
            count = connection.execute("SELECT COUNT(*) FROM checkpoint_files WHERE session_id=? AND turn=?",
                                       (session_id, turn)).fetchone()[0]
            if count >= MAX_FILES_PER_TURN:
                raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, f"checkpoint file limit is {MAX_FILES_PER_TURN} per turn; "
                                  "the file was not written")
            if original is not None:
                connection.execute("INSERT OR IGNORE INTO checkpoint_blobs(sha256, size, content) VALUES(?, ?, ?)",
                                   (before, len(original), original))
            connection.execute("""INSERT INTO checkpoint_files(session_id, turn, path, before_sha256, after_sha256,
                                  worker_id, writer_session_id, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                               (session_id, turn, relative, before, after, worker_id, writer, now, now))
    except sqlite3.Error as exc:
        raise ToolFailure(ErrorCode.EXECUTION_FAILED, "checkpoint could not be recorded "
                          f"({type(exc).__name__}), so the file was not written") from None
    return Capture(store, workspace, path, session_id, turn, relative=relative, before=before, after=after,
                   new_entry=True, new_checkpoint=row is None, previous_after=None)


def check_capacity(workspace, paths, context):
    """Batch pre-check (``apply_patch``): the turn's checkpoint is open and has room for every path.

    Raises the same ToolFailure codes as ``capture`` but writes nothing. Without a store: no-op.
    """
    owner = _owner(context)
    if owner is None:
        return
    store, session_id, turn, _, _ = owner
    relatives = {workspace._path(path).relative_to(workspace.root).as_posix() for path in paths}
    try:
        with store._connection() as connection:
            row = connection.execute("SELECT restored_at FROM checkpoints WHERE session_id=? AND turn=?",
                                     (session_id, turn)).fetchone()
            if row is not None and row["restored_at"] is not None:
                raise ToolFailure(ErrorCode.CONFLICT, f"turn {turn} was already undone; its checkpoint is closed")
            existing = {item["path"] for item in connection.execute(
                "SELECT path FROM checkpoint_files WHERE session_id=? AND turn=?", (session_id, turn))}
    except sqlite3.Error as exc:
        raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"checkpoint could not be read ({type(exc).__name__})") from None
    if len(existing | relatives) > MAX_FILES_PER_TURN:
        raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, f"checkpoint file limit is {MAX_FILES_PER_TURN} per turn; "
                          f"{len(existing)} already recorded, the patch needs {len(relatives - existing)} more")


def _collect_blobs(connection, hashes=None):
    """Delete content blocks no checkpoint file refers to (all, or only the given hashes)."""
    query = "DELETE FROM checkpoint_blobs WHERE NOT EXISTS (SELECT 1 FROM checkpoint_files WHERE before_sha256=checkpoint_blobs.sha256)"
    if hashes is None:
        return connection.execute(query).rowcount
    removed = 0
    for value in set(hashes):
        removed += connection.execute(query + " AND sha256=?", (value,)).rowcount
    return removed


def delete_records(connection, session_id):
    """Session deletion/cleanup: drop its checkpoints and every block nothing else refers to."""
    connection.execute("DELETE FROM checkpoint_files WHERE session_id=?", (session_id,))
    connection.execute("DELETE FROM checkpoints WHERE session_id=?", (session_id,))
    _collect_blobs(connection)


# ----- listing ----------------------------------------------------------------------------
def _iso(value):
    return None if value is None else datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")


def _change(before, after):
    if before == "missing":
        return "created"
    return "deleted" if after == "missing" else "modified"


def list_checkpoints(store, session_id):
    """Every checkpoint of a session, oldest turn first (no file contents)."""
    with store._connection() as connection:
        if connection.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
            raise ValueError("session does not exist")
        rows = connection.execute("SELECT * FROM checkpoints WHERE session_id=? ORDER BY turn", (session_id,)).fetchall()
        files = connection.execute("SELECT * FROM checkpoint_files WHERE session_id=? ORDER BY turn, created_at, path",
                                   (session_id,)).fetchall()
    by_turn = {}
    for item in files:
        by_turn.setdefault(item["turn"], []).append({
            "path": item["path"], "change": _change(item["before_sha256"], item["after_sha256"]),
            "worker_id": item["worker_id"], "before_sha256": item["before_sha256"], "after_sha256": item["after_sha256"]})
    checkpoints = []
    for row in rows:
        entries = by_turn.get(row["turn"], [])
        checkpoints.append({"turn": row["turn"], "created_at": _iso(row["created_at"]),
                            "restored": row["restored_at"] is not None, "restored_at": _iso(row["restored_at"]),
                            "file_count": len(entries),
                            "workers": sorted({entry["worker_id"] for entry in entries if entry["worker_id"]}),
                            "files": entries})
    return {"session_id": session_id, "checkpoints": checkpoints}


def describe(listing):
    """The /checkpoints text shown by every chat frontend."""
    checkpoints = listing["checkpoints"]
    if not checkpoints:
        return "还没有检查点：这个会话里文件工具还没有写过文件。"
    lines = [f"检查点（会话 {listing['session_id']}，每个用户轮次一个）："]
    for item in checkpoints:
        when = (item["created_at"] or "").replace("T", " ").replace("+00:00", " UTC")
        state = "  已撤销" if item["restored"] else ""
        lines.append(f"  第 {item['turn']} 轮  {when}  {item['file_count']} 个文件{state}")
        for entry in item["files"]:
            change = {"created": "新建", "deleted": "删除"}.get(entry["change"], "修改")
            source = f"  (worker {entry['worker_id']})" if entry["worker_id"] else ""
            lines.append(f"    {change} {entry['path']}{source}")
    lines.append("/undo 撤回最近一个未撤销的轮次；/rewind N 撤回第 N 轮及之后的所有轮次；冲突时加 --force。")
    return "\n".join(lines)


# ----- undo -------------------------------------------------------------------------------
def _probe(store, session_id):
    """True when another holder (a running turn or worker) has the session lock."""
    from .store import SessionBusy
    try:
        with store.exclusive(session_id):
            return False
    except SessionBusy:
        return True


def _blocking_workers(store, state):
    """Writable workers of a main-agent session that are running or waiting for approval."""
    blocking = []
    for record in (state.metadata.get("workers") or {}).values():
        if not record.get("allow_writes") or record.get("status") not in {"running", "awaiting_approval"}:
            continue
        worker_session = record.get("session_id")
        if not worker_session:
            continue
        if _probe(store, worker_session):
            blocking.append((record.get("id"), "running"))
            continue
        worker = store.load(worker_session)
        if worker is not None and worker.status == RunStatus.AWAITING_APPROVAL:
            blocking.append((record.get("id"), "awaiting_approval"))
    return blocking


def _select(rows, target):
    open_rows = [row for row in rows if row["restored_at"] is None]
    if not open_rows:
        raise CheckpointError("没有可撤销的检查点：文件工具还没有写过文件，或者所有轮次都已撤销。", reason="nothing_to_undo")
    if target == "last":
        return [open_rows[-1]]
    if type(target) is not int or target < 1:
        raise CheckpointError("轮次号必须是正整数或 last。", reason="invalid_turn")
    selected = [row for row in open_rows if row["turn"] >= target]
    if not selected:
        raise CheckpointError(f"第 {target} 轮及之后没有可撤销的文件改动。", reason="nothing_to_undo")
    return selected


def parse_target(value):
    """"last" or a positive turn number (CLI ``--rewind`` and chat ``/rewind``)."""
    text = str(value).strip().lower()
    if text == "last":
        return "last"
    if text.isdigit() and int(text) >= 1:
        return int(text)
    raise ValueError("rewind target must be a positive turn number or 'last'")


def _transactional(backend):
    """A backend that can write exact bytes back in its own namespace (DockerBackend)."""
    return callable(getattr(backend, "restore_bytes", None)) and callable(getattr(backend, "stat_files", None))


def _restore_file(workspace, path, content):
    """Write back one file: temporary file + fsync + atomic replace (or delete a file the agent created)."""
    from baseagent.backends import LocalBackend
    from baseagent.tools.editing import write_target
    if not isinstance(workspace.backend, LocalBackend):
        workspace.backend.restore_bytes(path, content)   # same steps, inside the backend (container)
        return
    target = write_target(workspace, path)
    if content is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    descriptor, name = tempfile.mkstemp(prefix=".baseagent-edit-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(name, mode)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def _current_hash(workspace, entry):
    """Current SHA-256 of a checkpointed file, read where the backend keeps it."""
    from baseagent.backends import LocalBackend
    if isinstance(workspace.backend, LocalBackend):
        return _hash_file(entry["file"])
    return workspace.backend.stat_files([entry["path"]])[entry["path"]][0]


def _hash_file(path):
    """Current SHA-256 of a workspace file without a size limit ("missing" when absent)."""
    try:
        with open(path, "rb") as handle:
            digest = sha256()
            for block in iter(lambda: handle.read(1 << 16), b""):
                digest.update(block)
            return digest.hexdigest()
    except FileNotFoundError:
        return "missing"


def rewind(store, workspace, session_id, target="last", *, force=False):
    """Undo the latest open turn ("last") or turn N and every later open turn, newest first.

    Refused (``CheckpointError``, nothing written) while the session's turn is running or
    unfinished, while a writable worker is running or waiting for approval, or when any file
    differs from the hash the agent left (unless ``force``). Writes stay inside the workspace
    boundary and protected paths. Returns what was restored; never calls a model.
    """
    from baseagent.backends import LocalBackend

    from .store import SessionBusy
    if type(force) is not bool:
        raise ValueError("force must be boolean")
    if not isinstance(workspace.backend, LocalBackend) and not _transactional(workspace.backend):
        raise CheckpointError("撤销需要能原样写回文件的后端（LocalBackend 或 DockerBackend）。", reason="unsupported_backend")
    lock = store.exclusive(session_id)
    try:
        lock.__enter__()
    except SessionBusy:
        raise CheckpointError("撤销被拒绝：这个会话的一轮正在运行，等它结束后再撤销。", reason="turn_running") from None
    try:
        return _rewind_locked(store, workspace, session_id, target, force)
    except DockerError as exc:
        raise CheckpointError(f"撤销被拒绝：{exc}。没有写回任何文件。", reason="backend_unavailable") from None
    finally:
        lock.__exit__(None, None, None)


def _rewind_locked(store, workspace, session_id, target, force):
    from baseagent.tools.editing import file_lock, write_target
    state = store.load(session_id)
    if state is None:
        raise ValueError("session does not exist")
    if state.workspace_root and Path(state.workspace_root).resolve() != workspace.root.resolve():
        raise ValueError("session belongs to a different workspace")
    if state.status != RunStatus.COMPLETED:
        what = "在等待审批" if state.status == RunStatus.AWAITING_APPROVAL else f"未完成（{RunStatus(state.status).value}）"
        raise CheckpointError(f"撤销被拒绝：当前这一轮{what}。先完成、审批或对账这一轮，再撤销。", reason="turn_unfinished")
    blocking = _blocking_workers(store, state)
    if blocking:
        names = "、".join(f"{worker}（{'运行中' if why == 'running' else '等待审批'}）" for worker, why in blocking)
        raise CheckpointError(f"撤销被拒绝：可写的 worker {names}还没结束。等它结束或 /cancel 后再撤销。",
                              reason="writer_active")
    with store._connection() as connection:
        rows = connection.execute("SELECT * FROM checkpoints WHERE session_id=? ORDER BY turn", (session_id,)).fetchall()
        selected = _select(rows, target)
        turns = [row["turn"] for row in selected]
        files = connection.execute(
            "SELECT * FROM checkpoint_files WHERE session_id=? AND turn IN (" + ",".join("?" for _ in turns) + ") ORDER BY turn",
            (session_id, *turns)).fetchall()
    # Per file: restore the original from the oldest selected turn; expect the hash the newest one left.
    plan = {}
    for item in files:
        entry = plan.get(item["path"])
        if entry is None:
            plan[item["path"]] = {"path": item["path"], "target": item["before_sha256"],
                                  "expected": item["after_sha256"], "chain": True}
            continue
        if entry["expected"] != item["before_sha256"]:
            entry["chain"] = False              # changed between two turns outside the file tools
        entry["expected"] = item["after_sha256"]
    conflicts, actions = [], []
    for path, entry in sorted(plan.items()):
        try:
            entry["file"] = write_target(workspace, path)   # workspace boundary, protected paths, no links
        except ToolFailure as exc:
            raise CheckpointError(f"撤销被拒绝：{path} 不能写回（{exc}）。没有写回任何文件。", reason="protected_path") from None
        current = entry["current"] = _current_hash(workspace, entry)
        entry["action"] = "deleted" if entry["target"] == "missing" else "restored"
        if current == entry["target"]:
            entry["action"] = "unchanged"
        elif current != entry["expected"] or not entry["chain"]:
            conflicts.append(path)
        actions.append(entry)
    if conflicts and not force:
        shown = ", ".join(conflicts[:20]) + (f" 等 {len(conflicts)} 个" if len(conflicts) > 20 else "")
        raise CheckpointError(f"撤销被拒绝：这些文件在 agent 写入之后又被改动过：{shown}。没有写回任何文件；"
                              "确认要覆盖时加 --force（对话里用 /undo --force 或 /rewind N --force）。",
                              reason="conflict", conflicts=conflicts)
    contents = {}
    with store._connection() as connection:
        for entry in actions:
            if entry["action"] != "restored":
                continue
            blob = connection.execute("SELECT content FROM checkpoint_blobs WHERE sha256=?", (entry["target"],)).fetchone()
            if blob is None or sha256(blob["content"]).hexdigest() != entry["target"]:
                raise CheckpointError(f"撤销被拒绝：{entry['path']} 的原貌内容块缺失或损坏，没有写回任何文件。",
                                      reason="missing_blob")
            contents[entry["path"]] = bytes(blob["content"])
    restored = []
    for entry in actions:
        if entry["action"] == "unchanged":
            continue
        try:
            with file_lock(workspace, entry["file"]):
                if _current_hash(workspace, entry) != entry["current"]:
                    raise CheckpointError(f"撤销中止：{entry['path']} 在撤销过程中又被改动；已写回 {len(restored)} 个文件："
                                          + (", ".join(restored) or "无") + "。请检查后再撤销。",
                                          reason="changed_during_undo", conflicts=[entry["path"]], restored=restored)
                _restore_file(workspace, entry["path"], contents.get(entry["path"]))
        except (ToolFailure, DockerError) as exc:
            # Another harness writer holds the file, the path became a link, or the container is gone:
            # stop here and say what was written.
            raise CheckpointError(f"撤销中止：{entry['path']} 不能写回（{exc}）；已写回 {len(restored)} 个文件："
                                  + (", ".join(restored) or "无") + "。", reason="write_blocked",
                                  conflicts=[entry["path"]], restored=restored) from None
        restored.append(entry["path"])
    forced = bool(force and conflicts)
    now = time.time()
    by_turn = {}
    for item in files:
        by_turn.setdefault(item["turn"], []).append(item)
    with store._connection() as connection:
        for turn in sorted(turns, reverse=True):
            connection.execute("UPDATE checkpoints SET restored_at=? WHERE session_id=? AND turn=?", (now, session_id, turn))
            entries = by_turn.get(turn, [])
            listed = [{"path": item["path"], "from_sha256": item["after_sha256"], "to_sha256": item["before_sha256"]}
                      for item in entries[:EVENT_FILE_LIMIT]]
            store._event(connection, state, event("checkpoint_restored", turn=turn, file_count=len(entries),
                                                  files=listed, files_truncated=len(entries) > EVENT_FILE_LIMIT,
                                                  forced=forced, conflict_count=len(conflicts)))
        notices = state.metadata.setdefault("checkpoint_notices", [])
        notices.extend(turn for turn in turns if turn not in notices)
        store._save(connection, state)
    return {"session_id": session_id, "restored_turns": sorted(turns, reverse=True), "forced": forced,
            "conflicts": conflicts,
            "files": [{"path": entry["path"], "action": entry["action"], "from_sha256": entry["current"],
                       "to_sha256": entry["target"]} for entry in actions]}


def summary(result):
    """One line for chat frontends after a successful undo."""
    turns = "、".join(str(turn) for turn in result["restored_turns"])
    counts = {"restored": 0, "deleted": 0, "unchanged": 0}
    for item in result["files"]:
        counts[item["action"]] += 1
    parts = [f"恢复 {counts['restored']} 个文件", f"删除 {counts['deleted']} 个新建文件"]
    if counts["unchanged"]:
        parts.append(f"{counts['unchanged']} 个已是原样")
    forced = "（--force：覆盖了冲突文件）" if result["forced"] else ""
    return f"已撤回第 {turns} 轮的文件改动：" + "，".join(parts) + f"。{forced}命令（run_command）造成的改动不在撤销范围内。"


__all__ = [
    "MAX_BLOB_BYTES",
    "MAX_FILES_PER_TURN",
    "CheckpointError",
    "capture",
    "check_capacity",
    "describe",
    "list_checkpoints",
    "parse_target",
    "rewind",
    "summary",
]

