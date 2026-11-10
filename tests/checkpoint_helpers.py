"""Fake models that write files through the real workspace tools. No provider calls."""

import json
from hashlib import sha256
from pathlib import Path

from baseagent.tools.workspace import Workspace


def tool_call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def current_hash(root, path):
    target = Path(root) / path
    return sha256(target.read_bytes()).hexdigest() if target.exists() else "missing"


def write_call(root, identifier, path, content):
    """A write_file call built when the model answers, with the file's hash at that moment."""
    digest = Workspace(root).get_instructions(path)["digest"]
    return tool_call(identifier, "write_file", {"path": path, "content": content,
                                                "expected_sha256": current_hash(root, path),
                                                "expected_instructions": digest})


def writes(root, *pairs, prefix="w"):
    """Callable model step: one assistant message that writes every (path, content) pair."""
    def step(messages=None):
        return {"tool_calls": [write_call(root, f"{prefix}{index}", path, content)
                               for index, (path, content) in enumerate(pairs, 1)]}
    return step


class Script:
    """Sequential fake model. Steps are responses, callables(messages) or exceptions."""

    def __init__(self, steps):
        self.steps = iter(steps)
        self.seen = []
        self.tools = []

    def complete(self, messages, tools):
        self.seen.append([dict(message) for message in messages])
        self.tools.append([tool["function"]["name"] for tool in tools])
        step = next(self.steps)
        if isinstance(step, BaseException):
            raise step
        return step(messages) if callable(step) else step
