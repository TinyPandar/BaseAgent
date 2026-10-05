from dataclasses import replace
import unittest

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.middleware import AgentMiddleware
from baseagent.middleware.retry import RetryMiddleware
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.result import ErrorCode, ToolResult


class Model:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("transient")
        return ModelResponse({"content": "done"}, TokenUsage(2, 1, 3))


def call():
    return {"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}}


class ToolModel:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return ModelResponse({"tool_calls": [call()]} if self.calls == 1 else {"content": "done"}, TokenUsage(2, 1, 3))


class RetryTests(unittest.TestCase):
    def test_transient_model_retry_is_bounded_and_accounted(self):
        model = Model()
        state = run_agent(model, "task", tools=ToolRegistry(), middleware=[RetryMiddleware(model_retries=1, delay=0)])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual((state.model_calls, state.total_tokens, state.unknown_usage_calls), (2, 3, 1))

    def test_token_budget_prevents_retry_with_unverified_usage(self):
        model = Model()
        state = run_agent(model, "task", tools=ToolRegistry(), max_total_tokens=100, middleware=[RetryMiddleware(model_retries=2, delay=0)])
        self.assertEqual(state.status, RunStatus.USAGE_UNAVAILABLE)
        self.assertEqual(model.calls, 1)

    def test_authentication_error_is_not_retried(self):
        class AuthenticationError(Exception):
            status_code = 401

        class Unauthorized(Model):
            def complete(self, messages, tools):
                self.calls += 1
                raise AuthenticationError("denied")

        model = Unauthorized()
        state = run_agent(model, "task", tools=ToolRegistry(), middleware=[RetryMiddleware(model_retries=4, delay=0)])
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertEqual(model.calls, 1)

    def test_declared_safe_tool_retries_explicit_transient_failure(self):
        attempts = []

        def handler():
            attempts.append("read")
            return ToolResult.failure(ErrorCode.TIMEOUT, "transient", retryable=True) if len(attempts) == 1 else "done"

        registry = ToolRegistry()
        registry.register("read", "read", {"type": "object"}, handler, retry_safe=True)
        state = run_agent(ToolModel(), "task", tools=registry, middleware=[RetryMiddleware(tool_retries=1, delay=0)])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(attempts, ["read", "read"])
        self.assertEqual(state.tool_calls, 2)
        self.assertEqual(state.unsafe_tool_calls, 0)

    def test_unsafe_or_nonretryable_tool_failure_is_not_retried(self):
        for declared_safe, retryable in [(False, True), (True, False)]:
            with self.subTest(declared_safe=declared_safe, retryable=retryable):
                attempts = []

                def handler():
                    attempts.append("call")
                    return ToolResult.failure(ErrorCode.EXECUTION_FAILED, "failure", retryable=retryable)

                registry = ToolRegistry()
                registry.register("read", "read", {"type": "object"}, handler, retry_safe=declared_safe)
                state = run_agent(ToolModel(), "task", tools=registry, middleware=[RetryMiddleware(tool_retries=4, delay=0)])
                self.assertEqual(state.status, RunStatus.COMPLETED)
                self.assertEqual(attempts, ["call"])

    def test_inner_redirect_to_unsafe_write_cannot_be_retried(self):
        class Redirect(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                return handler(replace(request, name="write"))

        effects = []
        registry = ToolRegistry()
        registry.register("read", "read", {"type": "object"}, lambda: "read", retry_safe=True)
        registry.register("write", "write", {"type": "object"}, lambda: effects.append("write") or ToolResult.failure(ErrorCode.TIMEOUT, "uncertain", retryable=True))
        state = run_agent(ToolModel(), "task", tools=registry, middleware=[RetryMiddleware(tool_retries=4, delay=0), Redirect()])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(effects, ["write"])
        self.assertEqual(state.unsafe_tool_calls, 1)

    def test_tool_retry_cannot_exceed_actual_dispatch_budget(self):
        registry = ToolRegistry()
        registry.register("read", "read", {"type": "object"}, lambda: ToolResult.failure(ErrorCode.TIMEOUT, "transient", retryable=True), retry_safe=True)
        state = run_agent(ToolModel(), "task", tools=registry, max_tool_calls=1, middleware=[RetryMiddleware(tool_retries=4, delay=0)])
        self.assertEqual(state.status, RunStatus.MAX_TOOL_CALLS_EXCEEDED)
        self.assertEqual(state.tool_calls, 1)

    def test_invalid_tool_error_envelope_cannot_opt_into_retry(self):
        registry = ToolRegistry()
        registry.register("read", "read", {"type": "object"}, lambda: ToolResult.failure("invalid-code", "failure", retryable=True), retry_safe=True)
        state = run_agent(ToolModel(), "task", tools=registry, middleware=[RetryMiddleware(tool_retries=4, delay=0)])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.tool_calls, 1)
        self.assertIn("invalid_result", state.messages[-2]["content"])


if __name__ == "__main__":
    unittest.main()
