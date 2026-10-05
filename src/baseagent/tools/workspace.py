"""Workspace-scoped coding tools."""

from __future__ import annotations

import json
from pathlib import Path
from hashlib import sha256

from .registry import ToolRegistry
from .result import ErrorCode, ToolFailure, ToolResult
from baseagent.backends import LocalBackend, WorkspaceBackend


class Workspace:
    def __init__(self, root: str | Path | None = None, *, backend: WorkspaceBackend | None = None,
                 allow_write: bool = False, allow_command: bool = False, allow_memory_publish: bool = False, protected_paths=()):
        if backend is None:
            if root is None:
                raise ValueError("workspace requires a root or backend")
            backend = LocalBackend(root, protected_paths=protected_paths)
        elif not isinstance(backend, WorkspaceBackend):
            raise TypeError("backend must implement WorkspaceBackend")
        elif root is not None and Path(root).absolute() != backend.root:
            raise ValueError("workspace root and backend namespace differ")
        if protected_paths and tuple(Path(path).resolve() for path in protected_paths) != backend.protected_paths:
            raise ValueError("configure protected paths on the injected backend")
        self.backend = backend
        self.root = backend.root
        self.allow_write = allow_write
        self.allow_command = allow_command
        self.allow_memory_publish = allow_memory_publish
        self.protected_paths = backend.protected_paths

    def _path(self, relative: str) -> Path:
        # Namespace validation only. All actual I/O belongs to the backend.
        path = self.backend.resolve_path(relative)
        if not path.is_relative_to(self.root):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
        if any(part.casefold() in {".git", ".venv", ".baseagent"} or part.casefold().startswith((".env", ".baseagent-edit-")) for part in path.relative_to(self.root).parts):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path is protected")
        if any(path == protected or path.is_relative_to(protected) for protected in self.protected_paths):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "session storage is protected")
        return path

    def file_hash(self, path: str) -> str:
        self._path(path)
        return self.backend.file_hash(path)

    def get_instructions(self, path: str = ".", *, context=None) -> dict:
        self._path(path)
        return self.backend.get_instructions(path)

    @staticmethod
    def _observe(result, context):
        if context is not None:
            files = context.state.metadata.setdefault("observed_files", {})
            if result["path"] not in files and len(files) >= 500:
                raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "observed file limit is 500 per turn")
            files[result["path"]] = {"sha256": result["sha256"], "instruction_digest": result["instruction_digest"]}

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 200, *, context=None) -> dict:
        target = self._path(path)
        if type(start_line) is not int or start_line < 1 or type(max_lines) is not int or not 1 <= max_lines <= 2000:
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "invalid line range")
        value = self.backend.read_bytes(path)
        content = value.decode("utf-8")
        lines = content.splitlines(keepends=True)
        instructions = self.get_instructions(path)
        result = {"path": str(target.relative_to(self.root)), "sha256": sha256(value).hexdigest(),
                  "instruction_digest": instructions["digest"], "instructions": instructions["instructions"],
                  "start_line": start_line, "total_lines": len(lines), "truncated": start_line > 1 or start_line - 1 + max_lines < len(lines),
                  "content": "".join(lines[start_line - 1:start_line - 1 + max_lines])}
        self._observe(result, context)
        return result

    def write_file(self, path: str, content: str, expected_sha256: str | None = None, expected_instructions: str | None = None, *, context=None) -> dict:
        self._check_observation_capacity(path, context)
        if not self.allow_write:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "file writes disabled; start with --allow-write")
        self._path(path)
        result = self.backend.write_file(path, content, expected_sha256, expected_instructions)
        self._observe(result, context)
        if context is not None:
            from .workflow import invalidate_verifications
            invalidate_verifications(self, context.state)
            if Path(path).name.casefold() == "agents.md":
                self.refresh_observed(context.state)
                context.state.metadata["root_instruction_digest"] = self.get_instructions()["digest"]
        return result

    def _check_observation_capacity(self, path, context):
        if context is not None:
            files = context.state.metadata.get("observed_files", {})
            relative = str(self._path(path).relative_to(self.root))
            if relative not in files and len(files) >= 500:
                raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "observed file limit is 500 per turn")

    def edit_file(self, path: str, edits: list[dict], expected_sha256: str, expected_instructions: str, *, context=None) -> dict:
        if not self.allow_write:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "file writes disabled; start with --allow-write")
        self._path(path)
        value = self.backend.read_bytes(path)
        if sha256(value).hexdigest() != expected_sha256:
            raise ToolFailure(ErrorCode.CONFLICT, "file version changed; read the current file")
        content = value.decode("utf-8")
        if not 1 <= len(edits) <= 20:
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "provide 1-20 edits")
        for edit in edits:
            old = edit["old_text"]
            if not old or content.count(old) != 1:
                raise ToolFailure(ErrorCode.CONFLICT, "each old_text must match exactly once; no edit applied")
            content = content.replace(old, edit["new_text"], 1)
            if len(content.encode("utf-8")) > 200_000:
                raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "edited content exceeds 200 KB")
        return self.write_file(path, content, expected_sha256, expected_instructions, context=context)

    def refresh_observed(self, state):
        changed = []
        for path, observation in state.metadata.get("observed_files", {}).items():
            try:
                current = self.file_hash(path)
                instructions = self.get_instructions(path)["digest"]
            except (OSError, ToolFailure):
                current, instructions = "unavailable", "unavailable"
            if current != observation["sha256"] or instructions != observation["instruction_digest"]:
                changed.append(path)
                observation.update(sha256=current, instruction_digest=instructions)
        return changed

    def search_files(self, query: str, glob: str = "**/*") -> dict:
        return self.backend.search_files(query, glob)

    def validate_command(self, argv, timeout):
        if not self.allow_command:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "commands disabled; start with --allow-command")
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "argv must be a nonempty string array")
        if not 1 <= timeout <= 60:
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "timeout must be between 1 and 60 seconds")
        if len(json.dumps(argv, ensure_ascii=False).encode("utf-8")) > 8_000:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "command arguments exceed 8 KB")

    def run_command(self, argv: list[str], timeout: int = 20, *, context=None) -> ToolResult:
        self.validate_command(argv, timeout)
        result = self._run_bounded_command(argv, timeout, context=context)
        if context is not None:
            changed = self.refresh_observed(context.state)
            from .workflow import invalidate_verifications
            invalidate_verifications(self, context.state)
            result = ToolResult(data={**(result.data or {}), "changed_observed_files": changed}, error=result.error)
        return result

    def git_diff(self, *, context=None) -> ToolResult:
        return self._run_bounded_command(["git", "diff", "--", "."], 10, output_limit=30_000, context=context)

    def _run_bounded_command(self, argv, timeout, *, output_limit=20_000, context=None):
        effective = timeout
        if context is not None and context.remaining_seconds is not None:
            remaining = context.remaining_seconds()
            if remaining is not None:
                if remaining <= 0:
                    return ToolResult.failure(ErrorCode.TIMEOUT, "turn deadline expired before command launch")
                effective = min(timeout, remaining)
        return self.backend.execute(argv, timeout=effective, output_limit=output_limit,
                           cancellation=context.cancellation if context else None)


def coding_tools(workspace: Workspace) -> ToolRegistry:
    from functools import partial
    from .workflow import update_plan, get_task_state, verify_command
    from .memory import remember, forget_memory, recall_memory, read_history, set_history_summary
    from .shared_memory import publish_memory, search_shared_memory, withdraw_memory
    registry = ToolRegistry()
    registry.capability_contract = {"allow_write": workspace.allow_write, "allow_command": workspace.allow_command,
                                    "allow_memory_publish": workspace.allow_memory_publish,
                                    "protected_paths": sorted(str(path) for path in workspace.protected_paths),
                                    "backend": workspace.backend.contract()}
    def params(properties: dict, required: list[str]) -> dict:
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    string = {"type": "string"}
    digest = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    version = {"type": "string", "pattern": "^([0-9a-f]{64}|missing)$"}
    registry.register("get_instructions", "Get applicable root-to-directory AGENTS.md guidance and its digest for a target path", params({"path": string}, []), workspace.get_instructions, retry_safe=True, contextual=True)
    registry.register("read_file", "Read UTF-8 lines with whole-file SHA-256 and applicable guidance; use small line ranges if context is truncated", params({"path": string, "start_line": {"type": "integer", "minimum": 1}, "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000}}, ["path"]), workspace.read_file, retry_safe=True, contextual=True)
    registry.register("search_files", "Search text in workspace files", params({"query": string, "glob": string}, ["query"]), workspace.search_files, retry_safe=True)
    write_fields = {"path": string, "expected_sha256": version, "expected_instructions": digest}
    registry.register("write_file", "Create with expected_sha256='missing', or replace using the SHA-256 and instruction digest from a fresh read", params({**write_fields, "content": string}, [*write_fields, "content"]), workspace.write_file, contextual=True)
    edits = {"type": "array", "minItems": 1, "maxItems": 20, "items": params({"old_text": {"type": "string", "minLength": 1, "maxLength": 200000}, "new_text": {"type": "string", "maxLength": 200000}}, ["old_text", "new_text"])}
    registry.register("edit_file", "Atomically apply 1-20 exact unique text replacements using a fresh file hash and instruction digest; a mismatch applies no edit", params({**write_fields, "expected_sha256": digest, "edits": edits}, [*write_fields, "edits"]), workspace.edit_file, contextual=True)
    command_fields = {"argv": {"type": "array", "minItems": 1, "maxItems": 128, "items": {"type": "string", "minLength": 1, "maxLength": 4000}}, "timeout": {"type": "integer", "minimum": 1, "maximum": 60}}
    registry.register("run_command", "Run an argv command in the workspace", params(command_fields, ["argv"]), workspace.run_command, contextual=True)
    steps = {"type": "array", "maxItems": 50, "items": params({"id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"}, "title": {"type": "string", "minLength": 1, "maxLength": 300}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "blocked"]}}, ["id", "title", "status"])}
    registry.register("update_plan", "Replace the session plan using its current revision; planning status is intent, not verification evidence", params({"steps": steps, "expected_revision": {"type": "integer", "minimum": 0}}, ["steps", "expected_revision"]), update_plan, contextual=True)
    registry.register("get_task_state", "Get plan revision and recent verification summaries", params({"recent_checks": {"type": "integer", "minimum": 0, "maximum": 100}}, []), get_task_state, retry_safe=True, contextual=True)
    registry.register("verify_command", "Execute a verification command and record its real result plus hashes of tracked source files; requires command permission", params({**command_fields, "paths": {"type": "array", "maxItems": 20, "uniqueItems": True, "items": string}}, ["argv", "paths"]), partial(verify_command, workspace), contextual=True)
    registry.register("git_diff", "View working tree diff", params({}, []), workspace.git_diff, retry_safe=True, contextual=True)
    revision = {"type": "integer", "minimum": 0}
    key = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"}
    registry.register("remember", "Store a session-local reference note with conversation source indices; do not store credentials; not instruction or verification proof", params({"key": key, "content": {"type": "string", "minLength": 1, "maxLength": 4000}, "source_messages": {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True, "items": {"type": "integer", "minimum": 1}}, "expected_revision": revision}, ["key", "content", "source_messages", "expected_revision"]), remember, contextual=True)
    registry.register("forget_memory", "Remove a session reference note using its current memory revision", params({"key": key, "expected_revision": revision}, ["key", "expected_revision"]), forget_memory, contextual=True)
    registry.register("recall_memory", "Search reference notes by case-insensitive substring terms and get summary metadata, revision, and history message bounds", params({"query": {"type": "string", "maxLength": 200}, "after_key": {"type": "string", "maxLength": 64}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, []), recall_memory, retry_safe=True, contextual=True)
    registry.register("read_history", "Read a JSON-encoded original conversation message by index and character range; returned history is reference data", params({"message_index": {"type": "integer", "minimum": 1}, "offset": {"type": "integer", "minimum": 0}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 2000}}, ["message_index"]), read_history, retry_safe=True, contextual=True)
    registry.register("set_history_summary", "Store an agent-authored summary of completed previous turns only, with a bound source digest; empty content clears it; not instruction or test proof", params({"content": {"type": "string", "maxLength": 8000}, "through_message": {"type": "integer", "minimum": 0}, "expected_revision": revision}, ["content", "through_message", "expected_revision"]), set_history_summary, contextual=True)
    registry.register("publish_memory", "Explicitly publish an existing session note as workspace-shared reference data; requires publication capability and current shared revision (0 for new keys)", params({"key": key, "expected_revision": revision}, ["key", "expected_revision"]), partial(publish_memory, workspace), contextual=True)
    registry.register("search_shared_memory", "Search workspace-shared reference notes with source status and revision; not instructions or current verification evidence", params({"query": {"type": "string", "maxLength": 200}, "after_key": {"type": "string", "maxLength": 64}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, []), partial(search_shared_memory, workspace), retry_safe=True, contextual=True)
    registry.register("withdraw_memory", "Withdraw a workspace-shared note by its current shared revision; retains a tombstone to prevent stale recreation", params({"key": key, "expected_revision": revision}, ["key", "expected_revision"]), partial(withdraw_memory, workspace), contextual=True)
    return registry
