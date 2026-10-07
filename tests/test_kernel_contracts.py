import ast
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from baseagent.agent.events import AgentEvent as LegacyAgentEvent, event
from baseagent.agent.state import RunStatus as LegacyRunStatus, State
from baseagent.kernel import AgentEvent, KernelState, RunStatus


ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / "src" / "baseagent" / "kernel"
BASELINE_STATE = Path(__file__).parent / "fixtures" / "state_v0_1_sync_baseline.json"
ALLOWED_THIRD_PARTY = {"jsonschema"}


def forbidden_imports(source, package="baseagent.kernel"):
    """Resolve relative imports as well as imports nested inside other code."""
    rejected = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                if node.level > len(parts):
                    rejected.append((node.lineno, "relative import outside package"))
                    continue
                prefix = ".".join(parts[:len(parts) - node.level + 1])
                modules = [prefix + ("." + node.module if node.module else "")]
            else:
                modules = [node.module or ""]
        else:
            continue
        for module in modules:
            if module == "baseagent.kernel" or module.startswith("baseagent.kernel."):
                continue
            if module.split(".")[0] not in sys.stdlib_module_names | ALLOWED_THIRD_PARTY:
                rejected.append((node.lineno, module))
    return rejected


class KernelStateTests(unittest.TestCase):
    def test_json_round_trip_preserves_nested_state_and_every_status(self):
        for status in RunStatus:
            with self.subTest(status=status):
                state = KernelState(
                    messages=[{"role": "user", "content": "任务"},
                              {"role": "assistant", "tool_calls": [
                                  {"id": "one", "function": {"name": "read", "arguments": "{}"}}]}],
                    step=2, max_steps=10, model_calls=3, tool_calls=4,
                    status=status, final_answer="答案", error="error",
                    max_model_calls=12, max_tool_calls=20, turn_start=0,
                    max_context_bytes=8000, max_tool_context_bytes=1000,
                    events=[{"type": "model_started", "timestamp": 123.0, "data": {"attempt": 3}}],
                )
                restored = KernelState.from_dict(json.loads(json.dumps(state.to_dict(), ensure_ascii=False, allow_nan=False)))
                self.assertEqual(restored, state)
                self.assertIs(restored.status, status)

    def test_default_instances_do_not_share_messages_or_events(self):
        first, second = KernelState(), KernelState()
        first.messages.append({"role": "user", "content": "one"})
        first.events.append({"type": "run_stopped", "data": {"status": "failed"}})
        self.assertEqual(second.messages, [])
        self.assertEqual(second.events, [])

    def test_loaded_states_and_exported_snapshot_do_not_alias_nested_data(self):
        data = KernelState(messages=[{"role": "user", "content": ["one"]}],
                           events=[{"type": "example", "data": {"values": [1]}}]).to_dict()
        original = deepcopy(data)
        first, second = KernelState.from_dict(data), KernelState.from_dict(data)
        first.messages[0]["content"].append("two")
        first.events[0]["data"]["values"].append(2)
        snapshot = second.to_dict()
        snapshot["messages"][0]["content"].append("three")
        self.assertEqual(data, original)
        self.assertEqual(second.to_dict(), original)

    def test_baseline_state_file_retains_all_extension_fields_and_field_order(self):
        # Captured from State in tag v0.1-sync-baseline, before extraction.
        data = json.loads(BASELINE_STATE.read_text(encoding="utf-8"))
        state = State.from_dict(data)
        self.assertEqual(state.to_dict(), data)
        self.assertEqual(list(state.__dataclass_fields__), list(data))
        self.assertIs(state.status, RunStatus.INTERRUPTED)
        self.assertEqual(State.from_dict(json.loads(json.dumps(state.to_dict()))).to_dict(), data)

    def test_legacy_state_file_without_usage_count_keeps_unknown_attempts(self):
        data = {"messages": [], "status": "interrupted", "model_calls": 3}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-state.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            saved = path.read_bytes()
            state = State.from_dict(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(state.unknown_usage_calls, 3)
            self.assertEqual(path.read_bytes(), saved)
        self.assertNotIn("unknown_usage_calls", data)

    def test_old_imports_share_contract_types(self):
        self.assertIs(LegacyRunStatus, RunStatus)
        self.assertIs(LegacyAgentEvent, AgentEvent)


class AgentEventTests(unittest.TestCase):
    def test_lifecycle_events_round_trip_in_existing_envelope(self):
        for kind, data in (
            ("model_started", {"attempt": 1}),
            ("model_returned", {"attempt": 1, "usage_reported": False}),
            ("model_failed", {"attempt": 1, "error_type": "ValueError"}),
            ("tool_started", {"call_id": "one"}),
            ("tool_returned", {"call_id": "one", "ok": True, "error_code": None}),
            ("assistant_checkpoint", {"step": 1, "tool_count": 1}),
            ("run_stopped", {"status": "completed", "model_calls": 1, "tool_calls": 1}),
        ):
            with self.subTest(kind=kind), patch("baseagent.agent.events.time.time", return_value=123.0):
                persisted = event(kind, **data)
                typed = AgentEvent.from_dict(persisted)
                self.assertEqual(typed, AgentEvent(kind, timestamp=123.0, data=data))
                self.assertEqual(typed.to_dict(), persisted)
                self.assertEqual(set(typed.to_dict()), {"type", "timestamp", "data"})
                self.assertEqual(AgentEvent.from_dict(json.loads(json.dumps(typed.to_dict(), allow_nan=False))), typed)

    def test_event_data_defaults_and_loaded_snapshots_are_independent(self):
        first, second = AgentEvent("one"), AgentEvent("two")
        first.data["count"] = 1
        self.assertEqual(second.data, {})
        data = {"type": "example", "timestamp": 123.0, "data": {"values": [1]}}
        restored = AgentEvent.from_dict(data)
        restored.data["values"].append(2)
        self.assertEqual(data["data"]["values"], [1])
        restored.to_dict()["data"]["values"].append(3)
        self.assertEqual(restored.data["values"], [1, 2])


class KernelImportBoundaryTests(unittest.TestCase):
    def test_all_kernel_python_imports_stay_within_stdlib_or_kernel(self):
        files = sorted(KERNEL.rglob("*.py"))
        self.assertTrue(files)
        for path in files:
            package = ".".join(("baseagent", "kernel", *path.parent.relative_to(KERNEL).parts))
            with self.subTest(path=path.relative_to(KERNEL)):
                self.assertEqual(forbidden_imports(path.read_text(encoding="utf-8"), package), [])

    def test_scanner_rejects_external_and_parent_imports_even_when_nested(self):
        for source in (
            "import pydantic", "from baseagent.tools.result import ToolResult",
            "from ..agent.state import State", "from .. import tools",
            "if False:\n    import openai", "def build():\n    from baseagent.llm import response",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from baseagent.agent.state import State",
        ):
            with self.subTest(source=source):
                self.assertTrue(forbidden_imports(source))
        self.assertEqual(forbidden_imports("import json\nfrom typing import Any\nfrom .state import KernelState"), [])
        self.assertEqual(forbidden_imports("from ..state import KernelState", "baseagent.kernel.tools"), [])

    def test_jsonschema_imports_are_confined_to_tools_module(self):
        importing_files = set()
        for path in KERNEL.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""] if not node.level else []
                else:
                    continue
                if any(module.split(".")[0] == "jsonschema" for module in modules):
                    importing_files.add(path.relative_to(KERNEL).as_posix())
        self.assertEqual(importing_files, {"tools.py"})
        self.assertEqual(forbidden_imports("from jsonschema import Draft202012Validator"), [])


if __name__ == "__main__":
    unittest.main()
