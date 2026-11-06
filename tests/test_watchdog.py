"""Watchdog task inspection. No provider and no HarnessKnight service."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.agent.watchdog import observe, summary
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry


SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
          "additionalProperties": False}
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def call(name, args, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Script:
    def __init__(self, responses):
        self.responses = iter(responses)

    def complete(self, messages, tools=None):
        return ModelResponse(next(self.responses), TokenUsage(2, 1, 3))


def watchdog(**overrides):
    config = {"enabled": True, "stall_steps": 0, "stall_abort_steps": 0, "repeat_abort": 0,
              "max_steps": 0, "max_tokens": 0, "max_seconds": 0, "tool_timeout_seconds": 0}
    config.update(overrides)
    return {"watchdog": config}


def reader():
    tools = ToolRegistry()
    tools.register("read_file", "read", SCHEMA, lambda path: {"path": path})
    return tools


class WatchdogTests(unittest.TestCase):
    def test_rules_do_not_fire_below_their_limits(self):
        state = type("S", (), {"step": 1, "total_tokens": 1, "metadata": {}, "status": "running"})()
        state.metadata["watchdog_config"] = {"enabled": True, "stall_abort_steps": 5, "repeat_abort": 4,
                                              "max_tokens": 100, "tool_timeout_seconds": 5, "stall_steps": 0,
                                              "max_steps": 0, "max_seconds": 0}
        self.assertIsNone(observe(state, phase="after_tool", name="read_file", arguments={"path": "a"},
                                  call_id="1", ok=True, duration=0.01))
        self.assertIsNone(observe(state, phase="before_model"))
        self.assertFalse(summary(state)["stopped"])

    def test_disabled_watchdog_ignores_a_loop(self):
        tools = reader()
        responses = [{"tool_calls": [call("read_file", {"path": "a"}, f"r{i}")]} for i in range(4)]
        responses.append({"content": "done"})
        state = run_agent(Script(responses), "read", tools=tools, max_steps=8,
                          runtime_config=watchdog(enabled=False, repeat_abort=2))
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.tool_calls, 4)

    def test_repeat_stops_before_a_later_write(self):
        root = Path(tempfile.mkdtemp())
        marker = root / "ran.txt"
        tools = reader()
        tools.register("write_file", "write", SCHEMA, lambda path: marker.write_text("x", encoding="utf-8") or {"path": path})
        responses = [{"tool_calls": [call("read_file", {"path": "a"}, f"r{i}")]} for i in range(3)]
        responses.append({"tool_calls": [call("write_file", {"path": "ran.txt"}, "w")]})
        store = SessionStore(root / "sessions.sqlite3")
        state = run_agent(Script(responses), "loop", tools=tools, store=store, session_id="wd",
                          workspace_root=root, max_steps=8, runtime_config=watchdog(repeat_abort=3))
        self.assertEqual(state.status, RunStatus.ABORTED_BY_WATCHDOG)
        self.assertFalse(marker.exists())
        self.assertGreaterEqual(summary(state)["repeat_count"], 1)
        events = store.events("wd")
        self.assertTrue(any(event["type"] == "watchdog_decision" and event["data"]["action"] == "stop" for event in events))

    def test_stall_hints_then_stops_without_progress(self):
        tools = reader()
        responses = [{"tool_calls": [call("read_file", {"path": "a"}, f"r{i}")]} for i in range(6)]
        state = run_agent(Script(responses), "stall", tools=tools, max_steps=8,
                          runtime_config=watchdog(stall_steps=2, stall_abort_steps=4))
        self.assertEqual(state.status, RunStatus.ABORTED_BY_WATCHDOG)
        self.assertTrue(any(message.get("content", "").startswith("watchdog stall") for message in state.messages))
        self.assertGreaterEqual(summary(state)["stall_count"], 1)

    def test_write_is_not_a_stall(self):
        tools = ToolRegistry()
        tools.register("edit_file", "edit", SCHEMA, lambda path: {"path": path, "ok": True})
        responses = [{"tool_calls": [call("edit_file", {"path": f"{i}.txt"}, f"e{i}")]} for i in range(3)]
        responses.append({"content": "done"})
        state = run_agent(Script(responses), "edit", tools=tools, max_steps=8,
                          runtime_config=watchdog(stall_abort_steps=2))
        self.assertEqual(state.status, RunStatus.COMPLETED)

    def test_budget_stop_leaves_remaining_and_skips_the_next_write(self):
        root = Path(tempfile.mkdtemp())
        marker = root / "ran.txt"
        tools = reader()
        tools.register("write_file", "write", SCHEMA, lambda path: marker.write_text("x", encoding="utf-8") or {"path": path})
        responses = [{"tool_calls": [call("read_file", {"path": "a"}, "r1")]},
                     {"tool_calls": [call("write_file", {"path": "ran.txt"}, "w")]}]
        state = run_agent(Script(responses), "budget", tools=tools, workspace_root=root, max_steps=8,
                          runtime_config=watchdog(max_steps=1))
        self.assertEqual(state.status, RunStatus.ABORTED_BY_WATCHDOG)
        self.assertFalse(marker.exists())
        self.assertIn("steps", summary(state)["budget_remaining"])

    def test_tool_timeout_does_not_run_the_following_write(self):
        root = Path(tempfile.mkdtemp())
        marker = root / "ran.txt"

        def slow():
            time.sleep(0.05)
            return {"ok": True}

        tools = ToolRegistry()
        tools.register("slow", "slow", EMPTY, slow)
        tools.register("write_file", "write", SCHEMA, lambda path: marker.write_text("x", encoding="utf-8") or {"path": path})
        responses = [{"tool_calls": [call("slow", {}, "s"), call("write_file", {"path": "ran.txt"}, "w")]}]
        state = run_agent(Script(responses), "timeout", tools=tools, workspace_root=root,
                          runtime_config=watchdog(tool_timeout_seconds=0.01))
        self.assertEqual(state.status, RunStatus.ABORTED_BY_WATCHDOG)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
