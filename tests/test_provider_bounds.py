from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.agent.cancellation import CancellationToken, Cancelled
from baseagent.agent.state import RunStatus
from baseagent.llm.bounds import DeepSeekContextBound, request_bound_profile
from baseagent.llm.model import Model
from baseagent.llm.reservation import ReservationUnavailable, TokenReservation
from baseagent.main import main
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry


class ProviderBoundTests(unittest.TestCase):
    def profile(self, cap=4096):
        return request_bound_profile("deepseek-context-v1", "deepseek-flash", "https://api.deepseek.com/v1", cap)

    def row(self, **changes):
        return {"id": "deepseek-flash", "name": "DeepSeek-V4.1-Flash", "context_window": 1048576,
                "max_output_tokens": 393216, **changes}

    def adapter(self, *, rows=None, on_metadata=None):
        bound = self.profile()
        adapter = Model("deepseek-flash", "fake-key", "https://api.deepseek.com", request_bound=bound)
        self.assertFalse(adapter.client._client.follow_redirects)
        adapter.client.close()
        self.metadata_calls, self.generation_calls = [], []
        def metadata(**kwargs):
            self.metadata_calls.append(kwargs)
            if on_metadata:
                on_metadata()
            return SimpleNamespace(data=[self.row()] if rows is None else rows)
        def generate(**kwargs):
            self.generation_calls.append(deepcopy(kwargs))
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=8, completion_tokens=2, total_tokens=10),
                                   choices=[SimpleNamespace(message={"content": "done"})])
        adapter.client = SimpleNamespace(models=SimpleNamespace(list=metadata), chat=SimpleNamespace(completions=SimpleNamespace(create=generate)))
        return adapter

    def test_capacity_includes_output_and_digest_binds_tools_and_unicode(self):
        messages = [{"role": "user", "content": "中文😀"}]
        tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
        quote = self.profile().reserve_request(messages, tools)
        self.assertEqual((quote.total_tokens, quote.max_completion_tokens), (1048576, 4096))
        quote.validate(messages, tools)
        with self.assertRaises(ReservationUnavailable):
            quote.validate(messages, [])

    def test_profile_refuses_untrusted_endpoints_aliases_and_invalid_caps(self):
        for endpoint in ["http://api.deepseek.com", "https://proxy.example", "https://api.deepseek.com.evil.example", "https://user:secret@api.deepseek.com", "https://api.deepseek.com/beta", "https://api.deepseek.com?override=1", "https://api.deepseek.com:444", "https://api.deepseek.com/#fragment"]:
            with self.subTest(endpoint=endpoint), self.assertRaises(ReservationUnavailable):
                self.profile().validate_endpoint(endpoint)
        for name in ["deepseek-chat", "deepseek-v4-flash", "other"]:
            with self.assertRaises(ReservationUnavailable):
                DeepSeekContextBound(name)
        for cap in [True, 0, 393217, 1.5]:
            with self.assertRaises(ReservationUnavailable):
                self.profile(cap)

    def test_official_metadata_verified_once_and_output_cap_enforced(self):
        adapter = self.adapter()
        for _ in range(2):
            quote = adapter.reserve_request([{"role": "user", "content": "task"}], [])
            adapter.complete_reserved([{"role": "user", "content": "task"}], [], reservation=quote, cancellation=CancellationToken(), timeout=2)
        self.assertEqual(len(self.metadata_calls), 1)
        self.assertEqual(self.metadata_calls[0]["timeout"], 2)
        self.assertTrue(all(call["max_tokens"] == 4096 and 0 < call["timeout"] <= 2 for call in self.generation_calls))

    def test_missing_changed_or_ambiguous_metadata_never_generates(self):
        for rows in [[], [self.row(context_window=2097152)], [self.row(name="New model")], [self.row(max_output_tokens=None)], [self.row(), self.row()], [self.row(context_window=True)]]:
            adapter = self.adapter(rows=rows)
            quote = adapter.reserve_request([], [])
            with self.assertRaises(ReservationUnavailable):
                adapter.complete_reserved([], [], reservation=quote, cancellation=CancellationToken())
            self.assertEqual(self.generation_calls, [])

    def test_quote_cannot_change_capacity_or_output_cap(self):
        adapter = self.adapter()
        quote = adapter.reserve_request([], [])
        for forged in [TokenReservation(100, 10, quote.input_digest), TokenReservation(1048576, 9000, quote.input_digest)]:
            with self.assertRaises(ReservationUnavailable):
                adapter.complete_reserved([], [], reservation=forged, cancellation=CancellationToken())
        self.assertEqual((self.metadata_calls, self.generation_calls), ([], []))

    def test_cancellation_and_deadline_during_metadata_refuse_generation(self):
        token = CancellationToken()
        adapter = self.adapter(on_metadata=token.cancel)
        with self.assertRaises(Cancelled):
            adapter.complete_reserved([], [], reservation=adapter.reserve_request([], []), cancellation=token)
        self.assertEqual(self.generation_calls, [])
        adapter = self.adapter()
        with patch("baseagent.llm.model.time.monotonic", side_effect=[1, 3]):
            with self.assertRaises(TimeoutError):
                adapter.complete_reserved([], [], reservation=adapter.reserve_request([], []), cancellation=CancellationToken(), timeout=1)
        self.assertEqual(self.generation_calls, [])

    def test_core_releases_capacity_reservation_for_actual_provider_usage(self):
        adapter = self.adapter()
        state = run_agent(adapter, "task", tools=ToolRegistry(), preauthorize_model=True, max_total_tokens=1048576)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual((state.total_tokens, state.unknown_usage_calls, state.model_reservations), (10, 0, {}))
        self.assertEqual(next(event for event in state.events if event["type"] == "model_started")["data"]["reserved_tokens"], 1048576)

    def test_metadata_failure_retains_conservative_unknown_attempt(self):
        adapter = self.adapter(rows=[])
        state = run_agent(adapter, "task", tools=ToolRegistry(), preauthorize_model=True, max_total_tokens=1048576)
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertEqual((state.unknown_usage_calls, state.model_reservations), (1, {"1": 1048576}))
        self.assertEqual(self.generation_calls, [])
        self.assertNotIn("fake-key", state.error)

    def cli(self, arguments):
        with patch.object(sys, "argv", ["baseagent", *arguments]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main()

    def environment(self):
        return patch.dict(os.environ, {"DEEPSEEK_MODEL": "deepseek-flash", "DEEPSEEK_BASE_URL": "https://api.deepseek.com", "DEEPSEEK_API_KEY": "fake-key"})

    def test_cli_small_budget_refuses_before_adapter_or_metadata_network(self):
        with tempfile.TemporaryDirectory() as directory, self.environment(), patch("baseagent.main.load_dotenv"), patch("baseagent.main.Model", side_effect=AssertionError("unexpected adapter")), patch("socket.socket.connect", side_effect=AssertionError("unexpected network")):
            database = Path(directory) / "sessions.sqlite3"
            code = self.cli(["task", "--root", directory, "--db", str(database), "--session", "demo", "--preauthorize-model", "--request-bound-profile", "deepseek-context-v1", "--max-total-tokens", "100"])
            self.assertEqual(code, 1)
            state = SessionStore(database).load("demo")
            self.assertEqual((state.status, state.model_calls, state.unknown_usage_calls), (RunStatus.MAX_TOKENS_EXCEEDED, 0, 0))

    def test_cli_bound_cap_drift_refuses_resume_before_any_request(self):
        with tempfile.TemporaryDirectory() as directory, self.environment(), patch("baseagent.main.load_dotenv"), patch("baseagent.main.Model", side_effect=AssertionError("unexpected adapter")):
            database = Path(directory) / "sessions.sqlite3"
            options = ["--root", directory, "--db", str(database), "--session", "demo", "--preauthorize-model", "--request-bound-profile", "deepseek-context-v1", "--max-total-tokens", "100"]
            self.assertEqual(self.cli(["task", *options]), 1)
            before = SessionStore(database).load("demo").to_dict()
            self.assertEqual(self.cli(["--resume", *options, "--max-completion-tokens", "8192"]), 1)
            self.assertEqual(SessionStore(database).load("demo").to_dict(), before)


if __name__ == "__main__":
    unittest.main()
