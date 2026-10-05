from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus, State
from baseagent.llm.model import Model as Adapter
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.main import main
from baseagent.middleware import AgentMiddleware
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.result import ToolResult


def response(message, tokens=5):
    return ModelResponse(message, TokenUsage(tokens - 1, 1, tokens))


def tool_call():
    return {"id": "a", "type": "function", "function": {"name": "write", "arguments": '{}'}}


class Model:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(deepcopy(messages))
        return next(self.responses)


class UsageEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sessions.db"
        self.store = SessionStore(self.path)
        self.effects = []
        self.tools = ToolRegistry()
        self.tools.register("write", "write", {"type": "object"}, lambda: self.effects.append("write") or "SECRET_TOOL_BODY")

    def run_turn(self, model, prompt=None, **kwargs):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id="demo", **kwargs)

    def test_reported_usage_accumulates_and_resets_only_on_new_turn(self):
        state = self.run_turn(Model([response({"tool_calls": [tool_call()]}, 5), response({"content": "done"}, 7)]), "task", max_total_tokens=20)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual((state.prompt_tokens, state.completion_tokens, state.total_tokens, state.unknown_usage_calls), (10, 2, 12, 0))
        self.assertEqual(self.run_turn(Model()).total_tokens, 12)
        next_turn = self.run_turn(Model([response({"content": "again"}, 3)]), "next")
        self.assertEqual(next_turn.total_tokens, 3)
        self.assertEqual(next_turn.max_total_tokens, 20)

    def test_over_budget_preserves_response_and_resume_does_not_recall_model(self):
        state = self.run_turn(Model([response({"content": "done"}, 12)]), "task", max_total_tokens=10)
        self.assertEqual(state.status, RunStatus.MAX_TOKENS_EXCEEDED)
        self.assertEqual(state.messages[-1]["content"], "done")
        unused = Model()
        state = self.run_turn(unused, max_total_tokens=20)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.final_answer, "done")
        self.assertEqual(unused.seen, [])
        self.assertEqual(state.model_calls, 1)

    def test_unknown_usage_blocks_tools_until_verified_reconciliation(self):
        state = self.run_turn(Model([{"tool_calls": [tool_call()]}]), "task", max_total_tokens=20)
        self.assertEqual(state.status, RunStatus.USAGE_UNAVAILABLE)
        self.assertEqual(state.unknown_usage_calls, 1)
        self.assertEqual(self.effects, [])
        with self.assertRaises(ValueError):
            self.store.resolve_usage("demo", TokenUsage(3, 2, 5), 2)
        self.store.resolve_usage("demo", TokenUsage(3, 2, 5), 1)
        state = self.run_turn(Model([response({"content": "done"}, 5)]))
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.total_tokens, 10)
        self.assertEqual(self.effects, ["write"])

    def test_failed_model_attempt_is_not_assumed_free_after_restart(self):
        state = self.run_turn(Model(), "task", max_total_tokens=20)
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertEqual(state.unknown_usage_calls, 1)
        self.store = SessionStore(self.path)
        unused = Model()
        self.assertEqual(self.run_turn(unused).status, RunStatus.USAGE_UNAVAILABLE)
        self.assertEqual(unused.seen, [])
        self.store.resolve_usage("demo", TokenUsage(0, 0, 0), 1)
        state = self.run_turn(Model([response({"content": "done"})]))
        self.assertEqual(state.model_calls, 2)
        self.assertEqual(state.total_tokens, 5)

    def test_wrapper_retry_accounts_for_every_actual_response(self):
        class Retry(AgentMiddleware):
            def wrap_model_call(self, request, handler):
                handler(request)
                return handler(request)

        state = self.run_turn(Model([response({"content": "first"}, 5), response({"content": "second"}, 7)]), "task", middleware=[Retry()], max_total_tokens=20)
        self.assertEqual(state.total_tokens, 12)
        self.assertEqual(state.model_calls, 2)
        self.assertEqual(state.final_answer, "second")

    def test_exhausted_budget_blocks_retry_without_extra_dispatch(self):
        class Retry(AgentMiddleware):
            def wrap_model_call(self, request, handler):
                handler(request)
                return handler(request)

        model = Model([response({"content": "first"}, 5)])
        state = self.run_turn(model, "task", middleware=[Retry()], max_total_tokens=5)
        self.assertEqual(state.status, RunStatus.MAX_TOKENS_EXCEEDED)
        self.assertEqual(state.model_calls, 1)
        self.assertEqual(len(model.seen), 1)

    def test_cached_model_response_has_no_usage_charge(self):
        class Cached(AgentMiddleware):
            def wrap_model_call(self, request, handler):
                return {"content": "cached"}

        state = self.run_turn(Model(), "task", middleware=[Cached()], max_total_tokens=1)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual((state.model_calls, state.total_tokens, state.unknown_usage_calls), (0, 0, 0))

    def test_exact_token_limit_allows_final_answer(self):
        state = self.run_turn(Model([response({"content": "done"}, 5)]), "task", max_total_tokens=5)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.final_answer, "done")

    def test_deadline_persists_across_resume_and_stops_before_pending_tool(self):
        clock = [100.0]

        class Slow(Model):
            def complete(inner, messages, tools):
                clock[0] = 111.0
                return response({"tool_calls": [tool_call()]})

        with patch("baseagent.agent.agent.time.time", side_effect=lambda: clock[0]):
            state = self.run_turn(Slow(), "task", max_duration_seconds=10)
            self.assertEqual(state.status, RunStatus.DEADLINE_EXCEEDED)
            self.assertEqual(self.effects, [])
            self.assertEqual(self.store.call(state, "a")["status"], "pending")
            self.store = SessionStore(self.path)
            unused = Model()
            self.assertEqual(self.run_turn(unused).status, RunStatus.DEADLINE_EXCEEDED)
            self.assertEqual(unused.seen, [])
            state = self.run_turn(Model([response({"content": "done"})]), max_duration_seconds=20)
            self.assertEqual(state.status, RunStatus.COMPLETED)
            self.assertEqual(state.turn_started_at, 100.0)
            self.assertEqual(self.effects, ["write"])

    def test_remaining_deadline_passed_to_capable_adapter(self):
        class Timed(Model):
            def complete_with_timeout(inner, messages, tools, *, timeout):
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 10)
                return response({"content": "done"})

        state = self.run_turn(Timed(), "task", max_duration_seconds=10)
        self.assertEqual(state.status, RunStatus.COMPLETED)

    def test_events_are_ordered_paged_and_exclude_sensitive_bodies(self):
        self.tools = ToolRegistry()
        self.tools.register("write", "write", {"type": "object", "properties": {"payload": {"type": "string"}}}, lambda payload: "SECRET_TOOL_BODY")
        call = tool_call()
        call["function"]["arguments"] = json.dumps({"payload": "SECRET_ARGUMENT"})
        self.run_turn(Model([response({"tool_calls": [call]}), response({"content": "SECRET_ANSWER"})]), "SECRET_PROMPT")
        values = self.store.events("demo")
        kinds = [item["type"] for item in values]
        for kind in ["run_started", "model_started", "model_returned", "assistant_checkpoint", "tool_started", "tool_attempt", "tool_returned", "tool_completed", "run_stopped"]:
            self.assertIn(kind, kinds)
        self.assertEqual(values[-1]["data"]["status"], "completed")
        self.assertNotIn("SECRET_", json.dumps(values))
        first = self.store.events("demo", limit=3)
        rest = self.store.events("demo", after=first[-1]["sequence"])
        self.assertEqual(first + rest, values)
        self.assertEqual(len(set(value["sequence"] for value in values)), len(values))

    def test_model_hard_exit_retains_unknown_usage_and_start_event(self):
        script = '''
import os, sys
from baseagent.agent import run_agent
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
class Model:
    def complete(self, messages, tools):
        os._exit(17)
run_agent(Model(), "task", tools=ToolRegistry(), store=SessionStore(sys.argv[1]), session_id="demo", max_total_tokens=100)
'''
        process = subprocess.run([sys.executable, "-c", script, str(self.path)], capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 17, process.stderr.decode())
        state = self.store.load("demo")
        self.assertEqual(state.unknown_usage_calls, 1)
        self.assertEqual(self.store.events("demo")[-1]["type"], "model_started")
        state = run_agent(Model(), tools=ToolRegistry(), store=self.store, session_id="demo")
        self.assertEqual(state.status, RunStatus.USAGE_UNAVAILABLE)
        self.assertEqual(state.model_calls, 1)

    def test_event_and_tool_completion_rollback_together(self):
        class Interrupt(AgentMiddleware):
            def after_model(self, state, message):
                raise KeyboardInterrupt

        state = self.run_turn(Model([response({"tool_calls": [tool_call()]})]), "task", middleware=[Interrupt()])
        self.store.start_call(state, "a")
        old_events = self.store.events("demo")
        state.messages.append({"role": "tool", "tool_call_id": "a", "content": "{}"})
        with patch.object(self.store, "_event", side_effect=RuntimeError("event disk error")):
            with self.assertRaises(RuntimeError):
                self.store.complete_call(state, "a", ToolResult(data="written"))
        self.assertEqual(self.store.call(state, "a")["status"], "running")
        self.assertEqual(self.store.load("demo").messages[-1]["role"], "assistant")
        self.assertEqual(self.store.events("demo"), old_events)

    def test_v1_database_migration_preserves_state_and_v2_rejects_future_version(self):
        state = self.run_turn(Model([response({"content": "done"})]), "task")
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("DROP TABLE events")
                connection.execute("PRAGMA user_version=1")
        migrated = SessionStore(self.path)
        self.assertEqual(migrated.load("demo").to_dict(), state.to_dict())
        self.assertEqual(migrated.events("demo"), [])
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            with connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
                connection.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            SessionStore(self.path)

    def test_legacy_usage_does_not_become_zero(self):
        legacy = {"messages": [], "status": "interrupted", "model_calls": 3}
        self.assertEqual(State.from_dict(legacy).unknown_usage_calls, 3)

    def test_usage_and_duration_validation(self):
        for value in [TokenUsage(0, 0, 0), TokenUsage(4, 2, 6)]:
            self.assertGreaterEqual(value.total_tokens, 0)
        for values in [(True, 0, 1), (-1, 2, 1), (4, 2, 5)]:
            with self.assertRaises(ValueError):
                TokenUsage(*values)
        for value in [float("nan"), float("inf"), 0, -1, True]:
            with self.assertRaises(ValueError):
                self.run_turn(Model(), "task", max_duration_seconds=value)

    def test_cli_usage_reconciliation_and_event_query_need_no_model(self):
        self.run_turn(Model([{"content": "done"}]), "task", max_total_tokens=10)
        verified = Path(self.temp.name) / "usage.json"
        verified.write_text(json.dumps({"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5, "unknown_calls": 1}))
        base = ["baseagent", "--session", "demo", "--db", str(self.path)]
        for flags in [["--resolve-usage", str(verified)], ["--events", "--event-limit", "2"]]:
            with patch.object(sys, "argv", base + flags), patch("baseagent.main.Model", side_effect=AssertionError("provider must not initialize")):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(), 0)
        self.assertEqual(self.store.load("demo").total_tokens, 5)

    def test_adapter_preserves_usage_and_request_timeout_without_network(self):
        message = {"role": "assistant", "content": "done"}
        completion = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2, total_tokens=6))
        with patch("baseagent.llm.model.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = completion
            model = Adapter("test-model", "mock-key")
            value = model.complete_with_timeout([{"role": "user", "content": "task"}], timeout=2.5)
            self.assertEqual(value.usage, TokenUsage(4, 2, 6))
            self.assertEqual(value.message, message)
            self.assertEqual(client.return_value.chat.completions.create.call_args.kwargs["timeout"], 2.5)
            self.assertEqual(client.call_args.kwargs["max_retries"], 0)


if __name__ == "__main__":
    unittest.main()
