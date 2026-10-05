"""Host filesystem backend. This backend provides no OS sandboxing."""

import os
from pathlib import Path

from baseagent.tools.result import ErrorCode, ToolFailure, ToolResult
from baseagent.tools.editing import atomic_write, read_bytes, file_hash
from baseagent.tools.process import run_process


class LocalBackend:
    def __init__(self, root, *, protected_paths=()):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace root must be a directory")
        self.protected_paths = tuple(Path(path).resolve() for path in protected_paths)
        # Tool permission is enforced by Workspace, before backend invocation.
        self.allow_write = True

    def contract(self):
        return {"kind": "local", "version": 1, "root": str(self.root),
                "isolation": "none", "protected_paths": sorted(str(path) for path in self.protected_paths)}

    def resolve_path(self, relative):
        return self._path(relative)

    def read_bytes(self, path, limit=200_000):
        return read_bytes(self._path(path), limit)

    def file_hash(self, path):
        return file_hash(self._path(path))

    def get_instructions(self, path="."):
        from baseagent.tools.instructions import get_local_instructions
        return get_local_instructions(self, path)

    def write_file(self, path, content, expected_sha256, expected_instructions):
        return atomic_write(self, path, content, expected_sha256, expected_instructions)

    def execute(self, argv, *, timeout, output_limit=20_000, cancellation=None):
        return run_process(argv, self.root, timeout=timeout, output_limit=output_limit, cancellation=cancellation)

    def _path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
        if any(part.casefold() in {".git", ".venv", ".baseagent"} or part.casefold().startswith((".env", ".baseagent-edit-")) for part in path.relative_to(self.root).parts):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path is protected")
        if any(path == protected or path.is_relative_to(protected) for protected in self.protected_paths):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "session storage is protected")
        return path

    def search_files(self, query: str, glob: str = "**/*") -> dict:
        if not query:
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "query must not be empty")
        if Path(glob).is_absolute() or ".." in Path(glob).parts:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "glob must stay inside workspace")
        matches = []
        inspected = 0
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = [name for name in dirs if name.casefold() not in {".git", ".venv", ".baseagent", "__pycache__"} and not name.casefold().startswith(".env")]
            for name in files:
                if name.startswith(".env"):
                    continue
                path = Path(directory) / name
                relative = path.relative_to(self.root)
                if glob != "**/*" and not relative.match(glob):
                    continue
                if path.is_symlink() or path.stat().st_size > 200_000:
                    continue
                inspected += 1
                if inspected > 2_000:
                    return {"matches": matches, "truncated": True}
                try:
                    self._path(str(relative))
                    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                        if query in line:
                            matches.append({"path": str(relative), "line": line_no, "text": line[:300]})
                            if len(matches) >= 100:
                                return {"matches": matches, "truncated": True}
                except (UnicodeError, OSError, ToolFailure):
                    continue
        return {"matches": matches, "truncated": False}
