"""Chat event layer (baseagent.chat): event ids and buffering, approvals, cancel/tell, CLI on top of it.

Fake models only. These tests need no optional extra.
"""

import contextlib
import io
import unittest
from unittest.mock import patch

from baseagent import cli_chat
from baseagent.agent.workers import WorkerManager
from baseagent.chat import ChatSession, EventBuffer
from baseagent.chat.session import strip_attachments
from baseagent.main import main
from chat_helpers import ChatTestCase, wait_until
from test_main_agent import WAIT, Duplex, Gate, delegate, delegation, feed, tool_call


class EventBufferTests(unittest.TestCase):
    def test_ids_are_monotonic_and_after_id_returns_the_rest_in_order(self):
        buffer = EventBuffer(capacity=10)
        ids = [buffer.publish("notice", stream="out", text=str(index)).id for index in range(5)]
        self.assertEqual(ids, [1, 2, 3, 4, 5])
        self.assertEqual([item.data["text"] for item in buffer.after(2)], ["2", "3", "4"])
        self.assertEqual(buffer.after(5), [])
        self.assertEqual(buffer.last_id, 5)

    def test_evicted_or_unknown_ids_are_never_silently_skipped(self):
        buffer = EventBuffer(capacity=3)
        for index in range(5):
            buffer.publish("notice", stream="out", text=str(index))
        self.assertEqual([item.id for item in buffer.after(2)], [3, 4, 5])  # oldest kept id is 3
        self.assertIsNone(buffer.after(1))   # event 2 was pushed out
        self.assertIsNone(buffer.after(0))
        self.assertIsNone(buffer.after(9))   # an id this buffer never issued (another process)
        self.assertIsNone(buffer.after(None))

    def test_sse_cursor_carries_the_process_epoch(self):
        buffer = EventBuffer()
        self.assertEqual(buffer.parse_cursor(buffer.cursor(7)), 7)
        self.assertEqual(buffer.parse_cursor("7"), 7)
        self.assertIsNone(buffer.parse_cursor("deadbeef:7"))
        self.assertIsNone(buffer.parse_cursor(""))
        self.assertIsNone(buffer.parse_cursor("x:y"))

    def test_unknown_kinds_are_rejected_and_listeners_see_each_event(self):
        buffer = EventBuffer()
        seen = []
        stop = buffer.subscribe(seen.append)
        buffer.publish("error", message="boom")
        stop()
        buffer.publish("error", message="unseen")
        self.assertEqual([item.data["message"] for item in seen], ["boom"])
        with self.assertRaises(ValueError):
            buffer.publish("resync", snapshot={})
        with self.assertRaises(ValueError):
            buffer.publish("model_started")

    def test_attached_worker_pushes_are_not_shown_as_user_text(self):
        text = "[worker 推送：系统自动附加的结果，不是用户输入]\n{\"task_id\": \"w1\"}\n[用户消息]\n结果呢"
        self.assertEqual(strip_attachments(text), "结果呢")
        self.assertEqual(strip_attachments("plain"), "plain")


class ChatSessionTests(ChatTestCase):
    def test_turn_events_are_ordered_and_ids_increase(self):
        model = Duplex(main=[delegate("d1", "Read README.md"), {"content": "已派出 w1。"}],
                       worker=[{"content": "README is a secret. README.md:1-1"}])
        session = self.session(["--session", "demo"], model)
        session.open()
        self.assertTrue(session.send("读 README"))
        self.idle(session)
        items = session.events_after(0)
        ids = [item["id"] for item in items]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        kinds = [item["kind"] for item in items]
        for earlier, later in (("user_message", "tool_call"), ("tool_call", "worker_started"),
                               ("worker_started", "tool_result"), ("tool_result", "assistant_message")):
            self.assertLess(kinds.index(earlier), kinds.index(later), (earlier, later))
        self.assertIn("worker_done", kinds)
        done = self.events(session, "worker_done")[0]
        self.assertEqual((done["worker"], done["citations"]), ("w1", ["README.md:1-1"]))
        self.assertTrue(done["text"].startswith("[w1 已完成]"))
        status = self.events(session, "status")[-1]
        self.assertEqual(status["session_id"], "demo")
        self.assertEqual((status["budget"]["max_model_calls"], status["workers"][0]["status"]), (16, "completed"))
        call = self.events(session, "tool_call")[0]
        self.assertEqual((call["worker"], call["name"]), (None, "delegate_task"))

    def test_overflowed_cursor_returns_a_resync_snapshot(self):
        model = Duplex(main=[{"content": "first"}, {"content": "second"}])
        session = self.session(["--session", "demo"], model)
        session.events = EventBuffer(capacity=3)
        session.open()
        for text in ("one", "two"):
            session.send(text)
            wait_until(lambda: not session.busy)
        (resync,) = session.events_after(1)
        self.assertEqual(resync["kind"], "resync")
        snapshot = resync["data"]["snapshot"]
        self.assertEqual(resync["id"], snapshot["last_id"])
        self.assertEqual(snapshot["history"], [{"role": "user", "text": "one"}, {"role": "assistant", "text": "first"},
                                               {"role": "user", "text": "two"}, {"role": "assistant", "text": "second"}])
        self.assertEqual(session.events_after(snapshot["last_id"]), [])

    def test_a_second_message_while_answering_is_refused_not_queued(self):
        gate = Gate({"content": "slow answer"})
        self.gates = [gate]
        session = self.session(["--session", "demo"], Duplex(main=[gate]))
        session.open()
        self.assertTrue(session.send("first"))
        self.assertTrue(gate.entered.wait(WAIT))
        self.assertFalse(session.send("second"))
        self.assertIn("上一条消息还在处理中", self.events(session, "error")[-1]["message"])
        gate.release()
        wait_until(lambda: not session.busy)
        self.assertEqual([item["text"] for item in self.events(session, "user_message")], ["first"])

    def write_worker(self):
        model = Duplex(main=[delegate("d1", "Create out.txt", allow_writes=True), {"content": "已派出 w1。"}],
                       worker=[{"tool_calls": [tool_call("w1c", "write_file", self.write_request())]},
                               {"content": "Created out.txt."}])
        options = ["--session", "demo", "--allow-write", "--tool-policy", self.policy(["write_file"])]
        session = self.session(options, model)
        session.open()
        session.send("create out.txt")
        wait_until(lambda: self.events(session, "approval_requested"), message="worker never asked")
        return session, self.events(session, "approval_requested")[0]

    def test_worker_approval_with_a_wrong_digest_or_call_is_refused(self):
        session, view = self.write_worker()
        self.assertEqual((view["worker"], view["name"], view["call_id"]), ("w1", "write_file", "w1c"))
        self.assertIn('"path": "out.txt"', view["arguments"])
        self.assertEqual(len(view["digest"]), 64)
        refused = session.approve("w1c", "0" * 64)
        self.assertFalse(refused["ok"])
        self.assertIn("digest does not match", refused["message"])
        self.assertFalse(session.approve("other", view["digest"])["ok"])
        self.assertFalse(session.approve("w1c", view["digest"], worker="w2")["ok"])
        self.assertFalse(session.approve("w1c", view["digest"], worker=None)["ok"])  # not a main-agent call
        self.assertEqual(self.worker().status, "awaiting_approval")
        self.assertFalse((self.root / "out.txt").exists())
        self.assertTrue(session.approve("w1c", view["digest"])["ok"])
        self.idle(session)
        self.assertEqual((self.root / "out.txt").read_text(), "hello")
        decided = self.events(session, "approval_decided")[0]
        self.assertEqual((decided["worker"], decided["decision"]), ("w1", "allow"))
        self.assertEqual(decided["text"], "Tool call allowed. Worker w1 continues in the background.")

    def test_worker_denial_is_recorded_for_that_call_only(self):
        session, view = self.write_worker()
        self.assertTrue(session.deny(view["call_id"], view["digest"], worker="w1")["ok"])
        self.idle(session)
        self.assertFalse((self.root / "out.txt").exists())
        self.assertEqual(self.tool_result("w1")["error"]["code"], "permission_denied")

    def test_main_agent_call_approval_checks_digest_then_resumes_the_turn(self):
        # Single-agent chat: Duplex routes calls without delegate_task to its worker script.
        model = Duplex(worker=[{"tool_calls": [tool_call("w1", "write_file", self.write_request())]}, {"content": "done"}])
        options = ["--session", "demo", "--no-main-agent", "--allow-write", "--tool-policy", self.policy(["write_file"])]
        session = self.session(options, model)
        session.open()
        session.send("write")
        wait_until(lambda: not session.busy)
        (view,) = session.pending_approvals()
        self.assertEqual((view["worker"], view["call_id"]), (None, "w1"))
        self.assertFalse(session.approve("w1", "f" * 64)["ok"])
        self.assertEqual(self.store().load("demo").status, "awaiting_approval")
        self.assertTrue(session.approve("w1", view["digest"], worker=None)["ok"])
        wait_until(lambda: not session.busy)
        self.assertEqual((self.root / "out.txt").read_text(), "hello")
        self.assertEqual(self.events(session, "assistant_message")[-1]["text"], "done")
        self.assertEqual(session.pending_approvals(), [])

    def test_cancel_and_tell_are_forwarded_to_the_worker_manager(self):
        gate = Gate({"tool_calls": [tool_call("g2", "glob", {"pattern": "*.md"})]})
        hold = Gate({"content": "never"})
        self.gates = [gate, hold]
        model = Duplex(main=[{"tool_calls": [delegation("d1", "A"), delegation("d2", "B")]}, {"content": "ok"}],
                       by_task={"A": [{"tool_calls": [tool_call("g1", "glob", {"pattern": "*"})]}, gate,
                                      {"content": "A finished"}],
                                "B": [hold]})
        session = self.session(["--session", "demo"], model)
        session.open()
        session.send("两个")
        self.assertTrue(gate.entered.wait(WAIT) and hold.entered.wait(WAIT))
        with patch.object(WorkerManager, "message", autospec=True, side_effect=WorkerManager.message) as message, \
             patch.object(WorkerManager, "cancel", autospec=True, side_effect=WorkerManager.cancel) as cancel:
            self.assertEqual(session.tell("w1", "also check LICENSE"),
                             (True, "queued for w1; it is injected before the worker's next step"))
            self.assertEqual(session.cancel("w2"), [(True, "w2 cancelled")])
            self.assertFalse(session.tell("w9", "x")[0])
        message.assert_any_call(session.workers, "w1", "also check LICENSE")
        cancel.assert_called_once_with(session.workers, "w2")
        gate.release()
        self.idle(session)
        self.assertEqual(session.workers.get("w2").status, "cancelled")
        self.assertIn("[主 Agent 转达的补充要求]\nalso check LICENSE",
                      [message.get("content") for message in self.worker("w1").messages])
        self.assertEqual(self.events(session, "worker_cancelled")[0]["worker"], "w2")
        self.assertIn("unknown worker: w9", [item["message"] for item in self.events(session, "error")])

    def test_exit_asks_first_while_workers_run_and_force_records_cancellation(self):
        gate = Gate({"content": "never"})
        self.gates = [gate]
        session = self.session(["--session", "demo"], Duplex(main=[delegate("d1", "slow"), {"content": "ok"}],
                                                            worker=[gate]))
        session.open()
        session.send("慢任务")
        self.assertTrue(gate.entered.wait(WAIT))
        wait_until(lambda: not session.busy)
        answer = session.exit()
        self.assertEqual((answer["exited"], answer["running"]), (False, ["w1"]))
        self.assertIn("还有 1 个 worker 在运行（w1）", answer["question"])
        self.assertTrue(session.exit(force=True)["exited"])
        self.assertEqual(self.registry()["w1"]["status"], "cancelled")

    def test_status_command_reuses_the_cli_lines(self):
        session = self.session(["--session", "demo"], Duplex(main=[{"content": "hi"}]))
        session.open()
        session.send("hi")
        wait_until(lambda: not session.busy)
        self.assertEqual(session.submit("/status"), "command")
        text = self.events(session, "notice")[-1]["text"]
        for line in ("Session: demo", "Status: completed", "Workers: none in this session"):
            self.assertIn(line, text)
        self.assertEqual(session.submit("/exit"), "exit")
        self.assertEqual(session.submit("   "), "empty")


class CliOnChatLayerTests(ChatTestCase):
    def test_cli_chat_runs_through_chat_session_and_publishes_events(self):
        captured = {}
        original = cli_chat.run_chat

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        model = Duplex(main=[delegate("d1", "Read README.md"), {"content": "已派出 w1。"}],
                       worker=[{"content": "read it. README.md:1-1"}])
        stdout = io.StringIO()
        with patch("baseagent.cli_chat.run_chat", spy), patch("builtins.input", side_effect=feed(["读", "/wait", "/exit"])), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--root", str(self.root), "--db", str(self.db), "--session", "demo"],
                        model_factory=lambda args: model)
        self.assertEqual(code, 0)
        session = captured["session"]
        self.assertIsInstance(session, ChatSession)
        self.assertTrue(session.closed)
        kinds = [item["kind"] for item in session.events_after(0)]
        for kind in ("user_message", "worker_started", "assistant_message", "worker_done", "notice"):
            self.assertIn(kind, kinds)
        self.assertIn("已派出 w1。", stdout.getvalue())
        self.assertIn("[w1 已完成] read it. README.md:1-1", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
