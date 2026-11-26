"""Side chat accounting (docs/side.md; README BAS-2 AC12, AC13, AC14). Fake models only, no provider calls.

Each attempt is recorded with unknown usage BEFORE its request is sent and settled at most once, to the
pool of the main turn that admitted it, also when the usage arrives after a stop or after the main task
moved on. An attempt whose usage never arrives stays unknown (never 0) and follows the existing rule: it
blocks further spend of a CNY-capped turn. A restart ends the side chat but keeps every record.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import config_isolation  # noqa: F401 -- no real .env or trust store

from baseagent.agent.side import SideChat, Snapshot, WholePort
from baseagent.agent.workers import TurnBudget, seed_turn_usage
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.main import main
from baseagent.session import SessionStore
from side_fakes import WAIT, Reply, Scripted, Streaming, call

RATE_IN, RATE_OUT = Decimal("0.8"), Decimal(2)


def cny(prompt, completion):
    return (Decimal(prompt) * RATE_IN + Decimal(completion) * RATE_OUT) / Decimal(1_000_000)


def wait_for(predicate, message="condition was never met"):
    deadline = time.monotonic() + WAIT
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(0.01)


class Chat(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / ".state" / "s.sqlite3"
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def chat(self, model, *extra):
        captured = {}

        def capture(session, **options):
            captured["session"] = session
            return 0

        with patch("baseagent.cli_chat.run_chat", capture), contextlib.redirect_stdout(io.StringIO()):
            code = main(["--root", str(self.root), "--db", str(self.db), "--session", "demo", "--no-main-agent",
                         *extra], model_factory=lambda args: model)
        self.assertEqual(code, 0)
        session = captured["session"]
        session.open()
        self.addCleanup(self.shutdown, session, model)
        return session

    def shutdown(self, session, model):
        for reply in [*getattr(model, "main", ()), *getattr(model, "side", ())]:
            if isinstance(reply, Reply):
                if reply.gate is not None:
                    reply.gate.set()
                for piece in reply.chunks or ():
                    if isinstance(piece, threading.Event):
                        piece.set()
        wait_for(lambda: not session.busy, "main turn did not finish")
        session.exit(force=True, why="test end")

    def turn(self, session, text):
        self.assertEqual(session.send(text), "sent")
        wait_for(lambda: not session.busy, "main turn did not finish")

    def ask(self, session, text, op_id, *, wait=True):
        result = session.side_ask(text, op_id=op_id)
        if wait and result["result"] == "accepted":
            session.side.attempts[-1].thread.join(WAIT)
        return result

    def rows(self):
        return [(row["main_turn"], row["status"], row["usage"], row["prompt_tokens"], row["completion_tokens"])
                for row in SessionStore(self.db).side_attempts("demo")]

    def rebuilt(self, turn, cap="1"):
        store = SessionStore(self.db)
        return seed_turn_usage(TurnBudget(None, None, budget_cny=cap), store, store.load("demo"), [], turn)


class CrossTurns(Chat):
    def test_usage_goes_to_the_admitting_turn_also_when_it_arrives_late(self):
        late = threading.Event()
        main_gate = threading.Event()
        model = Scripted(main=[Reply("A1", usage=(100, 20)), Reply("A2", usage=(300, 40), gate=main_gate),
                               Reply("A3", usage=(50, 5))],
                         side=[Reply("q1 answer", usage=(2000, 50)), Reply("q2 answer", usage=(4000, 80)),
                               Reply("late text", usage=(500, 30), gate=late)])
        session = self.chat(model, "--budget-cny", "1")
        self.turn(session, "first")                                       # main turn 1
        self.assertEqual(self.ask(session, "q1", "o1")["result"], "accepted")
        self.assertEqual(session.send("second"), "sent")                   # main turn 2, blocked in its model call
        model.entered("main", 2).wait(WAIT)
        self.ask(session, "q2", "o2")
        self.ask(session, "q3", "o3", wait=False)
        model.entered("side", 3).wait(WAIT)
        self.assertEqual(session.side_command("/side-stop")["result"], "stopped")
        main_gate.set()
        wait_for(lambda: not session.busy)
        self.turn(session, "third")                                       # main turn 3 starts before the late usage
        late.set()
        a1, a2, a3 = session.side.attempts
        a3.thread.join(WAIT)
        self.assertEqual([item.turn for item in (a1, a2, a3)], [1, 2, 2])
        self.assertIs(a3.pool, a2.pool)
        self.assertEqual(self.rows(), [(1, "completed", "reported", 2000, 50), (2, "completed", "reported", 4000, 80),
                                       (2, "stopped", "reported", 500, 30)])
        turn3 = session.side.pool_for()
        self.assertEqual(turn3[0], 3)
        self.assertEqual((turn3[1].side_calls, turn3[1].cost_cny), (0, cny(50, 5)))
        self.assertEqual(a1.pool.cost_cny, cny(100, 20) + cny(2000, 50))
        self.assertEqual(a2.pool.cost_cny, cny(300, 40) + cny(4000, 80) + cny(500, 30))
        self.assertEqual(model.side_seen.__len__(), 3)                     # no request was repeated
        # The late answer is not shown as an answer; its usage is.
        kinds = [item["kind"] for item in session.side_events_after(0) if item["data"].get("attempt") == a3.id]
        self.assertNotIn("side_answer", kinds)
        self.assertIn(("side_usage", True), [(item["kind"], item["data"].get("late")) for item in
                                             session.side_events_after(0) if item["data"].get("attempt") == a3.id])
        # Rebuilt from the database (what a restart does) gives the same per-turn spend.
        for turn, pool in ((1, a1.pool), (2, a2.pool), (3, turn3[1])):
            self.assertEqual(self.rebuilt(turn).cost_cny, pool.cost_cny, turn)

    def test_unknown_usage_blocks_a_capped_turn_and_nothing_else(self):
        for cap in ("1", None):
            with self.subTest(budget_cny=cap):
                self.setUp()
                tool_gate = threading.Event()
                stuck = threading.Event()
                model = Streaming(main=[{"tool_calls": [call("c1", "glob", {"pattern": "*.nothing"})]}, "after"],
                                  side=[Reply(chunks=["Hel", stuck], usage=(10, 1))])
                model.main[0] = Reply(tool_calls=[call("c1", "glob", {"pattern": "*.x"})], gate=tool_gate)
                session = self.chat(model, *(["--budget-cny", cap] if cap else []))
                self.assertEqual(session.send("go"), "sent")
                model.entered("main", 1).wait(WAIT)
                self.ask(session, "q", "o1", wait=False)
                wait_for(lambda: session.side.attempts[-1].text == "Hel")
                session.side_command("/side-stop")
                session.side.attempts[-1].thread.join(WAIT)       # the stream was closed: no usage ever arrives
                self.assertEqual(self.rows()[-1][1:3], ("stopped", "unknown"))
                tool_gate.set()
                wait_for(lambda: not session.busy)
                state = SessionStore(self.db).load("demo")
                if cap:
                    self.assertEqual(state.status, "max_tokens_exceeded")       # existing rule: unknown blocks spend
                    self.assertEqual(state.metadata["shared_budget_exhausted"]["reason"], "budget_cny")
                    refused = self.ask(session, "again?", "o2")
                    self.assertEqual(refused["result"], "refused")
                else:
                    self.assertEqual(state.status, "completed")
                self.assertEqual(len(model.side_seen), 1)


class MainAllowance(Chat):
    def chat_main_agent(self, model):
        captured = {}

        def capture(session, **options):
            captured["session"] = session
            return 0

        with patch("baseagent.cli_chat.run_chat", capture), contextlib.redirect_stdout(io.StringIO()):
            main(["--root", str(self.root), "--db", str(self.db), "--session", "demo", "--budget-cny", "1"],
                 model_factory=lambda args: model)
        session = captured["session"]
        session.open()
        self.addCleanup(self.shutdown, session, model)
        return session

    def test_side_requests_use_no_step_or_model_call_of_the_main_agent(self):
        from side_fakes import SideRouter
        from test_main_agent import Duplex
        model = SideRouter(Duplex(main=[Reply("A1", usage=(100, 20)).response()]),     # main usage known
                           side=[Reply("s1", usage=(10, 1)), Reply("s2", usage=(10, 1)), Reply("s3", usage=(10, 1))])
        session = self.chat_main_agent(model)
        self.turn(session, "hi")
        before = session.status()["budget"]
        for index in range(3):
            self.assertEqual(self.ask(session, f"q{index}", f"o{index}")["result"], "accepted")
        after = session.status()["budget"]
        self.assertEqual((after["model_calls"], after["steps"]), (before["model_calls"], before["steps"]))
        self.assertEqual(after["side_calls"], 3)
        self.assertEqual(Decimal(after["cost_cny"]) - Decimal(before["cost_cny"]), 3 * cny(10, 1))


class Restart(Chat):
    def test_crash_mid_answer_keeps_an_unknown_attempt_and_ends_the_side_chat(self):
        child = self.root / "child.py"
        child.write_text(textwrap.dedent(f"""
            import os, sys, threading
            sys.path[:0] = {[str(Path(__file__).parent), *[p for p in sys.path if p]]!r}
            import config_isolation
            from unittest.mock import patch
            from baseagent.main import main
            from side_fakes import Reply, Scripted
            model = Scripted(main=[Reply("A1", usage=(100, 20))],
                             side=[Reply("done", usage=(700, 70)), Reply("never", gate=threading.Event())])
            box = {{}}
            with patch("baseagent.cli_chat.run_chat", lambda session, **options: box.setdefault("s", session) and 0):
                main(["--root", {str(self.root)!r}, "--db", {str(self.db)!r}, "--session", "demo", "--no-main-agent",
                      "--budget-cny", "1"], model_factory=lambda args: model)
            session = box["s"]
            session.open()
            session.send("first")
            while session.busy: pass
            session.side_ask("q1", op_id="o1")
            session.side.attempts[-1].thread.join(20)
            session.side_ask("q2", op_id="o2")
            model.entered("side", 2).wait(20)
            print("CRASH", flush=True)
            os._exit(9)
        """), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(child)], capture_output=True, text=True, timeout=120,
                                env={**os.environ, "PYTHONUTF8": "1"})
        self.assertEqual(result.returncode, 9, result.stderr[-2000:])
        self.assertEqual(self.rows(), [(1, "completed", "reported", 700, 70), (1, "dispatching", "unknown", None, None)])
        model = Scripted(main=[Reply("A2", usage=(5, 5))], side=[Reply("new side", usage=(1, 1))])
        session = self.chat(model, "--budget-cny", "1")
        notices = [item["data"]["text"] for item in session.events_after(0) if item["kind"] == "notice"]
        self.assertTrue(any("上次进程里的侧聊已随进程结束" in text and "1 次用量未知" in text for text in notices), notices)
        self.assertIsNone(session.side)                                   # the side chat itself is gone
        self.assertEqual(self.rows()[1][1:3], ("interrupted", "unknown"))   # still unknown, not 0
        turn1 = self.rebuilt(1)
        self.assertEqual((turn1.cost_cny, len(turn1.side_unknown)), (cny(100, 20) + cny(700, 70), 1))
        self.assertEqual(turn1.admit_step(worker=False), "budget_cny")    # turn 1 stays blocked by the unknown
        self.assertEqual(self.ask(session, "q1 again", "o1")["result"], "duplicate")   # same op id: not resent
        self.assertEqual(model.side_seen, [])
        self.turn(session, "second")                                       # turn 2 has its own pool
        self.assertEqual(SessionStore(self.db).load("demo").status, "completed")
        self.assertEqual(self.ask(session, "q3", "o3")["result"], "accepted")
        self.assertEqual(len(model.side_seen), 1)


class StopAndLateUsage(Chat):
    def snapshot_chat(self, port):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        store = SessionStore(Path(temp.name) / "s.sqlite3")
        from baseagent.agent.state import State
        state = State(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "go"}], session_id="m")
        store.save(state)
        snapshot = Snapshot("m", "live", 1, time.time(), tuple(state.messages), (), (), "", 1, 96_000, 4_000)
        pool = TurnBudget(None, None, budget_cny="1")
        events = []
        side = SideChat(snapshot, store=store, port_factory=lambda: port, pool_for=lambda: (1, pool),
                        publish=lambda kind, **data: events.append((kind, data)), process_id="p")
        return side, pool, store, events

    def test_stop_and_finish_in_both_orders_record_usage_once(self):
        for stop_first in (True, False):
            with self.subTest(stop_first=stop_first):
                class Port:
                    def run(self, messages, tools, on_text):
                        on_text("x")
                        return ModelResponse({"role": "assistant", "content": "x"}, TokenUsage(7, 3, 10))

                    def abort(self):
                        pass

                side, pool, store, events = self.snapshot_chat(Port())
                at, go = threading.Event(), threading.Event()
                side.before_settle = lambda attempt: (at.set(), go.wait(WAIT))
                side.ask("op", "q")
                self.assertTrue(at.wait(WAIT))
                if stop_first:
                    result = side.stop()
                    go.set()
                    side.attempts[0].thread.join(WAIT)
                else:
                    go.set()
                    side.attempts[0].thread.join(WAIT)
                    result = side.stop()
                row = store.side_attempts("m")[0]
                expected = "stopped" if stop_first else "completed"
                self.assertEqual(result["result"], "stopped" if stop_first else "nothing_running")
                self.assertEqual((row["status"], row["usage"], row["total_tokens"]), (expected, "reported", 10))
                self.assertEqual((pool.cost_cny, pool.side_unknown), (cny(7, 3), set()))
                self.assertEqual(bool(side.turns), not stop_first)
                pool.side_unresolved(side.attempts[0].id)                 # a late "unknown" cannot undo a settlement
                self.assertEqual(pool.side_unknown, set())
                side._settle(side.attempts[0], TokenUsage(999, 999, 1998))   # a second, conflicting report
                self.assertFalse(store.side_settle(side.attempts[0].id, TokenUsage(5, 5, 10), settled_at=0))
                again = store.side_attempts("m")[0]
                self.assertEqual((again["prompt_tokens"], again["completion_tokens"]), (7, 3))
                self.assertEqual((pool.cost_cny, side.attempts[0].usage.total_tokens), (cny(7, 3), 10))

    def test_whole_reply_after_stop_is_settled_once_and_not_shown(self):
        gate = threading.Event()
        model = Scripted(side=[Reply("late text", usage=(500, 30), gate=gate)])
        side, pool, store, events = self.snapshot_chat(WholePort(model))
        threading.current_thread()
        result = {}

        def run():
            result.update(side.ask("op", "q"))

        thread = threading.Thread(target=run, name="side-test")
        thread.start()
        thread.join(WAIT)
        model.entered("side", 1).wait(WAIT)
        self.assertEqual(side.stop()["result"], "stopped")
        self.assertEqual(pool.admit_step(worker=False), None)            # in flight is not "unknown"
        gate.set()
        side.attempts[0].thread.join(WAIT)
        row = store.side_attempts("m")[0]
        self.assertEqual((row["status"], row["usage"], row["prompt_tokens"]), ("stopped", "reported", 500))
        self.assertEqual(pool.cost_cny, cny(500, 30))
        self.assertNotIn("side_answer", [kind for kind, _ in events])
        self.assertIn(("side_usage", True), [(kind, data.get("late")) for kind, data in events])
        self.assertEqual(side.turns, [])

    def test_duplicate_op_reconnect_and_explicit_retry(self):
        stuck = threading.Event()
        model = Streaming(main=[Reply("A1")], side=[Reply(chunks=["par", stuck], usage=(10, 1)), Reply("full answer")])
        session = self.chat(model)
        self.turn(session, "first")
        self.assertEqual(self.ask(session, "q", "op-1", wait=False)["result"], "accepted")
        self.assertEqual(self.ask(session, "q", "op-1")["result"], "duplicate")      # resubmitted: no new request
        wait_for(lambda: session.side.attempts[0].text == "par")
        cursor = session.side_events.last_id
        replay = session.side_events_after(0)                                           # a reconnect from the start
        resync = session.side_events_after(None)                                        # a reconnect without a cursor
        self.assertEqual([item["kind"] for item in resync], ["resync"])
        self.assertEqual(resync[0]["data"]["snapshot"]["side"]["attempts"][0]["text"], "par")
        self.assertEqual(len(model.side_seen), 1)
        session.side_command("/side-stop")
        session.side.attempts[0].thread.join(WAIT)
        retry = session.side_command("/side-retry", op_id="op-2")
        self.assertEqual(retry["result"], "accepted")
        session.side.attempts[-1].thread.join(WAIT)
        self.assertEqual(len(model.side_seen), 2)
        self.assertEqual(self.rows(), [(1, "stopped", "unknown", None, None), (1, "completed", "reported", 100, 20)])
        self.assertEqual(session.side_command("/side-retry", op_id="op-2")["result"], "duplicate")
        self.assertEqual(len(model.side_seen), 2)
        self.assertGreater(session.side_events.last_id, cursor)
        self.assertTrue(all(item["id"] <= cursor for item in replay))


if __name__ == "__main__":
    unittest.main()
