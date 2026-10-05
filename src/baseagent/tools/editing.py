"""Optimistic file edits, serialized between cooperating harness writers."""

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import tempfile

from .result import ErrorCode, ToolFailure


def read_bytes(path: Path, limit=200_000) -> bytes:
    with path.open("rb") as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "file exceeds read limit")
    return value


def file_hash(path: Path) -> str:
    return sha256(read_bytes(path)).hexdigest() if path.exists() else "missing"


@contextmanager
def file_lock(workspace, target):
    folder = workspace.root / ".baseagent" / "file-locks"
    if folder.resolve() != folder.absolute():
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "lock directory cannot use symlinks or junctions")
    folder.mkdir(parents=True, exist_ok=True)
    name = sha256(os.path.normcase(str(target)).encode("utf-8")).hexdigest() + ".lock"
    with (folder / name).open("a+b") as handle:
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
            raise ToolFailure(ErrorCode.CONFLICT, "another harness writer holds this file") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_target(workspace, path):
    target = workspace._path(path)
    lexical = Path(os.path.abspath(workspace.root / path))
    if lexical != target:
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "write paths cannot use symlinks or junctions")
    if target == workspace.root or (target.exists() and not target.is_file()):
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "write target must be a file")
    return target


def atomic_write(workspace, path, content, expected_sha256, expected_instructions):
    if not workspace.allow_write:
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "file writes disabled; start with --allow-write")
    if expected_sha256 is not None and expected_sha256 != "missing" and not re.fullmatch("[0-9a-f]{64}", expected_sha256):
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "expected_sha256 must be a digest or missing")
    value = content.encode("utf-8")
    if len(value) > 200_000:
        raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "content exceeds 200 KB write limit")
    target = write_target(workspace, path)
    with file_lock(workspace, target):
        original = file_hash(target)
        expected = "missing" if expected_sha256 is None else expected_sha256
        if original != expected:
            raise ToolFailure(ErrorCode.CONFLICT, "file version changed; read the current file before editing")
        instructions = workspace.get_instructions(path)
        if expected_instructions is not None and expected_instructions != instructions["digest"]:
            raise ToolFailure(ErrorCode.CONFLICT, "repository instructions changed; reload them before editing")
        final_instruction_digest = instructions["digest"]
        if target.name.casefold() == "agents.md":
            from .instructions import instruction_digest
            relative = str(target.relative_to(workspace.root))
            values = [item for item in instructions["instructions"] if item["path"] != relative]
            if len(value) > 20_000 or sum(len(item["content"].encode("utf-8")) for item in values) + len(value) > 40_000:
                raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "new repository instructions exceed the guidance limits")
            values.append({"path": relative, "sha256": sha256(value).hexdigest()})
            final_instruction_digest = instruction_digest(values)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".baseagent-edit-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            if original != "missing":
                os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
            if write_target(workspace, path) != target or file_hash(target) != original:
                raise ToolFailure(ErrorCode.CONFLICT, "file changed while preparing the edit")
            if workspace.get_instructions(path)["digest"] != instructions["digest"]:
                raise ToolFailure(ErrorCode.CONFLICT, "repository instructions changed while preparing the edit")
            if original == "missing":
                # Atomic no-overwrite creation, even against a noncooperating creator.
                os.link(temporary, target)
            else:
                os.replace(temporary, target)
        except FileExistsError as exc:
            raise ToolFailure(ErrorCode.CONFLICT, "file was created by another writer") from exc
        finally:
            if temporary.exists():
                os.chmod(temporary, stat.S_IREAD | stat.S_IWRITE)
            temporary.unlink(missing_ok=True)
    return {"path": str(target.relative_to(workspace.root)), "bytes": len(value), "sha256": sha256(value).hexdigest(),
            "instruction_digest": final_instruction_digest}
