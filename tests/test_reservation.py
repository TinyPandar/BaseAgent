from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.agent.cancellation import CancellationToken
from baseagent.agent.state import RunStatus
from baseagent.llm.model import Model
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.llm.reservation import TokenReservation, request_digest
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry


class ReservedModel:
    def __init__(self, messages=(), bound=10, usage=5):
        self.messages = iter(messages)
        self.bound, self.usage = bound, usage
        self.calls = 0

    def reserve_request(self, messages, tools):
        return TokenReservation(self.bound, min(8, self.bound), request_digest(messages, tools))

    def complete_reserved(self, messages, tools, *, reservation, cancellation, timeout):
        self.calls += 1
        message = next(self.messages)
        if isinstance(message, BaseException):
            raise message
        return ModelResponse(message, TokenUsage(2, self.usage - 2, self.usage) if self.usage else None)


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.sqlite3")
        self.tools = ToolRegistry()

    def run_turn(self, model, prompt=None, **kwargs):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id="demo", **kwargs)

    def test_insufficient_budget_refuses_before_dispatch(self):
        model = ReservedModel()
        state = self.run_turn(model, "task", preauthorize_model=True, max_total_tokens=9)
        self.assertEqual(state.status, RunStatus.MAX_TOKENS_EXCEEDED)
        self.assertEqual(model.calls, 0)
        self.assertEqual(state.model_calls, 0)
        self.assertEqual(state.unknown_usage_calls, 0)

    def test_deadline_expiring_during_quote_does_not_record_a_request(self):
        clock = [1000.0]

        class SlowQuote(ReservedModel):
            def reserve_request(inner, messages, tools):
                clock[0] += 2
                return super().reserve_request(messages, tools)

        model = SlowQuote()
        with patch("baseagent.agent.agent.time.time", side_effect=lambda: clock[0]):
            state = self.run_turn(model, "task", preauthorize_model=True, max_total_tokens=20, max_duration_seconds=1)
        self.assertEqual(state.status, RunStatus.DEADLINE_EXCEEDED)
        self.assertEqual(model.calls, 0)
        self.assertEqual(state.model_calls, 0)
        self.assertEqual(state.unknown_usage_calls, 0)
        self.assertEqual(self.store.load("demo").model_reservations, {})

    def test_cancellation_during_quote_does_not_record_a_request(self):
        cancellation = CancellationToken()

        class CancellingQuote(ReservedModel):
            def reserve_request(inner, messages, tools):
                cancellation.cancel()
                return super().reserve_request(messages, tools)

        model = CancellingQuote()
        state = self.run_turn(model, "task", preauthorize_model=True, max_total_tokens=20, cancellation=cancellation)
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(model.calls, 0)
        self.assertEqual(state.model_calls, 0)
        self.assertEqual(state.unknown_usage_calls, 0)

    def test_output_cap_violation_blocks_completion_even_within_total_bound(self):
        class LowCap(ReservedModel):
            def reserve_request(inner, messages, tools):
                return TokenReservation(10, 2, request_digest(messages, tools))

        state = self.run_turn(LowCap([{"content": "done"}], usage=5), "task", preauthorize_model=True, max_total_tokens=20)
        self.assertEqual(state.status, RunStatus.REQUEST_BOUND_VIOLATED)
        self.assertEqual(state.total_tokens, 5)
        self.assertIsNone(state.final_answer)
        self.assertEqual(state.messages[-1]["content"], "done")
        violation = self.store.load("demo").metadata["request_bound_violation"]
        self.assertEqual(violation["max_completion_tokens"], 2)
        self.assertEqual(violation["reported_completion_tokens"], 3)
        self.assertEqual(self.run_turn(ReservedModel()).status, RunStatus.REQUEST_BOUND_VIOLATED)

    def test_known_usage_releases_unused_reservation(self):
        model = ReservedModel([{"content": "done"}])
        state = self.run_turn(model, "task", preauthorize_model=True, max_total_tokens=10)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.total_tokens, 5)
        self.assertEqual(state.model_reservations, {})
        events = self.store.events("demo")
        started = next(item for item in events if item["type"] == "model_started")
        self.assertEqual(started["data"]["reserved_tokens"], 10)

    def test_unknown_attempt_retains_reservation_until_reconciled(self):
        state = self.run_turn(ReservedModel([ConnectionError("failed")]), "task", preauthorize_model=True, max_total_tokens=20)
        self.assertEqual(state.model_reservations, {"1": 10})
        self.assertEqual(self.run_turn(ReservedModel()).status, RunStatus.USAGE_UNAVAILABLE)
        self.store.resolve_usage("demo", TokenUsage(2, 3, 5), 1)
        self.assertEqual(self.store.load("demo").model_reservations, {})
        state = self.run_turn(ReservedModel([{"content": "done"}]))
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.total_tokens, 10)

    def test_reservation_is_durable_before_dispatch_and_missing_usage_blocks_tools(self):
        effects = []
        self.tools.register("effect", "effect", {"type": "object"}, lambda: effects.append(True))
        call = {"id": "a", "type": "function", "function": {"name": "effect", "arguments": "{}"}}
        store = self.store

        class Inspecting(ReservedModel):
            def complete_reserved(inner, messages, tools, **kwargs):
                saved = store.load("demo")
                self.assertEqual(saved.model_reservations, {"1": 10})
                self.assertEqual(saved.unknown_usage_calls, 1)
                return super().complete_reserved(messages, tools, **kwargs)

        model = Inspecting([{"tool_calls": [call]}], usage=0)
        state = self.run_turn(model, "task", preauthorize_model=True, max_total_tokens=20)
        self.assertEqual(state.status, RunStatus.USAGE_UNAVAILABLE)
        self.assertEqual(self.store.load("demo").model_reservations, {"1": 10})
        self.assertEqual(effects, [])
        self.assertEqual(self.store.call(state, "a")["status"], "pending")

    def test_second_request_admission_uses_actual_first_usage(self):
        self.tools.register("read", "read", {"type": "object"}, lambda: "ok")
        call = {"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        model = ReservedModel([{"tool_calls": [call]}, {"content": "done"}])
        state = self.run_turn(model, "task", preauthorize_model=True, max_total_tokens=14)
        self.assertEqual(state.status, RunStatus.MAX_TOKENS_EXCEEDED)
        self.assertEqual(model.calls, 1)
        self.assertEqual(state.total_tokens, 5)
        self.assertEqual(state.model_reservations, {})
        resumed = ReservedModel([{"content": "done"}])
        state = self.run_turn(resumed, max_total_tokens=15)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(resumed.calls, 1)
        self.assertEqual(state.total_tokens, 10)
        self.assertEqual(state.tool_calls, 1)

    def test_bound_violation_preserves_response_and_blocks_tools(self):
        effects = []
        self.tools.register("effect", "effect", {"type": "object"}, lambda: effects.append(True))
        call = {"id": "a", "type": "function", "function": {"name": "effect", "arguments": "{}"}}
        state = self.run_turn(ReservedModel([{"tool_calls": [call]}], usage=11), "task", preauthorize_model=True, max_total_tokens=30)
        self.assertEqual(state.status, RunStatus.REQUEST_BOUND_VIOLATED)
        self.assertEqual(state.total_tokens, 11)
        self.assertEqual(self.store.call(state, "a")["status"], "pending")
        self.assertEqual(effects, [])
        self.assertEqual(self.run_turn(ReservedModel()).status, RunStatus.REQUEST_BOUND_VIOLATED)
        state = self.run_turn(ReservedModel([{"content": "done"}]), accept_request_bound_violation=True)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(effects, [True])
        self.assertEqual(state.total_tokens, 16)

    def test_reconciled_usage_can_detect_reservation_violation(self):
        self.run_turn(ReservedModel([ConnectionError()]), "task", preauthorize_model=True, max_total_tokens=30)
        self.store.resolve_usage("demo", TokenUsage(2, 9, 11), 1)
        self.assertEqual(self.run_turn(ReservedModel()).status, RunStatus.REQUEST_BOUND_VIOLATED)

    def test_invalid_quote_digest_refuses_dispatch(self):
        class Bad(ReservedModel):
            def reserve_request(self, messages, tools):
                return TokenReservation(10, 2, "wrong")
        model = Bad()
        state = self.run_turn(model, "task", preauthorize_model=True, max_total_tokens=20)
        self.assertEqual(state.status, RunStatus.RESERVATION_UNAVAILABLE)
        self.assertEqual(model.calls, 0)

    def test_unsupported_adapter_and_missing_total_budget(self):
        with self.assertRaises(ValueError):
            self.run_turn(ReservedModel(), "task", preauthorize_model=True)
        class Unsupported:
            def complete(self, messages, tools):
                raise AssertionError("must not dispatch")
        self.assertEqual(self.run_turn(Unsupported(), "task", preauthorize_model=True, max_total_tokens=20).status, RunStatus.RESERVATION_UNAVAILABLE)

    def test_adapter_enforces_output_cap_with_no_network(self):
        adapter = Model("model", "fake-key", token_counter=lambda messages, tools: 3, max_completion_tokens=7)
        calls = []
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1, total_tokens=4), choices=[SimpleNamespace(message={"content": "done"})])
        adapter.client.close()
        adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: calls.append(deepcopy(kwargs)) or response)))
        quote = adapter.reserve_request([{"role": "user", "content": "task"}], [])
        self.assertEqual(quote.total_tokens, 10)
        adapter.complete_reserved([{"role": "user", "content": "task"}], [], reservation=quote, cancellation=CancellationToken(), timeout=1)
        self.assertEqual(calls[0]["max_tokens"], 7)
        self.assertEqual(calls[0]["timeout"], 1)

    def test_cli_without_trusted_counter_refuses_without_provider_credentials(self):
        result = subprocess.run([sys.executable, "-m", "baseagent", "task", "--db", str(self.store.path), "--session", "cli", "--preauthorize-model", "--max-total-tokens", "100"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        state = self.store.load("cli")
        self.assertEqual(state.status, RunStatus.RESERVATION_UNAVAILABLE)
        self.assertEqual(state.model_calls, 0)
        self.assertEqual(state.unknown_usage_calls, 0)


if __name__ == "__main__":
    unittest.main()
