"""One inbox for external input: steer/queue lines, worker results, job notices, /tell. Fake models only.

Delivery happens only right before a model call, after the last tool batch is fully answered, as one
user message; it never adds a model call. See docs/inbox.md.
"""

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from baseagent import cli_chat
from baseagent.agent import run_agent
from baseagent.agent.inbox import (INBOX_HEADER, JOB, LEAD, PUSH_HEADER, QUEUE, STEER, STEER_HEADER, USER, USER_HEADER,
                                   WORKER, Inbox, InboxMiddleware, compose, is_inbox_message, user_parts)
from baseagent.agent.state import RunStatus
from baseagent.agent.workers import WorkerRecord
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.session import SessionStore
from baseagent.session.checkpoints import turn_number
from baseagent.tools.jobs import NOTICE_HEADER
from baseagent.tools.registry import ToolRegistry
from chat_helpers import ChatTestCase, wait_until
from test_main_agent import WAIT, Duplex, Gate, delegate, tool_call

SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
          "additionalProperties": False}


class Script:
    """Scripted model that records what every call saw."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools=None):
        self.seen.append([dict(message) for message in messages])
        response = next(self.responses)
        if callable(response):
            response = response()
        if isinstance(response, BaseException):
            raise response
        return ModelResponse(response, TokenUsage(2, 1, 3))


def batch(*paths):
    return {"tool_calls": [tool_call(f"c{index}", "probe", {"path": path}) for index, path in enumerate(paths, 1)]}


def roles(messages):
    return [message["role"] for message in messages]


class InboxUnitTests(unittest.TestCase):
    def test_take_restore_and_delivered_once(self):
        inbox = Inbox()
        seen = []
        inbox.listeners.append(lambda kind, items, **data: seen.append((kind, [item.text for item in items], data)))
        first = inbox.put(USER, "steer me")
        inbox.put(USER, "later", QUEUE)
        self.assertEqual([item.text for item in inbox.take(mode=STEER)], ["steer me"])
        self.assertEqual([item.text for item in inbox.pending()], ["later"])
        saved = inbox.snapshot()
        # A stale snapshot that still lists the delivered item never brings it back.
        self.assertEqual(inbox.restore([first.to_dict(), *saved]), [])
        fresh = Inbox()
        self.assertEqual([item.text for item in fresh.restore(saved)], ["later"])
        self.assertEqual(fresh.restore(saved), [])
        inbox.delivered([first], "model_call")
        self.assertEqual(seen[-1], ("delivered", ["steer me"], {"how": "model_call"}))
        with self.assertRaises(ValueError):
            inbox.put("someone", "x")
        with self.assertRaises(ValueError):
            inbox.put(USER, "  ")

    def test_one_message_keeps_each_source_labelled(self):
        inbox = Inbox()
        inbox.put(USER, "use python 3.12")
        inbox.put(WORKER, json.dumps({"task_id": "w1", "status": "completed"}))
        inbox.put(JOB, "- j00000001 exited (exit code 0, 3 log bytes): make")
        inbox.put(LEAD, "also check README")
        text = compose(inbox.take(mode=STEER))
        self.assertTrue(text.startswith(INBOX_HEADER))
        self.assertLess(text.index(PUSH_HEADER), text.index(NOTICE_HEADER))
        self.assertLess(text.index(NOTICE_HEADER), text.index(STEER_HEADER))
        self.assertTrue(text.endswith(STEER_HEADER + "\nuse python 3.12"))
        self.assertTrue(is_inbox_message(text))
        self.assertEqual(user_parts(text), "use python 3.12")

    def test_delivered_messages_are_not_turns(self):
        turn_start = f"{PUSH_HEADER}\n{json.dumps({'task_id': 'w1'})}\n{USER_HEADER}\nnext question"
        self.assertFalse(is_inbox_message(turn_start))
        self.assertTrue(is_inbox_message(f"{PUSH_HEADER}\n{json.dumps({'task_id': 'w1'})}"))
        self.assertTrue(is_inbox_message(f"{PUSH_HEADER}\n{{}}\n\n{STEER_HEADER}\nmore"))
        self.assertFalse(is_inbox_message("plain"))
        state = SimpleNamespace(messages=[
            {"role": "user", "content": "first"}, {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "user", "content": f"{STEER_HEADER}\nmore"}, {"role": "user", "content": f"{NOTICE_HEADER}\n- j"},
            {"role": "user", "content": f"{INBOX_HEADER}\nx"}, {"role": "assistant", "content": "ok"},
            {"role": "user", "content": turn_start}])
        self.assertEqual(turn_number(state), 2)

    def test_worker_record_inbox_is_source_lead(self):
        record = WorkerRecord("w1", "s-w1", "task", False)
        record.put_inbox("check the tests too")
        self.assertEqual([(item.source, item.text) for item in record.inbox.pending()], [(LEAD, "check the tests too")])
        self.assertEqual(record.take_inbox(), ["check the tests too"])


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / "s.sqlite3")

    def tools(self, on_call):
        tools = ToolRegistry()
        tools.register("probe", "probe", SCHEMA, on_call)
        return tools

    def test_steer_reaches_the_next_model_call_of_the_same_turn_after_the_whole_batch(self):
        inbox = Inbox()

        def probe(path):
            if path == "a":   # typed while the first call of a two-call batch runs
                inbox.put(USER, "prefer the small fix")
            return {"path": path}

        model = Script([batch("a", "b"), {"content": "done"}])
        state = run_agent(model, "fix it", tools=self.tools(probe), middleware=[InboxMiddleware(inbox)],
                          store=self.store, session_id="s1", max_steps=4)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.model_calls, 2)   # delivery adds no call
        second = model.seen[1]
        self.assertEqual(roles(second)[-4:], ["assistant", "tool", "tool", "user"])
        self.assertEqual(second[-1]["content"], f"{STEER_HEADER}\nprefer the small fix")
        stored = self.store.load("s1")
        self.assertEqual(sum(1 for message in stored.messages if is_inbox_message(message.get("content"))), 1)
        self.assertNotIn("inbox", stored.metadata)
        self.assertEqual(turn_number(stored), 1)

    def test_a_steer_line_after_the_final_call_is_not_delivered_inside_the_turn(self):
        inbox = Inbox()
        model = Script([{"content": "answer"}])
        middleware = InboxMiddleware(inbox)
        original = model.complete

        def complete(messages, tools=None):
            inbox.put(USER, "too late for this turn")
            return original(messages, tools)

        model.complete = complete
        state = run_agent(model, "q", tools=self.tools(lambda path: {}), middleware=[middleware], store=self.store,
                          session_id="s2")
        self.assertEqual(state.model_calls, 1)
        self.assertEqual([item["text"] for item in self.store.load("s2").metadata["inbox"]], ["too late for this turn"])

    def test_undelivered_items_survive_an_interrupted_turn_and_are_delivered_once_on_resume(self):
        inbox = Inbox()

        def typed_then_ctrl_c():
            # Lines typed while the model call is in flight, then Ctrl+C before it returns.
            inbox.put(USER, "keep going but skip docs")
            inbox.put(USER, "after this turn", QUEUE)
            return KeyboardInterrupt()

        first = Script([batch("a"), typed_then_ctrl_c])
        state = run_agent(first, "task", tools=self.tools(lambda path: {"path": path}), middleware=[InboxMiddleware(inbox)],
                          store=self.store, session_id="s3", max_steps=4)
        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        saved = self.store.load("s3").metadata["inbox"]
        self.assertEqual([(item["mode"], item["text"]) for item in saved],
                         [(STEER, "keep going but skip docs"), (QUEUE, "after this turn")])
        # A new process: a fresh inbox restores the saved items from the session.
        resumed_inbox = Inbox()
        resumed = Script([{"content": "resumed"}])
        tools = self.tools(lambda path: {"path": path})
        state = run_agent(resumed, None, tools=tools, middleware=[InboxMiddleware(resumed_inbox)], store=self.store,
                          session_id="s3", max_steps=4)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(resumed.seen[0][-1]["content"], f"{STEER_HEADER}\nkeep going but skip docs")
        self.assertEqual(roles(resumed.seen[0])[-3:], ["assistant", "tool", "user"])
        self.assertEqual(len(resumed.seen), 1)
        stored = self.store.load("s3")
        self.assertEqual([item["text"] for item in stored.metadata["inbox"]], ["after this turn"])
        # A second resume of the same items (stale metadata, same process) delivers nothing again.
        again = Script([{"content": "next"}])
        run_agent(again, "next", tools=tools, middleware=[InboxMiddleware(resumed_inbox)], store=self.store,
                  session_id="s3", max_steps=4)
        self.assertEqual(sum(1 for message in self.store.load("s3").messages
                             if STEER_HEADER in str(message.get("content"))), 1)

    def test_job_notice_and_user_line_arrive_in_one_message(self):
        inbox = Inbox()
        notices = [[{"job_id": "j00000001", "status": "exited", "exit_code": 0, "log_bytes": 3, "argv": ["make"],
                     "tail": "ok"}]]
        jobs = SimpleNamespace(take_notices=lambda: notices.pop() if notices else [])
        inbox.put(USER, "and then summarize")
        model = Script([{"content": "fine"}])
        run_agent(model, "build", tools=self.tools(lambda path: {}), middleware=[InboxMiddleware(inbox, jobs=jobs)],
                  store=self.store, session_id="s4")
        last = model.seen[0][-1]["content"]
        self.assertTrue(last.startswith(NOTICE_HEADER))
        self.assertIn("j00000001 exited (exit code 0", last)
        self.assertIn("Use job_output to read the log.", last)
        self.assertTrue(last.endswith(f"{STEER_HEADER}\nand then summarize"))
        self.assertEqual(sum(1 for message in model.seen[0] if message["role"] == "user"), 2)


class ChatInboxTests(ChatTestCase):
    def test_gui_steer_is_delivered_at_the_next_model_call_of_the_running_turn(self):
        gate = Gate({"tool_calls": [tool_call("m1", "message_worker", {"task_id": "w9", "text": "a"}),
                                    tool_call("m2", "message_worker", {"task_id": "w8", "text": "b"})]})
        self.gates = [gate]
        model = Duplex(main=[gate, {"content": "adjusted"}])
        session = self.session(["--session", "demo"], model)
        session.open()
        self.assertEqual(session.send("start"), "sent")
        self.assertTrue(gate.entered.wait(WAIT))
        self.assertEqual(session.submit("use the other approach"), "queued")
        gate.release()
        self.idle(session)
        self.assertEqual(len(model.main_seen), 2)
        self.assertEqual(roles(model.main_seen[1])[-4:], ["assistant", "tool", "tool", "user"])
        self.assertEqual(model.main_seen[1][-1]["content"], f"{STEER_HEADER}\nuse the other approach")
        (delivered,) = self.events(session, "inbox_delivered")
        self.assertEqual((delivered["sources"], delivered["how"]), (["user"], "model_call"))
        self.assertEqual([item["text"] for item in self.events(session, "assistant_message")], ["adjusted"])
        self.assertEqual(session.history()[1], {"role": "user", "text": "use the other approach", "mode": "steer"})

    def test_queue_waits_for_the_turn_and_leftover_steer_goes_first(self):
        gate = Gate({"content": "one"})
        self.gates = [gate]
        model = Duplex(main=[gate, {"content": "two"}, {"content": "three"}])
        session = self.session(["--session", "demo"], model)
        session.open()
        session.send("first")
        self.assertTrue(gate.entered.wait(WAIT))
        self.assertEqual(session.send("/queue later question"), "queued")
        self.assertEqual(session.send("steer now"), "queued")
        self.assertFalse(session.send("/queue"))
        gate.release()
        self.idle(session)
        self.assertEqual([seen[-1]["content"] for seen in model.main_seen], ["first", "steer now", "later question"])
        self.assertEqual([item["text"] for item in self.events(session, "assistant_message")], ["one", "two", "three"])
        self.assertEqual(len(session.inbox), 0)
        self.assertNotIn("inbox", self.store().load("demo").metadata)

    def test_worker_result_is_delivered_inside_the_running_turn_once(self):
        worker_gate = Gate({"content": "README is one line. README.md:1-1"})
        self.gates = [worker_gate]
        holder = {}

        def release_and_wait(messages):
            worker_gate.release()
            wait_until(lambda: holder["session"].inbox.pending(source="worker"), message="no push")
            return {"tool_calls": [tool_call("m1", "message_worker", {"task_id": "w9", "text": "x"})]}

        model = Duplex(main=[delegate("d1", "Read README.md"), release_and_wait, {"content": "w1 says one line"},
                             {"content": "nothing new"}], worker=[worker_gate])
        session = self.session(["--session", "demo"], model)
        holder["session"] = session
        session.open()
        session.send("读 README")
        self.idle(session)
        pushed = model.main_seen[2][-1]["content"]
        self.assertTrue(pushed.startswith(PUSH_HEADER))
        self.assertTrue(is_inbox_message(pushed))
        self.assertEqual(json.loads(pushed.splitlines()[1])["task_id"], "w1")
        self.assertEqual(roles(model.main_seen[2])[-3:], ["assistant", "tool", "user"])
        self.assertEqual(len(model.main_seen), 3)    # the result rode on a call the turn made anyway
        session.send("还有吗")
        self.idle(session)
        self.assertEqual(model.main_seen[3][-1]["content"], "还有吗")
        self.assertNotIn(PUSH_HEADER, json.dumps(model.main_seen[3][-1], ensure_ascii=False))
        self.assertEqual(turn_number(self.store().load("demo")), 2)

    def test_cancelled_turn_keeps_pending_items_for_the_next_turn(self):
        gate = Gate({"content": "never shown"})
        self.gates = [gate]
        model = Duplex(main=[gate, {"content": "resumed"}, {"content": "next answer"}, {"content": "later answer"}])
        session = self.session(["--session", "demo"], model)
        session.open()
        session.send("long task")
        self.assertTrue(gate.entered.wait(WAIT))
        session.send("/queue later")
        self.assertTrue(session.interrupt())
        self.idle(session)
        self.assertEqual(self.store().load("demo").status, RunStatus.INTERRUPTED)
        self.assertEqual([item.text for item in session.inbox.pending()], ["later"])
        self.assertEqual([item["text"] for item in self.store().load("demo").metadata["inbox"]], ["later"])
        session.send("next")
        self.idle(session)
        self.assertEqual([seen[-1]["content"] for seen in model.main_seen[2:]], ["next", "later"])
        self.assertEqual(len(session.inbox), 0)

    def test_restart_restores_undelivered_lines_and_sends_them_with_the_next_turn(self):
        model = Duplex(main=[{"content": "ok"}, {"content": "both"}])
        session = self.session(["--session", "demo"], model)
        session.open()
        session.send("hello")
        self.idle(session)
        session.inbox.put(USER, "left from before")
        session.persist()
        session.exit(force=True)
        again = self.session(["--session", "demo"], Duplex(main=[{"content": "both"}]))
        again.open()
        self.assertEqual([item.text for item in again.inbox.pending()], ["left from before"])
        again.send("new line")
        self.idle(again)
        self.assertEqual(self.store().load("demo").messages[-2]["content"], "left from before\n\nnew line")


class ThreadedCliTests(ChatTestCase):
    def run_cli(self, session, script):
        out, err = io.StringIO(), io.StringIO()
        prompts = []
        items = iter(script)

        def read(prompt=""):
            prompts.append(prompt)
            item = next(items)
            item = item(prompt) if callable(item) else item
            if isinstance(item, BaseException):
                raise item
            return item

        with patch("builtins.input", side_effect=read):
            code = cli_chat.run_chat(session=session, out=out, err=err, concurrent=True)
        return code, out.getvalue(), err.getvalue(), prompts

    def test_lines_typed_during_a_turn_steer_it_and_the_next_line_answers_its_approval(self):
        gate = Gate({"tool_calls": [tool_call("m1", "message_worker", {"task_id": "w9", "text": "x"})]})
        self.gates = [gate]
        model = Duplex(main=[gate, {"content": "done with the steer"}])
        session = self.session(["--session", "demo", "--tool-policy", self.policy(["message_worker"])], model)
        out_box = {}

        def steer(prompt):
            self.assertTrue(gate.entered.wait(WAIT))
            return "also look at tests"

        def approve(prompt):
            gate.release()
            wait_until(lambda: "Allow message_worker?" in out_box["out"].getvalue(), message="no approval question")
            return "y"

        def finish(prompt):
            wait_until(lambda: not session.busy)
            return EOFError()

        real_stdout = io.StringIO()
        out_box["out"] = real_stdout
        items = iter(["first task", steer, approve, finish])
        prompts = []

        def read(prompt=""):
            prompts.append(prompt)
            item = next(items)
            item = item(prompt) if callable(item) else item
            if isinstance(item, BaseException):
                raise item
            return item

        with patch("builtins.input", side_effect=read):
            code = cli_chat.run_chat(session=session, out=real_stdout, err=io.StringIO(), concurrent=True)
        self.assertEqual(code, 0)
        self.assertEqual(prompts, ["> "] * 4)   # approvals are answered on the ordinary prompt line
        printed = real_stdout.getvalue()
        self.assertIn("已加入本轮", printed)
        self.assertIn("Tool call allowed.", printed)
        self.assertIn("done with the steer", printed)
        second = model.main_seen[1]
        self.assertEqual(roles(second)[-3:], ["assistant", "tool", "user"])
        self.assertEqual(second[-1]["content"], f"{STEER_HEADER}\nalso look at tests")
        decided = self.events(session, "approval_decided")
        self.assertEqual([item["decision"] for item in decided], ["allow"])

    def test_ctrl_c_at_a_pending_approval_denies_and_queue_runs_after_the_turn(self):
        model = Duplex(main=[{"tool_calls": [tool_call("m1", "message_worker", {"task_id": "w9", "text": "x"})]},
                             {"content": "denied, fine"}, {"content": "queued answer"}])
        session = self.session(["--session", "demo", "--tool-policy", self.policy(["message_worker"])], model)
        out = io.StringIO()

        def interrupt(prompt):
            wait_until(lambda: "Allow message_worker?" in out.getvalue(), message="no approval question")
            return KeyboardInterrupt()

        def finish(prompt):
            wait_until(lambda: len(model.main_seen) == 3 and not session.busy)
            return EOFError()

        items = iter(["first", "/queue then this", interrupt, finish])

        def read(prompt=""):
            item = next(items)
            item = item(prompt) if callable(item) else item
            if isinstance(item, BaseException):
                raise item
            return item

        with patch("builtins.input", side_effect=read):
            cli_chat.run_chat(session=session, out=out, err=io.StringIO(), concurrent=True)
        self.assertEqual([item["decision"] for item in self.events(session, "approval_decided")], ["deny"])
        self.assertEqual(model.main_seen[2][-1]["content"], "then this")
        self.assertIn("queued answer", out.getvalue())

    def test_other_text_at_a_pending_approval_denies_and_is_kept_as_a_steer_line(self):
        # A correction typed just as the question appears must not be thrown away as a bare "no".
        model = Duplex(main=[{"tool_calls": [tool_call("m1", "message_worker", {"task_id": "w9", "text": "x"})]},
                             {"content": "ok, changed plan"}])
        session = self.session(["--session", "demo", "--tool-policy", self.policy(["message_worker"])], model)
        out, err = io.StringIO(), io.StringIO()

        def correct(prompt):
            wait_until(lambda: "Allow message_worker?" in out.getvalue(), message="no approval question")
            return "don't message w9, check the tests first"

        def finish(prompt):
            wait_until(lambda: len(model.main_seen) == 2 and not session.busy)
            return EOFError()

        items = iter(["first", correct, finish])

        def read(prompt=""):
            item = next(items)
            item = item(prompt) if callable(item) else item
            if isinstance(item, BaseException):
                raise item
            return item

        with patch("builtins.input", side_effect=read):
            cli_chat.run_chat(session=session, out=out, err=err, concurrent=True)
        self.assertEqual([item["decision"] for item in self.events(session, "approval_decided")], ["deny"])
        self.assertIn(cli_chat.DENIED_WITH_LINE, err.getvalue())
        second = model.main_seen[1]
        self.assertEqual(roles(second)[-3:], ["assistant", "tool", "user"])
        self.assertEqual(second[-1]["content"], f"{STEER_HEADER}\ndon't message w9, check the tests first")

    def test_worker_approval_is_answered_by_the_next_line_in_threaded_mode(self):
        model = Duplex(main=[delegate("d1", "Create out.txt", allow_writes=True), {"content": "已派出 w1。"}],
                       worker=[{"tool_calls": [tool_call("w1c", "write_file", self.write_request())]},
                               {"content": "Created out.txt."}])
        session = self.session(["--session", "demo", "--allow-write", "--tool-policy", self.policy(["write_file"])],
                               model)

        def parked(prompt):
            wait_until(lambda: session.next_worker_approval() is not None and not session.busy,
                       message="worker never asked")
            return ""

        def finish(prompt):
            wait_until(lambda: not session.running())
            return EOFError()

        code, out, _, prompts = self.run_cli(session, ["create out.txt", parked, "y", finish])
        self.assertEqual(code, 0)
        self.assertEqual(prompts[2], "Allow write_file for worker w1? [y/N] ")
        self.assertTrue((self.root / "out.txt").exists())

    def test_sequential_cli_runs_a_queue_line_as_the_next_turn(self):
        model = Duplex(main=[{"content": "plain"}])
        session = self.session(["--session", "demo"], model)
        stdout = io.StringIO()
        with patch("builtins.input", side_effect=["/queue just ask", EOFError()]), contextlib.redirect_stdout(stdout):
            cli_chat.run_chat(session=session, out=stdout, err=io.StringIO(), concurrent=False)
        self.assertEqual(model.main_seen[0][-1]["content"], "just ask")


if __name__ == "__main__":
    unittest.main()
