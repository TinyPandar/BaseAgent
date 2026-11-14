"""Watchdog and HarnessKnight recognise apply_patch (README 施工段 AC 8-10).

Offline: real Workspace / apply_patch in a temporary directory, a scripted model and a fake
HarnessKnight transport. No provider and no HarnessKnight service.
"""

import json
import tempfile
import unittest
from pathlib import Path

from patch_helpers import operations, patch_call, patch_step

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.agent.watchdog import WRITE_TOOLS, observe, summary
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.middleware.harnessknight import (MAX_PATCH_IMPACTS, MAX_PATCH_TARGET_CHARS, infer_impacts,
                                                install_harnessknight)
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.workspace import Workspace, coding_tools


class Script:
    def __init__(self, steps):
        self.steps = iter(steps)

    def complete(self, messages, tools=None):
        step = next(self.steps)
        return ModelResponse(step(messages) if callable(step) else step, TokenUsage(2, 1, 3))


def watchdog(**overrides):
    config = {"enabled": True, "stall_steps": 0, "stall_abort_steps": 0, "repeat_abort": 0,
              "max_steps": 0, "max_tokens": 0, "max_seconds": 0, "tool_timeout_seconds": 0}
    config.update(overrides)
    return {"watchdog": config}


class FakeTransport:
    def __init__(self):
        self.calls = []

    def evaluate(self, event, idempotency_key, timeout):
        self.calls.append(json.loads(json.dumps(event)))
        return {"decision": "ALLOW", "risk_signals": [], "metadata": {}}


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for index in range(4):
            (self.root / f"f{index}.txt").write_text(f"v{index}\n", encoding="utf-8")

    def tools(self):
        return coding_tools(Workspace(self.root, allow_write=True))


class WatchdogTests(Case):
    def test_apply_patch_is_a_write_tool(self):
        self.assertIn("apply_patch", WRITE_TOOLS)

    def test_task_that_only_uses_apply_patch_is_not_stalled(self):
        # Three steps of apply_patch with stall_abort_steps=2: a stall would abort after the second.
        steps = [patch_step(self.root, ("modify", f"f{index}.txt", [{"old_text": f"v{index}", "new_text": "x"}]),
                            identifier=f"p{index}") for index in range(3)]
        state = run_agent(Script([*steps, {"content": "done"}]), "patch", tools=self.tools(), max_steps=8,
                          workspace_root=self.root, runtime_config=watchdog(stall_steps=1, stall_abort_steps=2))
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(summary(state)["stall_count"], 0)
        self.assertEqual((self.root / "f2.txt").read_text(encoding="utf-8"), "x\n")

    def test_same_task_with_reads_only_is_stalled(self):
        # Control: the identical schedule with a read instead of apply_patch is aborted.
        steps = [{"tool_calls": [{"id": f"r{index}", "type": "function",
                                  "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{index}.txt"})}}]}
                 for index in range(3)]
        state = run_agent(Script([*steps, {"content": "done"}]), "read", tools=self.tools(), max_steps=8,
                          workspace_root=self.root, runtime_config=watchdog(stall_steps=1, stall_abort_steps=2))
        self.assertEqual(state.status, RunStatus.ABORTED_BY_WATCHDOG)

    def test_failed_apply_patch_is_not_progress(self):
        state = type("S", (), {"step": 1, "total_tokens": 0, "metadata": {}, "status": "running"})()
        state.metadata["watchdog_config"] = watchdog(stall_abort_steps=0)["watchdog"]
        observe(state, phase="after_tool", name="apply_patch", arguments={}, call_id="p", ok=False, duration=0.0)
        self.assertFalse(state.metadata["watchdog"]["steps"]["1"]["write"])
        observe(state, phase="after_tool", name="apply_patch", arguments={}, call_id="q", ok=True, duration=0.0)
        self.assertTrue(state.metadata["watchdog"]["steps"]["1"]["write"])


class HarnessKnightTests(Case):
    def test_one_impact_per_file_with_delete_as_delete(self):
        arguments = {"operations": [{"op": "add", "path": "new.txt"}, {"op": "modify", "path": "a.txt"},
                                    {"op": "delete", "path": "old.txt"}]}
        self.assertEqual(infer_impacts("apply_patch", arguments), [
            {"kind": "file", "target": "new.txt", "operation": "write"},
            {"kind": "file", "target": "a.txt", "operation": "write"},
            {"kind": "file", "target": "old.txt", "operation": "delete"}])

    def test_impacts_are_bounded_in_count_and_length(self):
        long = "d/" * 3000 + "x.txt"
        arguments = {"operations": [{"op": "modify", "path": f"{index}-{long}"} for index in range(60)]}
        impacts = infer_impacts("apply_patch", arguments)
        self.assertEqual(len(impacts), MAX_PATCH_IMPACTS)
        self.assertTrue(all(len(item["target"]) <= MAX_PATCH_TARGET_CHARS for item in impacts))
        self.assertEqual(len({item["target"] for item in impacts}), MAX_PATCH_IMPACTS)  # still distinct
        self.assertLess(len(json.dumps(impacts)), 64 * 1024)

    def test_malformed_arguments_give_no_impacts(self):
        for arguments in ({}, {"operations": "x"}, {"operations": [1, {"op": "add"}, {"path": ""}]}):
            self.assertEqual(infer_impacts("apply_patch", arguments), [])

    def test_real_apply_patch_call_reports_each_file_to_harnessknight(self):
        transport = FakeTransport()
        policy, layers = install_harnessknight(ToolPolicy(), [], url="http://hk.invalid", transport=transport)
        step = patch_step(self.root, ("modify", "f0.txt", [{"old_text": "v0", "new_text": "z"}]),
                          ("delete", "f1.txt"), ("add", "n.txt", "new\n"))
        state = run_agent(Script([step, {"content": "done"}]), "patch", tools=self.tools(), middleware=layers,
                          tool_policy=policy, workspace_root=self.root)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        before = next(call for call in transport.calls if call["event_type"] == "tool.call.before")
        self.assertEqual(before["impacts"], [
            {"kind": "file", "target": "f0.txt", "operation": "write"},
            {"kind": "file", "target": "f1.txt", "operation": "delete"},
            {"kind": "file", "target": "n.txt", "operation": "write"}])
        self.assertFalse((self.root / "f1.txt").exists())

    def test_patch_call_helper_shape_matches_tool(self):
        # Guard against helper drift: the impacts come from the real argument shape.
        call = patch_call("p", operations(self.root, ("delete", "f3.txt")))
        arguments = json.loads(call["function"]["arguments"])
        self.assertEqual(infer_impacts("apply_patch", arguments)[0]["operation"], "delete")


if __name__ == "__main__":
    unittest.main()
