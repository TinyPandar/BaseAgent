"""Regression proofs for the measured issues (README 施工段 AC 1, 2, 4).

Most of these were fixed in cdcf54a; the tests here pin them down for every entry point (one-shot,
single-agent chat, main agent / worker, TUI and Web) and cover the parts that were missing. Offline:
scripted models, fake clocks, no provider.
"""

import io
import json
import contextlib
import unittest
from unittest.mock import patch

from chat_helpers import HAS_TEXTUAL, HAS_WEB, NO_TEXTUAL, NO_WEB, ChatTestCase, wait_until
from test_cli_chat import ChatTestCase as CliCase
from test_cli_chat import Model, ping_registry, tool_call
from test_main_agent import Duplex, delegate
from test_model_middleware import Clock

from baseagent.agent.router import ANSWER, MAIN_AGENT_PROMPT, route
from baseagent.kernel import KernelState, ModelRequest
from baseagent.kernel.errors import (AuthError, BadRequestError, ModelTimeoutError, RateLimitError,
                                     TransientError)
from baseagent.llm._common import http_error
from baseagent.main import DEFAULT_MODEL_RETRIES, SYSTEM_PROMPT, main
from baseagent.middleware import RetryMiddleware
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry

GREETING_RULE = ("Answer greetings, small talk and general questions that do not depend on earlier "
                 "work directly, without calling get_task_state, recall_memory or read_history.")
STATE_TOOLS = ("get_task_state", "recall_memory", "read_history")


def flat(text):
    return " ".join(text.split())


class GreetingTests(CliCase):
    """AC 1: the greeting rule is in every prompt that has the state tools, and the tools stay available."""

    def test_one_shot_prompt_has_the_rule_and_the_tools(self):
        model = Model([{"content": "hello"}])
        code, _, _ = self.invoke(self.chat_args("hi"), model, AssertionError("no input"))
        self.assertEqual(code, 0)
        self.assertIn(GREETING_RULE, flat(model.seen[0][0]["content"]))
        for name in STATE_TOOLS:
            self.assertIn(name, model.tools[0])

    def test_single_agent_chat_keeps_the_tools_for_questions_about_earlier_work(self):
        model = Model([{"content": "hello"},
                       {"tool_calls": [tool_call("t1", "get_task_state", {})]}, {"content": "we were at step 2"}])
        code, stdout, _ = self.invoke(self.chat_args("--no-main-agent"), model, ["hi", "上次做到哪了", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn(GREETING_RULE, flat(model.seen[0][0]["content"]))
        self.assertIn("we were at step 2", stdout)
        for name in STATE_TOOLS:
            self.assertIn(name, model.tools[1])

    def test_rule_is_prompt_guidance_not_a_keyword_list_or_disabled_package(self):
        self.assertIn(GREETING_RULE, flat(SYSTEM_PROMPT))
        self.assertIn("Use those tools only when the user refers to earlier tasks", flat(SYSTEM_PROMPT))


class MainAgentGreetingTests(ChatTestCase):
    def test_main_agent_answers_hi_without_a_worker_and_workers_get_the_rule(self):
        self.assertEqual(route("hi")["action"], ANSWER)
        self.assertIn("Greetings, small talk, general knowledge and clarifications: answer directly. Do not delegate.",
                      MAIN_AGENT_PROMPT)
        model = Duplex(main=[{"content": "你好！"}, delegate("d1", "read README.md"), {"content": "已派出 w1。"}],
                       worker=[{"content": "README.md:1-1 read"}])
        code, stdout, _ = self.invoke(["--session", "demo"], model, ["hi", "读 README", "/wait", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("你好！", stdout)
        # The greeting turn dispatched no worker; the main agent never has the state tools at all.
        first_turn = model.main_seen[0]
        self.assertEqual([m["content"] for m in first_turn if m["role"] == "user"], ["hi"])
        self.assertTrue(all(not set(STATE_TOOLS) & set(names) for names in model.main_tools))
        self.assertEqual(len(model.worker_seen), 1)
        # The worker's prompt carries the same rule and its tool list keeps the state tools.
        self.assertIn(GREETING_RULE, flat(model.worker_seen[0][0]["content"]))
        self.assertIn("get_task_state", model.worker_tools[0])


class ListPreviewTests(ChatTestCase):
    """AC 2: the TUI and Web get the same counts as the CLI trace through the tool_result event."""

    def listing_session(self, ui):
        for index in range(15):
            (self.root / f"file{index:02}.txt").write_text("x", encoding="utf-8")
        model = Model([{"tool_calls": [tool_call("g1", "glob", {"pattern": "*.txt", "max_results": 12})]},
                       {"tool_calls": [tool_call("g2", "glob", {"pattern": "file0*.txt"})]},
                       {"content": "listed"}])
        session = self.session(["--session", "demo", "--no-main-agent"], model, ui=ui)
        session.open()
        self.assertEqual(session.submit("list files"), "sent")
        wait_until(lambda: not session.busy)
        return session

    def check(self, previews):
        self.assertTrue(previews[0].startswith("12 items (tool stopped at max_results=12; more may exist); "
                                               "preview shows first 10, 2 more not shown"), previews[0])
        self.assertIn("\n  file09.txt", previews[0])
        self.assertNotIn("file10.txt", previews[0])
        self.assertTrue(previews[1].startswith("10 items (tool returned the complete result); preview shows all"))

    def test_tool_result_event_has_counts_for_tui_and_web(self):
        for ui in ("tui", "web"):
            with self.subTest(ui=ui):
                session = self.listing_session(ui)
                self.check([item["preview"] for item in self.events(session, "tool_result")])

    @unittest.skipUnless(HAS_TEXTUAL, NO_TEXTUAL)
    def test_tui_transcript_shows_the_whole_count_line(self):
        import asyncio
        from textual.widgets import Input
        from baseagent.tui.app import ChatApp
        for index in range(15):
            (self.root / f"file{index:02}.txt").write_text("x", encoding="utf-8")
        model = Model([{"tool_calls": [tool_call("g1", "glob", {"pattern": "*.txt", "max_results": 12})]},
                       {"content": "listed"}])
        session = self.session(["--session", "demo", "--no-main-agent"], model, ui="tui")

        async def drive():
            app = ChatApp(session, poll=0.02)
            async with app.run_test(size=(140, 40)) as pilot:
                field = app.query_one("#input", Input)
                field.focus()
                field.value = "list files"
                await pilot.press("enter")
                for _ in range(500):
                    if any(kind == "assistant" and text == "listed" for kind, text in app.entries):
                        break
                    await pilot.pause(0.02)
                return [text for kind, text in app.entries if kind == "tool"]

        lines = asyncio.run(drive())
        self.assertTrue(any("12 items (tool stopped at max_results=12; more may exist); preview shows first 10, "
                            "2 more not shown" in text and "\n  file09.txt" in text for text in lines), lines)

    @unittest.skipUnless(HAS_WEB, NO_WEB)
    def test_web_stream_carries_the_counts(self):
        from fastapi.testclient import TestClient
        from baseagent.web.server import create_app
        from test_web import parse_sse
        session = self.listing_session("web")
        client = TestClient(create_app(session, token=None, host="127.0.0.1"), base_url="http://127.0.0.1:8765")
        events = [event for _, event in parse_sse(client.get("/events", params={"wait": 0, "after": 0}).text)]
        snapshot_or_events = json.dumps(events, ensure_ascii=False)
        self.assertIn("2 more not shown", snapshot_or_events)


class RetryDefaultsTests(CliCase):
    """AC 4: default retries for every entry, the wait sequence, and no repeated tool execution."""

    def test_default_is_two_retries_and_range_is_zero_to_four(self):
        self.assertEqual(DEFAULT_MODEL_RETRIES, 2)
        for value in ("-1", "5"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                main(self.chat_args("task", "--model-retries", value))

    def test_single_agent_chat_retries_timeouts_by_default(self):
        model = Model([ModelTimeoutError("t"), ModelTimeoutError("t"), {"content": "recovered"}])
        code, stdout, _ = self.invoke(self.chat_args("--session", "demo", "--no-main-agent"), model, ["go", "/exit"],
                                      capability_factory=ping_registry)
        self.assertEqual(code, 0)
        self.assertIn("recovered", stdout)
        self.assertEqual(SessionStore(self.db).load("demo").model_calls, 3)

    def test_third_failure_is_not_retried_by_default(self):
        model = Model([ModelTimeoutError("t")] * 3 + [{"content": "unused"}])
        code, _, _ = self.invoke(self.chat_args("task", "--session", "demo"), model, AssertionError("no input"),
                                 capability_factory=ping_registry)
        self.assertEqual(code, 1)
        self.assertEqual(len(model.seen), 3)  # 1 request + 2 retries
        self.assertEqual(SessionStore(self.db).load("demo").model_calls, 3)  # every retry counts as a model call

    def test_model_retry_never_re_executes_a_tool_that_already_ran(self):
        executed = []

        def registry(*args):
            tools = ToolRegistry()
            # Not retry_safe: a side-effect tool.
            tools.register("touch", "side effect", {"type": "object", "properties": {}},
                           lambda: executed.append(1) or "done")
            return tools

        model = Model([{"tool_calls": [tool_call("t1", "touch", {})]}, ModelTimeoutError("t"),
                       ModelTimeoutError("t"), {"content": "ok"}])
        code, _, _ = self.invoke(self.chat_args("task", "--session", "demo"), model, AssertionError("no input"),
                                 capability_factory=registry)
        self.assertEqual(code, 0)
        self.assertEqual(executed, [1])
        state = SessionStore(self.db).load("demo")
        self.assertEqual((state.model_calls, state.tool_calls), (4, 1))

    def test_composition_uses_tool_retries_zero(self):
        seen = []
        original = RetryMiddleware.__init__

        def spy(self, **kwargs):
            seen.append(kwargs)
            original(self, **kwargs)

        with patch.object(RetryMiddleware, "__init__", spy):
            self.invoke(self.chat_args("task"), Model([{"content": "x"}]), AssertionError("no input"),
                        capability_factory=ping_registry)
        self.assertEqual(seen, [{"model_retries": 2, "tool_retries": 0}])


class WorkerRetryTests(ChatTestCase):
    def test_worker_retries_timeouts_by_default(self):
        model = Duplex(main=[delegate("d1", "read README.md"), {"content": "已派出 w1。"}],
                       worker=[ModelTimeoutError("t"), TransientError("connection failed"), {"content": "README.md:1-1"}])
        code, _, _ = self.invoke(["--session", "demo"], model, ["读 README", "/wait", "/exit"])
        self.assertEqual(code, 0)
        worker = self.worker()
        self.assertEqual(worker.status, "completed")
        self.assertEqual(worker.model_calls, 3)


class RetryPolicyTests(unittest.TestCase):
    def request(self, remaining=None):
        return ModelRequest(KernelState(), [], [], remaining_seconds=remaining) if remaining else ModelRequest(KernelState(), [], [])

    def attempts(self, retry, errors, request=None):
        calls = []

        def handler(_):
            calls.append(1)
            if len(calls) <= len(errors):
                raise errors[len(calls) - 1]
            return "done"

        try:
            outcome = retry.wrap_model_call(request or self.request(), handler)
        except Exception as exc:  # noqa: BLE001
            outcome = exc
        return outcome, len(calls)

    def test_transient_statuses_and_connection_failures_are_retried(self):
        for status in (408, 409, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                error = http_error(status)
                self.assertTrue(error.retryable, status)
                clock = Clock()
                retry = RetryMiddleware(model_retries=2, clock=clock, sleep=clock.sleep)
                self.assertEqual(self.attempts(retry, [error]), ("done", 2))
        for error in (ConnectionError("refused"), TimeoutError("slow"), TransientError("connection failed")):
            clock = Clock()
            self.assertEqual(self.attempts(RetryMiddleware(model_retries=2, clock=clock, sleep=clock.sleep), [error]),
                             ("done", 2))

    def test_non_transient_errors_are_not_retried(self):
        for error in (http_error(401), http_error(403), http_error(400), AuthError("denied"), BadRequestError("bad")):
            clock = Clock()
            outcome, calls = self.attempts(RetryMiddleware(model_retries=4, clock=clock, sleep=clock.sleep), [error] * 5)
            self.assertIs(outcome, error)
            self.assertEqual(calls, 1)

    def waits(self, retry, clock, errors):
        moments = []

        def handler(_):
            moments.append(clock())
            if len(moments) <= len(errors):
                raise errors[len(moments) - 1]
            return "done"

        retry.wrap_model_call(self.request(), handler)
        return [round(later - earlier, 6) for earlier, later in zip(moments, moments[1:])]

    def test_backoff_grows_and_is_capped(self):
        clock = Clock()
        # The CLI default: delay 0.25, two retries -> 0.25 s then 0.5 s.
        self.assertEqual(self.waits(RetryMiddleware(model_retries=2, delay=0.25, clock=clock, sleep=clock.sleep), clock,
                                    [ModelTimeoutError("t")] * 2), [0.25, 0.5])
        clock = Clock()
        self.assertEqual(self.waits(RetryMiddleware(model_retries=4, delay=3, clock=clock, sleep=clock.sleep), clock,
                                    [ModelTimeoutError("t")] * 4), [3, 6, 8, 8])

    def test_retry_after_is_respected(self):
        clock = Clock()
        self.assertEqual(self.waits(RetryMiddleware(model_retries=2, delay=0.25, clock=clock, sleep=clock.sleep), clock,
                                    [RateLimitError("slow down", retry_after=5), ModelTimeoutError("t")]), [5, 0.5])

    def test_task_deadline_stops_the_wait_without_another_request(self):
        clock = Clock()
        retry = RetryMiddleware(model_retries=4, delay=1, clock=clock, sleep=clock.sleep)
        outcome, calls = self.attempts(retry, [RateLimitError("slow", retry_after=30)] * 5,
                                       self.request(remaining=lambda: 2 - clock()))
        self.assertIsInstance(outcome, ModelTimeoutError)
        self.assertEqual(calls, 1)
        self.assertLessEqual(clock(), 2.0001)


if __name__ == "__main__":
    unittest.main()
