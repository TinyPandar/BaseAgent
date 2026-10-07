from copy import deepcopy
import json
from pathlib import Path
import tomllib
from types import SimpleNamespace
import unittest

from baseagent.agent.cancellation import CancellationToken as LegacyCancellationToken, Cancelled as LegacyCancelled
from baseagent.agent.context import ContextPolicy as LegacyContextPolicy
from baseagent.kernel import AgentEvent, AgentMiddleware, CancellationToken, Cancelled, KernelState, RunStatus, ToolRegistry, iter_agent
from baseagent.kernel.context import ContextPolicy
from baseagent.kernel.result import ErrorCode, ToolFailure, ToolResult
from baseagent.kernel.tool_context import ToolContext as KernelToolContext
from baseagent.middleware import AgentMiddleware as LegacyMiddleware
from baseagent.middleware.retry import RetryMiddleware
from baseagent.tools.context import ToolContext
from baseagent.tools.registry import ToolRegistry as LegacyToolRegistry
from baseagent.tools.result import ToolResult as LegacyToolResult


def call(name="add", arguments='{"a": 2, "b": 3}', identifier="one"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(deepcopy(messages))
        return next(self.responses)


def prepared(**kwargs):
    return KernelState(messages=[{"role": "system", "content": "system"},
                                 {"role": "user", "content": "task"}], **kwargs)


class KernelExecutionTests(unittest.TestCase):
    def test_model_and_tool_before_events_precede_effects_and_results_live_in_state(self):
        effects = []
        tools = ToolRegistry()
        tools.register("add", "Add", {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                                     "required": ["a", "b"]}, lambda a, b: effects.append((a, b)) or a + b)
        model = Model([{"tool_calls": [call()]}, {"content": "5"}])
        state = prepared()
        iterator = iter_agent(model, state, tools=tools)
        events = []
        for value in iterator:
            events.append(value)
            self.assertIsInstance(value, AgentEvent)
            if value.type == "model_started" and not model.seen:
                self.assertEqual(state.model_calls, 0)
                self.assertEqual(effects, [])
            if value.type == "tool_started":
                self.assertEqual(effects, [])
                self.assertEqual(state.tool_calls, 0)
            if value.type == "tool_returned":
                self.assertEqual(json.loads(state.messages[-1]["content"])["data"], 5)
        self.assertEqual([item.type for item in events], [
            "run_started", "model_started", "model_returned", "assistant_checkpoint",
            "tool_started", "tool_returned", "tool_completed",
            "model_started", "model_returned", "assistant_checkpoint", "run_stopped",
        ])
        self.assertEqual((state.status, state.final_answer, state.step, state.model_calls, state.tool_calls),
                         (RunStatus.COMPLETED, "5", 2, 2, 1))
        self.assertEqual(effects, [(2, 3)])
        self.assertEqual(json.loads(model.seen[1][-1]["content"])["data"], 5)
        self.assertEqual(KernelState.from_dict(json.loads(json.dumps(state.to_dict()))), state)

    def test_invalid_json_never_reaches_tool_and_retains_error_reply(self):
        for arguments in ('{"a": NaN}', '{"a": 1, "a": 2}', '[]', '{broken'):
            with self.subTest(arguments=arguments):
                effects = []
                tools = ToolRegistry()
                tools.register("add", "Add", {"type": "object"}, lambda **values: effects.append(values))
                state = prepared()
                list(iter_agent(Model([{"tool_calls": [call(arguments=arguments)]}, {"content": "done"}]), state, tools=tools))
                result = ToolResult.from_dict(json.loads(state.messages[3]["content"]))
                self.assertEqual(result.error.code, ErrorCode.INVALID_ARGUMENTS)
                self.assertEqual(effects, [])

    def test_pending_batch_is_dispatched_at_step_limit_without_replaying_replies(self):
        tools = ToolRegistry()
        effects = []
        tools.register("record", "Record", {"type": "object"}, lambda: effects.append("two") or "done")
        state = prepared(step=1, max_steps=1)
        state.messages.extend([
            {"role": "assistant", "tool_calls": [call("record", "{}", "one"), call("record", "{}", "two")]},
            {"role": "tool", "tool_call_id": "one", "content": json.dumps(ToolResult(data="already done").to_dict())},
        ])
        model = Model([])
        list(iter_agent(model, state, tools=tools))
        self.assertEqual(effects, ["two"])
        self.assertEqual(model.seen, [])
        self.assertEqual(state.status, RunStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(state.tool_calls, 1)

    def test_limits_stop_before_extra_dispatch(self):
        for limits, expected, counts in (
            ({"max_steps": 1}, RunStatus.MAX_STEPS_EXCEEDED, (1, 1)),
            ({"max_model_calls": 1}, RunStatus.MAX_MODEL_CALLS_EXCEEDED, (1, 1)),
            ({"max_tool_calls": 0}, RunStatus.MAX_TOOL_CALLS_EXCEEDED, (1, 0)),
        ):
            with self.subTest(limits=limits):
                state = prepared(**limits)
                model = Model([{"tool_calls": [call("missing", "{}")]}, {"content": "never"}])
                events = list(iter_agent(model, state, tools=ToolRegistry()))
                self.assertEqual(state.status, expected)
                self.assertEqual((state.model_calls, state.tool_calls), counts)
                self.assertEqual(len(model.seen), 1)
                self.assertEqual(events[-1].type, "run_stopped")

    def test_pre_cancelled_state_never_dispatches(self):
        token = CancellationToken()
        token.cancel()
        state, model = prepared(), Model([{"content": "never"}])
        events = list(iter_agent(model, state, tools=ToolRegistry(), cancellation=token))
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(model.seen, [])
        self.assertEqual([value.type for value in events], ["run_started", "run_stopped"])

    def test_cancellation_at_tool_before_event_prevents_handler(self):
        token, effects, tools = CancellationToken(), [], ToolRegistry()
        tools.register("record", "Record", {"type": "object"}, lambda: effects.append("effect"))
        state = prepared()
        for value in iter_agent(Model([{"tool_calls": [call("record", "{}")]}]), state, tools=tools, cancellation=token):
            if value.type == "tool_started":
                token.cancel()
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(effects, [])
        self.assertEqual(state.tool_calls, 0)

    def test_hook_order_and_cached_model_do_not_charge_dispatch(self):
        observed = []

        class Hook(AgentMiddleware):
            def before_agent(self, state):
                observed.append("before_agent")

            def before_model(self, state):
                observed.append("before_model")

            def wrap_model_call(self, request, handler):
                observed.append("wrapped")
                return {"content": "cached"}

            def after_model(self, state, message):
                observed.append("after_model")

            def after_agent(self, state):
                observed.append("after_agent")

        state, model = prepared(), Model([])
        list(iter_agent(model, state, tools=ToolRegistry(), hooks=[Hook()]))
        self.assertEqual(state.final_answer, "cached")
        self.assertEqual(state.model_calls, 0)
        self.assertEqual(model.seen, [])
        self.assertEqual(observed, ["before_agent", "before_model", "wrapped", "after_model", "after_agent"])

    def test_wrapper_retries_charge_actual_calls_and_obey_budget(self):
        class Twice(AgentMiddleware):
            def wrap_model_call(self, request, handler):
                handler(request)
                return handler(request)

        for limit, status, count in ((2, RunStatus.COMPLETED, 2), (1, RunStatus.MAX_MODEL_CALLS_EXCEEDED, 1)):
            with self.subTest(limit=limit):
                state = prepared(max_model_calls=limit)
                model = Model([{"content": "first"}, {"content": "second"}])
                list(iter_agent(model, state, tools=ToolRegistry(), hooks=[Twice()]))
                self.assertEqual((state.status, state.model_calls, len(model.seen)), (status, count, count))

    def test_concrete_response_envelope_needs_no_client_type_import(self):
        state = prepared()
        list(iter_agent(Model([SimpleNamespace(message={"content": "done"}, usage=None)]), state, tools=ToolRegistry()))
        self.assertEqual(state.final_answer, "done")

    def test_context_limit_stops_before_model_and_preserves_original_messages(self):
        state = prepared(max_context_bytes=256)
        state.messages[-1]["content"] = "long" * 200
        before = deepcopy(state.messages)
        model = Model([])
        list(iter_agent(model, state, tools=ToolRegistry()))
        self.assertEqual(state.status, RunStatus.CONTEXT_LIMIT_EXCEEDED)
        self.assertEqual(state.messages, before)
        self.assertEqual(model.seen, [])

    def test_reused_tool_id_is_rejected_without_another_effect(self):
        state = prepared()
        list(iter_agent(Model([{"tool_calls": [call("missing", "{}")]},
                              {"tool_calls": [call("missing", "{}")]}]), state, tools=ToolRegistry()))
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertIn("reused a tool call ID", state.error)
        self.assertEqual(state.tool_calls, 1)

    def test_tool_cancellation_is_not_normalized_as_an_ordinary_failure(self):
        def cancel():
            raise Cancelled()

        tools, state = ToolRegistry(), prepared()
        tools.register("cancel", "Cancel", {"type": "object"}, cancel)
        list(iter_agent(Model([{"tool_calls": [call("cancel", "{}")]}]), state, tools=tools))
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(state.messages[-1]["role"], "assistant")

    def test_existing_tool_retry_hook_retries_only_declared_safe_dispatches(self):
        for retry_safe, expected_calls in ((True, 2), (False, 1)):
            with self.subTest(retry_safe=retry_safe):
                effects, tools, state = [], ToolRegistry(), prepared()

                def flaky():
                    effects.append("call")
                    if len(effects) == 1:
                        raise ToolFailure(ErrorCode.TIMEOUT, "retry", retryable=True)
                    return "done"

                tools.register("flaky", "Flaky", {"type": "object"}, flaky, retry_safe=retry_safe)
                list(iter_agent(Model([{"tool_calls": [call("flaky", "{}")]}, {"content": "done"}]), state,
                                tools=tools, hooks=[RetryMiddleware(tool_retries=1, delay=0)]))
                self.assertEqual(state.status, RunStatus.COMPLETED)
                self.assertEqual((len(effects), state.tool_calls), (expected_calls, expected_calls))
                self.assertEqual(state.unsafe_tool_calls, 0 if retry_safe else 1)

    def test_failed_model_still_runs_cleanup_and_yields_stop(self):
        cleaned = []

        class Hook(AgentMiddleware):
            def after_agent(self, state):
                cleaned.append(state.status)

        state = prepared()
        events = list(iter_agent(Model([]), state, tools=ToolRegistry(), hooks=[Hook()]))
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertEqual(cleaned, [RunStatus.FAILED])
        self.assertEqual([value.type for value in events], ["run_started", "model_started", "model_failed", "run_stopped"])


class KernelMigrationTests(unittest.TestCase):
    def test_old_imports_reexport_the_same_types(self):
        for old, new in ((LegacyCancellationToken, CancellationToken), (LegacyCancelled, Cancelled),
                         (LegacyContextPolicy, ContextPolicy), (LegacyMiddleware, AgentMiddleware),
                         (LegacyToolRegistry, ToolRegistry), (LegacyToolResult, ToolResult)):
            self.assertIs(old, new)

    def test_extended_tool_context_stays_outside_kernel_with_original_field_order(self):
        self.assertEqual(list(KernelToolContext.__dataclass_fields__), ["state", "checkpoint", "cancellation"])
        self.assertEqual(list(ToolContext.__dataclass_fields__), [
            "state", "checkpoint", "cancellation", "store", "remaining_seconds", "call_id",
            "execution_scope", "tools", "policy", "model", "middleware", "accept_config_changes",
        ])
        marker = object()
        context = ToolContext(prepared(), lambda *args: None, None, marker)
        self.assertIs(context.store, marker)
        self.assertIsInstance(context, KernelToolContext)
        tools = ToolRegistry()
        tools.register("context", "Context", {"type": "object"}, lambda context: context.store is marker, contextual=True)
        self.assertTrue(tools.execute("context", {}, context=context).data)

    def test_full_schema_validation_including_refs_and_combinators_is_retained(self):
        effects, tools = [], ToolRegistry()
        schema = {"type": "object", "$defs": {"positive": {"type": "integer", "minimum": 1}},
                  "properties": {"value": {"anyOf": [{"$ref": "#/$defs/positive"}, {"enum": ["auto"]}]}},
                  "required": ["value"]}
        tools.register("record", "Record", schema, lambda value: effects.append(value))
        for value in (0, False, "invalid", None):
            self.assertEqual(tools.execute("record", {"value": value}).error.code, ErrorCode.INVALID_ARGUMENTS)
        self.assertEqual(effects, [])
        self.assertTrue(tools.execute("record", {"value": 1}).ok)
        self.assertTrue(tools.execute("record", {"value": "auto"}).ok)
        self.assertEqual(effects, [1, "auto"])

    def test_retryable_tool_failure_and_subset_preserve_result_contract(self):
        def fail():
            raise ToolFailure(ErrorCode.TIMEOUT, "retry", retryable=True)

        tools = ToolRegistry()
        tools.register("fail", "Fail", {"type": "object"}, fail, retry_safe=True)
        subset = tools.subset(["fail"])
        self.assertTrue(subset.retry_safe("fail"))
        result = subset.execute("fail", {})
        self.assertEqual(result.error.code, ErrorCode.TIMEOUT)
        self.assertTrue(result.error.retryable)

    def test_jsonschema_version_range_is_explicit_in_dependency_declaration(self):
        configuration = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual([value for value in configuration["project"]["dependencies"] if value.startswith("jsonschema")],
                         ["jsonschema>=4.23,<5"])


if __name__ == "__main__":
    unittest.main()
