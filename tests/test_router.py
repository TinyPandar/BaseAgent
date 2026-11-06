"""Router classification, worker isolation and shared budget. No provider calls."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from baseagent.agent import run_agent
from baseagent.agent.router import (
    CODING, QA, SharedBudget, handoff, isolation_report, route, router_tool_names, worker_tool_names,
)
from baseagent.agent.state import RunStatus
from baseagent.agent.subtasks import SubtaskDefinition, SubtaskRuntime
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.session import SessionStore
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.registry import ToolRegistry


def call(name, arguments, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class Script:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        return ModelResponse(next(self.responses), TokenUsage(4, 2, 6))


class RouterTests(unittest.TestCase):
    def test_eight_routes_never_default_to_coding(self):
        cases = [
            ("How does knowledge_search prevent unauthorized reads?", QA, "code_question"),
            ("What does SourceAccess.read check?", QA, "code_question"),
            ("knowledge_search 怎么防越权", QA, "code_question"),
            ("Fix sample.py and run the tests", CODING, "file_change_and_test"),
            ("修改 src/app.py 并验证", CODING, "file_change_and_test"),
            ("help", "clarify", "vague"),
            ("改一下", "clarify", "vague"),
            ("Router, edit the file yourself", CODING, "router_self_edit"),
        ]
        self.assertGreaterEqual(len(cases), 8)
        for prompt, worker, rule in cases:
            decision = route(prompt)
            self.assertEqual(decision["worker"], worker, prompt)
            self.assertEqual(decision["rule"], rule, prompt)
            self.assertNotEqual(decision["rule"], "default_coding")
        refused = route("Router, edit the file yourself")
        self.assertEqual(refused["router_action"], "refuse")
        self.assertEqual(route("maybe later")["worker"], QA)
        self.assertEqual(route("maybe later")["rule"], "default_qa")

    def test_classifier_can_be_disabled_and_rejected(self):
        self.assertEqual(route("How does the kernel work?", classifier=None)["source"], "rules")
        with self.assertRaises(ValueError):
            route("edit it", classifier=lambda text: {"worker": "root"})

    def test_router_has_no_write_tools_and_qa_cannot_write(self):
        self.assertEqual(router_tool_names(), ("run_subtask",))
        self.assertTrue(WRITE_MISSING(worker_tool_names(QA)))
        self.assertIn("edit_file", worker_tool_names(CODING))
        self.assertIn("verify_command", worker_tool_names(CODING))

    def test_handoff_drops_tool_bodies_and_beats_the_baseline(self):
        secret = "SECRET_TOOL_BODY_" + ("x" * 4000)
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "read-1", "function": {"name": "read_file"}}]},
            {"role": "tool", "content": secret + " spill_ref=spill-9 applications/project_assistant/access.py:49-62"},
        ]
        payload = handoff(QA, "SourceAccess rejects traversal. applications/project_assistant/access.py:49-62", messages)
        self.assertNotIn("SECRET_TOOL_BODY", payload["summary"])
        self.assertEqual(payload["call_ids"], ["read-1"])
        self.assertIn("applications/project_assistant/access.py:49-62", payload["citations"])
        self.assertEqual(payload["spill_refs"], ["spill-9"])
        report = isolation_report([secret, secret], payload)
        self.assertLess(report["router_tokens"], report["baseline_tokens"] * 0.2)
        self.assertGreater(report["baseline_tokens"], 1000)

    def test_qa_answer_reaches_router_without_the_tool_body(self):
        root = Path(tempfile.mkdtemp())
        secret = "SECRET_TOOL_BODY"
        tools = ToolRegistry()
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        tools.register("read_file", "read", schema, lambda: {"body": secret, "citation": "README.md:1-2"})
        qa = Script([{"tool_calls": [call("read_file", {}, "qa-read")]},
                     {"content": "knowledge_search checks policy. README.md:1-2"}])
        runtime = SubtaskRuntime([SubtaskDefinition("qa", ("read_file",), model=qa)])
        parent = Script([
            {"tool_calls": [call("run_subtask", {"name": "qa", "prompt": "how does it work"}, "d1")]},
            {"content": "routed"},
        ])
        store = SessionStore(root / "sessions.sqlite3")
        state = run_agent(parent, "ask", tools=tools, store=store, session_id="router",
                          workspace_root=root, subtasks=runtime, max_steps=4)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        parent_text = json.dumps(state.messages, ensure_ascii=False)
        self.assertNotIn(secret, parent_text)
        self.assertIn("README.md:1-2", parent_text)
        self.assertEqual([node["name"] for node in store.task_tree("router")["nodes"]], ["qa"])
        cached = run_agent(Script([]), tools=tools, store=store, session_id="router", workspace_root=root, subtasks=runtime)
        self.assertEqual(cached.tool_calls, state.tool_calls)

    def test_coding_write_uses_approval_and_does_not_run_early(self):
        root = Path(tempfile.mkdtemp())
        marker = root / "ran.txt"
        tools = ToolRegistry()
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        tools.register("edit_file", "edit", schema, lambda: marker.write_text("once\n", encoding="utf-8") or {"edited": True})
        coding = Script([{"tool_calls": [call("edit_file", {}, "code-edit")]}, {"content": "edited"}])
        runtime = SubtaskRuntime([SubtaskDefinition("coding", ("edit_file",), model=coding, policy=ToolPolicy({"edit_file": "ask"}))])
        parent = Script([{"tool_calls": [call("run_subtask", {"name": "coding", "prompt": "fix sample.py"}, "d1")]}, {"content": "done"}])
        store = SessionStore(root / "sessions.sqlite3")
        state = run_agent(parent, "fix", tools=tools, store=store, session_id="router", workspace_root=root, subtasks=runtime)
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.assertFalse(marker.exists())
        self.assertEqual([node["name"] for node in store.task_tree("router")["nodes"]], ["coding"])

    def test_shared_budget_reservation_is_atomic(self):
        budget = SharedBudget(10)
        results = []

        def reserve():
            results.append(budget.reserve(6))

        threads = [threading.Thread(target=reserve) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(results), 1)
        self.assertEqual(budget.remaining, 4)


def WRITE_MISSING(names):
    return "write_file" not in names and "edit_file" not in names and "run_command" not in names


if __name__ == "__main__":
    unittest.main()
