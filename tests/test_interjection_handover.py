"""A user's mid-turn line moves a running run_command to a background job (BAS-2, README AC01-AC14).

Fake models and local controlled processes only: no provider calls. The command records every start and
its pid, so "same process, started once" is checked, not assumed. See docs/background_jobs.md.
"""

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from checkpoint_helpers import Script, tool_call

from baseagent.agent import run_agent
from baseagent.agent.cancellation import CancellationToken
from baseagent.agent.inbox import JOB, LEAD, QUEUE, STEER, STEER_HEADER, USER, WORKER, Inbox, InboxMiddleware
from baseagent.agent.state import RunStatus
from baseagent.session import SessionStore
from baseagent.tools.jobs import JobManager, TimeoutHandover, user_interjection
from baseagent.tools.process import INTERJECTION
from baseagent.tools.workspace import Workspace, coding_tools
from chat_helpers import ChatTestCase, require_frontend
from chat_helpers import wait_until as chat_wait
from test_inbox import Script as ChatScript
from test_jobs import PY, SLOW, JobsCase, alive, wait_until
from test_main_agent import WAIT

LONG = "30"     # seconds the controlled command would run if nobody moved it
TIMEOUT = 50    # its run_command timeout: every handover below happens long before this


def run(argv, timeout=TIMEOUT, call_id="c1"):
    return tool_call(call_id, "run_command", {"argv": argv, "timeout": timeout})


def tool_data(messages, call_id):
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
            content = json.loads(message["content"])
            return content.get("data", content)
    raise AssertionError(f"no tool result for {call_id}")


def roles(messages):
    return [message["role"] for message in messages]


def tool_pairs_closed(messages):
    """Every assistant tool call has exactly one tool result before the next non-tool message."""
    open_ids = None
    for message in messages:
        if message["role"] == "assistant" and message.get("tool_calls"):
            if open_ids:
                return False
            open_ids = [call["id"] for call in message["tool_calls"]]
        elif message["role"] == "tool":
            if not open_ids or message.get("tool_call_id") not in open_ids:
                return False
            open_ids.remove(message["tool_call_id"])
        elif open_ids:
            return False
    return not open_ids


class InterjectionCase(JobsCase):
    def setUp(self):
        super().setUp()
        (self.root / "slow.py").write_text(SLOW, encoding="utf-8")
        self.marker = self.root / "starts.txt"
        self.store = SessionStore(self.root / "s.sqlite3")
        self.times = {}

    def slow(self, seconds=LONG):
        return [PY, "slow.py", str(self.marker), str(seconds)]

    def pid(self):
        path = Path(str(self.marker) + ".pid")
        self.assertTrue(wait_until(path.exists))
        return int(path.read_text())

    def starts(self):
        return self.marker.read_text().splitlines() if self.marker.exists() else []

    def type_when_running(self, inbox, *items, after=1.3):
        """Once the command has started, wait `after` seconds and put (source, text, mode) items."""
        def typist():
            wait_until(Path(str(self.marker) + ".pid").exists)
            self.times["pid_before"] = int(Path(str(self.marker) + ".pid").read_text())
            time.sleep(after)
            self.times["typed"] = time.monotonic()
            for source, text, mode in items:
                inbox.put(source, text, mode)
        thread = threading.Thread(target=typist, daemon=True)
        thread.start()
        return thread

    def agent(self, model, inbox, workspace, session_id="s1", **kwargs):
        start = time.monotonic()
        state = run_agent(model, "train the model", tools=coding_tools(workspace),
                          middleware=[InboxMiddleware(inbox, jobs=workspace.jobs)], store=self.store,
                          session_id=session_id, workspace_root=str(self.root), max_steps=6, **kwargs)
        self.times["returned"] = time.monotonic()
        self.times["elapsed"] = self.times["returned"] - start
        return state

    def stamped(self, response):
        """Model step that records when it was called (= when the interjection reached the model)."""
        def step(messages=None):
            self.times.setdefault("delivered", time.monotonic())
            return response
        return step


class HandoverTests(InterjectionCase):
    def test_ac02_ac03_user_line_moves_the_same_process_and_arrives_after_the_batch(self):
        workspace = self.workspace()
        inbox = Inbox()
        typist = self.type_when_running(inbox, (USER, "先别等了，先看下日志", STEER),
                                        (USER, "顺便确认 GPU", STEER))   # two quick lines: still one job
        model = Script([{"tool_calls": [run(self.slow())]}, self.stamped({"content": "ok"})])
        state = self.agent(model, inbox, workspace)
        typist.join()
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.model_calls, 2)                       # delivery adds no model call
        self.assertLess(self.times["elapsed"], 15, "waited for the command instead of moving it")
        self.assertLess(self.times["typed"], self.times["delivered"])
        second = model.seen[1]
        self.assertTrue(tool_pairs_closed(second))
        self.assertEqual(roles(second)[-3:], ["assistant", "tool", "user"])
        self.assertEqual(second[-1]["content"], f"{STEER_HEADER}\n先别等了，先看下日志\n\n顺便确认 GPU")
        data = tool_data(second, "c1")
        self.assertTrue(data["moved_to_background"])
        self.assertEqual(data["handover_reason"], INTERJECTION)
        self.assertIsNone(data["exit_code"])                         # not reported as finished
        self.assertIn("before-timeout", data["stdout"])
        # Same process, started once, still running, owned by exactly one job. (data["pid"] is the process
        # run_command started; on Windows a venv python.exe is a launcher, so the script's own pid is its child.)
        pid = self.pid()
        self.assertEqual(self.times["pid_before"], pid)
        self.assertTrue(alive(pid))
        self.assertTrue(alive(data["pid"]))
        self.assertEqual(workspace.jobs.describe(data["job_id"])["pid"], data["pid"])
        self.assertEqual(self.starts(), ["start"])
        jobs = workspace.jobs.list()["jobs"]
        self.assertEqual([(job["job_id"], job["status"], job["kind"]) for job in jobs],
                         [(data["job_id"], "running", "run_command")])
        # Lifecycle is the ordinary job one: job_kill stops it, nothing restarts it.
        workspace.jobs.kill(data["job_id"])
        self.assertTrue(wait_until(lambda: not alive(pid)))
        self.assertEqual(self.starts(), ["start"])

    def test_ac03_rest_of_the_batch_runs_first_and_short_commands_just_finish(self):
        workspace = self.workspace()
        inbox = Inbox()
        self.type_when_running(inbox, (USER, "stop waiting", STEER))
        quick = [PY, "-c", "print('quick done')"]
        model = Script([{"tool_calls": [run(self.slow(), call_id="c1"), run(quick, call_id="c2")]},
                        {"content": "ok"}])
        state = self.agent(model, inbox, workspace)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        second = model.seen[1]
        self.assertTrue(tool_pairs_closed(second))
        self.assertEqual(roles(second)[-4:], ["assistant", "tool", "tool", "user"])
        self.assertTrue(tool_data(second, "c1")["moved_to_background"])
        quick_data = tool_data(second, "c2")             # the line was still pending: the grace keeps it normal
        self.assertNotIn("moved_to_background", quick_data)
        self.assertEqual((quick_data["exit_code"], quick_data["stdout"].strip()), (0, "quick done"))
        self.assertEqual(len(workspace.jobs.list()["jobs"]), 1)

    def test_ac04_ac05_queue_worker_job_and_lead_items_do_not_move_a_command(self):
        workspace = self.workspace()
        inbox = Inbox()
        self.type_when_running(inbox, (USER, "next turn please", QUEUE), (WORKER, '{"task_id": "w1"}', STEER),
                               (LEAD, "from lead", STEER), (JOB, "- j1 exited", STEER), after=0.5)
        model = Script([{"tool_calls": [run(self.slow(2.5))]}, {"content": "ok"}])
        state = self.agent(model, inbox, workspace)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        data = tool_data(model.seen[1], "c1")
        self.assertNotIn("moved_to_background", data)
        self.assertEqual(data["exit_code"], 0)
        self.assertIn("after-timeout", data["stdout"])            # ran to its natural end
        self.assertEqual(workspace.jobs.list()["jobs"], [])
        # Non-user items arrive in one labelled message; the /queue line stays for the next turn.
        delivered = model.seen[1][-1]["content"]
        self.assertLess(delivered.index('"task_id": "w1"'), delivered.index("- j1 exited"))
        self.assertNotIn("next turn please", delivered)
        self.assertEqual([item.text for item in inbox.pending()], ["next turn please"])

    def test_ac04_a_line_for_another_agent_does_not_move_this_agents_command(self):
        workspace = self.workspace()
        main_inbox, worker_inbox = Inbox(), Inbox()
        self.type_when_running(main_inbox, (USER, "for the main agent", STEER), after=0.5)
        model = Script([{"tool_calls": [run(self.slow(2.5))]}, {"content": "ok"}])
        state = self.agent(model, worker_inbox, workspace)          # this agent only owns worker_inbox
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertNotIn("moved_to_background", tool_data(model.seen[1], "c1"))
        self.assertNotIn("for the main agent", json.dumps(model.seen[1], ensure_ascii=False))
        self.assertEqual([item.text for item in main_inbox.pending()], ["for the main agent"])

    def test_signal_reads_only_user_steer_of_the_own_inboxes(self):
        inbox = Inbox()
        context = type("Context", (), {"middleware": (InboxMiddleware(inbox),)})()
        signal = user_interjection(context)
        self.assertFalse(signal())
        for source, mode in ((USER, QUEUE), (WORKER, STEER), (LEAD, STEER), (JOB, STEER)):
            inbox.put(source, "x", mode)
        self.assertFalse(signal())
        inbox.put(USER, "now", STEER)
        self.assertTrue(signal())
        self.assertIsNone(user_interjection(type("Context", (), {"middleware": ()})()))
        self.assertIsNone(user_interjection(None))


class RefusalTests(InterjectionCase):
    def test_ac07_flag_off_never_kills_on_a_user_line(self):
        workspace = Workspace(self.root, allow_command=True)          # no --background-jobs
        inbox = Inbox()
        self.type_when_running(inbox, (USER, "hurry", STEER), after=0.5)
        model = Script([{"tool_calls": [run(self.slow(2.5))]}, {"content": "ok"}])
        state = self.agent(model, inbox, workspace)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        data = tool_data(model.seen[1], "c1")
        self.assertEqual(data["exit_code"], 0)
        self.assertNotIn("moved_to_background", data)
        self.assertFalse((self.root / ".baseagent" / "jobs").exists())
        self.assertEqual(model.seen[1][-1]["content"], f"{STEER_HEADER}\nhurry")

    def test_ac07_full_job_slots_keep_the_command_running_and_say_why(self):
        workspace = self.workspace()
        workspace.jobs.max_running = 0
        inbox = Inbox()
        self.type_when_running(inbox, (USER, "hurry", STEER), after=0.2)
        model = Script([{"tool_calls": [run(self.slow(3))]}, {"content": "ok"}])
        state = self.agent(model, inbox, workspace)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        data = tool_data(model.seen[1], "c1")
        self.assertEqual(data["exit_code"], 0)                       # not killed: it finished on its own
        self.assertIn("after-timeout", data["stdout"])
        self.assertIn("at most 0 background jobs", data["not_moved_on_interjection"])
        self.assertNotIn("job_id", data)
        self.assertEqual(self.starts(), ["start"])
        self.assertEqual(workspace.jobs.list()["jobs"], [])

    def test_ac07_refused_then_timeout_still_follows_the_timeout_rule(self):
        workspace = self.workspace()
        workspace.jobs.max_running = 0
        inbox = Inbox()
        self.type_when_running(inbox, (USER, "hurry", STEER), after=0.2)
        model = Script([{"tool_calls": [run(self.slow(), timeout=3)]}, {"content": "ok"}])
        self.agent(model, inbox, workspace)
        result = json.loads(next(message["content"] for message in model.seen[1] if message["role"] == "tool"))
        self.assertEqual(result["error"]["code"], "timeout")
        self.assertIn("not moved to background", result["error"]["message"])
        self.assertTrue(wait_until(lambda: not alive(self.pid())))

    def test_ac07_sleep_is_never_offered(self):
        workspace = self.workspace()
        self.assertIsNone(TimeoutHandover.for_command(workspace.jobs, ["sleep", "100"], self.root))


class CancellationTests(InterjectionCase):
    def handover(self, workspace):
        return TimeoutHandover.for_command(workspace.jobs, self.slow(), self.root)

    def test_ac08_cancellation_racing_the_handover_wins_and_stops_the_process(self):
        workspace = self.workspace()
        token = CancellationToken()

        def user_line_then_ctrl_c():
            token.cancel()          # cancellation lands just as the handover starts
            return True

        result = workspace.backend.execute(self.slow(), timeout=TIMEOUT, cancellation=token,
                                           on_timeout=self.handover(workspace), interjection=user_line_then_ctrl_c)
        self.assertEqual(result.error.code, "cancelled")
        self.assertNotIn("could not be confirmed", result.error.message)
        self.assertTrue(wait_until(lambda: not alive(self.pid())))
        (job,) = workspace.jobs.list()["jobs"]
        self.assertEqual(job["status"], "killed")                    # not a surviving background job
        self.assertEqual(self.starts(), ["start"])

    def test_ac08_ctrl_c_right_after_the_handover_stops_the_job_too(self):
        workspace = self.workspace()
        handover = self.handover(workspace)
        moved = threading.Event()

        def on_timeout(offer):
            result = handover(offer)
            moved.set()
            return result
        on_timeout.revoke = handover.revoke

        class CtrlC:                 # what the chat's async KeyboardInterrupt looks like to run_process
            def cancelled(self):
                if moved.is_set():
                    raise KeyboardInterrupt
                return False

            def check(self):
                pass

        with self.assertRaises(KeyboardInterrupt):
            workspace.backend.execute(self.slow(), timeout=TIMEOUT, cancellation=CtrlC(), on_timeout=on_timeout,
                                      interjection=lambda: True)
        self.assertTrue(wait_until(lambda: not alive(self.pid())))
        self.assertEqual([job["status"] for job in workspace.jobs.list()["jobs"]], ["killed"])

    def test_ac08_after_a_handover_a_turn_cancel_leaves_the_job_alone(self):
        workspace = self.workspace()
        inbox = Inbox()
        token = CancellationToken()
        self.type_when_running(inbox, (USER, "check later", STEER))

        def cancel_turn(messages=None):
            token.cancel()                                           # Ctrl+C after the handover
            return {"content": "never used"}

        model = Script([{"tool_calls": [run(self.slow())]}, cancel_turn, {"content": "unused"}])
        state = self.agent(model, inbox, workspace, cancellation=token)
        self.assertIn(state.status, (RunStatus.CANCELLED, RunStatus.INTERRUPTED))
        data = tool_data(model.seen[1], "c1")
        self.assertTrue(alive(data["pid"]))                          # documented lifecycle: jobs outlive turns
        self.assertEqual(workspace.jobs.describe(data["job_id"])["status"], "running")
        workspace.jobs.close()                                       # session end stops it
        self.assertTrue(wait_until(lambda: not alive(data["pid"])))

    def test_natural_exit_before_the_grace_is_an_ordinary_result(self):
        workspace = self.workspace()
        result = workspace.backend.execute([PY, "-c", "print('done')"], timeout=TIMEOUT,
                                           on_timeout=self.handover(workspace), interjection=lambda: True)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["exit_code"], 0)
        self.assertNotIn("moved_to_background", result.data)
        self.assertEqual(workspace.jobs.list()["jobs"], [])


class RecoveryTests(InterjectionCase):
    def test_ac10_resume_after_the_handover_never_restarts_the_command_or_redelivers(self):
        workspace = self.workspace()
        inbox = Inbox()
        self.type_when_running(inbox, (USER, "summarize when ready", STEER))
        first = Script([{"tool_calls": [run(self.slow())]}, KeyboardInterrupt()])
        state = self.agent(first, inbox, workspace, session_id="r1")
        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        data = tool_data(first.seen[1], "c1")
        self.assertTrue(data["moved_to_background"])
        # A new process resumes the session: the finished call is not run again, the line is not sent twice.
        resumed = Script([{"content": "resumed"}])
        state = run_agent(resumed, None, tools=coding_tools(workspace),
                          middleware=[InboxMiddleware(Inbox(), jobs=workspace.jobs)], store=self.store,
                          session_id="r1", workspace_root=str(self.root), max_steps=4)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(self.starts(), ["start"])
        stored = self.store.load("r1")
        self.assertEqual(sum(1 for message in stored.messages if "summarize when ready" in str(message.get("content"))), 1)
        self.assertTrue(tool_pairs_closed(resumed.seen[0]))
        self.assertEqual(len(workspace.jobs.list()["jobs"]), 1)


def raise_in_thread(thread, exc=KeyboardInterrupt):
    """What a chat Ctrl+C does to the turn thread (ChatSession.interrupt): an asynchronous exception."""
    import ctypes
    return ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread.ident), ctypes.py_object(exc))


def inside_adopt():
    """True when called (directly or not) from JobManager.adopt, whichever thread runs it."""
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_code.co_name == "adopt" and frame.f_code.co_filename.endswith("jobs.py"):
            return True
        frame = frame.f_back
    return False


class RegistrationFailureTests(InterjectionCase):
    """Review of PR 3: a handover must never leave a job without a record, or a running job without a process."""

    def turn(self, workspace, argv, *, timeout=TIMEOUT, interjection=None):
        """Run the command in a separate "turn" thread, as the chat does; returns (thread, outcome dict)."""
        outcome = {}
        handover = TimeoutHandover.for_command(workspace.jobs, argv, self.root)

        def body():
            try:
                outcome["result"] = workspace.backend.execute(argv, timeout=timeout, on_timeout=handover,
                                                              interjection=interjection)
                outcome["annotated"] = handover.annotate(outcome["result"])
            except BaseException as exc:  # noqa: BLE001 -- the test inspects it
                outcome["raised"] = exc
        thread = threading.Thread(target=body, name="chat-turn-test")
        thread.start()
        return thread, outcome

    def assert_slot_free(self, workspace):
        """No phantom running job: with one slot, a new background job still starts."""
        workspace.jobs.max_running = 1
        running = [job for job in workspace.jobs.list()["jobs"] if job["status"] == "running"]
        self.assertEqual(running, [], "a job is still marked running")
        started = workspace.jobs.start([PY, "-c", "print(1)"], self.root)
        self.assertTrue(started["job_id"])

    def test_record_write_failure_refuses_the_handover_and_keeps_the_command(self):
        from unittest.mock import patch as mock_patch
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                self.marker = self.root / f"starts-w{attempt}.txt"
                workspace = self.workspace()
                with mock_patch("baseagent.tools.jobs._write_json", side_effect=OSError("disk full (simulated)")):
                    thread, outcome = self.turn(workspace, self.slow(2.5), interjection=lambda: True)
                    thread.join(30)
                result = outcome["annotated"]
                self.assertTrue(result.ok, result)
                self.assertNotIn("moved_to_background", result.data)        # not reported as moved
                self.assertEqual(result.data["exit_code"], 0)                # ran on and finished by itself
                self.assertIn("could not record the job", result.data["not_moved_on_interjection"])
                self.assertEqual(self.starts(), ["start"])
                self.assertEqual(workspace.jobs.list()["jobs"], [])
                self.assertEqual(self.manager().list()["jobs"], [])          # nothing to find after a rebuild

    def test_record_write_failure_on_timeout_keeps_the_timeout_rule(self):
        from unittest.mock import patch as mock_patch
        workspace = self.workspace()
        with mock_patch("baseagent.tools.jobs._write_json", side_effect=OSError("disk full (simulated)")):
            thread, outcome = self.turn(workspace, self.slow(), timeout=2)
            thread.join(30)
        result = outcome["annotated"]
        self.assertEqual(result.error.code, "timeout")
        self.assertIn("could not record the job", result.error.message)
        self.assertTrue(wait_until(lambda: not alive(self.pid())))
        self.assertEqual(workspace.jobs.list()["jobs"], [])

    def ctrl_c_during(self, target, attempt):
        """Fire an asynchronous Ctrl+C at the turn thread while the handover is inside `target`."""
        from unittest.mock import patch as mock_patch
        self.marker = self.root / f"starts-{target}-{attempt}.txt"
        workspace = self.workspace()
        inside = threading.Event()
        holder = {}
        module, name = target.rsplit(".", 1)
        original = getattr(__import__(module, fromlist=[name]), name)

        def slow_step(*args, **kwargs):
            # Only calls made while a job is being registered: monitors of earlier jobs write records too.
            if inside_adopt():
                inside.set()
                time.sleep(0.6)        # the Ctrl+C arrives while this step runs
            return original(*args, **kwargs)

        with mock_patch(target, slow_step):
            thread, outcome = self.turn(workspace, self.slow(), interjection=lambda: True)
            holder["thread"] = thread
            self.assertTrue(inside.wait(20), "handover never started")
            self.assertEqual(raise_in_thread(thread), 1)
            thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome.get("raised"), KeyboardInterrupt, outcome)
        self.assertTrue(wait_until(lambda: not alive(self.pid())), "process kept running after Ctrl+C")
        self.assertTrue(wait_until(lambda: all(job["status"] != "running" for job in workspace.jobs.list()["jobs"])),
                        f"phantom running job: {workspace.jobs.list()['jobs']}")
        # The handover completed on its helper thread, then the Ctrl+C stopped that job: recorded as killed,
        # also for a rebuilt manager reading the records from disk.
        def mine(manager):   # earlier attempts share the workspace: only this attempt's command
            return [job["status"] for job in manager.list()["jobs"] if str(self.marker) in job["argv"]]
        self.assertEqual(mine(workspace.jobs), ["killed"])
        self.assertEqual(mine(self.manager()), ["killed"])
        self.assert_slot_free(workspace)
        self.assertEqual(self.starts(), ["start"])

    def test_ctrl_c_while_the_job_is_being_registered_leaves_no_running_job(self):
        for target in ("baseagent.tools.jobs.probe_process", "baseagent.tools.jobs._write_json"):
            for attempt in range(3):
                with self.subTest(target=target, attempt=attempt):
                    self.ctrl_c_during(target, attempt)

    def test_ctrl_c_while_a_timeout_handover_registers_leaves_no_running_job(self):
        from unittest.mock import patch as mock_patch
        workspace = self.workspace()
        inside = threading.Event()
        original = __import__("baseagent.tools.jobs", fromlist=["probe_process"]).probe_process

        def slow_probe(*args, **kwargs):
            if inside_adopt():
                inside.set()
                time.sleep(0.6)
            return original(*args, **kwargs)

        with mock_patch("baseagent.tools.jobs.probe_process", slow_probe):
            thread, outcome = self.turn(workspace, self.slow(), timeout=2)
            self.assertTrue(inside.wait(20))
            self.assertEqual(raise_in_thread(thread), 1)
            thread.join(30)
        self.assertIsInstance(outcome.get("raised"), KeyboardInterrupt, outcome)
        self.assertTrue(wait_until(lambda: not alive(self.pid())))
        self.assert_slot_free(workspace)


class FrontendTests(ChatTestCase):
    """AC12: the CLI, TUI and Web send paths of a single-agent chat move a running command the same way."""

    def live_session(self, argv, model, ui):
        """``session`` with the job manager kept open until the test ends.

        The helper replaces the frontend with a function that returns at once, so ``main()`` reaches its
        ``finally`` (which closes the job manager) before the turn runs; in a real run the frontend blocks
        inside ``main()`` for the whole session.
        """
        managers = []
        with patch.object(JobManager, "close", lambda manager: managers.append(manager)):
            session = self.session(argv, model, ui)
        self.addCleanup(lambda: [manager.close() for manager in managers])
        return session

    def test_ac12_a_line_sent_while_the_command_runs_moves_it_and_is_delivered_once(self):
        for ui in ("cli", "tui", "web"):
            with self.subTest(ui=ui):
                require_frontend(self, ui)
                (self.root / "slow.py").write_text(SLOW, encoding="utf-8")
                marker = self.root / f"starts-{ui}.txt"
                pid_file = Path(str(marker) + ".pid")
                model = ChatScript([{"tool_calls": [run([PY, "slow.py", str(marker), LONG])]},
                                    {"content": "moved; reading the log"}])
                session = self.live_session(["--session", f"demo-{ui}", "--no-main-agent", "--allow-command",
                                             "--background-jobs"], model, ui)
                session.open()
                self.assertEqual(session.send("train"), "sent")
                chat_wait(pid_file.exists, message="command never started")
                time.sleep(1.2)
                started = time.monotonic()
                self.assertEqual(session.submit("先停一下，告诉我进度"), "queued")
                self.idle(session)
                self.assertLess(time.monotonic() - started, 15, "the line waited for the command")
                second = model.seen[1]
                self.assertTrue(tool_pairs_closed(second))
                self.assertEqual(second[-1]["content"], f"{STEER_HEADER}\n先停一下，告诉我进度")
                data = tool_data(second, "c1")
                self.assertEqual((data["moved_to_background"], data["handover_reason"]), (True, INTERJECTION))
                self.assertEqual(marker.read_text().splitlines(), ["start"])
                (queued,) = [event for event in self.events(session, "user_message") if event["mode"] == STEER]
                self.assertTrue(queued["queued"])
                (delivered,) = self.events(session, "inbox_delivered")
                self.assertEqual((delivered["ids"], delivered["how"]), ([queued["id"]], "model_call"))
                self.assertEqual(len(model.seen), 2)
                self.assertEqual(len(session.inbox), 0)               # a resync has nothing left to resend
                self.assertNotIn("inbox", self.store().load(f"demo-{ui}").metadata)
                session.exit(force=True, why="test end")
                session.wait(WAIT)


if __name__ == "__main__":
    unittest.main()
