from copy import deepcopy
import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import baseagent.agent.agent as adapter
from baseagent.agent import iter_agent as public_iter_agent
from baseagent.agent.state import RunStatus
from baseagent.agent.errors import WorkspaceChanged
from baseagent.kernel import AgentMiddleware, CancellationToken, Cancelled, iter_agent
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.policy import ToolPolicy


BASELINE_PATH = Path(__file__).parent / "fixtures" / "sync_agent_v0_1_baseline.py"
spec = importlib.util.spec_from_file_location("baseagent.agent._sync_baseline_for_tests", BASELINE_PATH)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


def call(arguments='{"a": 2, "b": 3}', name="add", identifier="one"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


class Model:
    def __init__(self, responses):
        self.responses = iter(deepcopy(responses))
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(deepcopy((messages, tools)))
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        return value


class Cached(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        return {"content": "cached"}


class Twice(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        handler(request)
        return handler(request)


class InterruptAfterModel(AgentMiddleware):
    def after_model(self, state, message):
        raise KeyboardInterrupt


class FailCleanup(AgentMiddleware):
    def after_agent(self, state):
        raise RuntimeError("cleanup failed")


class CancelCleanup(AgentMiddleware):
    def after_agent(self, state):
        raise Cancelled()


class DenyWrappedTool(AgentMiddleware):
    def wrap_tool_call(self, request, handler):
        from baseagent.tools.result import ErrorCode, ToolResult
        return ToolResult.failure(ErrorCode.PERMISSION_DENIED, "wrapped denial")


class ChangedWorkspace(AgentMiddleware):
    def before_model(self, state):
        raise WorkspaceChanged("workspace changed")


class CompletionRequirement:
    def contract(self):
        return {"require_second_step": True}

    def instructions(self):
        return {"instruction": "Complete two steps."}

    def evaluate(self, state):
        return {"passed": state.step >= 2, "issues": [] if state.step >= 2 else ["one more step required"]}


SCENARIOS = (
    ("answer", [{"content": "done"}], {}, (), RunStatus.COMPLETED),
    ("tool", [{"tool_calls": [call()]}, {"content": "5"}], {}, (), RunStatus.COMPLETED),
    ("contextual", [{"tool_calls": [call("{}", "context")]}, {"content": "done"}], {}, (), RunStatus.COMPLETED),
    ("unknown_tool", [{"tool_calls": [call("{}", "missing")]}, {"content": "done"}], {}, (), RunStatus.COMPLETED),
    ("bad_schema_argument", [{"tool_calls": [call('{"a": true, "b": 3}')]}, {"content": "done"}], {}, (), RunStatus.COMPLETED),
    ("duplicate_json_key", [{"tool_calls": [call('{"a": 2, "a": 4, "b": 3}')]}, {"content": "done"}], {}, (), RunStatus.COMPLETED),
    ("nonfinite_json", [{"tool_calls": [call('{"a": NaN, "b": 3}')]}, {"content": "done"}], {}, (), RunStatus.COMPLETED),
    ("step_limit", [{"tool_calls": [call()]}], {"max_steps": 1}, (), RunStatus.MAX_STEPS_EXCEEDED),
    ("model_limit", [{"tool_calls": [call()]}], {"max_model_calls": 1}, (), RunStatus.MAX_MODEL_CALLS_EXCEEDED),
    ("tool_limit", [{"tool_calls": [call()]}], {"max_tool_calls": 0}, (), RunStatus.MAX_TOOL_CALLS_EXCEEDED),
    ("model_failure", [RuntimeError("model failed")], {}, (), RunStatus.FAILED),
    ("model_interrupt", [KeyboardInterrupt()], {}, (), RunStatus.INTERRUPTED),
    ("cached_model", [], {}, (Cached,), RunStatus.COMPLETED),
    ("model_retry", [{"content": "first"}, {"content": "second"}], {}, (Twice,), RunStatus.COMPLETED),
    ("retry_limit", [{"content": "first"}], {"max_model_calls": 1}, (Twice,), RunStatus.MAX_MODEL_CALLS_EXCEEDED),
    ("wrapped_tool_denial", [{"tool_calls": [call()]}, {"content": "done"}], {}, (DenyWrappedTool,), RunStatus.COMPLETED),
    ("after_model_interrupt", [{"tool_calls": [call()]}], {}, (InterruptAfterModel,), RunStatus.INTERRUPTED),
    ("cleanup_failure", [{"content": "done"}], {}, (FailCleanup,), RunStatus.FAILED),
    ("cleanup_cancellation", [{"content": "done"}], {}, (CancelCleanup,), RunStatus.CANCELLED),
    ("token_usage", [ModelResponse({"content": "done"}, TokenUsage(4, 1, 5))], {"max_total_tokens": 5}, (), RunStatus.COMPLETED),
    ("unknown_usage", [{"tool_calls": [call()]}], {"max_total_tokens": 5}, (), RunStatus.USAGE_UNAVAILABLE),
    ("reused_id", [{"tool_calls": [call()]}, {"tool_calls": [call()]}], {}, (), RunStatus.FAILED),
    ("bad_role", [{"role": "user", "content": "invalid"}], {}, (), RunStatus.FAILED),
    ("approval", [{"tool_calls": [call()]}], {"tool_policy": ToolPolicy(default="ask")}, (), RunStatus.AWAITING_APPROVAL),
    ("policy_denial", [{"tool_calls": [call()]}, {"content": "denied"}], {"tool_policy": ToolPolicy(default="deny")}, (), RunStatus.COMPLETED),
    ("completion_requirement", [{"content": "first"}, {"content": "done"}], {"completion_policy": CompletionRequirement()}, (), RunStatus.COMPLETED),
    ("context_limit", [], {"max_context_bytes": 256}, (), RunStatus.CONTEXT_LIMIT_EXCEEDED),
    ("missing_reservation", [], {"max_total_tokens": 5, "preauthorize_model": True}, (), RunStatus.RESERVATION_UNAVAILABLE),
    ("workspace_change", [], {}, (ChangedWorkspace,), RunStatus.WORKSPACE_CHANGED),
    ("model_cancellation", [Cancelled()], {}, (), RunStatus.CANCELLED),
)


class KernelCompatibilityTests(unittest.TestCase):
    def tools(self, effects):
        tools = ToolRegistry()
        tools.register("add", "Add", {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                                     "required": ["a", "b"]}, lambda a, b: effects.append((a, b)) or a + b)

        def context(*, context):
            context.state.metadata["contextual_flag"] = context.store is not None
            context.checkpoint("context_used", call_id=context.call_id)
            effects.append("context")
            return "recorded"

        tools.register("context", "Context", {"type": "object"}, context, contextual=True)
        return tools

    def run_case(self, implementation, directory, persistent, responses, options, hooks):
        effects = []
        tools, model = self.tools(effects), Model(responses)
        store = SessionStore(Path(directory) / (implementation.__name__.rsplit(".", 1)[-1] + ".sqlite3")) if persistent else None
        state = implementation.run_agent(model, "task", tools=tools, middleware=[hook() for hook in hooks],
                                         store=store, session_id="demo" if persistent else None, **options)
        return state.to_dict(), store.events("demo") if store else state.events, effects, model.seen

    def deterministic(self):
        # Freeze only nondeterminism, not model/tool/checkpoint behavior.
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("baseagent.agent.agent.time.time", return_value=1000.0))
        stack.enter_context(patch("baseagent.agent.agent.time.monotonic", return_value=1000.0))
        stack.enter_context(patch.object(adapter, "uuid4", return_value=SimpleNamespace(hex="fixed-turn")))
        stack.enter_context(patch.object(baseline, "uuid4", return_value=SimpleNamespace(hex="fixed-turn")))
        return stack

    def test_same_inputs_match_baseline_state_answers_events_and_effects(self):
        for persistent in (False, True):
            for name, responses, options, hooks, status in SCENARIOS:
                with self.subTest(persistent=persistent, scenario=name), tempfile.TemporaryDirectory() as directory, self.deterministic():
                    old = self.run_case(baseline, directory, persistent, responses, options, hooks)
                    new = self.run_case(adapter, directory, persistent, responses, options, hooks)
                    self.assertEqual(new[0]["status"], status)
                    self.assertEqual(new, old)

    def test_pending_resume_and_completed_cache_match_baseline_without_repeating_effects(self):
        results = []
        with tempfile.TemporaryDirectory() as directory, self.deterministic():
            for implementation in (baseline, adapter):
                effects = []
                tools = self.tools(effects)
                store = SessionStore(Path(directory) / (implementation.__name__.rsplit(".", 1)[-1] + ".sqlite3"))
                interrupted = implementation.run_agent(Model([{"tool_calls": [call()]}]), "task", tools=tools,
                                                       middleware=[InterruptAfterModel()], store=store, session_id="demo")
                self.assertEqual(interrupted.status, RunStatus.INTERRUPTED)
                self.assertEqual(effects, [])
                resumed = implementation.run_agent(Model([{"content": "5"}]), tools=tools, store=store, session_id="demo")
                self.assertEqual(effects, [(2, 3)])
                before = store.events("demo")
                unused = Model([])
                cached = implementation.run_agent(unused, tools=tools, store=store, session_id="demo")
                self.assertEqual(cached.to_dict(), resumed.to_dict())
                self.assertEqual(store.events("demo"), before)
                self.assertEqual(unused.seen, [])
                results.append((resumed.to_dict(), before, effects, store.calls("demo", resumed.turn_id)))
        self.assertEqual(results[0], results[1])

    def test_pre_cancelled_sync_execution_matches_baseline(self):
        results = []
        with self.deterministic():
            for implementation in (baseline, adapter):
                token = CancellationToken()
                token.cancel()
                model = Model([])
                state = implementation.run_agent(model, "task", tools=ToolRegistry(), cancellation=token)
                self.assertEqual(model.seen, [])
                results.append(state.to_dict())
        self.assertEqual(results[0], results[1])

    def test_sync_adapter_consumes_kernel_iterator_and_returns_its_same_state(self):
        observed = []

        def observe(model, state, **kwargs):
            observed.append(state)
            for value in iter_agent(model, state, **kwargs):
                observed.append(value.type)
                yield value

        with patch.object(adapter, "iter_agent", side_effect=observe):
            state = adapter.run_agent(Model([{"content": "done"}]), "task", tools=ToolRegistry())
        self.assertIs(observed[0], state)
        self.assertEqual(observed[-1], "run_stopped")
        self.assertEqual(state.final_answer, "done")

    def test_sync_signature_and_public_kernel_export_are_preserved(self):
        self.assertEqual(str(inspect.signature(adapter.run_agent)), str(inspect.signature(baseline.run_agent)))
        self.assertIs(public_iter_agent, iter_agent)


if __name__ == "__main__":
    unittest.main()
