"""Scripted models for the local Harbor fixture. No provider, no network, zero tokens.

They drive the real tool path (get_instructions -> write_file -> run_command) through
``environment.exec``, so Harbor's verifier grades actual container state: ``correct``
writes the expected answer and earns reward 1, ``wrong`` writes another one and earns 0.
"""

from __future__ import annotations

import json

from baseagent.llm.response import ModelResponse, TokenUsage

ANSWERS = {"correct": "ok\n", "wrong": "not ok\n"}
ANSWER_FILE = "answer.txt"


def _call(name, arguments, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _last_tool_data(messages):
    for message in reversed(messages):
        if message.get("role") == "tool":
            return json.loads(message["content"]).get("data") or {}
    return {}


class ScriptedFixtureModel:
    """Writes ``answer.txt`` in the task workdir, shows it, then stops. Reports zero usage."""

    def __init__(self, variant: str):
        if variant not in ANSWERS:
            raise ValueError(f"scripted model must be one of {sorted(ANSWERS)}")
        self.variant = variant
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        step = self.calls
        if step == 1:
            message = {"role": "assistant", "content": None,
                       "tool_calls": [_call("get_instructions", {"path": ANSWER_FILE}, "instructions")]}
        elif step == 2:
            digest = _last_tool_data(messages)["digest"]
            message = {"role": "assistant", "content": None, "tool_calls": [_call("write_file", {
                "path": ANSWER_FILE, "content": ANSWERS[self.variant], "expected_sha256": "missing",
                "expected_instructions": digest}, "write")]}
        elif step == 3:
            message = {"role": "assistant", "content": None,
                       "tool_calls": [_call("run_command", {"argv": ["cat", ANSWER_FILE]}, "show")]}
        else:
            message = {"role": "assistant", "content": f"Wrote {ANSWER_FILE} ({self.variant} script)."}
        return ModelResponse(message, TokenUsage(0, 0, 0))
