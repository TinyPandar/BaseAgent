"""Execution-environment interface shared by files, guidance and commands.

Paths passed to operations are relative to a stable workspace namespace.
resolve_path/root are namespace identifiers used by policy and persistence;
consumers must never perform native filesystem I/O on the returned Path.
Backends enforce traversal/link protections in their actual environment.
A backend must declare identity/configuration in contract() for safe resume.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

from baseagent.tools.result import ToolResult


@runtime_checkable
class WorkspaceBackend(Protocol):
    root: Path
    protected_paths: tuple[Path, ...]

    def contract(self) -> dict: ...
    def resolve_path(self, path: str) -> Path: ...
    def read_bytes(self, path: str, limit: int = 200_000) -> bytes: ...
    def file_hash(self, path: str) -> str: ...
    def get_instructions(self, path: str = ".") -> dict: ...
    def search_files(self, query: str, glob: str = "**/*") -> dict: ...
    def write_file(self, path: str, content: str, expected_sha256: str | None,
                   expected_instructions: str | None) -> dict: ...
    def execute(self, argv: list[str], *, timeout: float, output_limit: int = 20_000,
                cancellation=None) -> ToolResult: ...
