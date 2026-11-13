"""apply_patch test helpers: operations built from the file's hash at the moment the model answers."""

import json
from hashlib import sha256
from pathlib import Path

from baseagent.tools.workspace import Workspace


def current_hash(root, path):
    target = Path(root) / path
    return sha256(target.read_bytes()).hexdigest() if target.exists() else "missing"


def operation(root, spec):
    """("modify", path, edits) | ("add", path, content) | ("delete", path) -> one operation."""
    op, path = spec[0], spec[1]
    value = {"op": op, "path": path, "expected_sha256": "missing" if op == "add" else current_hash(root, path),
             "expected_instructions": Workspace(root).get_instructions(".")["digest"]}
    if op == "modify":
        value["edits"] = spec[2]
    elif op == "add":
        value["content"] = spec[2]
    return value


def operations(root, *specs):
    return [operation(root, spec) for spec in specs]


def patch_call(identifier, ops):
    return {"id": identifier, "type": "function",
            "function": {"name": "apply_patch", "arguments": json.dumps({"operations": ops})}}


def patch_step(root, *specs, identifier="p1"):
    """Callable model step: one apply_patch call built when the model answers."""
    return lambda messages=None: {"tool_calls": [patch_call(identifier, operations(root, *specs))]}


def tree(root):
    """Every workspace file's bytes, harness state excluded (byte-for-byte comparisons)."""
    root = Path(root)
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*"))
            if path.is_file() and ".baseagent" not in path.relative_to(root).parts}
