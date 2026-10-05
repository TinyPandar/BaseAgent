"""Bounded root-to-directory AGENTS.md guidance within a workspace."""

from hashlib import sha256
import json

from .editing import read_bytes
from .result import ErrorCode, ToolFailure


def instruction_digest(values):
    contract = [{"path": value["path"], "sha256": value["sha256"]} for value in values]
    return sha256(json.dumps(contract, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def get_local_instructions(workspace, path="."):
    target = workspace._path(path)
    directory = target if target.is_dir() else target.parent
    relative = directory.relative_to(workspace.root)
    directories = [workspace.root]
    for part in relative.parts:
        directories.append(directories[-1] / part)
    values = []
    total = 0
    for directory in directories:
        candidate = directory / "AGENTS.md"
        if not candidate.exists():
            continue
        canonical = workspace._path(str(candidate.relative_to(workspace.root)))
        if canonical != candidate.absolute():
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "repository instructions cannot use symlinks")
        content = read_bytes(candidate, 20_000)
        total += len(content)
        if total > 40_000:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "applicable instructions exceed 40 KB")
        values.append({"path": str(candidate.relative_to(workspace.root)), "sha256": sha256(content).hexdigest(), "content": content.decode("utf-8")})
    digest = instruction_digest(values)
    return {"scope": str(directory.relative_to(workspace.root)), "instructions": values, "digest": digest}


def get_instructions(workspace, path="."):
    return workspace.get_instructions(path)
