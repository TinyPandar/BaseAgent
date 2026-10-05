import json
import sqlite3
import subprocess
import sys
import unittest
import time
from uuid import uuid4

import test_scope as fixtures
from baseagent.agent.agent import _run_loop
from baseagent.agent.cancellation import CancellationToken
from baseagent.agent.state import RunStatus
from baseagent.agent.state import State
from baseagent.llm.response import TokenUsage
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.result import ToolResult


class TaskRecoveryTests(unittest.TestCase):
    setUp = fixtures.ScopeTests.setUp
    node = fixtures.ScopeTests.node
    run_node = fixtures.ScopeTests.run_node

    def unknown_node(self):
        with self.store.exclusive("demo"):
            view = self.node()
            self.state.max_total_tokens, self.state.preauthorize_model = 100, True
            self.run_node(view, fixtures.Model([ConnectionError("failed")]))
        return view.task_id

    def approval_node(self):
        self.effects = []
        self.tools.register("read", "effect", {"type": "object"}, lambda: self.effects.append(True))
        self.policy = ToolPolicy({"read": "ask"})
        with self.store.exclusive("demo"):
            view = self.node()
            state = _run_loop(fixtures.Model([{"tool_calls": [fixtures.call()]}]), view.state, self.tools, (), view,
                              self.policy, CancellationToken(), None, execution_scope=view.execution_scope())
            self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        return view.task_id, state.metadata["approval_request"]["request_digest"]

    def reopen(self, task_id):
        self.state = self.store.load("demo")
        return self.store.task_node(self.state, task_id)

    def test_usage_receipt_charges_ancestors_once_and_resume_keeps_budget(self):
        task_id = self.unknown_node()
        state = self.store.resolve_task_usage("demo", task_id, TokenUsage(2, 2, 4), 1)
        self.assertEqual(state.unknown_usage_calls, 0)
        self.assertEqual(state.metadata["direct_usage"]["unknown_usage_calls"], 0)
        tree = self.store.task_tree("demo")
        self.assertEqual(tree["current_root_state"]["total_tokens"], 6)
        self.assertEqual(tree["current_root_state"]["metadata"]["direct_usage"]["total_tokens"], 2)
        self.assertEqual(tree["current_root_state"]["model_reservations"], {})
        with self.assertRaises(ValueError):
            self.store.resolve_task_usage("demo", task_id, TokenUsage(2, 2, 4), 1)
        with self.store.exclusive("demo"):
            view = self.reopen(task_id)
            self.assertEqual(self.run_node(view, fixtures.Model([{"content": "done"}])).status, RunStatus.COMPLETED)
        self.assertEqual(self.store.load("demo").total_tokens, 10)
        self.assertEqual(self.store.load("demo").model_calls, 3)

    def test_wrong_count_and_missing_target_have_no_changes(self):
        task_id = self.unknown_node()
        before = self.store.task_tree("demo")
        for target, count in ((task_id, 2), (task_id, True), ("missing", 1)):
            with self.assertRaises(ValueError):
                self.store.resolve_task_usage("demo", target, TokenUsage(2, 2, 4), count)
        self.assertEqual(self.store.task_tree("demo"), before)

    def test_usage_event_failure_rolls_back_all_ancestor_counters(self):
        task_id = self.unknown_node()
        before = self.store.task_tree("demo")
        with self.store._connection() as connection:
            connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.resolve_task_usage("demo", task_id, TokenUsage(2, 2, 4), 1)
        self.assertEqual(self.store.task_tree("demo"), before)

    def test_sibling_reservations_are_reconciled_independently(self):
        with self.store.exclusive("demo"):
            first = self.node()
            first.state.preauthorize_model, first.state.max_total_tokens = True, 100
            self.run_node(first, fixtures.Model([ConnectionError()]))
            self.store.save(self.state, [fixtures.call("other", "delegate")])
            self.store.start_call(self.state, "other")
            candidate = State(session_id="demo", turn_id=uuid4().hex, workspace_root=str(self.root), messages=first.state.messages[:2], turn_started_at=time.time())
            record = self.store.create_task(self.state, call_id="other", name="reader", state=candidate)
            second = self.store.task_node(self.state, record["task_id"])
            second.state.preauthorize_model, second.state.max_total_tokens = True, 100
            self.run_node(second, fixtures.Model([ConnectionError()]))
        self.assertEqual(self.store.load("demo").unknown_usage_calls, 2)
        self.store.resolve_task_usage("demo", first.task_id, TokenUsage(2, 2, 4), 1)
        root = self.store.load("demo")
        self.assertEqual(root.model_reservations, {f"{second.state.turn_id}:1": 6})
        self.assertEqual(root.unknown_usage_calls, 1)
        self.assertEqual(root.total_tokens, 6)
        self.store.resolve_task_usage("demo", second.task_id, TokenUsage(2, 2, 4), 1)
        root = self.store.load("demo")
        self.assertEqual(root.total_tokens, 10)
        self.assertEqual(root.model_reservations, {})
        self.assertEqual(root.unknown_usage_calls, 0)

    def test_parent_direct_receipt_does_not_clear_leaf_unknown_usage(self):
        with self.store.exclusive("demo"):
            parent = self.node()
            self.run_node(parent, fixtures.Model([ConnectionError()]))
            parent.state.status = RunStatus.RUNNING
            parent.save(parent.state, [fixtures.call("nested", "delegate")])
            parent.start_call(parent.state, "nested")
            parent.execution_scope().tool_started(retry_safe=False)
            parent.record_attempt(parent.state, "nested", {"name": "delegate", "arguments": {}})
            candidate = State(session_id="demo", turn_id=uuid4().hex, workspace_root=str(self.root), messages=parent.state.messages[:2], turn_started_at=time.time())
            record = self.store.create_task(self.state, parent_task_id=parent.task_id, call_id="nested", name="reader", state=candidate)
            leaf = self.store.task_node(self.state, record["task_id"])
            self.run_node(leaf, fixtures.Model([ConnectionError()]))
        self.store.resolve_task_usage("demo", parent.task_id, TokenUsage(2, 2, 4), 1)
        tree = self.store.task_tree("demo")
        self.assertEqual(tree["current_root_state"]["unknown_usage_calls"], 1)
        self.assertEqual(tree["nodes"][0]["state"]["unknown_usage_calls"], 1)
        self.assertEqual(tree["nodes"][0]["state"]["metadata"]["direct_usage"]["unknown_usage_calls"], 0)
        self.assertEqual(tree["nodes"][1]["state"]["unknown_usage_calls"], 1)
        self.store.resolve_task_usage("demo", leaf.task_id, TokenUsage(2, 2, 4), 1)
        tree = self.store.task_tree("demo")
        self.assertEqual([item["state"]["total_tokens"] for item in tree["nodes"]], [8, 4])
        self.assertEqual(tree["current_root_state"]["total_tokens"], 10)
        self.assertEqual(tree["current_root_state"]["unknown_usage_calls"], 0)

    def test_reconciliation_detects_bound_excess_and_targeted_ack_keeps_usage(self):
        task_id = self.unknown_node()
        state = self.store.resolve_task_usage("demo", task_id, TokenUsage(2, 5, 7), 1)
        self.assertIn("request_bound_violation", state.metadata)
        self.assertEqual(self.store.load("demo").total_tokens, 9)
        self.store.acknowledge_task_bound("demo", task_id)
        tree = self.store.task_tree("demo")
        for snapshot in (tree["current_root_state"], tree["nodes"][0]["state"]):
            self.assertNotIn("request_bound_violation", snapshot["metadata"])
            self.assertIn("acknowledged_request_bound_violation", snapshot["metadata"])
        self.assertEqual(tree["current_root_state"]["total_tokens"], 9)
        with self.assertRaises(ValueError):
            self.store.acknowledge_task_bound("demo", task_id)

    def test_approval_is_exact_node_call_and_has_no_immediate_effect(self):
        task_id, digest = self.approval_node()
        before = self.store.task_tree("demo")
        with self.assertRaises(ValueError):
            self.store.decide_task_tool("demo", task_id, "read", "allow", "wrong")
        self.assertEqual(self.store.task_tree("demo"), before)
        self.store.decide_task_tool("demo", task_id, "read", "allow", digest)
        self.assertEqual(self.effects, [])
        self.assertNotIn("tool_approvals", self.store.load("demo").metadata)
        with self.store.exclusive("demo"):
            view = self.reopen(task_id)
            view.state.status = RunStatus.RUNNING
            result = _run_loop(fixtures.Model([{"content": "done"}]), view.state, self.tools, (), view, self.policy,
                               CancellationToken(), None, execution_scope=view.execution_scope())
            self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, [True])
        self.assertEqual(self.store.load("demo").tool_calls, 2)

    def test_denial_does_not_execute_and_changed_policy_does_not_reuse_allow(self):
        task_id, digest = self.approval_node()
        self.store.decide_task_tool("demo", task_id, "read", "allow", digest)
        with self.store.exclusive("demo"):
            view = self.reopen(task_id)
            view.state.status = RunStatus.RUNNING
            changed = ToolPolicy({"read": "ask", "other": "deny"})
            result = _run_loop(fixtures.Model(), view.state, self.tools, (), view, changed, CancellationToken(), None,
                               execution_scope=view.execution_scope())
            self.assertEqual(result.status, RunStatus.AWAITING_APPROVAL)
        self.assertEqual(self.effects, [])
        self.store.decide_task_tool("demo", task_id, "read", "deny", digest)
        with self.store.exclusive("demo"):
            view = self.reopen(task_id)
            view.state.status = RunStatus.RUNNING
            result = _run_loop(fixtures.Model([{"content": "done"}]), view.state, self.tools, (), view, changed,
                               CancellationToken(), None, execution_scope=view.execution_scope())
            self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.store.load("demo").tool_calls, 1)

    def interrupted_node(self):
        self.effects = []
        def effect():
            self.effects.append(True)
            raise KeyboardInterrupt()
        self.tools.register("read", "effect", {"type": "object"}, effect)
        with self.store.exclusive("demo"):
            view = self.node()
            self.assertEqual(self.run_node(view, fixtures.Model([{"tool_calls": [fixtures.call()]}])).status, RunStatus.INTERRUPTED)
        return view.task_id

    def test_manually_verified_tool_reply_resumes_without_replaying_effect(self):
        task_id = self.interrupted_node()
        self.store.resolve_task_call("demo", task_id, "read", ToolResult(data="checked"))
        self.assertEqual(self.store.call(self.store.load("demo"), "delegate")["status"], "running")
        with self.assertRaises(ValueError):
            self.store.resolve_task_call("demo", task_id, "read", ToolResult(data="checked"))
        with self.store.exclusive("demo"):
            view = self.reopen(task_id)
            self.assertEqual(self.run_node(view, fixtures.Model([{"content": "done"}])).status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, [True])
        self.assertEqual(self.store.load("demo").tool_calls, 2)

    def test_tool_recovery_event_failure_keeps_running_ledger(self):
        task_id = self.interrupted_node()
        before = self.store.task_tree("demo")
        with self.store._connection() as connection:
            connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.resolve_task_call("demo", task_id, "read", ToolResult(data="checked"))
        self.assertEqual(self.store.task_tree("demo"), before)

    def test_cli_node_usage_recovery_without_provider_credentials(self):
        task_id = self.unknown_node()
        receipt = self.root / "usage.json"
        receipt.write_text(json.dumps({"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4, "unknown_calls": 1}))
        result = subprocess.run([sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo", "--task-id", task_id, "--resolve-usage", str(receipt)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.load("demo").total_tokens, 6)
        self.assertEqual(self.store.load("demo").unknown_usage_calls, 0)

    def test_cli_node_tool_recovery_and_invalid_modifier(self):
        task_id = self.interrupted_node()
        receipt = self.root / "result.json"
        receipt.write_text(json.dumps(ToolResult(data="checked").to_dict()))
        args = [sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo", "--task-id", task_id]
        result = subprocess.run([*args, "--resolve-tool", "read", "--result-file", str(receipt)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        invalid = subprocess.run([*args, "--resume"], capture_output=True, timeout=10)
        self.assertEqual(invalid.returncode, 2)
        empty_target = subprocess.run([sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo", "--task-id", "", "--resolve-tool", "delegate", "--result-file", str(receipt)], capture_output=True, timeout=10)
        self.assertEqual(empty_target.returncode, 2)
        self.assertEqual(self.store.call(self.store.load("demo"), "delegate")["status"], "running")

    def test_cli_node_approval_without_execution(self):
        task_id, digest = self.approval_node()
        result = subprocess.run([sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo", "--task-id", task_id, "--approve-tool", "read", "--approval-digest", digest], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        node = self.store.task_tree("demo")["nodes"][0]
        self.assertEqual(node["state"]["metadata"]["tool_approvals"]["read"]["decision"], "allow")
        self.assertEqual(node["tool_calls"][0]["status"], "pending")
        self.assertEqual(self.effects, [])

    def test_cli_node_bound_acknowledgment_keeps_charged_usage(self):
        task_id = self.unknown_node()
        self.store.resolve_task_usage("demo", task_id, TokenUsage(2, 5, 7), 1)
        result = subprocess.run([sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo", "--task-id", task_id, "--acknowledge-task-bound"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.load("demo").total_tokens, 9)
        self.assertNotIn("request_bound_violation", self.store.load("demo").metadata)


if __name__ == "__main__":
    unittest.main()
