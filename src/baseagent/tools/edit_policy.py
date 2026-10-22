"""Workspace edit constraints and exact-call approval integration.

Patterns use workspace-relative POSIX paths. A pattern without a slash matches
basenames at any depth; ``**`` matches zero or more directory components.
Deny rules take precedence. Checks are trusted host configuration, never shell
strings or model arguments. Transactional checks currently require LocalBackend.
"""

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from fnmatch import fnmatchcase
from hashlib import sha256
import json
import math
from pathlib import Path
from types import SimpleNamespace

from .policy import _strict_arguments, request_digest
from .result import ErrorCode, ToolFailure


DEFAULT_DENYLIST = (".env", ".env.*", ".git/**", "*.pem", "*.key", "secrets/**", "id_rsa*")


def _matches(path, pattern):
    parts, globs = path.casefold().split("/"), pattern.casefold().split("/")
    if len(globs) == 1:
        return fnmatchcase(parts[-1], globs[0])

    def match(parts, globs):
        if not globs:
            return not parts
        if globs[0] == "**":
            return match(parts, globs[1:]) or bool(parts) and match(parts[1:], globs)
        return bool(parts) and fnmatchcase(parts[0], globs[0]) and match(parts[1:], globs[1:])

    # Directory denials also protect descendants of a matching directory.
    return match(parts, globs)


def changed_lines(before: str, after: str) -> int:
    """Count added plus removed lines, including changed line endings."""
    return sum(i2 - i1 + j2 - j1 for tag, i1, i2, j1, j2 in
               SequenceMatcher(None, before.splitlines(keepends=True),
                               after.splitlines(keepends=True)).get_opcodes()
               if tag != "equal")


@dataclass(frozen=True)
class EditPolicy:
    allowlist: tuple[str, ...] | None = None
    denylist: tuple[str, ...] = DEFAULT_DENYLIST
    max_changed_lines: int | None = None
    post_edit_check: list[str] | None = None
    post_edit_check_timeout: float = 20

    def __post_init__(self):
        for name in ("allowlist", "denylist"):
            patterns = getattr(self, name)
            if patterns is None and name == "allowlist":
                continue
            if not isinstance(patterns, (list, tuple)) or any(
                not isinstance(p, str) or not p or p.startswith("/") or "\\" in p
                or ":" in p or ".." in p.split("/") for p in patterns
            ):
                raise ValueError(f"{name} must contain relative workspace glob patterns")
            object.__setattr__(self, name, tuple(patterns))
        if self.max_changed_lines is not None and (type(self.max_changed_lines) is not int or self.max_changed_lines < 0):
            raise ValueError("max_changed_lines must be a nonnegative integer or None")
        if self.post_edit_check is not None:
            if not isinstance(self.post_edit_check, (list, tuple)) or not self.post_edit_check or any(
                not isinstance(arg, str) or not arg or "\0" in arg for arg in self.post_edit_check
            ):
                raise ValueError("post_edit_check must be a nonempty argv list")
            object.__setattr__(self, "post_edit_check", tuple(self.post_edit_check))
        if type(self.post_edit_check_timeout) not in (int, float) or not math.isfinite(self.post_edit_check_timeout) or self.post_edit_check_timeout <= 0:
            raise ValueError("post_edit_check_timeout must be positive and finite")

    def contract(self):
        return asdict(self)

    def check_path(self, workspace, path):
        target = workspace._path(path)
        relative = target.relative_to(workspace.root).as_posix()
        # Apply to every prefix so a denied directory cannot be traversed.
        prefixes = [relative, *(p.as_posix() for p in Path(relative).parents if p != Path("."))]
        if any(_matches(p, pattern) for p in prefixes for pattern in self.denylist):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "write denied by edit policy")
        if self.allowlist is not None and not any(_matches(relative, p) for p in self.allowlist):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "write path is outside edit policy allowlist")

    def requires_approval(self, before, after):
        return self.max_changed_lines is not None and changed_lines(before, after) > self.max_changed_lines


def _approved(context, name, arguments):
    if context is None or getattr(context, "policy", None) is None:
        return False
    approval = context.state.metadata.get("tool_approvals", {}).get(context.call_id)
    valid = bool(approval and approval.get("turn_id") == context.state.turn_id
                and approval.get("policy") == context.policy.fingerprint
                and approval.get("decision") == "allow")
    if not valid:
        return False
    # Retain the original argument serialization used by exact-call approvals.
    for message in reversed(context.state.messages):
        for call in message.get("tool_calls", []):
            if call.get("id") != context.call_id:
                continue
            function = call.get("function", {})
            serialized = function.get("arguments", "")
            try:
                return (function.get("name") == name and _strict_arguments(serialized) == arguments
                        and approval["request_digest"] == request_digest(name, serialized))
            except (ValueError, TypeError):
                return False
    return False


def write_file(workspace, path, content, expected_sha256, expected_instructions, *, context, request):
    """Enforce policy even for callers that bypass the agent/registry."""
    policy = workspace.edit_policy
    policy.check_path(workspace, path)

    def authorize(original):
        if policy.requires_approval((original or b"").decode("utf-8"), content) and not _approved(context, *request):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "edit exceeds max_changed_lines; explicit tool approval required")

    from baseagent.backends import LocalBackend
    from .editing import atomic_write
    if not isinstance(workspace.backend, LocalBackend):
        if policy.post_edit_check is not None:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "post_edit_check requires a transactional local backend")
        if policy.max_changed_lines is not None:
            authorize(None if workspace.backend.file_hash(path) == "missing" else workspace.backend.read_bytes(path))
        return workspace.backend.write_file(path, content, expected_sha256, expected_instructions)
    if policy.max_changed_lines is None and policy.post_edit_check is None:
        return workspace.backend.write_file(path, content, expected_sha256, expected_instructions)

    def check(target, original):
        import stat
        mode = stat.S_IMODE(target.stat().st_mode)
        interrupted = None
        try:
            result = workspace._run_bounded_command(list(policy.post_edit_check), policy.post_edit_check_timeout, context=context)
            if result.ok and workspace.backend.read_bytes(path) == content.encode("utf-8"):
                return
            output = result.data or {}
            message = (result.error.message if result.error else "check modified the edited file") + "\n" + output.get("stdout", "") + output.get("stderr", "")
        except BaseException as exc:
            interrupted = exc if not isinstance(exc, Exception) else None
            message = f"{type(exc).__name__}: {exc}"
        # Still under the write lock. Restore exact bytes, including CRLF/non-UTF8.
        if original is None:
            target.unlink(missing_ok=True)
        else:
            import os
            import tempfile
            descriptor, name = tempfile.mkstemp(prefix=".baseagent-edit-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(original)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(name, mode)
                os.replace(name, target)
            finally:
                Path(name).unlink(missing_ok=True)
        if interrupted is not None:
            raise interrupted
        raise ToolFailure(ErrorCode.EXECUTION_FAILED, "post-edit check failed; edit rolled back: " + message)

    return atomic_write(workspace.backend, path, content, expected_sha256, expected_instructions,
                        pre_edit_check=authorize if policy.max_changed_lines is not None else None,
                        post_edit_check=check if policy.post_edit_check is not None else None)


class EditToolPolicy:
    """Intersect host tool policy with diff limits using existing approvals."""

    def __init__(self, parent, workspaces):
        self.parent, self.workspaces = parent, workspaces

    def contract(self):
        return {"tool_policy": self.parent.contract(), "edit_policies": {
            name: {"root": str(w.root), "policy": w.edit_policy.contract()}
            for name, w in self.workspaces.items()}}

    @property
    def fingerprint(self):
        return sha256(json.dumps(self.contract(), sort_keys=True).encode()).hexdigest()

    def action(self, state, call_id, name, arguments):
        raw = SimpleNamespace(turn_id=state.turn_id, metadata={})
        action = self.parent.action(raw, call_id, name, arguments)
        if action == "deny":
            return action
        workspace = self.workspaces.get(name)
        if workspace is not None:
            try:
                args = _strict_arguments(arguments)
                path = args["path"]
                workspace.edit_policy.check_path(workspace, path)
                before = "" if workspace.backend.file_hash(path) == "missing" else workspace.backend.read_bytes(path).decode("utf-8")
                if name == "edit_file":
                    from .workspace import apply_edits
                    after = apply_edits(before, args["edits"])
                else:
                    after = args["content"]
                if isinstance(after, str) and workspace.edit_policy.requires_approval(before, after):
                    action = "ask"
            except (ToolFailure, OSError, ValueError, TypeError, KeyError, RecursionError):
                # Let normal schema/conflict/path validation produce a tool error.
                pass
        if action != "ask":
            return action
        approval = state.metadata.get("tool_approvals", {}).get(call_id)
        if approval and approval.get("turn_id") == state.turn_id and approval.get("policy") == self.fingerprint and approval.get("request_digest") == request_digest(name, arguments):
            return approval["decision"]
        return "ask"


def bind_edit_policy(policy, tools):
    workspaces = {}
    for name in ("write_file", "edit_file"):
        entry = tools._tools.get(name)
        workspace = getattr(entry[1], "__self__", None) if entry else None
        if getattr(workspace, "edit_policy", None) is not None and workspace.edit_policy.max_changed_lines is not None:
            workspaces[name] = workspace
    return EditToolPolicy(policy, workspaces) if workspaces else policy
