from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from baseagent.agent import run_agent
from baseagent.agent.completion import CompletionPolicy
from baseagent.agent.state import RunStatus
from baseagent.middleware import AgentMiddleware
from baseagent.session import SessionStore
from baseagent.tools.result import ToolResult
from baseagent.tools.workspace import Workspace, coding_tools


class Model:
    def __init__(self, messages=()):
        self.messages = iter(messages)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(deepcopy(messages))
        return next(self.messages)


def call(name, args, identifier="a"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sample.py").write_text("answer = 5\n", encoding="utf-8")
        self.workspace = Workspace(self.root, allow_write=True, allow_command=True)
        self.tools = coding_tools(self.workspace)
        self.store = SessionStore(self.root / ".baseagent" / "sessions.sqlite3")
        self.argv = [sys.executable, "-B", "-c", "import sample; assert sample.answer == 5"]
        self.requirements = {"require_plan": False, "checks": [{"name": "answer", "argv": self.argv, "paths": ["sample.py"]}], "artifacts": ["sample.py"]}
        self.policy = CompletionPolicy(self.workspace, self.requirements)

    def run_turn(self, model, prompt=None, **kwargs):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id="demo",
                         completion_policy=kwargs.pop("completion_policy", self.policy), **kwargs)

    def verify(self, argv=None, paths=None, identifier="a"):
        return {"tool_calls": [call("verify_command", {"argv": argv or self.argv, "paths": ["sample.py"] if paths is None else paths}, identifier)]}

    def test_actual_check_and_artifact_pass(self):
        model = Model([self.verify(), {"content": "verified"}])
        state = self.run_turn(model, "task")
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertTrue(state.metadata["completion_report"]["passed"])
        self.assertEqual(state.metadata["completion_report"]["checks"]["answer"], state.verifications[-1]["id"])
        self.assertTrue(any("completion_requirements" in message.get("content", "") for message in model.seen[0]))
        self.assertFalse(any("completion_requirements" in message.get("content", "") for message in state.messages))

    def test_claim_without_check_rejected_and_budget_remains_bounded(self):
        state = self.run_turn(Model([{"content": "all tests passed"}]), "task", max_steps=1)
        self.assertEqual(state.status, RunStatus.MAX_STEPS_EXCEEDED)
        self.assertIsNone(state.final_answer)
        self.assertFalse(state.metadata["completion_report"]["passed"])
        self.assertEqual(state.model_calls, 1)
        self.assertEqual(state.messages[-1]["role"], "system")

    def test_rejection_then_model_fixes_evidence(self):
        state = self.run_turn(Model([{"content": "done"}, self.verify(), {"content": "actually verified"}]), "task")
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.model_calls, 3)
        self.assertEqual(sum(item["type"] == "completion_rejected" for item in self.store.events("demo")), 1)

    def test_resume_after_gate_rejection(self):
        self.run_turn(Model([{"content": "done"}]), "task", max_steps=1)
        state = self.run_turn(Model([self.verify(), {"content": "done"}]), max_steps=3, max_model_calls=3)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.tool_calls, 1)

    def test_old_turn_evidence_cannot_satisfy_new_turn(self):
        self.run_turn(Model([self.verify(), {"content": "done"}]), "first")
        state = self.run_turn(Model([{"content": "done again"}]), "next", max_steps=1)
        self.assertEqual(state.status, RunStatus.MAX_STEPS_EXCEEDED)
        self.assertFalse(state.metadata["completion_report"]["passed"])

    def test_source_change_during_final_model_request_invalidates_evidence(self):
        class ChangingModel(Model):
            def complete(inner, messages, tools):
                message = super().complete(messages, tools)
                if message.get("content"):
                    (self.root / "sample.py").write_text("answer = 6\n")
                return message
        state = self.run_turn(ChangingModel([self.verify(), {"content": "done"}]), "task", max_steps=2)
        self.assertEqual(state.status, RunStatus.MAX_STEPS_EXCEEDED)
        self.assertIn("stale", " ".join(state.metadata["completion_report"]["issues"]))

    def test_wrong_command_or_missing_tracked_path_does_not_pass(self):
        for argv, paths in [([sys.executable, "-c", "print('pass')"], ["sample.py"]), (self.argv, [])]:
            with self.subTest(argv=argv, paths=paths):
                state = run_agent(Model([self.verify(argv, paths), {"content": "done"}]), "task", tools=self.tools,
                                  completion_policy=self.policy, max_steps=2)
                self.assertEqual(state.status, RunStatus.MAX_STEPS_EXCEEDED)
                self.assertFalse(state.metadata["completion_report"]["passed"])

    def test_latest_failure_overrides_previous_success(self):
        class BreakModel(Model):
            def complete(inner, messages, tools):
                if len(inner.seen) == 1:
                    (self.root / "sample.py").write_text("answer = 6\n")
                return super().complete(messages, tools)
        state = self.run_turn(BreakModel([self.verify(), self.verify(identifier="b"), {"content": "done"}]), "task", max_steps=3)
        self.assertEqual(state.verifications[-1]["status"], "failed")
        self.assertFalse(state.metadata["completion_report"]["passed"])

    def test_wrapper_fake_success_is_not_actual_verification(self):
        class Fake(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                return ToolResult(data={"verification_status": "passed"})
        state = self.run_turn(Model([self.verify(), {"content": "done"}]), "task", middleware=[Fake()], max_steps=2)
        self.assertEqual(state.verifications, [])
        self.assertFalse(state.metadata["completion_report"]["passed"])

    def test_plan_required_and_completed_plan_alone_not_proof(self):
        required = {**self.requirements, "require_plan": True}
        policy = CompletionPolicy(self.workspace, required)
        state = self.run_turn(Model([self.verify(), {"content": "done"}]), "task", completion_policy=policy, max_steps=2)
        self.assertFalse(state.metadata["completion_report"]["passed"])
        plan = {"tool_calls": [call("update_plan", {"steps": [{"id": "work", "title": "work", "status": "completed"}], "expected_revision": 0}, "b")]}
        state = self.run_turn(Model([plan, {"content": "done"}]), completion_policy=policy, max_steps=4, max_model_calls=4)
        self.assertEqual(state.status, RunStatus.COMPLETED)

    def test_missing_artifact_rejects_completion(self):
        policy = CompletionPolicy(self.workspace, {"require_plan": False, "checks": [], "artifacts": ["missing.txt"]})
        state = self.run_turn(Model([{"content": "done"}]), "task", completion_policy=policy, max_steps=1)
        self.assertFalse(state.metadata["completion_report"]["passed"])

    def test_policy_change_requires_acceptance(self):
        self.run_turn(Model([{"content": "done"}]), "task", max_steps=1)
        changed = CompletionPolicy(self.workspace, {"require_plan": False, "checks": [], "artifacts": []})
        with self.assertRaises(ValueError):
            self.run_turn(Model(), completion_policy=changed)

    def test_legacy_check_without_turn_id_does_not_pass(self):
        state = self.run_turn(Model([self.verify(), {"content": "done"}]), "task")
        del state.verifications[-1]["turn_id"]
        self.assertFalse(self.policy.evaluate(state)["passed"])

    def test_policy_validation(self):
        for value in ({}, {"require_plan": "yes", "checks": [], "artifacts": []},
                      {**self.requirements, "checks": self.requirements["checks"] * 2}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CompletionPolicy(self.workspace, value)


if __name__ == "__main__":
    unittest.main()
