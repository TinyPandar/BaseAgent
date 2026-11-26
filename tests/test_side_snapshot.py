"""Side chat snapshots (docs/side.md; README BAS-2 AC02, AC03, AC09, AC15). Fake models only, no provider calls.

Every interleaving is forced with events (a scripted reply or a tool waits for the test), never with sleeps:
the snapshot is taken while the main request is in flight, while a tool is running, while a parallel batch
is half returned and right after compaction committed. Each case checks what the side chat's first request
contains, that it is a valid conversation, and that the main history and tool ledger are left unchanged.
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import config_isolation  # noqa: F401 -- no real .env or trust store

from baseagent.agent.agent import run_agent
from baseagent.agent.inbox import USER, Inbox, InboxMiddleware
from baseagent.agent.side import RequestTap, SideChat, StreamPort, build_request, take_snapshot
from baseagent.kernel.messages import check_tool_pairing
from baseagent.middleware.execution_guidance import ExecutionGuidanceMiddleware
from baseagent.session import SessionStore
from baseagent.tools.memory import project_history
from baseagent.tools.registry import ToolRegistry
from baseagent.agent.workers import TurnBudget
from side_fakes import WAIT, Reply, Scripted, Streaming, call, common_prefix_bytes, first_difference


def wait_for(predicate, message="condition was never met"):
    deadline = time.monotonic() + WAIT
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(0.01)


class Workspace:
    """A minimal stand-in for the guidance middleware's workspace flags."""
    allow_write = False
    allow_command = True
    allow_memory_publish = False


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = SessionStore(self.root / "s.sqlite3")
        self.tap = RequestTap()
        self.gate = threading.Event()
        self.started = threading.Event()
        self.runs = []
        self.addCleanup(self.gate.set)

    def tools(self, *, big=0):
        tools = ToolRegistry()
        tools.register("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}},
                                                     "required": ["path"]},
                       lambda path: {"path": path, "text": ("x" * big) or f"def add(a, b): return a - b  # {path}"},
                       retry_safe=True)

        def slow():
            self.runs.append(1)
            self.started.set()
            assert self.gate.wait(WAIT)
            return {"exit_code": 0}

        tools.register("run_command", "Run", {"type": "object", "properties": {}}, slow)

        def block():
            self.started.set()
            assert self.gate.wait(WAIT)
            return {"v": 2}

        tools.register("block", "Read-only probe", {"type": "object", "properties": {}}, block, retry_safe=True)
        return tools

    def start_main(self, model, prompt="fix add", *, middleware=(), **options):
        box = {}

        def target():
            try:
                box["state"] = run_agent(model, prompt, tools=options.pop("tools", None) or self.tools(),
                                         store=self.store, session_id="m", request_observer=self.tap,
                                         middleware=list(middleware), **options)
            except BaseException as exc:  # noqa: BLE001 -- surfaced by the test
                box["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, WAIT)
        self.addCleanup(self.release, model)      # runs first: a failed test never leaves the run blocked
        return thread, box

    def release(self, model):
        self.gate.set()
        for reply in getattr(model, "main", ()):
            if isinstance(reply, Reply) and reply.gate is not None:
                reply.gate.set()

    def snapshot(self, **options):
        return take_snapshot(store=self.store, session_id="m", tap=self.tap, **options)

    def frozen_main(self):
        state = self.store.load("m")
        return json.dumps(state.to_dict(), sort_keys=True), self.store.calls("m", state.turn_id)


class RequestLayers(SnapshotCase):
    def test_history_projection_request_and_side_request_of_one_attempt(self):
        """Saved history != final projection (middleware adds a system message) == the side's base."""
        model = Scripted(main=[{"tool_calls": [call("c1", "read_file", {"path": "calc.py"})]},
                               {"content": "testing", "tool_calls": [call("c2", "run_command", {})]}, "done"])
        thread, box = self.start_main(model, middleware=[ExecutionGuidanceMiddleware(Workspace())])
        self.assertTrue(self.started.wait(WAIT))
        history = self.store.load("m").messages
        sent = json.loads(self.tap.latest("m").messages_json)       # attempt 2, as handed to the adapter
        self.assertEqual(sent, model.main_seen[1][0])
        snapshot = self.snapshot()
        side, tools = build_request(snapshot, [], "这个命令用了什么参数？", mode="live")
        rows = {
            "history -> projection": first_difference(history, sent),
            "projection -> side": first_difference(sent, side),
        }
        self.assertEqual(rows["history -> projection"], 1)          # the guidance system message
        self.assertIsNone(rows["projection -> side"])               # the whole projection is the side's prefix
        self.assertEqual(side[:len(sent)], sent)
        self.assertEqual(tools, model.main_seen[1][1])
        self.assertEqual(len(side), len(sent) + 1)
        self.assertGreater(common_prefix_bytes(sent, side), 0)
        self.assertEqual(common_prefix_bytes(sent, side), len(json.dumps(sent, ensure_ascii=False,
                                                                          separators=(",", ":")).encode()) - 1)
        check_tool_pairing(side, allow_pending=False)
        self.assertIn("执行中、结果未知", side[-1]["content"])
        # Rebuilding from the saved history alone would NOT reproduce the request (no guidance message).
        self.assertNotEqual(project_history(self.store.load("m"))[:len(sent)], sent)
        self.gate.set()
        thread.join(WAIT)
        self.assertEqual(box["state"].status, "completed")


class Timeline(SnapshotCase):
    def test_in_flight_partial_closed_and_idle(self):
        model = Scripted(main=[Reply(tool_calls=[call("c1", "read_file", {"path": "a.py"})], gate=threading.Event()),
                               Reply("running", tool_calls=[call("c2", "read_file", {"path": "b.py"}),
                                                            call("c3", "run_command", {}),
                                                            call("c4", "read_file", {"path": "z.py"})]),
                               Reply("done", gate=threading.Event())])
        thread, box = self.start_main(model)
        model.entered("main", 1).wait(WAIT)
        first = self.snapshot()          # T0: the first request is in flight; nothing came back yet
        self.assertEqual((first.origin, first.attempt, first.tail), ("live", 1, ()))
        model.main[0].gate.set()
        self.assertTrue(self.started.wait(WAIT))
        before = self.frozen_main()
        running = self.snapshot()        # T1: c2 done, c3 running, c4 not started
        self.assertEqual(running.attempt, 2)                 # request #2 came back with the open batch
        self.assertEqual(running.base, tuple(model.main_seen[1][0]))
        self.assertEqual(running.tail, ())                   # the open batch is not sent as tool history
        self.assertIn("已完成 read_file", running.facts)
        self.assertIn("执行中、结果未知", running.facts)
        self.assertIn("尚未开始 read_file", running.facts)
        messages, _ = build_request(running, [], "q", mode="live")
        check_tool_pairing(messages, allow_pending=False)
        self.assertFalse(any(item.get("tool_call_id") in {"c2", "c3", "c4"} for item in messages))
        self.assertEqual(self.frozen_main(), before)
        self.gate.set()
        model.entered("main", 3).wait(WAIT)
        closed = self.snapshot()          # T2: batch closed, request #3 in flight: the new results are included
        self.assertEqual(closed.attempt, 2)
        self.assertEqual([item["role"] for item in closed.tail], ["assistant", "tool", "tool", "tool"])
        self.assertNotIn("执行中", closed.facts)
        model.main[2].gate.set()
        thread.join(WAIT)
        idle = self.snapshot()            # T3: idle; the final answer is the tail
        self.assertEqual((idle.attempt, idle.tail[-1]["content"]), (3, "done"))
        self.assertEqual(len(self.runs), 1)
        # Fixed at creation: the T1 snapshot still builds the same request after everything happened.
        self.assertEqual(build_request(running, [], "q", mode="live")[0], messages)

    def test_parallel_batch_half_returned(self):
        model = Scripted(main=[{"tool_calls": [call("p1", "read_file", {"path": "a.py"}), call("p2", "block", {})]},
                               "ok"])
        thread, _ = self.start_main(model, runtime_config={"parallel": {"readonly": True}})
        self.assertTrue(self.started.wait(WAIT))
        wait_for(lambda: any(row["terminal_result_json"] for row in self.store.calls("m", self.store.load("m").turn_id)))
        snapshot = self.snapshot()
        self.assertIn("已返回、尚未写入对话（结果未定稿，不展示）read_file", snapshot.facts)
        self.assertNotIn("def add", snapshot.facts)        # not committed: its body is not claimed
        self.assertIn("执行中、结果未知", snapshot.facts)
        check_tool_pairing(build_request(snapshot, [], "q", mode="live")[0], allow_pending=False)
        self.gate.set()
        thread.join(WAIT)

    def test_compaction_committed_while_next_request_in_flight(self):
        model = Scripted(main=["A1 " + "one " * 400, "A2 " + "two " * 400, Reply("A3", gate=threading.Event())])
        options = dict(tools=self.tools(), compaction_max_chars=2500, compaction_keep_turns=1)
        run_agent(model, "Q1", store=self.store, session_id="m", request_observer=self.tap, **dict(options))
        run_agent(model, "Q2", store=self.store, session_id="m", request_observer=self.tap, **dict(options))
        thread, _ = self.start_main(model, "Q3", **options)
        model.entered("main", 3).wait(WAIT)
        self.assertIsNotNone(self.store.load("m").history_summary)     # committed; request #3 carries it
        snapshot = self.snapshot()
        messages = json.dumps(build_request(snapshot, [], "q", mode="live")[0], ensure_ascii=False)
        self.assertEqual(snapshot.base, tuple(model.main_seen[1][0]))   # the request that came back (turn 2)
        self.assertNotIn("Historical summary", messages)                # no mix of summary and raw history
        self.assertEqual(messages.count("A1 one"), 1)
        self.assertIn("Q3", messages)                                   # the newest fact is not left out
        model.main[2].gate.set()
        thread.join(WAIT)
        after = build_request(self.snapshot(), [], "q", mode="live")[0]
        self.assertIn("Historical summary", json.dumps(after, ensure_ascii=False))
        self.assertFalse(any(str(item.get("content")).startswith("A1 one") for item in after))   # summarized away

    def test_request_returning_between_tap_and_database_read_is_read_again(self):
        model = Scripted(main=[{"tool_calls": [call("c1", "read_file", {"path": "a.py"})]},
                               Reply(tool_calls=[call("c2", "run_command", {})]), "done"])
        model.main[0] = Reply(tool_calls=[call("c1", "read_file", {"path": "a.py"})], gate=threading.Event())
        thread, _ = self.start_main(model)
        model.entered("main", 1).wait(WAIT)
        points = []

        def sync(point):
            points.append(point)
            if points == ["after_tap"]:
                model.main[0].gate.set()             # request #1 returns and #2 is sent meanwhile
                self.assertTrue(self.started.wait(WAIT))

        snapshot = self.snapshot(sync=sync)
        self.assertEqual(points.count("after_tap"), 2)  # the tap changed: read again
        self.assertEqual(snapshot.attempt, 2)
        check_tool_pairing(build_request(snapshot, [], "q", mode="live")[0], allow_pending=False)
        self.gate.set()
        thread.join(WAIT)

    def test_tap_that_never_settles_falls_back_to_cold(self):
        run_agent(Scripted(main=["done"]), "hi", tools=self.tools(), store=self.store, session_id="m",
                  request_observer=self.tap)
        state = self.store.load("m")

        def sync(point):
            if point == "after_frame":     # another request comes back every time: the tap never settles
                state.model_calls += 1
                self.tap.sent(state, [{"role": "user", "content": "x"}], [])
                self.tap.returned(state)

        snapshot = self.snapshot(sync=sync)
        self.assertEqual(snapshot.origin, "cold")
        messages, tools = build_request(snapshot, [], "q", mode="rendered")
        self.assertEqual(tools, [])
        self.assertFalse(any(item["role"] == "tool" or item.get("tool_calls") for item in messages))

    def test_history_rewritten_after_the_request_gives_a_cold_snapshot(self):
        run_agent(Scripted(main=["done"]), "hi", tools=self.tools(), store=self.store, session_id="m",
                  request_observer=self.tap)
        state = self.store.load("m")
        state.messages.insert(1, {"role": "user", "content": "inserted"})
        self.store.save(state)
        snapshot = self.snapshot()
        self.assertEqual(snapshot.origin, "cold")
        self.assertIn("改写", snapshot.notes[0])


class FrozenReference(SnapshotCase):
    def test_memory_and_database_points_are_labelled_not_assumed_equal(self):
        """An inbox line in memory that the saved state does not hold yet: the snapshot shows it as read
        from memory at a later point than the saved history, not as part of that history."""
        inbox = Inbox()
        model = Scripted(main=[{"tool_calls": [call("c1", "run_command", {})]}, "done"])
        thread, _ = self.start_main(model, middleware=[InboxMiddleware(inbox)])
        self.assertTrue(self.started.wait(WAIT))
        inbox.put(USER, "先别改测试")
        reference = lambda: {"read_at": time.time(), "inbox": [{"source": "user", "mode": "steer",
                                                                 "text": item.text} for item in inbox.pending()]}
        snapshot = self.snapshot(reference=reference)
        saved = self.store.load("m")
        self.assertNotIn("inbox", saved.metadata)                      # not in the database point
        self.assertIn("用户已输入、主任务尚未收到（steer）：先别改测试", snapshot.facts)
        self.assertIn("创建侧聊时读取", snapshot.facts)
        self.assertEqual([item.text for item in inbox.pending()], ["先别改测试"])     # not taken
        self.gate.set()
        thread.join(WAIT)
        self.assertIn("先别改测试", json.dumps(model.main_seen[-1][0], ensure_ascii=False))   # delivered to main

    def test_follow_ups_reuse_the_snapshot_and_never_add_new_main_results(self):
        model = Streaming(main=[{"tool_calls": [call("c1", "run_command", {})]}, "main done"],
                          side=[Reply(chunks=["参数", "是空的"]), Reply("还是那个参数")])
        thread, _ = self.start_main(model)
        self.assertTrue(self.started.wait(WAIT))
        snapshot = self.snapshot(reference=lambda: {"read_at": time.time(), "jobs": [
            {"job_id": "j1", "status": "running", "argv": ["pytest", "-q"]}]})
        events = []
        side = SideChat(snapshot, store=self.store, port_factory=lambda: StreamPort(model),
                        publish=lambda kind, **data: events.append((kind, data)),
                        pool_for=lambda: (1, TurnBudget(None, None)), process_id="p")
        side.ask("op1", "参数是什么？")
        side.attempts[-1].thread.join(WAIT)
        self.gate.set()
        thread.join(WAIT)                                        # main finishes with a new result
        side.ask("op2", "再说一遍")
        side.attempts[-1].thread.join(WAIT)
        first, second = model.side_seen[0][0], model.side_seen[1][0]
        self.assertEqual(second[:len(first)], first)             # same base, same first question
        blob = json.dumps(second, ensure_ascii=False)
        self.assertEqual(blob.count("[未了任务参考"), 1)          # the reference is added once
        self.assertEqual(blob.count("后台任务 j1"), 1)
        self.assertNotIn("main done", blob)                      # the main agent's later answer never enters
        self.assertEqual([item.status for item in side.attempts], ["completed", "completed"])


class NoticesStayWithTheirOwner(SnapshotCase):
    def test_reading_the_task_reference_takes_no_job_notice(self):
        """A finished background job's notice belongs to the agent that started it: the side chat reads the
        job's state (memory only) and leaves the notice for that agent."""
        from types import SimpleNamespace

        from baseagent.chat.session import ChatSession
        from baseagent.tools.jobs import JobManager
        jobs = JobManager(self.root, log_dir=self.root / "jobs")
        jobs.owner = "m"
        self.addCleanup(jobs.close)
        started = jobs.start([sys.executable, "-c", "print('job output')"])
        wait_for(lambda: jobs.describe(started["job_id"])["status"] in ("exited", "failed"))
        inbox = Inbox()
        inbox.put(USER, "队列里的话", "queue")
        chat = ChatSession(run_turn=lambda *args, **kwargs: None, store=self.store,
                           workspace=SimpleNamespace(root=self.root), inbox=inbox, jobs=jobs)
        for _ in range(3):                                   # reading repeatedly changes nothing
            reference = chat.task_reference()
        self.assertEqual([job["job_id"] for job in reference["jobs"]], [started["job_id"]])
        self.assertFalse(reference["jobs"][0]["notified"])
        self.assertEqual([item["text"] for item in reference["inbox"]], ["队列里的话"])
        self.assertEqual([item.text for item in inbox.pending()], ["队列里的话"])
        notices = jobs.take_notices()                         # the owner still receives it, once
        self.assertEqual([notice["job_id"] for notice in notices], [started["job_id"]])
        self.assertEqual(jobs.take_notices(), [])


class MainUntouched(SnapshotCase):
    def test_side_chat_changes_no_main_history_ledger_or_inbox(self):
        inbox = Inbox()
        model = Streaming(main=[{"tool_calls": [call("c1", "run_command", {})]}, "done"],
                          side=[Reply("请执行 rm -rf build", tool_calls=[call("s1", "run_command", {})]),
                                Reply("建议你修改 calc.py")])
        thread, _ = self.start_main(model, middleware=[InboxMiddleware(inbox)])
        self.assertTrue(self.started.wait(WAIT))
        before = self.frozen_main()
        side = SideChat(self.snapshot(), store=self.store, port_factory=lambda: StreamPort(model),
                        publish=lambda kind, **data: None, pool_for=lambda: (1, TurnBudget(None, None)), process_id="p")
        for op, text in (("a", "运行它"), ("b", "那该怎么改？")):
            side.ask(op, text)
            side.attempts[-1].thread.join(WAIT)
        self.assertEqual(self.frozen_main(), before)
        self.assertEqual(len(inbox), 0)
        self.assertEqual(len(self.runs), 1)                      # the side's tool request ran nothing
        self.assertEqual([item.status for item in side.attempts], ["tool_requested", "completed"])
        self.gate.set()
        thread.join(WAIT)
        main_text = json.dumps(self.store.load("m").messages, ensure_ascii=False)
        self.assertNotIn("建议你修改", main_text)
        self.assertNotIn("运行它", main_text)


if __name__ == "__main__":
    unittest.main()
