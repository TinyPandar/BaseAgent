"""job_wait through the real ``baseagent.main`` composition: CLI, TUI, Web, and main agent + workers.

Fake models and controlled local processes only. The frontends are the real ones (run_chat, ChatApp,
the FastAPI app); the Web page's own script is checked separately (docs/job_wait.md).
"""

import asyncio
import io
import json
import threading
import time
import unittest
from unittest.mock import patch

from chat_helpers import HAS_TEXTUAL, HAS_WEB, NO_TEXTUAL, NO_WEB, ChatTestCase, wait_until
from job_wait_helpers import Process, jw, text_of, tool_call, tool_data, tool_pairs_closed, wait_call
from test_inbox import Script
from test_main_agent import WAIT, Duplex, Gate, delegate

from baseagent import cli_chat
from baseagent.agent.inbox import STEER_HEADER
from baseagent.agent.state import RunStatus
from baseagent.tools.jobs import JobManager

if HAS_WEB:
    from fastapi.testclient import TestClient

    from baseagent.web.server import create_app
    from test_web import LOCAL, parse_sse
if HAS_TEXTUAL:
    from baseagent.tui.app import ChatApp
    from test_tui import SIZE, until

ARGS = ["--session", "demo", "--allow-command", "--background-jobs"]


class JobWaitChatCase(ChatTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(jw, "WAIT_MIN", 1))
        self.process = Process(self.root)
        self.addCleanup(self.process.open)

    def live(self, argv, model, ui="cli"):
        """The real session with the job manager kept open (the helper's frontend returns at once)."""
        managers = []
        with patch.object(JobManager, "close", lambda manager: managers.append(manager)):
            session = self.session(argv, model, ui)
        self.addCleanup(lambda: [manager.close() for manager in managers])
        self.jobs = session.jobs
        return session

    def jid(self):
        (job,) = self.jobs.list()["jobs"]
        return job["job_id"]

    def phase(self):
        waits = self.jobs.waits()
        return waits[0]["phase"] if waits else None

    def single(self, answer="正在跑第一步。"):
        """start, wait, answer + wait again, final reply."""
        return Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv, "purpose": "验证"})]},
                       lambda: {"tool_calls": [wait_call("w1", self.jid())]},
                       lambda: {"content": answer, "tool_calls": [wait_call("w2", self.jid(), still_needed="还要结果")]},
                       {"content": "退出码 0。"}])

    def check_path(self, model, session, line):
        """What every frontend must have produced."""
        self.assertEqual(len(model.seen), 4)
        self.assertTrue(all(tool_pairs_closed(seen) for seen in model.seen))
        self.assertEqual(tool_data(model.seen[2], "w1")["outcome"], "interrupted")
        self.assertEqual(model.seen[2][-1]["content"], f"{STEER_HEADER}\n{line}")
        data = tool_data(model.seen[3], "w2")
        self.assertEqual((data["outcome"], data["answer_shown"]), ("finished", True))
        self.assertLessEqual(data["answer_shown_at"], data["waiting_since"])
        self.assertEqual(self.process.starts(), 1)
        interim = [item for item in self.events(session, "assistant_message") if item.get("interim")]
        self.assertEqual([item["text"] for item in interim], ["正在跑第一步。"])
        self.assertEqual(self.store().load("demo").status, RunStatus.COMPLETED)
        self.assertEqual(len(session.inbox), 0)


class CliTests(JobWaitChatCase):
    def test_ac13_threaded_terminal_shows_the_answer_before_waiting_again(self):
        model = self.single()
        session = self.live([*ARGS, "--no-main-agent"], model)
        out = io.StringIO()

        def interject(prompt):
            wait_until(lambda: self.phase() == "waiting", message="never waited")
            return "到哪了？"

        def status(prompt):
            # The answer is on the terminal while the wait is still running.
            wait_until(lambda: len(model.seen) == 3 and self.phase() == "waiting", message="never waited again")
            self.assertIn("正在跑第一步。", out.getvalue())
            return "/status"

        def release(prompt):
            self.assertIn("正在等待它结束", out.getvalue())
            self.process.open()
            wait_until(lambda: not session.busy, message="turn never ended")
            return EOFError()
        items = iter(["跑起来并等结果", interject, status, release])

        def read(prompt=""):
            item = next(items)
            item = item(prompt) if callable(item) else item
            if isinstance(item, BaseException):
                raise item
            return item
        with patch("builtins.input", side_effect=read):
            code = cli_chat.run_chat(session=session, out=out, err=io.StringIO(), concurrent=True)
        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertLess(printed.index("正在跑第一步。"), printed.index("退出码 0。"))
        self.assertEqual(printed.count("正在跑第一步。"), 1)
        self.check_path(model, session, "到哪了？")

    def test_ac12_ctrl_c_in_the_terminal_stops_the_wait_and_the_next_line_works(self):
        model = Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv})]},
                        lambda: {"tool_calls": [wait_call("w1", self.jid())]},
                        {"content": "已停止等待，任务还在跑。"}, {"content": "好的。"}])
        session = self.live([*ARGS, "--no-main-agent"], model)
        pid = {}

        def ctrl_c(prompt):
            wait_until(lambda: self.phase() == "waiting", message="never waited")
            pid["before"] = self.process.started()
            return KeyboardInterrupt()

        def next_line(prompt):
            wait_until(lambda: not session.busy, message="Ctrl+C did not stop the wait")
            self.assertEqual(self.store().load("demo").status, RunStatus.INTERRUPTED)
            self.assertTrue(self.process.alive())
            return "换个话题"

        def finish(prompt):
            wait_until(lambda: not session.busy)
            return EOFError()
        items = iter(["跑起来", ctrl_c, next_line, finish])

        def read(prompt=""):
            item = next(items)
            item = item(prompt) if callable(item) else item
            if isinstance(item, BaseException):
                raise item
            return item
        with patch("builtins.input", side_effect=read):
            cli_chat.run_chat(session=session, out=io.StringIO(), err=io.StringIO(), concurrent=True)
        state = self.store().load("demo")
        self.assertEqual((state.status, state.blocked_tool_calls), (RunStatus.COMPLETED, []))
        self.assertEqual(tool_data(model.seen[2], "w1")["outcome"], "wait_stopped")
        self.assertEqual(model.seen[3][-1]["content"], "换个话题")
        self.assertEqual((self.process.starts(), self.process.started()), (1, pid["before"]))

    def test_ac06_queue_keeps_its_order_and_does_not_end_the_wait(self):
        model = Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv})]},
                        lambda: {"tool_calls": [wait_call("w1", self.jid())]}, {"content": "done"},
                        {"content": "A"}, {"content": "B"}])
        session = self.live([*ARGS, "--no-main-agent"], model, "web")
        session.open()
        session.submit("go")
        wait_until(lambda: self.phase() == "waiting")
        for text in ("/queue A?", "/queue B?"):
            self.assertEqual(session.submit(text), "queued")
        time.sleep(0.5)
        self.assertEqual((self.phase(), len(model.seen)), ("waiting", 2))
        self.process.open()
        self.idle(session)
        self.assertEqual(tool_data(model.seen[2], "w1")["outcome"], "finished")
        self.assertEqual([model.seen[3][-1]["content"], model.seen[4][-1]["content"]], ["A?", "B?"])


@unittest.skipUnless(HAS_WEB, NO_WEB)
class WebTests(JobWaitChatCase):
    def client(self, session, token=None):
        session.open()
        return TestClient(create_app(session, token=token), base_url=LOCAL)

    def stream(self, client, last=None):
        return parse_sse(client.get("/events", params={"wait": 0}, headers={"Last-Event-ID": last} if last else {}).text)

    def reach_unshown(self):
        model = self.single()
        session = self.live([*ARGS, "--no-main-agent"], model, "web")
        client = self.client(session)
        client.post("/messages", json={"text": "跑起来"})
        wait_until(lambda: self.phase() == "waiting")
        self.assertEqual(client.post("/messages", json={"text": "到哪了？"}).json(), {"result": "queued"})
        wait_until(lambda: len(model.seen) == 3 and self.phase() == "showing_answer", message="answer never published")
        return model, session, client

    def test_ac13_the_wait_starts_only_after_the_page_confirms(self):
        model, session, client = self.reach_unshown()
        events = self.stream(client, session.events.cursor(0))
        (shown_id, interim), = [(sse, event) for sse, event in events
                                if event["kind"] == "assistant_message" and event["data"].get("interim")]
        self.assertEqual(interim["data"]["text"], "正在跑第一步。")
        # Reading the stream is not seeing: nothing is confirmed by delivery alone.
        time.sleep(0.5)
        self.assertEqual(self.phase(), "showing_answer")
        status = client.get("/status").json()
        self.assertEqual((status["busy"], status["jobs"][0]["waiting"], status["jobs"][0]["status"]),
                         (True, "showing_answer", "running"))
        other = next(sse for sse, event in events if event["kind"] == "tool_call")
        for ids in ([other], ["0123abcd:" + shown_id.split(":")[1]], ["nonsense"], []):
            self.assertEqual(client.post("/shown", json={"ids": ids}).status_code, 200)
        time.sleep(0.3)
        self.assertEqual(self.phase(), "showing_answer")           # another event, another process, garbage
        for _ in range(2):                                          # a repeat after a reconnect is harmless
            client.post("/shown", json={"ids": [shown_id]})
        wait_until(lambda: self.phase() == "waiting", message="confirmation did not start the wait")
        self.assertEqual(client.get("/status").json()["jobs"][0]["waiting"], "waiting")
        self.process.open()
        self.idle(session)
        self.assertEqual(client.get("/status").json()["jobs"][0]["waiting"], None)
        self.check_path(model, session, "到哪了？")

    def test_closing_the_page_neither_cancels_nor_needs_recovery_and_a_reload_confirms(self):
        model, session, client = self.reach_unshown()
        # The page was closed: no stream is open. The turn and the job go on; nothing is resubmitted.
        before = len(model.seen)
        time.sleep(0.5)
        self.assertEqual((self.phase(), len(model.seen), session.busy), ("showing_answer", before, True))
        self.assertEqual(self.store().load("demo").status, RunStatus.RUNNING)
        # Reload: a snapshot with the answer in the history and the unconfirmed id.
        (cursor, resync), = self.stream(client)
        snapshot = resync["data"]["snapshot"]
        (pending,) = snapshot["interim"]
        in_history = [item for item in snapshot["history"] if item.get("key") == pending["key"]]
        self.assertEqual([item["text"] for item in in_history], ["正在跑第一步。"])
        client.post("/shown", json={"ids": [f"{session.events.epoch}:{pending['id']}"]})
        wait_until(lambda: self.phase() == "waiting")
        (_, again), = self.stream(client)
        self.assertEqual(again["data"]["snapshot"]["interim"], [])
        self.process.open()
        self.idle(session)
        self.assertEqual(len([1 for item in self.events(session, "user_message")]), 2)     # nothing resubmitted
        self.check_path(model, session, "到哪了？")

    def test_a_result_is_delivered_even_if_no_page_ever_returns(self):
        model, session, client = self.reach_unshown()
        self.process.open(3)
        self.idle(session)
        data = tool_data(model.seen[3], "w2")
        self.assertEqual((data["outcome"], data["answer_shown"], data["job"]["exit_code"]), ("finished", False, 3))
        history = self.stream(client)[0][1]["data"]["snapshot"]["history"]
        self.assertEqual([item["text"] for item in history][-2:], ["正在跑第一步。", "退出码 0。"])

    def test_shown_validates_like_the_other_posts(self):
        session = self.live([*ARGS, "--no-main-agent"], Script([]), "web")
        client = self.client(session, token="tok")
        self.assertEqual(client.post("/shown", json={"ids": []}).status_code, 401)
        headers = {"Authorization": "Bearer tok"}
        self.assertEqual(client.post("/shown", json={"ids": "x"}, headers=headers).status_code, 400)
        self.assertEqual(client.post("/shown", json={"ids": [1]}, headers=headers).status_code, 400)
        self.assertEqual(client.post("/shown", content="x", headers=headers).status_code, 415)

    def test_stopping_the_server_during_a_wait_closes_it_without_a_model_call_or_needs_recovery(self):
        model = Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv})]},
                        lambda: {"tool_calls": [wait_call("w1", self.jid())]}, {"content": "never"}])
        session = self.live([*ARGS, "--no-main-agent"], model, "web")
        session.open()
        session.submit("go")
        wait_until(lambda: self.phase() == "waiting")
        session.exit(force=True, why="Ctrl+C")
        JobManager.close(self.jobs)                                  # what main()'s finally does
        wait_until(lambda: not session.busy, message="the turn never stopped")
        self.assertEqual(len(model.seen), 2)
        state = self.store().load("demo")
        self.assertEqual(state.status, RunStatus.CANCELLED)
        again = Script([{"content": "任务随会话结束了。"}, {"content": "在。"}])
        session = self.live([*ARGS, "--no-main-agent"], again, "web")
        session.open()
        session.submit("还在吗")
        self.idle(session)
        self.assertEqual(self.store().load("demo").status, RunStatus.COMPLETED)
        self.assertIn("killed", again.seen[0][-1]["content"])
        self.assertEqual(self.process.starts(), 1)


@unittest.skipUnless(HAS_TEXTUAL, NO_TEXTUAL)
class TuiTests(JobWaitChatCase):
    def test_ac13_the_tui_confirms_after_drawing_the_answer(self):
        model = self.single()
        session = self.live([*ARGS, "--no-main-agent"], model, "tui")
        marks = []
        original = session.mark_shown

        async def drive():
            app = ChatApp(session, poll=0.02)

            def spy(ids):
                marks.append([text for kind, text in app.entries if kind == "assistant"])
                original(ids)
            session.mark_shown = spy
            async with app.run_test(size=SIZE) as pilot:
                session.submit("跑起来")
                await until(pilot, lambda: self.phase() == "waiting", "never waited")
                session.submit("到哪了？")
                await until(pilot, lambda: len(model.seen) == 3 and self.phase() == "waiting", "never waited again")
                self.assertIn("正在跑第一步。", [text for kind, text in app.entries if kind == "assistant"])
                bar = app.query_one("#statusbar")
                await until(pilot, lambda: "正在等待它结束" in str(bar.render()), "wait not visible")
                self.process.open()
                await until(pilot, lambda: not session.busy and "退出码 0。" in
                            [text for kind, text in app.entries if kind == "assistant"], "no final reply")
                await until(pilot, lambda: "正在等待它结束" not in str(bar.render()), "wait still shown")
        asyncio.run(drive())
        self.assertEqual(marks[0], ["正在跑第一步。"])      # on screen when it was confirmed
        self.check_path(model, session, "到哪了？")


class WorkerTests(JobWaitChatCase):
    """Default composition: the main agent only dispatches, a writing worker starts and waits for the job."""

    def test_the_worker_waits_the_main_agent_answers_and_gets_the_report(self):
        model = Duplex(
            [delegate("d1", "run and report", True), {"content": "已派出 w1。"}, {"content": "还在跑。"},
             {"content": "w1 汇报：退出码 0。"}],
            [{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv, "purpose": "run"})]},
             lambda messages: {"tool_calls": [wait_call("w1", self.jid())]}, {"content": "exit code 0"}])
        session = self.live(ARGS, model, "web")
        session.open()
        session.submit("跑起来并汇报")
        wait_until(lambda: not session.busy and self.phase() == "waiting", message="worker never waited")
        (wait,) = self.jobs.waits()
        self.assertEqual(wait["agent"], "demo-w1")
        # A main-line message is answered at once by the main agent and does not touch the worker's wait.
        self.assertEqual(session.submit("到哪了？"), "sent")
        wait_until(lambda: not session.busy)
        self.assertEqual(self.phase(), "waiting")
        self.assertEqual(len(model.worker_seen), 2)
        self.assertNotIn(self.jid(), text_of(model.main_seen[-1]))          # not the main agent's job
        self.process.open()
        self.idle(session)
        self.assertEqual(tool_data(model.worker_seen[2], "w1")["outcome"], "finished")
        session.submit("结果？")
        self.idle(session)
        pushed = model.main_seen[-1][-1]["content"]
        self.assertIn("exit code 0", pushed)
        self.assertNotIn(f"- {self.jid()} ", text_of(model.main_seen[-1]))  # the report, not a second notice
        self.assertEqual(self.jobs.link_of(self.jid())["delivery"]["settled"]["session"], "demo-w1")
        self.assertTrue(all(set(names) == {"delegate_task", "message_worker"} for names in model.main_tools))
        self.assertEqual(self.process.starts(), 1)

    def test_tell_reaches_the_waiting_worker_and_cancel_stops_only_its_wait(self):
        model = Duplex(
            [delegate("d1", "run and report", True), {"content": "已派出 w1。"}, {"content": "w1 已取消。"}],
            [{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv})]},
             lambda messages: {"tool_calls": [wait_call("w1", self.jid())]},
             lambda messages: {"tool_calls": [wait_call("w2", self.jid(), still_needed="still the task")]},
             {"content": "never"}])
        session = self.live(ARGS, model, "web")
        session.open()
        session.submit("跑起来")
        wait_until(lambda: not session.busy and self.phase() == "waiting")
        self.assertTrue(session.tell("w1", "顺便看日志")[0])
        wait_until(lambda: len(model.worker_seen) == 3 and self.phase() == "waiting", message="never waited again")
        self.assertEqual(tool_data(model.worker_seen[2], "w1")["outcome"], "interrupted")
        self.assertIn("顺便看日志", model.worker_seen[2][-1]["content"])
        self.assertTrue(session.cancel("w1")[0][0])
        self.assertIsNone(self.phase())
        self.assertTrue(self.process.alive())                                 # cancelling a worker never killed jobs
        self.assertEqual(len(model.worker_seen), 3)
        # The worker is gone, so its result goes to the main agent, once, with where it came from.
        job = self.jobs._get(self.jid())
        self.process.open()
        self.assertTrue(job.done.wait(WAIT))
        session.submit("任务呢？")
        self.idle(session)
        notice = model.main_seen[-1][-1]["content"] if f"- {self.jid()} " in str(model.main_seen[-1][-1]["content"]) \
            else text_of(model.main_seen[-1])
        self.assertIn(f"- {self.jid()} exited", notice)
        self.assertIn("demo-w1, which has ended", notice)

    def test_the_stop_of_another_agent_does_not_end_the_wait(self):
        gate = Gate({"content": "never"})
        self.gates = [gate]
        model = Duplex(main=[delegate("d1", "JOB", True), delegate("d2", "OTHER"), {"content": "ok"}],
                       by_task={"JOB": [{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv})]},
                                        lambda messages: {"tool_calls": [wait_call("w1", self.jid())]},
                                        {"content": "report"}], "OTHER": [gate]})
        find = Duplex.complete_with_control

        def by_real_task(inner, messages, tools, **kwargs):
            # With an open job the request starts with the reference; the task is the next user message.
            plain = [m for m in messages if not str(m.get("content")).startswith("[未了后台任务参考")]
            return find(inner, plain, tools, **kwargs)
        with patch.object(Duplex, "complete_with_control", by_real_task):
            session = self.live(ARGS, model, "web")
            session.open()
            session.submit("两个任务")
            wait_until(lambda: self.phase() == "waiting" and gate.entered.is_set(), message="workers never started")
            self.assertTrue(session.cancel("w2")[0][0])
            time.sleep(0.4)
            self.assertEqual(self.phase(), "waiting")
            self.process.open()
            self.idle(session)
        self.assertEqual(session.workers.get("w1").status, "completed")


class OneShotTests(JobWaitChatCase):
    def test_text_mode_prints_the_mid_turn_answer_to_stderr_and_only_the_reply_to_stdout(self):
        model = Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv})]},
                        lambda: {"content": "started, waiting", "tool_calls": [wait_call("w1", self.only_job())]},
                        {"content": "exit 0"}])
        threading.Timer(1.0, self.process.open).start()
        code, stdout, stderr = self.invoke(["--session", "one", "--allow-command", "--background-jobs", "run it"], model, [])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "exit 0")
        self.assertIn("started, waiting", stderr)
        self.assertEqual(tool_data(model.seen[2], "w1")["outcome"], "finished")

    def only_job(self):
        (path,) = [p for p in (self.root / ".baseagent" / "jobs").glob("j????????.json")]
        return path.stem

    def test_flag_off_changes_nothing_in_status_snapshot_or_events(self):
        session = self.session(["--session", "demo", "--no-main-agent"], Script([{"content": "hi"}]), "cli")
        session.turn("hello", lambda view: (None, True))
        self.assertNotIn("jobs", session.status())
        self.assertNotIn("interim", session.snapshot())
        self.assertFalse([line for line in session.status_lines() if "Background jobs" in line])
        self.assertEqual(json.dumps(session.snapshot()["history"], ensure_ascii=False),
                         '[{"role": "user", "text": "hello"}, {"role": "assistant", "text": "hi"}]')


if __name__ == "__main__":
    unittest.main()
