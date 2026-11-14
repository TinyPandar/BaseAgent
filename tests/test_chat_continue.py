"""Continue a turn that hit its limit without leaving the chat: ``/continue [N]`` (README 施工段 AC 3).

Offline: real ``baseagent.main`` composition with scripted models. Covers the CLI (blocking), the
TUI/Web path (``ChatSession.submit`` runs it on the turn thread, and the Web endpoint), single-agent and
main-agent chat, counters that are kept, tool calls that are not repeated and limits that are never
lowered by a later chat resume.
"""

import json
import unittest

from chat_helpers import HAS_WEB, NO_WEB, ChatTestCase, wait_until
from test_cli_chat import ChatTestCase as CliCase
from test_cli_chat import ping_registry, tool_call
from test_main_agent import Duplex

from baseagent.chat.text import stop_explanation
from baseagent.agent.state import RunStatus, State
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry

if HAS_WEB:
    from fastapi.testclient import TestClient

    from baseagent.web.server import create_app


class Loop:
    """Distinct ping calls until ``finish_after`` model calls, then a final answer."""

    def __init__(self, finish_after):
        self.calls, self.finish_after = 0, finish_after

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls > self.finish_after:
            return {"content": "finished"}
        return {"tool_calls": [tool_call(f"p{self.calls}", "ping", {"n": self.calls})]}


def counting_registry(executed):
    def factory(workspace=None, args=None, skills=None, policy=None):
        tools = ToolRegistry()
        tools.register("ping", "ping", {"type": "object", "properties": {"n": {"type": "integer"}}},
                       lambda n=0: executed.append(n) or "ok", retry_safe=True)
        return tools
    return factory


class CliContinueTests(CliCase):
    def run_chat(self, lines, model, *extra):
        self.executed = []
        return self.invoke(self.chat_args("--session", "demo", "--no-main-agent", *extra), model, lines,
                           capability_factory=counting_registry(self.executed))

    def state(self):
        return SessionStore(self.db).load("demo")

    def test_continue_finishes_the_same_turn_with_counters_kept(self):
        code, stdout, stderr = self.run_chat(["work", "/continue", "/exit"], Loop(20))
        self.assertEqual(code, 0)
        self.assertIn("Step limit reached: max-steps 16/16 used", stderr)
        self.assertIn("To keep going here, type /continue", stderr)
        self.assertIn("Continuing this turn with raised limits: steps 16/32, model calls 16/32, tool calls 16/48", stdout)
        self.assertIn("finished", stdout)
        state = self.state()
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual((state.step, state.model_calls, state.tool_calls), (21, 21, 20))
        self.assertEqual((state.max_steps, state.max_model_calls, state.max_tool_calls), (32, 32, 48))
        # Every tool call ran exactly once; the first 16 were not repeated after /continue.
        self.assertEqual(self.executed, list(range(1, 21)))
        self.assertEqual(len([m for m in state.messages if m.get("role") == "user"]), 1)

    def test_continue_n_gives_n_more_and_a_plain_message_does_not_lower_limits(self):
        code, stdout, stderr = self.run_chat(["work", "/continue 2", "go on", "/continue 10", "/exit"], Loop(20))
        self.assertEqual(code, 0)
        self.assertIn("steps 16/18, model calls 16/18", stdout)
        # The plain message resumed with the stored (raised) limits, not the chat default of 16.
        self.assertIn("max-steps 18/18 used", stderr)
        self.assertNotIn("max-steps 16/18", stderr)
        self.assertIn("your new message was not sent. Type /continue", stderr)
        state = self.state()
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.max_steps, 28)
        self.assertEqual(self.executed, list(range(1, 21)))

    def test_tool_call_limit_is_raised_too(self):
        code, stdout, stderr = self.run_chat(["work", "/continue", "/exit"], Loop(6), "--max-tool-calls", "3")
        self.assertEqual(code, 0)
        self.assertIn("Tool-call limit reached: max-tool-calls 3/3 used", stderr)
        self.assertIn("tool calls 3/6", stdout)
        self.assertEqual(self.state().status, RunStatus.COMPLETED)
        self.assertEqual(self.executed, [1, 2, 3, 4, 5, 6])

    def test_usage_errors_and_nothing_to_continue(self):
        code, stdout, stderr = self.run_chat(["/continue", "hi", "/continue", "/continue 0", "/continue x", "/exit"],
                                             Loop(0))
        self.assertEqual(code, 0)
        self.assertEqual(stdout.count("Nothing to continue"), 2)
        self.assertEqual(stderr.count("Usage: /continue [N]"), 2)

    def test_one_shot_resume_usage_is_unchanged(self):
        self.executed = []
        code, _, stderr = self.invoke(self.chat_args("work", "--session", "demo"), Loop(10), AssertionError("no input"),
                                      capability_factory=counting_registry(self.executed))
        self.assertEqual(code, 1)
        self.assertIn("baseagent --session demo --resume --max-steps 16 --max-model-calls 16", stderr)
        self.assertNotIn("/continue", stderr)


class StopExplanationTests(unittest.TestCase):
    def test_every_unfinished_stop_without_error_detail_gives_used_limits_and_a_way_on(self):
        for status in RunStatus:
            if status in (RunStatus.COMPLETED, RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL):
                continue
            state = State(messages=[], session_id="s1", status=status, step=3, max_steps=8,
                          model_calls=3, max_model_calls=8, tool_calls=2, max_tool_calls=32)
            for interactive in (True, False):
                text = stop_explanation(state, interactive=interactive)
                self.assertNotIn("no error detail was recorded", text)
                if status == RunStatus.INTERRUPTED:
                    continue
                self.assertTrue("3/8" in text or "--resolve-usage" in text, (status, text))
                self.assertTrue("--resume" in text or "/continue" in text, (status, text))


class FrontendContinueTests(ChatTestCase):
    """Single- and main-agent chat through the session the TUI and Web frontends drive (submit)."""

    def looping_main(self, count):
        # The main agent's only tools are delegate_task / message_worker: message a missing worker repeatedly.
        steps = [{"tool_calls": [{"id": f"m{index}", "type": "function", "function": {
            "name": "message_worker", "arguments": json.dumps({"task_id": f"w{index}", "text": "x"})}}]}
            for index in range(count)]
        return Duplex(main=[*steps, {"content": "main finished"}])

    def test_tui_session_continue_runs_on_the_turn_thread(self):
        session = self.session(["--session", "demo", "--max-steps", "3"], self.looping_main(5), ui="tui")
        session.open()
        self.assertEqual(session.submit("work"), "sent")
        wait_until(lambda: not session.busy)
        state = self.store().load("demo")
        self.assertEqual(state.status, RunStatus.MAX_STEPS_EXCEEDED)
        notices = " ".join(item["text"] for item in self.events(session, "notice"))
        self.assertIn("Step limit reached: max-steps 3/3 used", notices)
        self.assertIn("type /continue", notices)
        self.assertEqual(session.submit("/continue 5"), "command")
        wait_until(lambda: not session.busy)
        state = self.store().load("demo")
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual((state.step, state.max_steps), (6, 8))
        # The shared main-agent budget was raised with the same numbers.
        self.assertEqual(session.workers.budget.snapshot()["max_steps"], 8)
        texts = [item["text"] for item in self.events(session, "assistant_message")]
        self.assertIn("main finished", texts)

    def test_continue_is_refused_while_a_turn_is_running(self):
        session = self.session(["--session", "demo", "--no-main-agent"], self.looping_main(0), ui="tui")
        session.open()
        session._turn.acquire()
        try:
            session.state = None
            self.store()  # no session yet: nothing to continue
            self.assertEqual(session.submit("/continue"), "command")
        finally:
            session._turn.release()
        notices = [item["text"] for item in self.events(session, "notice")]
        self.assertTrue(any("Nothing to continue" in text for text in notices))

    @unittest.skipUnless(HAS_WEB, NO_WEB)
    def test_web_message_endpoint_runs_continue(self):
        session = self.session(["--session", "demo", "--max-steps", "2"], self.looping_main(3), ui="web")
        session.open()
        client = TestClient(create_app(session, token=None, host="127.0.0.1"), base_url="http://127.0.0.1:8765")
        self.assertEqual(client.post("/messages", json={"text": "work"}).json(), {"result": "sent"})
        wait_until(lambda: not session.busy)
        self.assertEqual(self.store().load("demo").status, RunStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(client.post("/messages", json={"text": "/continue"}).json(), {"result": "command"})
        wait_until(lambda: not session.busy)
        state = self.store().load("demo")
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.max_steps, 4)  # 2 used + the --max-steps allowance of 2


if __name__ == "__main__":
    unittest.main()
