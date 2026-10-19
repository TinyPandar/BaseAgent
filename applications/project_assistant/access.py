"""Host-configured source visibility shared by ingestion and knowledge tools."""

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from baseagent.tools.result import ErrorCode, ToolFailure
from baseagent.tools.workspace import Workspace


DEFAULT_EXCLUDES = (
    "venv", "__pycache__", "node_modules", "build", "dist", "*.egg-info",
    "cassettes_local", "credentials*", "secret*", "password*", "id_rsa*",
    "id_ed25519*", "*.pem", "*.key", "*.p12", "*.pfx",
)


def excluded_path(path: str, patterns=()) -> bool:
    """Match components and ancestor paths, including multi-component globs."""
    parts = PurePosixPath(path).parts
    lowered = path.casefold()
    ancestors = ["/".join(parts[:end]).casefold() for end in range(1, len(parts) + 1)]
    return any(part.startswith(".") for part in parts) or any(
        fnmatchcase(candidate, pattern.casefold().rstrip("/"))
        for pattern in (*DEFAULT_EXCLUDES, *patterns)
        for candidate in [lowered, *(part.casefold() for part in parts), *ancestors]
    )


@dataclass(frozen=True)
class SourceAccess:
    """Explicit knowledge ACL; configure it alongside the host's coding policy.

    The backend remains responsible for filesystem access and TOCTOU boundaries.
    No host filesystem I/O is performed here, so injected backends remain usable.
    """

    workspace: Workspace
    include_patterns: tuple[str, ...] = ("*.md", "*.py")
    exclude_patterns: tuple[str, ...] = ()

    def __post_init__(self):
        for field in ("include_patterns", "exclude_patterns"):
            patterns = tuple(getattr(self, field))
            if any(not isinstance(p, str) or not p for p in patterns):
                raise ValueError("source patterns must be nonempty strings")
            object.__setattr__(self, field, patterns)

    def read(self, path: str) -> bytes:
        if not isinstance(path, str) or not path or any(c in path for c in "\\:\x00"):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "source is not visible")
        relative = PurePosixPath(path)
        if (relative.is_absolute() or relative.as_posix() != path
                or any(p in {".", ".."} or p != p.rstrip(" .") for p in relative.parts)
                or excluded_path(path, self.exclude_patterns)
                or not any(fnmatchcase(path.casefold(), p.casefold()) for p in self.include_patterns)):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "source is not visible")
        target = self.workspace._path(path)
        # Reject aliases even when a link/junction points back inside the root.
        if target != self.workspace.root.joinpath(*relative.parts):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "source aliases are not visible")
        return self.workspace.backend.read_bytes(path)

    def contract(self):
        return {"version": 1, "backend": self.workspace.backend.contract(),
                "include": list(self.include_patterns), "exclude": list(self.exclude_patterns),
                "mandatory_exclude": list(DEFAULT_EXCLUDES), "hidden_paths": "deny"}
