"""Offline completion-boundary test-and-fix integration tests."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.agent.completion import CompletionPolicy
from baseagent.agent.state import RunStatus
from baseagent.main import main
from baseagent.middleware import AgentMiddleware
from baseagent.session import SessionStore
from baseagent.tools.result import ToolResult
from baseagent.tools.workspace import Workspace, coding_tools


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(deepcopy(messages))
        response = next(self.responses)
        return response() if callable(response) else response


def tool(name, arguments, identifier):
    return {"tool_calls": [{"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}]}


DONE = {"content": "done"}


class TestFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root, allow_write=True, allow_command=True)
        self.tools = coding_tools(self.workspace)
        self.argv = [sys.executable, "-B", "-c",
                     "from pathlib import Path; assert Path('answer').read_text() == 'good', 'bad answer'"]

    def policy(self, **spec):
        return CompletionPolicy(self.workspace, {"require_plan": False, "checks": [], "artifacts": [],
                                                "test_fix": {"argv": self.argv, **spec}})

    def write(self, content, identifier="write", name="write_file"):
        def response():
            observed = self.workspace.read_file("answer") if (self.root / "answer").exists() else None
            args = {"path": "answer", "expected_sha256": observed["sha256"] if observed else "missing",
                    "expected_instructions": self.workspace.get_instructions("answer")["digest"]}
            if name == "edit_file":
                args["edits"] = [{"old_text": observed["content"], "new_text": content}]
            else:
                args["content"] = content
            return tool(name, args, identifier)
        return response

    def run_script(self, responses, **options):
        model = ScriptedModel(responses)
        state = run_agent(model, "fix", tools=self.tools, completion_policy=options.pop("completion_policy", self.policy()),
                          max_steps=20, **options)
        return state, model

    def test_one_or_two_failures_then_fix_passes(self):
        for failures in (1, 2):
            with self.subTest(failures=failures):
                script = [self.write("bad"), DONE]
                if failures == 2:
                    script.extend([self.write("still bad", "again", "edit_file"), DONE])
                script.extend([self.write("good", "fix", "edit_file"), DONE])
                state, model = self.run_script(script)
                self.assertEqual(state.status, RunStatus.COMPLETED)
                self.assertEqual(state.metadata["test_fix_feedback_count"], failures)
                checks = [event for event in state.events if event["type"] == "test_fix_completed"]
                self.assertEqual(len(checks), failures + 1)
                self.assertEqual(len(state.verifications), failures + 1)
                self.assertEqual(state.verifications[-1]["status"], "passed")
                self.assertTrue(any("bad answer" in msg.get("content", "") for msg in model.seen[2]))

    def test_exhaustion_allows_exactly_k_feedback_messages(self):
        state, _ = self.run_script([self.write("bad"), DONE, DONE, DONE], completion_policy=self.policy(max_iterations=2))
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertIsNone(state.final_answer)
        self.assertIn("test_fix_iterations_exhausted", state.error)
        self.assertEqual(state.metadata["test_fix_feedback_count"], 2)
        self.assertEqual(len(state.verifications), 1)
        self.assertFalse(state.metadata["edited_this_turn"])
        self.assertTrue(any(event["type"] == "test_fix_exhausted" for event in state.events))

    def test_no_edit_and_failed_edit_do_not_run(self):
        for script in ([DONE], [tool("write_file", {"path": "answer", "content": "bad",
                            "expected_sha256": "wrong", "expected_instructions": "wrong"}, "bad"), DONE]):
            state, _ = self.run_script(script)
            self.assertEqual(state.status, RunStatus.COMPLETED)
            self.assertEqual(state.verifications, [])

    def test_k_unsuccessful_repairs_exhaust_budget(self):
        script = [self.write("bad"), DONE, self.write("still bad", "fix1"), DONE,
                  self.write("bad again", "fix2"), DONE]
        state, _ = self.run_script(script, completion_policy=self.policy(max_iterations=2))
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertIsNone(state.final_answer)
        self.assertIn("test_fix_iterations_exhausted", state.error)
        self.assertEqual(state.metadata["test_fix_feedback_count"], 2)
        self.assertEqual(len(state.verifications), 3)
        self.assertFalse(state.metadata["edited_this_turn"])

    def test_disabled_by_default(self):
        policy = CompletionPolicy(self.workspace, {"require_plan": False, "checks": [], "artifacts": []})
        state, _ = self.run_script([self.write("bad"), DONE], completion_policy=policy)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.verifications, [])

    def test_feedback_output_truncated(self):
        argv = [sys.executable, "-c", "import sys; print('x'*1000); sys.exit(1)"]
        state, model = self.run_script([self.write("bad"), DONE, DONE],
                                       completion_policy=self.policy(argv=argv, max_iterations=1, output_max_chars=25))
        feedback = next(msg["content"] for msg in model.seen[-1] if msg.get("content", "").startswith("Harness test command failed."))
        payload = json.loads(feedback[feedback.index('{'):])
        self.assertEqual(payload["output"], "x" * 25)
        self.assertTrue(payload["truncated"])
        self.assertNotEqual(state.status, RunStatus.COMPLETED)

    def test_require_plan_and_command_checks_coexist(self):
        policy = CompletionPolicy(self.workspace, {"require_plan": True, "checks": [
            {"name": "answer", "argv": self.argv, "paths": ["answer"]}], "artifacts": ["answer"],
            "test_fix": {"argv": self.argv}})
        plan = tool("update_plan", {"expected_revision": 0, "steps": [
            {"id": "fix", "title": "fix", "status": "completed"}]}, "plan")
        verify = tool("verify_command", {"argv": self.argv, "paths": ["answer"]}, "verify")
        state, _ = self.run_script([self.write("bad"), DONE, self.write("good", "fix"), DONE, plan, verify, DONE],
                                   completion_policy=policy)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertTrue(state.metadata["completion_report"]["passed"])
        self.assertEqual(len([e for e in state.events if e["type"] == "test_fix_completed"]), 2)
        self.assertFalse(state.metadata["edited_this_turn"])
        self.assertEqual(state.metadata["test_fix_feedback_count"], 1)

    def test_explicit_check_before_automatic_test_keeps_its_evidence(self):
        policy = CompletionPolicy(self.workspace, {"require_plan": False, "checks": [
            {"name": "answer", "argv": self.argv, "paths": ["answer"]}], "artifacts": [],
            "test_fix": {"argv": self.argv}})
        verify = tool("verify_command", {"argv": self.argv, "paths": ["answer"]}, "verify")
        state, _ = self.run_script([self.write("good"), verify, DONE], completion_policy=policy)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.metadata["completion_report"]["checks"]["answer"], state.verifications[0]["id"])
        self.assertFalse(state.metadata["edited_this_turn"])

    def test_failed_test_without_edit_cannot_complete_or_rerun(self):
        model = ScriptedModel([self.write("bad"), DONE, DONE, DONE])
        with patch.object(self.workspace, "run_command", wraps=self.workspace.run_command) as command:
            state = run_agent(model, "fix", tools=self.tools, completion_policy=self.policy(max_iterations=2), max_steps=10)
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(state.metadata["test_fix_feedback_count"], 2)

    def test_resume_preserves_counter_and_new_turn_resets_edit_marker(self):
        store = SessionStore(self.root / "sessions.db")
        policy = self.policy(max_iterations=1)
        options = {"tools": self.tools, "completion_policy": policy, "store": store, "session_id": "fix"}
        first = run_agent(ScriptedModel([self.write("bad"), DONE]), "fix", max_steps=2, **options)
        self.assertEqual(first.status, RunStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(first.metadata["test_fix_feedback_count"], 1)
        state = run_agent(ScriptedModel([self.write("good", "fix"), DONE]), max_steps=4, max_model_calls=4, **options)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        count = len(state.verifications)
        state = run_agent(ScriptedModel([DONE]), "next", **options)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(len(state.verifications), count)
        self.assertNotIn("test_fix_feedback_count", state.metadata)

    def test_fake_tool_success_does_not_count_as_edit(self):
        class Fake(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                return ToolResult(data={"path": "answer"})
        state, _ = self.run_script([self.write("bad"), DONE], middleware=[Fake()])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.verifications, [])

    def test_validation_and_command_permission(self):
        for spec in ({"argv": "shell"}, {"argv": []}, {"max_iterations": -1}, {"output_max_chars": 0}, {"timeout": 61}):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.policy(**spec)
        self.workspace.allow_command = False
        with self.assertRaises(ValueError):
            self.policy()

    def test_cli_and_harness_config(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"version": 1, "workspace": {"root": ".", "db": "sessions.db"},
            "permissions": {"allow_write": True, "allow_command": True},
            "policies": {"test_command": self.argv, "test_fix_max_iterations": 0}}))
        for via_cli in (False, True):
            with self.subTest(via_cli=via_cli):
                args = ["fix", "--config", str(config)]
                if via_cli:
                    args += ["--test-command", json.dumps([sys.executable, "-c", "pass"]),
                             "--test-fix-max-iterations", "1", "--test-fix-output-max-chars", "20"]
                model = ScriptedModel([self.write("bad"), DONE])
                with patch("baseagent.main.initialize_model", side_effect=AssertionError("network forbidden")), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = main(args, model_factory=lambda _, model=model: model)
                self.assertEqual(code, 0 if via_cli else 1)
