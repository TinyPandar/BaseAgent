"""Side chat in the three frontends and its routing boundaries (docs/side.md; README BAS-2 AC01, AC05, AC06,
AC07, AC10, AC12, AC13). Fake models only, no provider calls; the TUI and Web parts are skipped with a
reason when their optional extras are not installed.

The default chat (main agent + background workers) is used unless a test says otherwise. Side requests
are answered by ``SideRouter`` (tests/side_fakes.py) so the main agent's scripted replies are never used
by the side chat, and the counts of main-agent requests show that the side chat added none.
"""

import ast
import asyncio
import io
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from baseagent import cli_chat
from chat_helpers import HAS_TEXTUAL, HAS_WEB, NO_TEXTUAL, NO_WEB, ChatTestCase, wait_until
from side_fakes import Reply, SideRouter, Streaming, call
from test_main_agent import WAIT, Duplex, Gate, delegate, tool_call

if HAS_WEB:
    from fastapi.testclient import TestClient

    from baseagent.web.server import create_app
    from test_web import parse_sse

SIDE_KINDS = {"side_opened", "side_question", "side_delta", "side_answer", "side_stopped", "side_failed",
              "side_usage", "side_notice", "side_ended"}


class CliTests(ChatTestCase):
    def run_cli(self, session, script, concurrent):
        out, err = io.StringIO(), io.StringIO()
        items = iter(script)

        def read(prompt=""):
            item = next(items)
            item = item(prompt, out) if callable(item) else item
            if isinstance(item, BaseException):
                raise item
            return item

        with patch("builtins.input", side_effect=read):
            cli_chat.run_chat(session=session, out=out, err=err, concurrent=concurrent)
        return out.getvalue(), err.getvalue()

    def test_piped_input_answers_each_side_question_in_order_without_touching_the_main_agent(self):
        model = SideRouter(Duplex(main=[{"content": "主线回答 A1"}]),
                           side=[Reply("第一行\n第二行"), Reply("还是那样")])
        session = self.session(["--session", "demo"], model)
        out, _ = self.run_cli(session, ["你好", "/side 刚才说了什么？", "/side 再说一遍", "/side-end", "/exit"],
                              concurrent=False)
        self.assertIn("[侧聊] 已打开", out)
        self.assertIn("[侧聊] 第一行\n[侧聊] 第二行", out)
        self.assertIn("[侧聊] 还是那样", out)
        self.assertIn("[侧聊] 侧聊已结束：2 次请求", out)
        self.assertEqual(len(model.inner.main_seen), 1)          # the side chat made no main-agent request
        self.assertEqual(len(model.side_seen), 2)
        history = json.dumps(self.store().load("demo").messages, ensure_ascii=False)
        self.assertNotIn("刚才说了什么", history)
        self.assertNotIn("还是那样", history)

    def test_side_line_during_an_approval_leaves_the_approval_pending(self):
        model = SideRouter(Duplex(main=[{"tool_calls": [tool_call("m1", "message_worker",
                                                                  {"task_id": "w9", "text": "x"})]},
                                        {"content": "after approval"}]),
                           side=[Reply("它要给 w9 发消息")])

        session = self.session(["--session", "demo", "--tool-policy", self.policy(["message_worker"])], model)

        def side_question(prompt, out):
            wait_until(lambda: "Allow message_worker?" in out.getvalue(), message="no approval question")
            return "/side 它要做什么？"

        def approve(prompt, out):
            wait_until(lambda: "[侧聊] 它要给 w9 发消息" in out.getvalue(), message="no side answer")
            return "y"

        def finish(prompt, out):
            wait_until(lambda: len(model.inner.main_seen) == 2 and not session.busy)
            return EOFError()

        out, _ = self.run_cli(session, ["go", side_question, approve, finish], concurrent=True)
        self.assertIn("（主 Agent 的审批仍在等待", out)
        self.assertEqual([item["decision"] for item in self.events(session, "approval_decided")], ["allow"])
        self.assertNotIn("它要做什么", json.dumps(self.store().load("demo").messages, ensure_ascii=False))

    def test_ctrl_c_stops_only_the_side_answer_when_the_main_agent_is_idle(self):
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        inner = Duplex(main=[{"content": "A1"}, {"content": "A2"}])
        model = Streaming(side=[Reply(chunks=["半句", stuck])])
        model.complete_with_control = lambda messages, tools, **kw: inner.complete_with_control(messages, tools, **kw)
        session = self.session(["--session", "demo"], model)

        def side_question(prompt, out):
            wait_until(lambda: "A1" in out.getvalue() and not session.busy)
            return "/side 讲讲"

        def interrupt(prompt, out):
            wait_until(lambda: session.side is not None and session.side.attempts
                       and session.side.attempts[0].text == "半句")
            return KeyboardInterrupt()

        def next_turn(prompt, out):
            wait_until(lambda: session.side.attempts[0].status == "stopped")
            return "继续"

        def finish(prompt, out):
            wait_until(lambda: len(inner.main_seen) == 2 and not session.busy)
            return EOFError()

        out, _ = self.run_cli(session, ["第一句", side_question, interrupt, next_turn, finish], concurrent=True)
        self.assertIn("[侧聊] 已停止（主任务、Worker 和后台任务不受影响）", out)
        self.assertIn("A2", out)                                  # the CLI did not exit on that Ctrl+C


class RoutingAndBoundaries(ChatTestCase):
    def test_queue_tell_and_workers_keep_their_targets_while_a_side_chat_runs(self):
        gate = Gate({"content": "worker done: README.md:1-1"})
        self.gates = [gate]
        model = SideRouter(Duplex(main=[delegate("d1", "Read README.md"), {"content": "派出 w1"},
                                        {"content": "收到 w1 结果"}], worker=[gate]),
                           side=[Reply("w1 在读 README", tool_calls=[call("s1", "delegate_task", {"task": "rm -rf ."}),
                                                                      call("s2", "write_file", {"path": "x", "content": "y"})])])
        session = self.session(["--session", "demo"], model)
        session.send("读 README")
        wait_until(lambda: session.workers.get("w1") is not None and gate.entered.is_set())
        self.idle_main(session)
        result = session.side_ask("w1 在做什么？", op_id="o1")
        session.side.attempts[0].thread.join(WAIT)
        self.assertEqual(result["result"], "accepted")
        self.assertEqual(session.side.attempts[0].status, "tool_requested")
        self.assertEqual(list(session.workers.records), ["w1"])            # no worker started by the side chat
        self.assertFalse((self.root / "x").exists())
        first = model.side_seen[0][0][-1]["content"]
        self.assertIn("Worker w1（running，只读）：任务 Read README.md", first)
        self.assertEqual(session.tell("w1", "顺便看 LICENSE")[0], True)      # /tell still reaches the worker
        self.assertEqual([item.text for item in session.workers.get("w1").inbox.pending()], ["顺便看 LICENSE"])
        gate.release()
        self.idle(session)
        self.assertEqual(session.workers.get("w1").status, "completed")
        session.send("结果呢？")                                           # the worker's push goes to the main agent
        self.idle(session)
        self.assertIn("worker done", json.dumps(model.inner.main_seen[-1], ensure_ascii=False))
        self.assertEqual(len(model.inner.main_seen), 3)
        self.assertNotIn("w1 在读 README", json.dumps(self.store().load("demo").messages, ensure_ascii=False))

    def test_side_stop_and_end_leave_the_main_turn_and_workers_running(self):
        main_gate = Gate({"content": "main reply"})
        worker_gate = Gate({"content": "worker reply"})
        self.gates = [main_gate, worker_gate]
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        inner = Duplex(main=[delegate("d1", "Read README.md"), main_gate, {"content": "x"}], worker=[worker_gate])
        model = Streaming(side=[Reply(chunks=["...", stuck])])
        model.complete_with_control = lambda messages, tools, **kw: inner.complete_with_control(messages, tools, **kw)
        session = self.session(["--session", "demo"], model)
        session.send("读 README")
        wait_until(lambda: main_gate.entered.is_set() and worker_gate.entered.is_set())
        session.side_ask("在干嘛？", op_id="o1")
        wait_until(lambda: session.side.attempts[0].text == "...")
        self.assertEqual(session.side_command("/side-stop")["result"], "stopped")
        session.side_command("/side-end")
        self.assertTrue(session.busy)                                      # the main turn still runs
        self.assertEqual(session.workers.get("w1").status, "running")      # so does the worker
        main_gate.release()
        worker_gate.release()
        self.idle(session)
        self.assertEqual(self.store().load("demo").status, "completed")

    def test_no_side_request_without_a_side_command(self):
        model = SideRouter(Duplex(main=[{"content": "hi"}]))
        session = self.session(["--session", "demo"], model)
        session.send("你好")
        self.idle(session)
        session.send("/queue 之后再说")
        self.idle(session)
        self.assertEqual(model.side_seen, [])
        self.assertEqual(session.side_events.last_id, 0)
        self.assertIsNone(side_table(self.db))               # no side table either

    def idle_main(self, session):
        wait_until(lambda: not session.busy, message="main turn did not finish")


def side_table(db):
    import contextlib
    import sqlite3
    with contextlib.closing(sqlite3.connect(db)) as connection:
        return connection.execute("SELECT name FROM sqlite_master WHERE name='side_attempts'").fetchone()


@unittest.skipUnless(HAS_TEXTUAL, NO_TEXTUAL)
class TuiTests(ChatTestCase):
    def test_side_panel_has_its_own_input_and_transcript(self):
        from textual.widgets import Input

        from baseagent.tui.app import ChatApp
        model = SideRouter(Duplex(main=[{"content": "主线回复"}]), side=[Reply("侧聊回复一"), Reply("侧聊回复二")])
        session = self.session(["--session", "demo"], model, ui="tui")

        async def until(pilot, predicate):
            deadline = time.monotonic() + WAIT
            while not predicate():
                assert time.monotonic() < deadline, "condition was never met"
                await pilot.pause(0.02)

        async def drive():
            app = ChatApp(session, poll=0.02)
            async with app.run_test(size=(160, 50)) as pilot:
                main_input = app.query_one("#input", Input)
                main_input.value = "你好"
                main_input.focus()
                await pilot.press("enter")
                await until(pilot, lambda: ("assistant", "主线回复") in app.entries)
                side_input = app.query_one("#side-input", Input)
                for _ in range(6):                                # Tab moves focus to the side input
                    if side_input.has_focus:
                        break
                    await pilot.press("tab")
                self.assertTrue(side_input.has_focus)
                side_input.value = "刚才说了什么？"
                await pilot.press("enter")
                await until(pilot, lambda: ("assistant", "侧聊回复一") in app.side_entries)
                main_input.value = "/side 再说一次"               # the main input also routes /side lines
                main_input.focus()
                await pilot.press("enter")
                await until(pilot, lambda: ("assistant", "侧聊回复二") in app.side_entries)
                self.assertNotIn("侧聊回复一", [text for _, text in app.entries])
                self.assertEqual(len(model.inner.main_seen), 1)
                side_input.value = "/side-end"
                side_input.focus()
                await pilot.press("enter")
                await until(pilot, lambda: any(text.startswith("侧聊已结束") for _, text in app.side_entries))

        asyncio.run(drive())


@unittest.skipUnless(HAS_WEB, NO_WEB)
class WebTests(ChatTestCase):
    def test_side_stream_commands_reconnect_and_resubmission(self):
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        inner = Duplex(main=[{"content": "主线回复"}])
        model = Streaming(side=[Reply(chunks=["第一段", stuck, "第二段"], usage=(10, 2))])
        model.complete_with_control = lambda messages, tools, **kw: inner.complete_with_control(messages, tools, **kw)
        session = self.session(["--session", "demo"], model, ui="web")
        session.open()
        client = TestClient(create_app(session), base_url="http://127.0.0.1:8765")
        client.post("/messages", json={"text": "你好"})
        wait_until(lambda: not session.busy)
        body = {"text": "讲讲", "op_id": "web-op-1"}
        self.assertEqual(client.post("/side/messages", json=body).json()["result"], "accepted")
        self.assertEqual(client.post("/side/messages", json=body).json()["result"], "duplicate")   # resubmitted
        wait_until(lambda: session.side.attempts[0].shown == len("第一段"))
        side = parse_sse(client.get("/side/events", params={"wait": 0}).text)
        self.assertEqual(side[0][1]["kind"], "resync")
        self.assertEqual(side[0][1]["data"]["snapshot"]["side"]["attempts"][0]["text"], "第一段")
        replay_id = side[0][0]
        stuck.set()
        session.side.attempts[0].thread.join(WAIT)
        later = parse_sse(client.get("/side/events", params={"wait": 0}, headers={"Last-Event-ID": replay_id}).text)
        kinds = [event["kind"] for _, event in later]
        self.assertIn("side_answer", kinds)
        self.assertEqual(kinds.count("side_answer"), 1)
        again = parse_sse(client.get("/side/events", params={"wait": 0}, headers={"Last-Event-ID": replay_id}).text)
        self.assertEqual([event["id"] for _, event in again], [event["id"] for _, event in later])
        self.assertEqual(len(model.side_seen), 1)                    # reconnects replayed, never resent
        main_kinds = {event["kind"] for _, event in parse_sse(client.get("/events", params={"wait": 0}).text)}
        self.assertFalse(main_kinds & SIDE_KINDS)
        self.assertEqual(client.post("/side/stop", json={}).status_code, 409)       # nothing running
        self.assertEqual(client.post("/side/end", json={}).json()["attempts"], 1)
        self.assertEqual(client.post("/side/messages", json={"text": "x", "op_id": "bad id!"}).status_code, 400)


class ImportBoundary(unittest.TestCase):
    def test_side_module_has_no_way_to_execute_tools(self):
        source = Path(__file__).resolve().parents[1] / "src" / "baseagent" / "agent" / "side.py"
        modules = set()
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
            elif isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
        forbidden = ("baseagent.tools.registry", "baseagent.tools.workspace", "baseagent.tools.jobs",
                     "baseagent.tools.process", "baseagent.mcp", "baseagent.backends", "baseagent.agent.workers",
                     "baseagent.agent.agent", "baseagent.agent.subtasks", "baseagent.kernel.agent",
                     "baseagent.kernel.tools", "subprocess")
        self.assertEqual([name for name in modules if name.startswith(forbidden)], [])


if __name__ == "__main__":
    unittest.main()
