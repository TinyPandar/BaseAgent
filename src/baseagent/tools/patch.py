"""``apply_patch``: add, modify and delete several files as one all-or-nothing batch.

Flow (docs/patch.md): validate the whole batch -> snapshot every target -> write one file
at a time through ``Workspace._write_file`` / ``Workspace._delete_file`` -> on any failure
restore the snapshots newest first and withdraw the batch's checkpoint records.

``modify`` reuses ``apply_edits`` (the ``edit_file`` rules, unchanged); ``add`` takes the
full content with ``expected_sha256 = "missing"``; ``delete`` takes the current hash. The
unified diff shown to people is computed here from the snapshot and the new content; the
model never supplies a diff.
"""

from __future__ import annotations

import os
import re
import stat
from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher, unified_diff
from hashlib import sha256
from pathlib import Path

from baseagent.kernel.cancellation import Cancelled

from .result import ErrorCode, ToolFailure

MAX_PATCH_FILES = 50              # operations per call
MAX_PATCH_DIFF_CHARS = 20_000     # diff text in results, events and approval previews
MAX_FILE_BYTES = 200_000          # same as checkpoints.MAX_BLOB_BYTES and the write limit
OPERATIONS = ("add", "modify", "delete")
COMMON_FIELDS = frozenset({"op", "path", "expected_sha256", "expected_instructions"})
FIELDS = {"add": COMMON_FIELDS | {"content"}, "modify": COMMON_FIELDS | {"edits"}, "delete": COMMON_FIELDS}
DIGEST = re.compile("[0-9a-f]{64}")

DESCRIPTION = ("Atomically add, modify and delete up to 50 files in one call; any failure writes nothing. "
               "Every operation needs path, op, expected_sha256 and expected_instructions from a fresh read. "
               "modify: 'edits' exactly like edit_file (1-20 exact unique {old_text, new_text} replacements). "
               "add: full 'content' with expected_sha256='missing'. delete: only the current expected_sha256.")


def schema():
    """JSON Schema of the tool arguments (flat item object; per-op fields are checked in code)."""
    string = {"type": "string"}
    digest = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    edits = {"type": "array", "minItems": 1, "maxItems": 20, "items": {
        "type": "object", "properties": {"old_text": {"type": "string", "minLength": 1, "maxLength": 200000},
                                         "new_text": {"type": "string", "maxLength": 200000}},
        "required": ["old_text", "new_text"], "additionalProperties": False}}
    item = {"type": "object", "properties": {
        "op": {"type": "string", "enum": list(OPERATIONS)}, "path": string,
        "expected_sha256": {"type": "string", "pattern": "^([0-9a-f]{64}|missing)$"},
        "expected_instructions": digest, "content": string, "edits": edits},
        "required": ["op", "path", "expected_sha256", "expected_instructions"], "additionalProperties": False}
    return {"type": "object", "properties": {"operations": {
        "type": "array", "minItems": 1, "maxItems": MAX_PATCH_FILES, "items": item}},
        "required": ["operations"], "additionalProperties": False}


@dataclass
class Planned:
    index: int
    op: str
    path: str                 # as given (backend namespace)
    relative: str             # workspace-relative POSIX path (results, diff, checkpoints)
    expected_sha256: str
    expected_instructions: str
    before: bytes | None      # snapshot: original bytes, None = did not exist
    after: str | None         # new content, None = delete
    mode: int | None = None   # LocalBackend file mode, restored after a rollback re-creates a file

    @property
    def before_text(self):
        return None if self.before is None else self.before.decode("utf-8")

    @property
    def before_sha256(self):
        return "missing" if self.before is None else sha256(self.before).hexdigest()

    @property
    def after_sha256(self):
        return "missing" if self.after is None else sha256(self.after.encode("utf-8")).hexdigest()


def _fail(label, exc, suffix="no file was written"):
    return ToolFailure(exc.code, f"{label}: {exc}; {suffix}", retryable=exc.retryable)


def _plan_one(workspace, index, item, seen):
    from .workspace import apply_edits
    if not isinstance(item, dict):
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "each operation must be an object")
    op = item.get("op")
    if op not in OPERATIONS:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "op must be add, modify or delete")
    extra, missing = set(item) - FIELDS[op], FIELDS[op] - set(item)
    if extra:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, f"{op} does not accept {', '.join(sorted(extra))}")
    if missing:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, f"{op} requires {', '.join(sorted(missing))}")
    path, expected, instructions = item["path"], item["expected_sha256"], item["expected_instructions"]
    if not isinstance(path, str) or not path:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "path must be a nonempty string")
    if not isinstance(instructions, str) or not DIGEST.fullmatch(instructions):
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "expected_instructions must be an instruction digest")
    if op == "add" and expected != "missing":
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "add requires expected_sha256 'missing'")
    if op != "add" and (not isinstance(expected, str) or not DIGEST.fullmatch(expected)):
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, f"{op} requires the current file's expected_sha256")
    # Workspace boundary and protected paths, then links/junctions, then EditPolicy path rules.
    target = workspace._path(path)
    if Path(os.path.abspath(workspace.root / path)) != target:
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "write paths cannot use symlinks or junctions")
    from baseagent.backends import LocalBackend
    local = isinstance(workspace.backend, LocalBackend)
    if target == workspace.root or (local and target.exists() and not target.is_file()):
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "write target must be a file")
    workspace.edit_policy.check_path(workspace, path)
    relative = target.relative_to(workspace.root).as_posix()
    key = relative.casefold()   # one rule on every system: paths that differ only by case collide
    if key in seen:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, f"path appears twice in this patch (operation {seen[key] + 1})")
    seen[key] = index
    current = workspace.backend.file_hash(path)
    if op == "add" and current != "missing":
        raise ToolFailure(ErrorCode.CONFLICT, "add requires a file that does not exist; read it and use modify")
    if op != "add" and current == "missing":
        raise ToolFailure(ErrorCode.CONFLICT, f"{op} requires an existing file; it does not exist")
    if op != "add" and current != expected:
        raise ToolFailure(ErrorCode.CONFLICT, "file version changed; read the current file")
    if workspace.get_instructions(path)["digest"] != instructions:
        raise ToolFailure(ErrorCode.CONFLICT, "repository instructions changed; reload them before editing")
    before = None if op == "add" else workspace.backend.read_bytes(path, MAX_FILE_BYTES)
    if before is not None and sha256(before).hexdigest() != expected:
        raise ToolFailure(ErrorCode.CONFLICT, "file changed while reading it; read the current file")
    try:
        text = None if before is None else before.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "file is not UTF-8 text") from None
    if op == "add":
        if not isinstance(item["content"], str):
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "content must be a string")
        after = item["content"]
    elif op == "modify":
        after = apply_edits(text, item["edits"])     # the edit_file rules, word for word
    else:
        after = None
    if after is not None and len(after.encode("utf-8")) > MAX_FILE_BYTES:
        raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, f"new content exceeds {MAX_FILE_BYTES // 1000} KB")
    mode = None
    if before is not None and local:
        mode = stat.S_IMODE(target.stat().st_mode)
    return Planned(index, op, path, relative, expected, instructions, before, after, mode)


def plan(workspace, operations):
    """Validate every operation in order; the first failure names its file. Writes nothing."""
    if not isinstance(operations, list) or not operations:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "operations must be a nonempty array; no file was written")
    if len(operations) > MAX_PATCH_FILES:
        raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, f"a patch may contain at most {MAX_PATCH_FILES} files; "
                          "no file was written")
    planned, seen = [], {}
    for index, item in enumerate(operations):
        path = item.get("path") if isinstance(item, dict) else None
        label = path if isinstance(path, str) and path else f"operations[{index}]"
        try:
            planned.append(_plan_one(workspace, index, item, seen))
        except ToolFailure as exc:
            raise _fail(label, exc) from None
        except OSError as exc:
            raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"{label}: could not read the file ({type(exc).__name__}); "
                              "no file was written") from None
    return planned


# ----- diff ---------------------------------------------------------------------------------
def _counts(before, after):
    old, new = (before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True)
    added = removed = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new).get_opcodes():
        if tag != "equal":
            removed += i2 - i1
            added += j2 - j1
    return added, removed


def file_diff(item):
    """Unified diff lines of one file: a/ b/ prefixes, /dev/null for a new or deleted file."""
    before, after = item.before_text, item.after
    lines = [f"--- {'/dev/null' if before is None else 'a/' + item.relative}\n",
             f"+++ {'/dev/null' if after is None else 'b/' + item.relative}\n"]
    hunks = list(unified_diff((before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True)))[2:]
    for line in hunks:
        if line.endswith("\n"):
            lines.append(line)
        else:
            lines += [line + "\n", "\\ No newline at end of file\n"]
    return lines


def render_diff(planned, limit=None):
    """(text, truncated). Whole lines up to ``limit`` (MAX_PATCH_DIFF_CHARS) characters, then one marker line."""
    limit = MAX_PATCH_DIFF_CHARS if limit is None else limit
    per_file = [file_diff(item) for item in planned]
    total = sum(len(line) for lines in per_file for line in lines)
    if total <= limit:
        return "".join(line for lines in per_file for line in lines), False
    kept, used, omitted_lines, omitted_files, full = [], 0, 0, 0, True
    for lines in per_file:
        shown = 0
        for line in lines:
            if full and used + len(line) <= limit:
                kept.append(line)
                used += len(line)
                shown += 1
            else:
                full = False
        if shown < len(lines):
            omitted_lines += len(lines) - shown
            omitted_files += 1
    marker = (f"[diff truncated at {limit} characters: {omitted_lines} more lines in {omitted_files} file(s) not shown; "
              "the full request is in the tool ledger (--inspect-session)]\n")
    return "".join(kept) + marker, True


def summarize(planned):
    files, added_total, removed_total = [], 0, 0
    for item in planned:
        added, removed = _counts(item.before_text, item.after)
        added_total, removed_total = added_total + added, removed_total + removed
        files.append({"path": item.relative, "op": item.op, "before_sha256": item.before_sha256,
                      "after_sha256": item.after_sha256, "added": added, "removed": removed})
    diff, truncated = render_diff(planned)
    return {"files": files, "added": added_total, "removed": removed_total, "diff": diff, "diff_truncated": truncated}


def preview_patch(workspace, operations):
    """What the batch would change right now (approval previews, max_changed_lines). Writes nothing."""
    value = summarize(plan(workspace, operations))
    value["changed_lines"] = value["added"] + value["removed"]
    return value


def describe(data, *, pending=False):
    """Text for people: header line, then the diff (CLI, TUI/Web notice, approval prompt)."""
    files = data.get("files") or []
    counts = {op: sum(1 for item in files if item["op"] == op) for op in OPERATIONS}
    parts = [f"{label} {counts[op]}" for op, label in (("add", "新建"), ("modify", "修改"), ("delete", "删除")) if counts[op]]
    head = ("apply_patch 待审批" if pending else "apply_patch 已应用") + f"：{len(files)} 个文件（" + "，".join(parts) + \
        f"），+{data.get('added', 0)} -{data.get('removed', 0)} 行"
    return head + "\n" + (data.get("diff") or "").rstrip("\n")


# ----- apply ----------------------------------------------------------------------------------
def _check_batch(workspace, planned, context, operations):
    from baseagent.session.checkpoints import check_capacity
    if context is not None:
        files = context.state.metadata.get("observed_files", {})
        relatives = {str(workspace._path(item.path).relative_to(workspace.root)) for item in planned}
        if len(set(files) | relatives) > 500:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "observed file limit is 500 per turn; no file was written")
    try:
        check_capacity(workspace, [item.path for item in planned], context)
    except ToolFailure as exc:
        raise ToolFailure(exc.code, f"{exc}; no file was written") from None
    policy = workspace.edit_policy
    if policy.max_changed_lines is not None:
        from .edit_policy import _approved
        changed = sum(sum(_counts(item.before_text, item.after)) for item in planned)
        if changed > policy.max_changed_lines and not _approved(context, "apply_patch", {"operations": operations}):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, f"patch changes {changed} lines, over max_changed_lines "
                              f"{policy.max_changed_lines}; explicit tool approval required; no file was written")


def _restore(workspace, item, *, failed=False):
    """Put one file back to its snapshot. Returns True when nothing of ours is left in it.

    ``failed``: the file whose write raised. Unless it holds our new content, our write did not
    land (for example a CAS conflict with an external edit), so it is left exactly as it is.
    """
    backend = workspace.backend
    current = backend.file_hash(item.path)
    if current == item.before_sha256:
        return True
    if current != item.after_sha256:
        return failed    # not ours: never overwrite content someone else wrote
    digest = backend.get_instructions(item.path)["digest"]
    if item.before is None:
        backend.delete_file(item.path, current, digest)
    else:
        backend.write_file(item.path, item.before_text, current, digest)
        if item.after is None and item.mode is not None:
            os.chmod(workspace._path(item.path), item.mode)
    return backend.file_hash(item.path) == item.before_sha256


def _rollback(workspace, attempted, captures):
    """Restore attempted files newest first. Returns the paths whose state is unknown."""
    unknown = []
    for item in reversed(attempted):
        try:
            if not _restore(workspace, item, failed=item is attempted[-1]):
                unknown.append(item.relative)
        except Exception:  # noqa: BLE001 -- second failure: report the file, keep going
            unknown.append(item.relative)
    if not unknown:
        # Nothing of this batch is left on disk: withdraw its checkpoint records too.
        for saved in reversed(captures):
            saved.abort()
    return list(reversed(unknown))


def apply_patch(workspace, operations, *, context=None):
    if not workspace.allow_write:
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "file writes disabled; start with --allow-write")
    planned = plan(workspace, operations)
    _check_batch(workspace, planned, context, operations)
    request = ("apply_patch", {"operations": operations})
    state = getattr(context, "state", None)
    saved_state = None
    if state is not None:
        saved_state = {key: deepcopy(state.metadata[key]) for key in ("observed_files", "edited_this_turn",
                                                                     "root_instruction_digest") if key in state.metadata}
        saved_verifications = deepcopy(state.verifications)
    attempted, captures = [], []
    cancellation = getattr(context, "cancellation", None)
    item = planned[0]
    try:
        for item in planned:
            if cancellation is not None:
                cancellation.check()
            attempted.append(item)
            if item.after is None:
                workspace._delete_file(item.path, item.expected_sha256, item.expected_instructions,
                                       context=context, request=request, captures=captures)
            else:
                workspace._write_file(item.path, item.after, item.expected_sha256, item.expected_instructions,
                                      context=context, request=request, captures=captures)
    except BaseException as exc:
        failed = item.relative
        unknown = _rollback(workspace, attempted, captures)
        if state is not None and not unknown:
            for key in ("observed_files", "edited_this_turn", "root_instruction_digest"):
                if key in saved_state:
                    state.metadata[key] = saved_state[key]
                else:
                    state.metadata.pop(key, None)
            state.verifications[:] = saved_verifications
        if unknown:
            reason = str(exc) if isinstance(exc, Exception) else type(exc).__name__
            message = (f"{failed}: {reason}; rollback failed: state unknown for {', '.join(unknown)}. "
                       "Their originals stay in this turn's checkpoint; inspect them and use /undo "
                       "(or --rewind last) to restore")
            if not isinstance(exc, Exception) and not isinstance(exc, Cancelled):
                raise
            raise ToolFailure(ErrorCode.EXECUTION_FAILED, message) from None
        if isinstance(exc, Cancelled):
            raise ToolFailure(ErrorCode.CANCELLED, f"{failed}: cancelled; patch rolled back, no file changed") from None
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, ToolFailure):
            raise ToolFailure(exc.code, f"{failed}: {exc}; patch rolled back, no file changed",
                              retryable=exc.retryable) from None
        if isinstance(exc, OSError):
            raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"{failed}: write failed ({type(exc).__name__}); "
                              "patch rolled back, no file changed") from None
        raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"{failed}: write failed ({type(exc).__name__}: {exc}); "
                          "patch rolled back, no file changed") from None
    data = summarize(planned)
    if context is not None and getattr(context, "checkpoint", None) is not None:
        # Metadata only (jsonl): call id, paths, operations and line counts; never the diff or content.
        context.checkpoint("patch_applied", call_id=getattr(context, "call_id", None), file_count=len(planned),
                           files=[{key: entry[key] for key in ("path", "op", "added", "removed")}
                                  for entry in data["files"]])
    return data
