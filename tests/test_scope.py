from pathlib import Path
import sys
import tempfile
import time
import unittest
from uuid import uuid4

from baseagent.agent.agent import _run_loop
from baseagent.agent.cancellation import CancellationToken
from baseagent.agent.scope import ExecutionScope
from baseagent.agent.state import RunStatus, State
from baseagent.llm.reservation import TokenReservation, request_digest
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace
from baseagent.session import SessionStore


def call(identifier="read", name="read"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": "{}"}}


class Model:
    def __init__(self, messages=(), usage=True):
        self.messages, self.usage = iter(messages), usage
        self.calls = 0
        self.observe = None
        self.timeouts = []

    def reserve_request(self, messages, tools):
        return TokenReservation(6, 4, request_digest(messages, tools))

    def complete_with_control(self, messages, tools, *, cancellation, timeout):
        self.timeouts.append(timeout)
        self.calls += 1
        if self.observe:
            self.observe()
        message = next(self.messages)
        if isinstance(message, BaseException):
            raise message
        return ModelResponse(message, TokenUsage(2, 2, 4) if self.usage else None)

    def complete_reserved(self, messages, tools, *, reservation, cancellation, timeout):
        return self.complete_with_control(messages, tools, cancellation=cancellation, timeout=timeout)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.sqlite3")
        self.state = State(session_id="demo", turn_id="root", workspace_root=str(self.root),
                           messages=[{"role": "system", "content": "root"}, {"role": "user", "content": "task"}],
                           model_calls=1, tool_calls=1, prompt_tokens=1, completion_tokens=1, total_tokens=2, turn_started_at=time.time())
        self.store.save(self.state, [call("delegate", "delegate")])
        self.store.start_call(self.state, "delegate")
        self.tools = ToolRegistry()

    def node(self):
        state = State(session_id="demo", turn_id=uuid4().hex, workspace_root=str(self.root),
                      messages=[{"role": "system", "content": "child"}, {"role": "user", "content": "task"}], turn_started_at=time.time())
        record = self.store.create_task(self.state, call_id="delegate", name="reader", state=state)
        return self.store.task_node(self.state, record["task_id"])

    def run_node(self, view, model, *, cancellation=None):
        view.state.status = RunStatus.RUNNING
        return _run_loop(model, view.state, self.tools, (), view, ToolPolicy(), CancellationToken(), None,
                         execution_scope=view.execution_scope(cancellation=cancellation))

    def test_actual_child_calls_charge_root_once_and_keep_direct_usage(self):
        self.tools.register("read", "read", {"type": "object"}, lambda: "ok", retry_safe=True)
        with self.store.exclusive("demo"):
            view = self.node()
            result = self.run_node(view, Model([{"tool_calls": [call()]}, {"content": "done"}]))
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual((self.state.model_calls, self.state.tool_calls, self.state.total_tokens), (3, 2, 10))
        self.assertEqual((result.model_calls, result.tool_calls, result.total_tokens), (2, 1, 8))
        self.assertEqual(self.state.metadata["direct_usage"]["model_calls"], 1)
        self.assertEqual(self.state.metadata["direct_usage"]["total_tokens"], 2)
        tree = self.store.task_tree("demo")
        self.assertEqual(tree["current_root_state"]["total_tokens"], 10)
        self.assertEqual(tree["nodes"][0]["state"]["metadata"]["direct_usage"]["total_tokens"], 8)

    def test_root_and_local_call_limits_refuse_before_dispatch(self):
        with self.store.exclusive("demo"):
            view = self.node()
            for root_limit, child_limit in ((1, 8), (8, 0)):
                self.state.max_model_calls, view.state.max_model_calls = root_limit, child_limit
                model = Model()
                result = self.run_node(view, model)
                self.assertEqual(result.status, RunStatus.MAX_MODEL_CALLS_EXCEEDED)
                self.assertEqual(model.calls, 0)
                self.assertEqual(self.state.model_calls, 1)

    def test_root_tool_limit_blocks_child_effect_with_pending_record(self):
        effects = []
        self.tools.register("read", "read", {"type": "object"}, lambda: effects.append(True))
        with self.store.exclusive("demo"):
            view = self.node()
            self.state.max_tool_calls = 1
            result = self.run_node(view, Model([{"tool_calls": [call()]}]))
            self.assertEqual(result.status, RunStatus.MAX_TOOL_CALLS_EXCEEDED)
            self.assertEqual(view.call(result, "read")["status"], "pending")
        self.assertEqual(effects, [])

    def test_root_preauthorization_cannot_be_disabled_by_child(self):
        with self.store.exclusive("demo"):
            view = self.node()
            self.state.preauthorize_model, self.state.max_total_tokens = True, 7
            model = Model()
            result = self.run_node(view, model)
            self.assertEqual(result.status, RunStatus.MAX_TOKENS_EXCEEDED)
            self.assertEqual(model.calls, 0)
            self.state.max_total_tokens = 8
            result = self.run_node(view, Model([{"content": "done"}]))
            self.assertEqual(result.status, RunStatus.COMPLETED)
            self.assertEqual(self.state.total_tokens, 6)
            self.assertEqual(self.state.model_reservations, {})

    def test_unknown_child_usage_is_durable_in_root_and_blocks_aggregate_clear(self):
        with self.store.exclusive("demo"):
            view = self.node()
            self.state.preauthorize_model, self.state.max_total_tokens = True, 20
            def observe():
                tree = self.store.task_tree("demo")
                self.assertEqual(tree["current_root_state"]["unknown_usage_calls"], 1)
                self.assertEqual(tree["nodes"][0]["state"]["unknown_usage_calls"], 1)
                self.assertEqual(sum(tree["current_root_state"]["model_reservations"].values()), 6)
            model = Model([ConnectionError("failed")])
            model.observe = observe
            self.assertEqual(self.run_node(view, model).status, RunStatus.FAILED)
            self.assertEqual(self.run_node(view, Model()).status, RunStatus.USAGE_UNAVAILABLE)
        before = self.store.task_tree("demo")
        with self.assertRaises(ValueError):
            self.store.resolve_usage("demo", TokenUsage(2, 2, 4), 1)
        self.assertEqual(self.store.task_tree("demo"), before)

    def test_ancestor_cancellation_stops_before_model(self):
        token = CancellationToken()
        token.cancel()
        with self.store.exclusive("demo"):
            view = self.node()
            model = Model()
            self.assertEqual(self.run_node(view, model, cancellation=token).status, RunStatus.CANCELLED)
            self.assertEqual(model.calls, 0)

    def test_persistent_root_cancellation_reaches_node_adapter_and_stops_tools(self):
        effects = []
        self.tools.register("read", "read", {"type": "object"}, lambda: effects.append(True))
        with self.store.exclusive("demo"):
            view = self.node()
            model = Model([{"tool_calls": [call()]}])
            model.observe = lambda: self.store.request_cancellation("demo")
            self.assertEqual(self.run_node(view, model).status, RunStatus.CANCELLED)
            self.assertEqual(view.state.total_tokens, 4)
            self.assertEqual(self.state.total_tokens, 6)
        self.assertEqual(effects, [])

    def test_original_root_deadline_limits_actual_child_command(self):
        workspace = Workspace(self.root, allow_command=True)
        self.tools.register("read", "command", {"type": "object"},
                            lambda context: workspace.run_command([sys.executable, "-c", "import time; time.sleep(20)"], timeout=20, context=context), contextual=True)
        with self.store.exclusive("demo"):
            view = self.node()
            self.state.max_duration_seconds = 0.7
            self.state.turn_started_at = time.time()
            model = Model([{"tool_calls": [call()]}])
            started = time.monotonic()
            result = self.run_node(view, model)
            self.assertEqual(result.status, RunStatus.DEADLINE_EXCEEDED)
            self.assertLess(time.monotonic() - started, 5)
            self.assertLessEqual(model.timeouts[0], 0.7)
            self.assertEqual(view.call(result, "read")["status"], "completed")
            self.assertEqual(self.state.tool_calls, 2)

    def test_persistent_scope_cannot_omit_ancestor_budget(self):
        with self.store.exclusive("demo"):
            view = self.node()
            with self.assertRaises(ValueError):
                _run_loop(Model(), view.state, self.tools, (), view, ToolPolicy(), CancellationToken(), None)

    def test_nonpersistent_nested_scope_applies_all_ancestor_limits(self):
        root = State(turn_id="root", model_calls=1, max_model_calls=1)
        parent, child = State(turn_id="parent"), State(turn_id="child", messages=[{"role": "user", "content": "task"}])
        model = Model()
        result = _run_loop(model, child, self.tools, (), None, ToolPolicy(), CancellationToken(), None,
                           execution_scope=ExecutionScope(child, [parent, root]))
        self.assertEqual(result.status, RunStatus.MAX_MODEL_CALLS_EXCEEDED)
        self.assertEqual(model.calls, 0)

    def test_three_level_persistent_usage_is_not_double_charged(self):
        with self.store.exclusive("demo"):
            parent = self.node()
            parent.save(parent.state, [call("nested", "delegate")])
            parent.start_call(parent.state, "nested")
            parent.execution_scope().tool_started(retry_safe=False)
            parent.record_attempt(parent.state, "nested", {"name": "delegate", "arguments": {}})
            child_state = State(session_id="demo", turn_id=uuid4().hex, workspace_root=str(self.root),
                                messages=[{"role": "system", "content": "child"}, {"role": "user", "content": "task"}], turn_started_at=time.time())
            record = self.store.create_task(self.state, parent_task_id=parent.task_id, call_id="nested", name="reader", state=child_state)
            child = self.store.task_node(self.state, record["task_id"])
            self.state.max_model_calls = 2
            self.assertEqual(self.run_node(child, Model([{"content": "done"}])).status, RunStatus.COMPLETED)
        tree = self.store.task_tree("demo")
        root, middle, leaf = tree["current_root_state"], tree["nodes"][0]["state"], tree["nodes"][1]["state"]
        self.assertEqual([item["total_tokens"] for item in (root, middle, leaf)], [6, 4, 4])
        self.assertEqual([item["metadata"]["direct_usage"]["total_tokens"] for item in (root, middle, leaf)], [2, 0, 4])
        self.assertEqual([item["tool_calls"] for item in (root, middle, leaf)], [2, 1, 0])

    def test_child_output_cap_violation_pauses_ancestors_and_preserves_usage(self):
        class LowCap(Model):
            def reserve_request(self, messages, tools):
                return TokenReservation(6, 1, request_digest(messages, tools))

        with self.store.exclusive("demo"):
            view = self.node()
            self.state.max_total_tokens, self.state.preauthorize_model = 20, True
            result = self.run_node(view, LowCap([{"content": "done"}]))
            self.assertEqual(result.status, RunStatus.REQUEST_BOUND_VIOLATED)
        self.assertEqual(self.state.total_tokens, 6)
        self.assertEqual(self.store.load("demo").metadata["request_bound_violation"]["node_turn_id"], result.turn_id)
        self.assertEqual(result.messages[-1]["content"], "done")


if __name__ == "__main__":
    unittest.main()
