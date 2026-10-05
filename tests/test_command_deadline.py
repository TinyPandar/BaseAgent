import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.agent.state import State, RunStatus
from baseagent.session import SessionStore
from baseagent.tools.context import ToolContext
from baseagent.tools.result import ToolResult
from baseagent.tools.workspace import Workspace, coding_tools


class Model:
    def __init__(self, messages=()):
        self.messages = iter(messages)

    def complete(self, messages, tools):
        return next(self.messages)


class CommandDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root, allow_command=True)

    def context(self, remaining):
        return ToolContext(State(), lambda *args, **kwargs: None, remaining_seconds=lambda: remaining)

    def test_remaining_turn_time_caps_command_timeout(self):
        with patch("baseagent.backends.local.run_process", return_value=ToolResult(data={"exit_code": 0})) as process:
            self.workspace.run_command(["command"], 60, context=self.context(0.25))
        self.assertEqual(process.call_args.kwargs["timeout"], 0.25)

    def test_command_timeout_is_not_extended_by_turn_time(self):
        with patch("baseagent.backends.local.run_process", return_value=ToolResult(data={"exit_code": 0})) as process:
            self.workspace.run_command(["command"], 2, context=self.context(50))
        self.assertEqual(process.call_args.kwargs["timeout"], 2)

    def test_expired_deadline_prevents_launch(self):
        with patch("baseagent.backends.local.run_process") as process:
            result = self.workspace.run_command(["command"], context=self.context(-1))
        self.assertEqual(result.error.code, "timeout")
        process.assert_not_called()

    def test_git_diff_obeys_remaining_time_without_command_capability(self):
        workspace = Workspace(self.root)
        with patch("baseagent.backends.local.run_process", return_value=ToolResult(data={"exit_code": 0})) as process:
            workspace.git_diff(context=self.context(0.4))
        self.assertEqual(process.call_args.kwargs["timeout"], 0.4)
        self.assertEqual(process.call_args.kwargs["output_limit"], 30000)

    def test_actual_command_deadline_result_is_durable_and_not_replayed(self):
        store = SessionStore(self.root / ".baseagent/sessions.sqlite3")
        tools = coding_tools(self.workspace)
        argv = [sys.executable, "-c", "import time; print('started',flush=True); time.sleep(20)"]
        call = {"id": "a", "type": "function", "function": {"name": "verify_command", "arguments": json.dumps({"argv": argv, "timeout": 20, "paths": []})}}
        started = time.monotonic()
        state = run_agent(Model([{"tool_calls": [call]}]), "verify", tools=tools, store=store, session_id="deadline", workspace_root=self.root, max_duration_seconds=0.7)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(state.status, RunStatus.DEADLINE_EXCEEDED)
        record = store.call(state, "a")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(json.loads(record["result_json"])["error"]["code"], "timeout")
        self.assertEqual(state.verifications[-1]["status"], "failed")
        resumed = run_agent(Model([{"content": "timed out check recorded"}]), tools=tools, store=store, session_id="deadline", workspace_root=self.root, max_duration_seconds=30)
        self.assertEqual(resumed.status, RunStatus.COMPLETED)
        self.assertEqual(resumed.tool_calls, 1)


if __name__ == "__main__":
    unittest.main()
