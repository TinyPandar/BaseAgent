"""TUI/Web leftovers (README plan D, AC 19-21). Fake models only.

AC 19: where ``WorkerManager._display`` actually grew. ``ChatSession.open()`` makes TUI/Web
sessions swallow pushes, so the queue only grew when ``on_notice`` was ``None``: in the CLI
while a turn or ``/wait`` blocks, and in TUI/Web after ``close()`` while a worker was still
running (nobody drains it then). The queue is now bounded with a drop counter, and closed
TUI/Web sessions keep swallowing.

AC 20: SSE ``: ping`` comments arrive at the injected heartbeat interval while idle; when
the client disconnects the stream generator stops polling and leaves no task behind.

AC 21: the CLI's end-of-input wait and the ``--resume`` path of a main-agent turn both go
through ``ChatSession.finish_workers`` with unchanged output.
"""

import asyncio
import io
import json
import time
import unittest
from unittest.mock import patch

from chat_helpers import HAS_TEXTUAL, HAS_WEB, NO_WEB, ChatTestCase, wait_until
from test_main_agent import Duplex, Gate, delegate

from baseagent.agent.workers import DISPLAY_LIMIT, WorkerManager, WorkerRecord
from baseagent.chat import ChatSession

if HAS_WEB:
    from starlette.requests import ClientDisconnect

    from baseagent.web.server import create_app


class DisplayQueueTests(ChatTestCase):
    def test_cli_path_queue_is_bounded_and_counts_dropped_lines(self):
        from baseagent.capabilities import assemble_capabilities
        from baseagent.tools.workspace import Workspace
        manager = WorkerManager(assemble_capabilities(Workspace(self.root)), system_prompt="p", store=self.store(),
                                runner=lambda *args, **kwargs: None)
        record = WorkerRecord("w1", "demo-w1", "task", False)
        self.assertIsNone(manager.on_notice)                    # the CLI queues while a line is read
        for index in range(10000):
            manager._progress(record, f"step {index}")
        self.assertEqual(len(manager._display), DISPLAY_LIMIT)
        self.assertEqual(manager.display_dropped, 10000 - DISPLAY_LIMIT)
        items = manager.take_display()
        self.assertEqual(len(items), DISPLAY_LIMIT + 1)
        self.assertIn(f"较早的 {10000 - DISPLAY_LIMIT} 行 worker 输出没有显示", items[0][1])
        self.assertEqual(items[-1], ("err", "[w1] step 9999"))  # the newest lines are kept
        self.assertEqual((manager.take_display(), manager.display_dropped), ([], 0))

    def test_tui_and_web_sessions_do_not_queue_even_after_close_while_a_worker_runs(self):
        for ui in (["web"] if HAS_WEB else []) + (["tui"] if HAS_TEXTUAL else []):
            with self.subTest(ui=ui):
                self.db = self.root / f".state-{ui}" / "sessions.sqlite3"
                gate = Gate({"content": "done"})
                self.gates = [gate]
                model = Duplex(main=[delegate("d1", "slow"), {"content": "已派出 w1。"}], worker=[gate])
                session = self.session(["--session", "demo"], model, ui=ui)
                session.open()
                self.assertTrue(session.send("慢任务"))
                self.assertTrue(gate.entered.wait(20))
                wait_until(lambda: not session.busy)
                record = session.workers.get("w1")
                for index in range(10000):
                    session.workers._progress(record, f"step {index}")
                self.assertEqual(len(session.workers._display), 0)
                session.close()                                    # e.g. the Web server stops; w1 still runs
                for index in range(10000):
                    session.workers._progress(record, f"step {index}")
                self.assertLessEqual(len(session.workers._display), DISPLAY_LIMIT)
                self.assertEqual(len(session.workers._display), 0)
                gate.release()
                wait_until(lambda: not session.running())


@unittest.skipUnless(HAS_WEB, NO_WEB)
class ServerSentEventsTests(ChatTestCase):
    def asgi(self, app, *, query=b"", spec="2.0", disconnect_after=None):
        """Drive one GET /events through the ASGI app; returns [(monotonic time, body chunk)]."""
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec}, "http_version": "1.1",
                 "method": "GET", "scheme": "http", "path": "/events", "raw_path": b"/events", "query_string": query,
                 "root_path": "", "headers": [(b"host", b"127.0.0.1:8765")], "client": ("127.0.0.1", 50000),
                 "server": ("127.0.0.1", 8765)}
        chunks = []

        async def run():
            disconnected = asyncio.Event()
            started = False

            async def receive():
                nonlocal started
                if not started:
                    started = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if disconnected.is_set() and spec >= "2.4":
                    raise OSError("client disconnected")       # ASGI 2.4: send fails after a disconnect
                if message["type"] == "http.response.body" and message.get("body"):
                    chunks.append((time.monotonic(), message["body"].decode()))

            if disconnect_after is not None:
                asyncio.get_running_loop().call_later(disconnect_after, disconnected.set)
            try:
                await asyncio.wait_for(app(scope, receive, send), timeout=10)
            except ClientDisconnect:
                pass                                              # how a 2.4 server learns the stream ended
            # Nothing the stream started may outlive the response.
            await asyncio.sleep(0.05)
            return [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

        leftover = asyncio.run(run())
        return chunks, leftover

    def test_idle_stream_sends_pings_at_the_injected_interval(self):
        session = self.session(["--session", "demo"], Duplex(), ui="web")
        session.open()
        app = create_app(session, stream_seconds=1.0, heartbeat_seconds=0.2, poll_seconds=0.02)
        chunks, leftover = self.asgi(app, query=b"wait=1")
        self.assertEqual(leftover, [])
        self.assertIn("resync", chunks[1][1])
        pings = [moment for moment, body in chunks if body == ": ping\n\n"]
        self.assertGreaterEqual(len(pings), 3)
        self.assertLessEqual(len(pings), 5)
        start = chunks[0][0]
        self.assertGreaterEqual(pings[0] - start, 0.18)
        gaps = [later - earlier for earlier, later in zip(pings, pings[1:])]
        self.assertTrue(all(0.18 <= gap <= 0.4 for gap in gaps), gaps)

    def test_disconnect_ends_the_generator_without_background_tasks(self):
        # 2.3 is what uvicorn sends today; 2.4 servers make send() fail after a disconnect.
        for spec in ("2.3", "2.4"):
            with self.subTest(spec=spec):
                session = self.session(["--session", f"demo{spec}"], Duplex(), ui="web")
                session.open()
                calls = []
                original = session.events_after

                def counted(cursor):
                    calls.append(time.monotonic())
                    return original(cursor)

                session.events_after = counted
                app = create_app(session, stream_seconds=60.0, heartbeat_seconds=0.05, poll_seconds=0.02)
                started = time.monotonic()
                chunks, leftover = self.asgi(app, spec=spec, disconnect_after=0.3)
                self.assertLess(time.monotonic() - started, 5)       # not the 60 s stream limit
                self.assertEqual(leftover, [])
                self.assertTrue(any(body == ": ping\n\n" for _, body in chunks))
                polled = len(calls)
                time.sleep(0.3)
                self.assertEqual(len(calls), polled)                 # the generator stopped polling

    def test_heartbeat_and_poll_must_be_positive(self):
        session = self.session(["--session", "demo"], Duplex(), ui="web")
        for options in ({"heartbeat_seconds": 0}, {"poll_seconds": -1}):
            with self.assertRaisesRegex(ValueError, "positive"):
                create_app(session, **options)


class FinishWorkersTests(ChatTestCase):
    def test_cli_end_of_input_goes_through_chat_session(self):
        gate = Gate({"content": "late answer"})
        model = Duplex(main=[delegate("d1", "slow"), {"content": "ok"}], worker=[gate])

        def end(prompt):
            gate.release()
            raise EOFError

        with patch.object(ChatSession, "finish_workers", autospec=True, side_effect=ChatSession.finish_workers) as spy:
            code, stdout, stderr = self.invoke(["--session", "demo"], model, ["慢任务", end])
        self.assertEqual(code, 0)
        self.assertEqual(spy.call_count, 1)
        self.assertIn("输入已结束：等待运行中的 worker 完成后退出（Ctrl+C 取消它们）。", stderr)
        self.assertIn("[w1 已完成] late answer", stdout)
        self.assertEqual(self.registry()["w1"]["status"], "completed")

    def resumable(self):
        """A main-agent turn interrupted before its first reply (Ctrl+C during the model call)."""
        code, _, _ = self.invoke(["--session", "demo"], Duplex(main=[KeyboardInterrupt()]), ["开始", "/exit"])
        self.assertEqual(code, 0)
        self.assertEqual(self.store().load("demo").status, "interrupted")

    def test_resume_of_a_main_agent_turn_goes_through_chat_session(self):
        self.resumable()
        model = Duplex(main=[delegate("d1", "Read README.md"), {"content": "已派出 w1。"}],
                       worker=[{"content": "README read. README.md:1-1"}])
        with patch.object(ChatSession, "finish_workers", autospec=True, side_effect=ChatSession.finish_workers) as spy:
            code, stdout, stderr = self.invoke(["--session", "demo", "--resume"], model, [])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(spy.call_count, 1)
        self.assertIn("已派出 w1。", stdout)
        self.assertIn("[w1 已完成] README read. README.md:1-1", stdout)
        self.assertIn("Session: demo", stderr)

    def test_resume_with_json_output_keeps_worker_pushes_off_stdout(self):
        self.resumable()
        model = Duplex(main=[delegate("d1", "Read README.md"), {"content": "已派出 w1。"}],
                       worker=[{"content": "README read. README.md:1-1"}])
        code, stdout, stderr = self.invoke(["--session", "demo", "--resume", "--output-format", "json"], model, [])
        self.assertEqual(code, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual((result["status"], result["final_answer"]), ("completed", "已派出 w1。"))
        self.assertIn("[w1 已完成] README read. README.md:1-1", stderr)

    def test_old_finish_workers_helper_still_accepts_a_worker_manager(self):
        from baseagent.capabilities import assemble_capabilities
        from baseagent.cli_chat import finish_workers
        from baseagent.tools.workspace import Workspace
        manager = WorkerManager(assemble_capabilities(Workspace(self.root)), system_prompt="p", store=self.store(),
                                runner=lambda *args, **kwargs: None)
        manager.post("out", "[w1 已完成] queued push")
        out, err = io.StringIO(), io.StringIO()
        finish_workers(manager, out, err)
        self.assertEqual(out.getvalue(), "[w1 已完成] queued push\n")
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
