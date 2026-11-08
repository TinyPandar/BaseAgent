"""Main-agent decisions, generic worker isolation and shared budget. No provider calls."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from baseagent.agent import run_agent
from baseagent.agent.router import (
    ANSWER, CLARIFY, DELEGATE, SharedBudget, WorkerRuntime, handoff, isolation_report, main_agent_tool_names, route,
)
from baseagent.agent.state import RunStatus
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
    def test_routes_decide_delegation_and_never_default_to_writes(self):
        cases = [
            ("hi", ANSWER, False, "small_talk"),
            ("你好", ANSWER, False, "small_talk"),
            ("How does knowledge_search prevent unauthorized reads?", DELEGATE, False, "workspace_read"),
            ("What does SourceAccess.read check?", DELEGATE, False, "workspace_read"),
            ("knowledge_search 怎么防越权", DELEGATE, False, "workspace_read"),
            ("读一下 README 并总结", DELEGATE, False, "workspace_read"),
            ("Fix sample.py and run the tests", DELEGATE, True, "file_change_and_test"),
            ("修改 sample.py 并跑测试", DELEGATE, True, "file_change_and_test"),
            ("修改 src/app.py 并验证", DELEGATE, True, "file_change_and_test"),
            ("能不能帮我处理一下这个文件", CLARIFY, False, "read_or_write_unclear"),
            ("help", CLARIFY, False, "vague"),
            ("改一下", CLARIFY, False, "vague"),
            ("Router, edit the file yourself", DELEGATE, True, "main_agent_self_edit"),
        ]
        self.assertGreaterEqual(len(cases), 8)
        for prompt, action, writes, rule in cases:
            decision = route(prompt)
            self.assertEqual(decision["action"], action, prompt)
            self.assertEqual(decision["writes"], writes, prompt)
            self.assertEqual(decision["rule"], rule, prompt)
            self.assertNotIn("worker", decision)
        refused = route("Router, edit the file yourself")
        self.assertEqual(refused["router_action"], "refuse")
        self.assertEqual(route("maybe later")["action"], ANSWER)
        self.assertFalse(route("maybe later")["writes"])

    def test_classifier_can_be_disabled_and_rejected(self):
        self.assertEqual(route("How does the kernel work?", classifier=None)["source"], "rules")
        with self.assertRaises(ValueError):
            route("edit it", classifier=lambda text: {"worker": "coding"})
        with self.assertRaises(ValueError):
            route("edit it", classifier=lambda text: {"action": DELEGATE, "writes": "yes"})
        self.assertFalse(route("x", classifier=lambda text: {"action": DELEGATE})["writes"])

    def test_main_agent_only_delegates_and_worker_inherits_entry_tools(self):
        self.assertEqual(main_agent_tool_names(), ("delegate_task",))
        tools = ToolRegistry()
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        tools.register("read_file", "read", schema, lambda: "ok", retry_safe=True)
        tools.register("edit_file", "edit", schema, lambda: "ok")
        runtime = WorkerRuntime(tools, system_prompt="entry prompt")
        self.assertEqual(list(runtime.bind_tools(tools)._tools), ["delegate_task"])
        self.assertEqual(runtime.definitions["worker"].tool_names, ("read_file", "edit_file"))
        self.assertTrue(runtime.definitions["worker"].system_prompt.startswith("entry prompt"))
        with self.assertRaises(ValueError):
            runtime.bind_tools(ToolRegistry())

    def test_handoff_drops_tool_bodies_and_beats_the_baseline(self):
        secret = "SECRET_TOOL_BODY_" + ("x" * 4000)
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "read-1", "function": {"name": "read_file"}}]},
            {"role": "tool", "content": secret + " spill_ref=spill-9 applications/project_assistant/access.py:49-62"},
        ]
        payload = handoff("worker", "SourceAccess rejects traversal. applications/project_assistant/access.py:49-62", messages)
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
        worker = Script([{"tool_calls": [call("read_file", {}, "worker-read")]},
                         {"content": "knowledge_search checks policy. README.md:1-2"}])
        runtime = WorkerRuntime(tools, system_prompt="entry", model=worker)
        parent = Script([
            {"tool_calls": [call("delegate_task", {"task": "how does it work"}, "d1")]},
            {"content": "routed"},
        ])
        store = SessionStore(root / "sessions.sqlite3")
        state = run_agent(parent, "ask", tools=tools, store=store, session_id="router",
                          workspace_root=root, subtasks=runtime, max_steps=4)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        parent_text = json.dumps(state.messages, ensure_ascii=False)
        self.assertNotIn(secret, parent_text)
        self.assertIn("README.md:1-2", parent_text)
        self.assertEqual([node["name"] for node in store.task_tree("router")["nodes"]], ["worker"])
        cached = run_agent(Script([]), tools=tools, store=store, session_id="router", workspace_root=root, subtasks=runtime)
        self.assertEqual(cached.tool_calls, state.tool_calls)

    def test_coding_write_uses_approval_and_does_not_run_early(self):
        root = Path(tempfile.mkdtemp())
        marker = root / "ran.txt"
        tools = ToolRegistry()
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        tools.register("edit_file", "edit", schema, lambda: marker.write_text("once\n", encoding="utf-8") or {"edited": True})
        worker = Script([{"tool_calls": [call("edit_file", {}, "code-edit")]}, {"content": "edited"}])
        runtime = WorkerRuntime(tools, system_prompt="entry", model=worker)
        parent = Script([{"tool_calls": [call("delegate_task", {"task": "fix sample.py", "allow_writes": True}, "d1")]}, {"content": "done"}])
        store = SessionStore(root / "sessions.sqlite3")
        state = run_agent(parent, "fix", tools=tools, store=store, session_id="router", workspace_root=root, subtasks=runtime,
                          tool_policy=ToolPolicy({"edit_file": "ask"}))
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.assertFalse(marker.exists())
        self.assertEqual([node["name"] for node in store.task_tree("router")["nodes"]], ["worker"])

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


if __name__ == "__main__":
    unittest.main()
