"""job_wait: quiet, interruptible waiting for a background job (BAS-2 job-wait, README AC01-AC14).

Fake models and controlled local processes only: no provider calls. See docs/job_wait.md for the map
from acceptance criteria to tests. Frontends and main agent + workers: test_job_wait_chat.py.
"""

import itertools
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from job_wait_helpers import (Script, Ticks, WaitCase, jw, text_of, tool_call, tool_data, tool_pairs_closed,
                              wait_call, wait_until)
from test_interjection_handover import raise_in_thread
from test_jobs import PY, alive

from baseagent.agent.cancellation import CancellationToken
from baseagent.agent.inbox import (JOB, LEAD, QUEUE, STEER, STEER_HEADER, USER, WORKER, Inbox, InboxMiddleware,
                                   JobOwnerMiddleware, direct_inputs)
from baseagent.agent.interim import InterimAnswerMiddleware, InterimAnswers
from baseagent.agent.state import RunStatus
from baseagent.agent.workers import WorkerManager, WorkerRecord
from baseagent.backends.docker import DockerBackend
from baseagent.kernel.tools import ToolAnnotations, ToolRegistry
from baseagent.testing.docker import FakeDocker
from baseagent.tools.job_links import REFERENCE_HEADER
from baseagent.tools.jobs import JOB_TOOLS, NOTICE_HEADER, JobManager
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.workspace import Workspace, coding_tools


class DecideTests(unittest.TestCase):
    """Every combination of what one look can see has exactly one outcome, and a known end never goes back."""

    def test_all_combinations(self):
        names = ("terminal", "lost", "unknown", "closing", "cancelled", "interjected", "expired")
        for values in itertools.product((False, True), repeat=len(names)):
            facts = dict(zip(names, values))
            outcome = jw.decide(jw.Facts(**facts))
            expected = (jw.CANCELLED if facts["closing"] else
                        (jw.ENDED_UNKNOWN if facts["lost"] else jw.FINISHED) if facts["terminal"] else
                        jw.CANCELLED if facts["cancelled"] else
                        jw.UNKNOWN_STATE if facts["unknown"] else
                        jw.INTERRUPTED if facts["interjected"] else
                        jw.EXPIRED if facts["expired"] else None)
            self.assertEqual(outcome, expected, facts)


class SurfaceTests(WaitCase):
    def test_ac03_tools_exist_only_with_the_flag_and_are_never_retried_or_read_only(self):
        plain = coding_tools(Workspace(self.root, allow_command=True))
        self.assertFalse({"job_wait", "job_settle"} & set(plain._tools))
        tools = coding_tools(self.workspace())
        for name in ("job_wait", "job_settle"):
            self.assertIn(name, JOB_TOOLS)
            self.assertFalse(tools.retry_safe(name))
            self.assertFalse(tools.annotations(name).read_only)
            self.assertFalse(tools.annotations(name).destructive or tools.annotations(name).open_world)
        specs = {spec["function"]["name"]: spec for spec in tools.specs()}
        plain_specs = {spec["function"]["name"]: spec for spec in plain.specs()}
        self.assertEqual(specs["run_command"], plain_specs["run_command"])

    def test_ac03_read_only_workers_get_neither_tool_and_the_main_agent_gets_no_job_tool(self):
        tools = coding_tools(self.workspace())
        manager = WorkerManager(tools, system_prompt="p", store=self.store, runner=None)
        reader = manager.tools_for(WorkerRecord("w1", "s-w1", "t", False))
        writer = manager.tools_for(WorkerRecord("w2", "s-w2", "t", True))
        self.assertEqual({name for name in reader._tools if "job" in name}, {"job_list", "job_output"})
        self.assertTrue({"job_wait", "job_settle"} <= set(writer._tools))
        self.assertEqual(set(manager.bind_tools(tools)._tools), {"delegate_task", "message_worker"})

    def test_ac03_docker_refuses_and_a_policy_still_applies(self):
        backend = DockerBackend(self.root, runner=FakeDocker(), start=False, owner=False)
        refused = coding_tools(Workspace(backend=backend, allow_command=True, background_jobs=True)).execute(
            "job_wait", {"job_id": "j00000000"})
        self.assertEqual(refused.error.code, "permission_denied")
        policy = ToolPolicy({"job_wait": "deny"})
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        state = self.turn(Script([{"tool_calls": [wait_call("w", job_id)]}, {"content": "ok"}]), workspace,
                          tool_policy=policy, session_id="s2")
        self.assertIn("permission_denied", text_of(state.messages))
        self.assertTrue(self.process.alive())

    def test_ac01_unknown_and_invalid_jobs(self):
        tools = coding_tools(self.workspace())
        self.assertEqual(tools.execute("job_wait", {"job_id": "j00000000"}).error.code, "not_found")
        self.assertEqual(tools.execute("job_wait", {"job_id": "nope"}).error.code, "invalid_arguments")
        self.assertEqual(tools.execute("job_settle", {"job_id": "j00000000", "state": "handled"}).error.code, "not_found")
        self.assertEqual(tools.execute("job_list", {}).data["jobs"], [])

    def test_only_harmless_tools_may_declare_how_a_cut_off_call_is_closed(self):
        registry = ToolRegistry()
        for notes in (None, ToolAnnotations(), ToolAnnotations(destructive=False, open_world=True)):
            with self.assertRaises(ValueError):
                registry.register("t", "t", {"type": "object", "properties": {}}, lambda: None, annotations=notes,
                                  on_interrupted=lambda arguments, context=None: {})
        self.assertNotIn("t", registry._tools)


class WaitingTests(WaitCase):
    def test_ac01_ac04_quiet_until_the_job_ends_same_process_started_once(self):
        workspace = self.own(self.workspace())
        seen_at = {}
        ticks = self.ticks({20: lambda: seen_at.update(calls=len(model.seen), pid=self.process.started()),
                            30: lambda: self.finish(workspace)})
        model = Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv})]},
                        lambda messages: {"tool_calls": [wait_call("c2", self.jid(workspace))]}, {"content": "done"}])
        state = self.turn(model, workspace)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual((seen_at["calls"], len(model.seen), state.model_calls), (2, 3, 3))   # 0 calls while waiting
        self.assertGreaterEqual(ticks.count, 30)
        data = tool_data(model.seen[2], "c2")
        self.assertEqual((data["outcome"], data["job"]["status"], data["job_succeeded"]), ("finished", "exited", True))
        self.assertIn("last line", data["log_tail"])
        self.assertEqual(data["job"]["pid"], tool_data(model.seen[1], "c1")["pid"])
        self.assertEqual(self.process.starts(), 1)
        self.assertEqual(seen_at["pid"], self.process.started())
        # The wait's result and the notice state the same fact; only one of them is announced.
        self.assertNotIn(f"- {data['job']['job_id']} ", text_of(model.seen[2]))

    def test_ac05_interrupted_then_answered_then_the_same_job_is_waited_for_again(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace, purpose="measure accuracy", expect="a number", next_step="compare it")
        pid = self.process.started()
        facts = {}
        self.ticks({3: lambda: self.inbox.put(USER, "用的哪个数据集？"), 8: lambda: facts.update(alive=self.process.alive()),
                    12: lambda: self.finish(workspace)})
        model = Script([self.first(job_id),
                        {"content": "用的是 A 数据集。", "tool_calls": [wait_call("w2", job_id, still_needed="accuracy still wanted")]},
                        {"content": "accuracy is 0.9"}])
        state = self.turn(model, workspace, "wait for it", answers=self.printing())
        self.assertEqual(state.status, RunStatus.COMPLETED)
        second = model.seen[1]
        self.assertTrue(tool_pairs_closed(second))
        first = tool_data(second, "w1")
        self.assertEqual((first["outcome"], first["job"]["status"], first["job_succeeded"]), ("interrupted", "running", None))
        self.assertEqual(second[-1]["content"], f"{STEER_HEADER}\n用的哪个数据集？")
        self.assertTrue(facts["alive"])
        again = tool_data(model.seen[2], "w2")
        self.assertEqual(again["outcome"], "finished")                  # no extra user line was needed
        self.assertEqual(again["brief"]["purpose"], "measure accuracy")
        self.assertTrue(again["answer_shown"])
        self.assertEqual([text for _, text in self.published], ["用的是 A 数据集。"])
        self.assertLessEqual(again["answer_shown_at"], again["waiting_since"])
        self.assertEqual((self.process.starts(), self.process.started(), len(model.seen)), (1, pid, 3))

    def test_ac05_rest_of_the_batch_runs_before_the_message_is_delivered(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.ticks({2: lambda: self.inbox.put(USER, "hello")})
        quick = tool_call("c2", "run_command", {"argv": [PY, "-c", "print('quick')"]})
        model = Script([{**self.first(job_id), "tool_calls": [*self.first(job_id)["tool_calls"], quick]}, {"content": "hi"}])
        self.turn(model, workspace, "wait")
        self.assertEqual([m["role"] for m in model.seen[1]][-4:], ["assistant", "tool", "tool", "user"])
        self.assertEqual(tool_data(model.seen[1], "c2")["stdout"].strip(), "quick")

    def test_ac06_only_messages_for_this_agent_end_the_wait(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        other = Inbox()

        def noise():
            self.inbox.put(USER, "next turn", QUEUE)
            self.inbox.put(WORKER, '{"task_id": "w1"}')
            self.inbox.put(JOB, "- j00000000 exited")
            other.put(USER, "for somebody else")
            other.put(LEAD, "for a worker")
        self.ticks({2: noise, 15: lambda: self.finish(workspace)})
        model = Script([self.first(job_id), {"content": "ok"}])
        self.turn(model, workspace, "wait")
        self.assertEqual(tool_data(model.seen[1], "w1")["outcome"], "finished")
        self.assertEqual([item.text for item in self.inbox.pending()], ["next turn"])
        self.assertEqual(len(other), 2)

    def test_ac06_a_worker_yields_to_tell(self):
        workspace = self.own(self.workspace(), "main")
        self.ticks({3: lambda: self.inbox.put(LEAD, "also check the log"), 9: lambda: self.finish(workspace)})
        layers = [type("Tag", (InboxMiddleware,), {})(self.inbox, jobs=workspace.jobs)]
        model = Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv})]},
                        lambda messages: {"tool_calls": [wait_call("w1", self.jid(workspace))]},
                        # A worker's text is not the user's answer: a reason is enough to wait again.
                        lambda messages: {"tool_calls": [wait_call("w2", self.jid(workspace), still_needed="task needs it")]},
                        {"content": "report"}])
        from baseagent.main import SessionTagMiddleware
        state = self.turn(model, workspace, session_id="main-w1",
                          middleware=[SessionTagMiddleware(session_mode="worker"), *layers])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(tool_data(model.seen[2], "w1")["outcome"], "interrupted")
        self.assertEqual(tool_data(model.seen[3], "w2")["outcome"], "finished")

    def test_ac07_expiry_is_not_a_failure_and_waiting_again_is_possible(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        model = Script([self.first(job_id, timeout_seconds=1),
                        {"tool_calls": [wait_call("w2", job_id, timeout_seconds=30)]}, {"content": "done"}])
        expired = {}

        def after_first():
            if len(model.seen) == 2 and not expired:
                expired.update(alive=self.process.alive(), status=workspace.jobs.describe(job_id)["status"])
                self.finish(workspace)
        with patch.object(jw, "_on_tick", lambda job: after_first()):
            state = self.turn(model, workspace, "wait")
        first = tool_data(model.seen[1], "w1")
        self.assertEqual((first["outcome"], first["job"]["status"], first["job_succeeded"]), ("wait_expired", "running", None))
        self.assertNotIn("timeout", json.dumps(first["job"]))
        self.assertEqual(expired, {"alive": True, "status": "running"})
        self.assertEqual(tool_data(model.seen[2], "w2")["outcome"], "finished")
        self.assertEqual((state.status, self.process.starts()), (RunStatus.COMPLETED, 1))

    def test_ac03_ac07_the_turn_deadline_caps_the_wait_and_keeps_its_own_status(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        model = Script([self.first(job_id, timeout_seconds=600), {"content": "never"}])
        started = time.monotonic()
        state = self.turn(model, workspace, "wait", max_duration_seconds=2)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(state.status, RunStatus.DEADLINE_EXCEEDED)
        self.assertEqual(len(model.seen), 1)
        data = tool_data(state.messages, "w1")
        self.assertEqual((data["outcome"], data["limit"]), ("wait_expired", "the turn's remaining time"))
        self.assertTrue(self.process.alive())

    def test_ac03_a_wait_counts_like_any_tool_call(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.ticks({2: lambda: self.finish(workspace)})
        state = self.turn(Script([self.first(job_id), {"content": "ok"}]), workspace, "wait",
                          max_tool_calls=1)
        self.assertEqual((state.tool_calls, state.unsafe_tool_calls, state.step), (1, 1, 2))
        model = Script([{"tool_calls": [wait_call("w2", job_id)]}, {"content": "never"}])
        state = self.turn(model, workspace, "again", max_tool_calls=0)
        self.assertEqual(state.status, RunStatus.MAX_TOOL_CALLS_EXCEEDED)


class RaceTests(WaitCase):
    def test_ac08_finished_before_the_wait(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.inbox.put(USER, "and then?")      # waiting in the inbox too: delivered with the turn's first request
        self.finish(workspace)
        model = Script([self.first(job_id), {"content": "ok"}])
        self.turn(model, workspace, "wait")
        self.assertIn(f"- {job_id} exited", model.seen[0][-1]["content"])      # announced by the first request
        self.assertIn("and then?", model.seen[0][-1]["content"])
        self.assertEqual(tool_data(model.seen[1], "w1")["outcome"], "finished")
        self.assertEqual(text_of(model.seen[1]).count(f"- {job_id} exited"), 1)

    def test_ac08_finished_and_interrupted_in_the_same_look(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)

        def both():
            self.inbox.put(USER, "stop waiting")
            self.finish(workspace)
        self.ticks({4: both})
        model = Script([self.first(job_id), {"content": "ok"}])
        state = self.turn(model, workspace, "wait")
        self.assertEqual(tool_data(model.seen[1], "w1")["outcome"], "finished")
        self.assertEqual(model.seen[1][-1]["content"], f"{STEER_HEADER}\nstop waiting")
        self.assertEqual(text_of(state.messages).count(f"- {job_id} "), 0)     # told once, by the wait
        self.assertEqual(len(model.seen), 2)

    def test_ac08_interrupted_first_finished_before_the_next_request(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.ticks({3: lambda: self.inbox.put(USER, "stop waiting")})

        class FinishLater(InboxMiddleware):
            def after_tool(inner, request, result):
                if request.name == "job_wait":
                    self.finish(workspace)
        model = Script([self.first(job_id), {"content": "ok"}])
        state = self.turn(model, workspace, "wait", middleware=[FinishLater(self.inbox, jobs=workspace.jobs, settles=True),
                                                                InterimAnswerMiddleware(self.printing())])
        self.assertEqual(tool_data(model.seen[1], "w1")["job"]["status"], "running")      # true when it was said
        last = model.seen[1][-1]["content"]
        self.assertTrue(last.startswith(NOTICE_HEADER))
        self.assertIn(f"- {job_id} exited", last)
        self.assertTrue(last.endswith(f"{STEER_HEADER}\nstop waiting"))
        self.assertEqual(text_of(state.messages).count(f"- {job_id} exited"), 1)

    def test_ac08_finished_and_cancelled_in_the_same_look_keeps_the_end(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        token = CancellationToken()

        def both():
            token.cancel()
            self.finish(workspace)
        self.ticks({4: both})
        model = Script([self.first(job_id), {"content": "never"}])
        state = self.turn(model, workspace, "wait", cancellation=token)
        self.assertEqual((state.status, len(model.seen)), (RunStatus.CANCELLED, 1))
        self.assertEqual(tool_data(state.messages, "w1")["outcome"], "finished")
        # Resumed: told by the saved result, never announced as a new completion.
        resumed = Script([{"content": "ok"}])
        state = self.turn(resumed, self.own(self.workspace()), None, inbox=Inbox())
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(text_of(state.messages).count(f"- {job_id} "), 0)
        self.assertEqual(self.link(job_id)["delivery"]["settled"]["as"], "reported")

    def test_ac08_a_result_that_arrives_while_the_model_answers_waits_for_the_next_request(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)

        def answering(messages):
            self.finish(workspace)
            return {"content": "answer"}
        model = Script([answering, {"content": "handled"}])
        self.turn(model, workspace, "question")
        self.assertEqual(len(model.seen), 1)                                   # no turn or request of its own
        self.turn(model, workspace, "next")
        self.assertIn(f"- {job_id} exited", model.seen[1][-1]["content"])
        self.assertEqual(len(model.seen), 2)


class ReportingTests(WaitCase):
    def finished(self, code=0, **manager):
        workspace = self.workspace()
        for key, value in manager.items():
            setattr(workspace.jobs, key, value)
        job_id = self.started_job(workspace)
        self.ticks({2: lambda: self.finish(workspace, code)})
        model = Script([self.first(job_id), {"content": "ok"}])
        self.turn(model, workspace, "wait")
        return workspace, job_id, tool_data(model.seen[1], "w1")

    def test_ac09_nonzero_exit_is_not_success(self):
        _, _, data = self.finished(3)
        self.assertEqual((data["outcome"], data["job"]["status"], data["job"]["exit_code"], data["job_succeeded"]),
                         ("finished", "failed", 3, False))

    def test_ac09_ac12_job_kill_ends_the_wait_with_the_real_state(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.ticks({3: lambda: workspace.jobs.kill(job_id)})
        model = Script([self.first(job_id), {"content": "ok"}])
        self.turn(model, workspace, "wait")
        data = tool_data(model.seen[1], "w1")
        self.assertEqual((data["outcome"], data["job"]["status"], data["job_succeeded"]), ("finished", "killed", False))
        self.assertTrue(wait_until(lambda: not self.process.alive()))

    def test_ac09_lost_and_unknown_are_neither_success_nor_failure(self):
        workspace = self.own(self.workspace())
        job_id = self.started_job(workspace)
        self.finish(workspace)
        record = self.record(job_id)
        for status_case, fields, outcome in (
                ("lost", {"leader_pid": record["leader_pid"], "leader_identity": "win:1"}, "ended_unknown"),
                ("unknown", {"leader_pid": os.getpid(), "leader_identity": None}, "unknown_state")):
            with self.subTest(status_case):
                path = self.root / ".baseagent" / "jobs" / f"{job_id}.json"
                path.write_text(json.dumps({**record, "status": "running", "exit_code": None, **fields}), encoding="utf-8")
                fresh = self.own(self.workspace())
                started = time.monotonic()
                data = jw.job_wait(fresh.jobs, job_id, context=self.context()).data
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual((data["outcome"], data["job"]["status"], data["job_succeeded"], data["job"]["exit_code"]),
                                 (outcome, status_case, None, None))
                self.assertIn("not", data["message"])

    def test_ac09_truncated_and_missing_logs_are_said(self):
        _, _, data = self.finished(0, max_log_bytes=5)
        self.assertIn("truncated", data["log_note"])
        workspace = self.own(self.workspace())
        (job,) = workspace.jobs.list()["jobs"]
        (self.root / ".baseagent" / "jobs" / f"{job['job_id']}.log").unlink()
        data = jw.job_wait(workspace.jobs, job["job_id"], context=self.context()).data
        self.assertIn("missing", data["log_note"])
        self.assertEqual(data["log_tail"], "")

    def context(self, session_id="s1"):
        state = type("S", (), {"session_id": session_id, "messages": [], "turn_start": 0, "metadata": {}, "turn_id": "t"})()
        return type("C", (), {"state": state, "middleware": (), "cancellation": None, "remaining_seconds": None,
                              "call_id": "w"})

    def test_results_stay_below_the_context_clip(self):
        workspace = self.workspace()
        wide = "目" * 200
        job_id = self.started_job(workspace, purpose=wide, expect=wide, next_step=wide)
        workspace.jobs._get(job_id).argv.append("长" * 2000)
        self.ticks({2: lambda: self.finish(workspace)})
        model = Script([self.first(job_id), {"content": "ok"}])
        self.turn(model, workspace, "等" * 900)
        content = next(m["content"] for m in model.seen[1] if m.get("tool_call_id") == "w1")
        self.assertLess(len(content.encode("utf-8")), 4000)
        self.assertNotIn("context_truncated", content)


class GoalTests(WaitCase):
    def test_ac02_purpose_is_declared_once_and_never_made_up(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        link = self.link(job_id)
        self.assertEqual((link["purpose"], link["expect"], link["next_step"]), (None, None, None))
        self.assertEqual((link["agent_session"], link["origin"]["call_id"]), ("s1", "start"))
        self.assertEqual(link["origin"]["request"], "train the model")             # the user's words, marked as such
        self.ticks({3: lambda: self.inbox.put(USER, "hm"), 8: lambda: self.finish(workspace)})
        model = Script([self.first(job_id, purpose="baseline"),
                        {"content": "yes", "tool_calls": [wait_call("w2", job_id, purpose="something else",
                                                                     next_step="compare", still_needed="baseline needed")]},
                        {"content": "ok"}])
        self.turn(model, workspace, "wait")
        link = self.link(job_id)
        self.assertEqual((link["purpose"], link["next_step"], link["expect"]), ("baseline", "compare", None))
        self.assertEqual(tool_data(model.seen[2], "w2")["brief"]["purpose"], "baseline")

    def test_ac02_a_moved_command_keeps_the_users_words_and_the_responsible_agent(self):
        workspace = self.own(self.workspace())
        self.type_when_running(self.inbox, (USER, "what is it doing?", STEER))
        model = Script([{"tool_calls": [tool_call("c1", "run_command", {"argv": self.slow(), "timeout": 50})]},
                        {"content": "training"}])
        self.turn(model, workspace, "train and tell me the loss")
        job_id = tool_data(model.seen[1], "c1")["job_id"]
        link = self.link(job_id)
        self.assertEqual((link["agent_session"], link["origin"]["request"]), ("s1", "train and tell me the loss"))
        self.assertIsNone(link["purpose"])

    def test_ac10_a_newer_input_must_be_confirmed_and_both_are_visible(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace, purpose="train model A", next_step="deploy A")
        model = Script([{"tool_calls": [wait_call("w1", job_id)]},
                        {"tool_calls": [wait_call("w2", job_id, still_needed="yes")]},
                        {"content": "A is off, B it is"}])
        self.turn(model, workspace, "forget A, look at B")
        refused = tool_data(model.seen[1], "w1")
        self.assertEqual((refused["outcome"], refused["brief"]["purpose"]), ("confirm_first", "train model A"))
        self.assertEqual(refused["user_said_since_confirmed"], ["forget A, look at B"])
        self.assertEqual(len(refused["missing"]), 2)
        self.assertEqual(len(tool_data(model.seen[2], "w2")["missing"]), 1)      # a reason alone is not an answer
        self.assertIn('"user_inputs_since_confirmed": 1', text_of(model.seen[0]))
        self.assertEqual(self.link(job_id)["goal"]["confirmed_inputs"], 1)

    def test_ac10_dropping_the_goal_touches_neither_the_process_nor_the_result(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace, purpose="train A", next_step="deploy A")
        model = Script([{"tool_calls": [tool_call("d", "job_settle", {"job_id": job_id, "state": "dropped",
                                                                       "note": "user wants B"})]},
                        {"tool_calls": [wait_call("w1", job_id)]},
                        {"content": "ok, B"}])
        self.turn(model, workspace, "forget A, do B")
        self.assertEqual(tool_data(model.seen[1], "d")["process"], "not touched")
        self.assertEqual(tool_data(model.seen[2], "w1")["outcome"], "goal_dropped")
        self.assertTrue(self.process.alive())
        self.assertIsNone(self.link(job_id)["delivery"]["settled"])
        self.finish(workspace)
        later = Script([{"content": "B continues"}])
        self.turn(later, workspace, "how is B?")
        notice = later.seen[0][-1]["content"]
        self.assertIn(f"- {job_id} exited", notice)
        self.assertIn("goal was dropped", notice)
        self.assertIn("train A", notice)
        self.assertEqual(self.link(job_id)["delivery"]["settled"]["as"], "reported")     # nothing left to do
        self.assertEqual(self.process.starts(), 1)

    def test_ac10_a_dropped_goal_needs_a_new_user_input_to_come_back(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace, purpose="train A")
        model = Script([{"tool_calls": [tool_call("d", "job_settle", {"job_id": job_id, "state": "dropped", "note": "B"})]},
                        {"content": "x", "tool_calls": [wait_call("w1", job_id, still_needed="I changed my mind")]},
                        {"content": "B"}])
        self.turn(model, workspace, "do B")
        self.assertEqual(tool_data(model.seen[2], "w1")["outcome"], "goal_dropped")
        self.ticks({2: lambda: self.finish(workspace)})
        model = Script([{"content": "back to A", "tool_calls": [wait_call("w2", job_id, still_needed="user wants A again")]},
                        {"content": "done"}])
        self.turn(model, workspace, "no, A after all")
        self.assertEqual(tool_data(model.seen[1], "w2")["outcome"], "finished")
        self.assertEqual(self.link(job_id)["goal"]["standing"], "active")

    def test_a_refused_wait_is_identical_each_time_so_the_loop_guard_sees_it(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        model = Script([{"tool_calls": [wait_call(f"w{index}", job_id)]} for index in range(5)])
        from baseagent.middleware.execution_guidance import ExecutionGuidanceMiddleware
        state = self.turn(model, workspace, "new question", middleware=[
            ExecutionGuidanceMiddleware(workspace), InboxMiddleware(self.inbox, jobs=workspace.jobs, settles=True),
            InterimAnswerMiddleware(self.printing())])
        self.assertEqual((state.status, len(model.seen)), (RunStatus.TOOL_LOOP_DETECTED, 3))


class DisplayTests(WaitCase):
    """The answer must be on the user's screen before the next wait begins; text in the output is not enough."""

    def interrupted(self, workspace, answers, actions, still="yes", **wait):
        job_id = self.started_job(workspace)
        self.ticks({2: lambda: self.inbox.put(USER, "question?"), **actions})
        model = Script([self.first(job_id),
                        {"content": "ANSWER", "tool_calls": [wait_call("w2", job_id, still_needed=still, **wait)]},
                        {"content": "done"}])
        state = self.turn(model, workspace, "wait", answers=answers)
        return job_id, model, state

    def test_waiting_begins_only_after_the_frontend_confirms(self):
        workspace = self.workspace()
        answers, seen = self.gui(), {}
        phase = lambda: workspace.jobs.waits()[0]["phase"]
        job = {}

        def unconfirmed():
            job["id"] = self.jid(workspace)
            seen["before"] = (phase(), [text for _, text in self.published],
                              self.link(job["id"])["goal"]["confirmed_inputs"])
            answers.mark_shown([999])           # some other output (for example a side conversation's)
        actions = {10: unconfirmed, 12: lambda: seen.update(other=phase()), 13: lambda: answers.mark_shown([1]),
                   16: lambda: seen.update(after=(phase(), self.link(job["id"])["goal"]["confirmed_inputs"])),
                   18: lambda: self.finish(workspace)}
        job_id, model, state = self.interrupted(workspace, answers, actions)
        self.assertEqual(seen["before"], ("showing_answer", ["ANSWER"], 2))      # shown to nobody yet: not confirmed
        self.assertEqual(seen["other"], "showing_answer")
        self.assertEqual(seen["after"], ("waiting", 3))
        data = tool_data(model.seen[2], "w2")
        self.assertEqual((data["outcome"], data["answer_shown"]), ("finished", True))
        self.assertLessEqual(data["answer_shown_at"], data["waiting_since"])
        self.assertEqual(len(self.published), 1)                                 # shown once, sent to the model once
        self.assertEqual(len(model.seen), 3)

    def test_an_answer_nobody_saw_never_starts_the_wait(self):
        workspace = self.workspace()
        job_id, model, state = self.interrupted(workspace, self.gui(), {}, timeout_seconds=1)
        data = tool_data(model.seen[2], "w2")
        self.assertEqual((data["outcome"], data["answer_shown"], data["waiting_since"]), ("wait_expired", False, None))
        self.assertIn("never began", data["note"])
        self.assertEqual(self.link(job_id)["goal"]["confirmed_inputs"], 2)
        self.assertTrue(self.process.alive())

    def test_a_result_or_a_message_is_not_held_back_while_the_answer_is_unconfirmed(self):
        workspace = self.workspace()
        job_id, model, _ = self.interrupted(workspace, self.gui(), {8: lambda: self.finish(workspace)})
        data = tool_data(model.seen[2], "w2")
        self.assertEqual((data["outcome"], data["answer_shown"]), ("finished", False))

    def test_a_second_message_while_the_answer_is_unconfirmed(self):
        workspace = self.workspace()
        job_id, model, _ = self.interrupted(workspace, self.gui(), {8: lambda: self.inbox.put(USER, "one more")})
        self.assertEqual(tool_data(model.seen[2], "w2")["outcome"], "interrupted")
        self.assertIn("one more", model.seen[2][-1]["content"])

    def test_without_any_frontend_the_wait_is_refused(self):
        workspace = self.workspace()
        job_id, model, _ = self.interrupted(workspace, InterimAnswers(), {})
        data = tool_data(model.seen[2], "w2")
        self.assertEqual(data["outcome"], "confirm_first")
        self.assertIn("no frontend", data["missing"][0])

    def test_an_answer_given_before_other_tool_calls_is_shown_too(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.ticks({2: lambda: self.inbox.put(USER, "log?"), 6: lambda: self.finish(workspace)})
        model = Script([self.first(job_id),
                        {"content": "let me look", "tool_calls": [tool_call("o", "job_output", {"job_id": job_id})]},
                        {"tool_calls": [wait_call("w2", job_id, still_needed="yes")]}, {"content": "done"}])
        self.turn(model, workspace, "wait")
        self.assertEqual(tool_data(model.seen[3], "w2")["outcome"], "finished")
        self.assertEqual([text for _, text in self.published], ["let me look"])


class DeliveryTests(WaitCase):
    """Taken, received and handled are separate; what was taken and lost is found again."""

    def test_stages_and_the_open_follow_up(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace, purpose="train", next_step="write the report")
        stage = lambda: workspace.jobs.overview()[0]["delivery"]
        self.assertEqual(stage(), "running")
        self.finish(workspace)
        self.assertEqual((stage(), self.record(job_id)["notified"]), ("finished", False))
        seen = {}

        def reply(messages):
            seen["during"] = (stage(), self.link(job_id)["delivery"]["received"])
            return {"content": "will do"}
        self.turn(Script([reply]), workspace, "go on")
        self.assertEqual(seen["during"], ("taken", []))                  # in the request, but not received yet
        self.assertEqual(stage(), "received")                            # the stated next step is still open
        self.assertEqual([item["session"] for item in self.link(job_id)["delivery"]["received"]], ["s1"])
        model = Script([{"tool_calls": [tool_call("h", "job_settle", {"job_id": job_id, "state": "handled"})]},
                        {"content": "report written"}])
        self.turn(model, workspace, "did you?")
        self.assertIn('"delivery": "received"', text_of(model.seen[0]))  # rediscovered: not in this turn's history
        self.assertNotIn(REFERENCE_HEADER, text_of(model.seen[1]))
        self.assertEqual(stage(), "handled")
        self.assertEqual(text_of(self.store.load("s1").messages).count(f"- {job_id} exited"), 1)

    def test_without_a_stated_follow_up_an_answered_turn_is_enough(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.finish(workspace)
        self.turn(Script([{"content": "it finished"}]), workspace, "and?")
        self.assertEqual(self.link(job_id)["delivery"]["settled"]["as"], "reported")
        model = Script([{"content": "bye"}])
        self.turn(model, workspace, "thanks")
        self.assertNotIn(job_id, text_of(model.seen[0][-1:]))
        self.assertNotIn(REFERENCE_HEADER, text_of(model.seen[0]))

    def test_a_notice_taken_and_lost_in_a_crash_is_found_again_once(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.finish(workspace)
        # Taken and recorded as announced, then the process died before the session checkpoint.
        self.assertEqual([item["job_id"] for item in workspace.jobs.take_notices()], [job_id])
        self.assertTrue(self.record(job_id)["notified"])
        restarted = self.own(self.workspace())
        self.assertEqual(restarted.jobs.take_notices(), [])                  # the old path alone would lose it
        model = Script([{"content": "seen"}, {"content": "bye"}])
        self.turn(model, restarted, "hello again", inbox=Inbox())
        self.assertIn(f"- {job_id} exited", model.seen[0][-1]["content"])
        self.turn(model, self.own(self.workspace()), "and now?", inbox=Inbox())
        self.assertEqual(text_of(self.store.load("s1").messages).count(f"- {job_id} exited"), 1)

    def test_a_notice_in_the_saved_request_is_not_repeated_and_counts_after_the_reply(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.finish(workspace)
        state = self.turn(Script([KeyboardInterrupt()]), workspace, "go on")        # Ctrl+C during the request
        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        self.assertEqual(self.link(job_id)["delivery"]["received"], [])
        model = Script([{"content": "ok"}])
        self.turn(model, self.own(self.workspace()), None, inbox=Inbox())
        self.assertEqual(text_of(model.seen[0]).count(f"- {job_id} exited"), 1)
        self.assertEqual([item["session"] for item in self.link(job_id)["delivery"]["received"]], ["s1"])

    def test_a_wait_result_that_was_never_committed_leaves_the_notice(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.ticks({2: lambda: self.finish(workspace)})
        original = type(self.store).complete_call
        calls = []

        def cut(store, state, call_id, result):
            if call_id == "w1" and not calls:
                calls.append(call_id)
                raise KeyboardInterrupt()
            return original(store, state, call_id, result)
        with patch.object(type(self.store), "complete_call", cut):
            state = self.turn(Script([self.first(job_id)]), workspace, "wait")
        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        model = Script([{"content": "ok"}])
        state = self.turn(model, self.own(self.workspace()), None, inbox=Inbox())
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(tool_data(model.seen[0], "w1")["outcome"], "finished")     # the result it had returned
        self.assertNotIn(f"- {job_id} exited", text_of(model.seen[0]))
        self.assertEqual(self.process.starts(), 1)

    def test_handled_needs_the_result_and_dropped_needs_a_reason(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        settle = lambda call, **args: tool_call(call, "job_settle", {"job_id": job_id, **args})
        model = Script([{"tool_calls": [settle("a", state="handled"), settle("b", state="dropped")]}, {"content": "ok"}])
        state = self.turn(model, workspace, "x")
        results = [json.loads(m["content"])["ok"] for m in state.messages if m.get("tool_call_id") in ("a", "b")]
        self.assertEqual(results, [False, False])
        self.assertIsNone(self.link(job_id)["delivery"]["settled"])

    def test_a_legacy_record_is_not_announced_again_and_says_nothing_it_does_not_know(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.finish(workspace)
        workspace.jobs.take_notices()
        (self.root / ".baseagent" / "jobs" / f"{job_id}.link.json").unlink()
        model = Script([{"content": "ok"}])
        self.turn(model, self.own(self.workspace()), "x", session_id="s1", inbox=Inbox())
        self.assertNotIn(job_id, text_of(model.seen[0][-1:]))


class OwnershipTests(WaitCase):
    def agent_layers(self, workspace, **options):
        return [InboxMiddleware(Inbox(), jobs=workspace.jobs, **options)]

    def test_the_result_goes_to_the_agent_that_started_the_job_whoever_calls_first(self):
        workspace = self.own(self.workspace(), "main")
        worker = Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv})]}, KeyboardInterrupt()])
        self.turn(worker, workspace, "task", session_id="main-w1", middleware=self.agent_layers(workspace))
        job_id = self.jid(workspace)
        self.finish(workspace)
        active = {"main-w1"}
        main_layers = lambda: self.agent_layers(workspace, active=lambda sid: sid in active)
        # The main agent, another worker and a side conversation all call a model first.
        for session_id, layers in (("main", main_layers()), ("main-w2", self.agent_layers(workspace)),
                                   ("main-side", self.agent_layers(workspace))):
            model = Script([{"content": "nothing for me"}])
            self.turn(model, workspace, "hi", session_id=session_id, middleware=layers)
            self.assertNotIn(job_id, text_of(model.seen[0]), session_id)
        self.assertFalse(self.record(job_id)["notified"])
        model = Script([{"content": "report"}])
        self.turn(model, workspace, None, session_id="main-w1", middleware=self.agent_layers(workspace))
        self.assertIn(f"- {job_id} exited", model.seen[0][-1]["content"])
        self.assertEqual(self.link(job_id)["delivery"]["settled"],
                         {**self.link(job_id)["delivery"]["settled"], "as": "reported", "session": "main-w1"})
        model = Script([{"content": "nothing new"}])
        self.turn(model, workspace, "again", session_id="main", middleware=main_layers())
        self.assertNotIn(job_id, text_of(model.seen[0]))

    def test_the_main_agent_takes_over_from_a_worker_that_ended_once(self):
        workspace = self.own(self.workspace(), "main")
        worker = Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv, "purpose": "train"})]},
                         {"content": "started it, not waiting"}])
        self.turn(worker, workspace, "task", session_id="main-w1", middleware=self.agent_layers(workspace))
        job_id = self.jid(workspace)
        self.finish(workspace)
        layers = lambda: self.agent_layers(workspace, active=lambda sid: False)
        model = Script([{"content": "the training ended"}, {"content": "bye"}])
        self.turn(model, workspace, "status?", session_id="main", middleware=layers())
        notice = model.seen[0][-1]["content"]
        self.assertIn(f"- {job_id} exited", notice)
        self.assertIn("main-w1, which has ended", notice)
        self.assertIn("purpose: train", notice)
        self.turn(model, workspace, "ok", session_id="main", middleware=layers())
        self.assertNotIn(job_id, text_of(model.seen[1][-1:]))

    def test_another_agent_cannot_wait_for_it_or_settle_it(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        model = Script([{"tool_calls": [wait_call("w1", job_id), tool_call("d", "job_settle", {
            "job_id": job_id, "state": "dropped", "note": "mine now"})]}, {"content": "ok"}])
        state = self.turn(model, workspace, "take it", session_id="side", inbox=Inbox())
        self.assertEqual(tool_data(state.messages, "w1")["outcome"], "not_responsible")
        self.assertIn("permission_denied", text_of(state.messages))
        self.assertEqual(self.link(job_id)["goal"]["standing"], "active")

    def test_a_session_without_a_name_owns_its_jobs(self):
        workspace = self.workspace()
        self.assertIsNone(workspace.jobs.owner)
        model = Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv})]}, {"content": "ok"}])
        state = self.turn(model, workspace, "go", session_id=None, middleware=[
            JobOwnerMiddleware(workspace.jobs), InboxMiddleware(Inbox(), jobs=workspace.jobs)])
        job_id = self.jid(workspace)
        self.assertEqual(self.record(job_id)["owner_session"], state.session_id)
        self.finish(workspace)
        # Another unnamed session gets nothing; the same session, reopened by its id, gets its notice.
        model = Script([{"content": "a"}])
        other = self.workspace()
        self.turn(model, other, "hi", session_id=None, middleware=[JobOwnerMiddleware(other.jobs),
                                                                   InboxMiddleware(Inbox(), jobs=other.jobs)])
        self.assertNotIn(job_id, text_of(model.seen[0]))
        model = Script([{"content": "b"}])
        self.turn(model, self.own(self.workspace(), state.session_id), "back", session_id=state.session_id, inbox=Inbox())
        self.assertIn(f"- {job_id} exited", model.seen[0][-1]["content"])

    def test_the_reference_is_a_frozen_text_that_takes_nothing(self):
        """What a side conversation would freeze: facts public now, unchanged later, and no notice is taken."""
        workspace = self.workspace()
        job_id = self.started_job(workspace, purpose="train", next_step="compare")
        layer = InboxMiddleware(self.inbox, jobs=workspace.jobs, settles=True)
        state = self.store.load("s1")
        before = json.dumps(state.to_dict(), default=str)
        frozen = layer.reference(state)
        for fact in (job_id, "running", "train", "compare", '"responsible": "s1"'):
            self.assertIn(fact, frozen)
        self.finish(workspace)
        self.assertIn("exited", layer.reference(state))               # the main line's next request is current
        self.assertIn('"status": "running"', frozen)                  # the frozen one is not touched
        self.assertEqual((self.record(job_id)["notified"], self.link(job_id)["delivery"]["received"]), (False, []))
        self.assertEqual(json.dumps(state.to_dict(), default=str), before)   # never part of the conversation

    def test_the_reference_is_added_per_request_and_never_stored(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace, purpose="train")
        model = Script([{"content": "ok"}])
        self.turn(model, workspace, "hi")
        (reference,) = [m for m in model.seen[0] if str(m.get("content")).startswith(REFERENCE_HEADER)]
        self.assertEqual(reference["role"], "user")                   # labelled data, never a system instruction
        self.assertEqual(model.seen[0][-1]["content"], "hi")
        self.assertNotIn(REFERENCE_HEADER, text_of(self.store.load("s1").messages))
        self.assertEqual([text for _, text in direct_inputs(self.store.load("s1").messages)], ["train the model", "hi"])
        self.assertIn(job_id, reference["content"])


class RecoveryTests(WaitCase):
    def interrupted_by_ctrl_c(self, workspace, job_id, model):
        """Run the turn in its own thread and raise Ctrl+C there once the wait is under way."""
        outcome, entered = {}, threading.Event()
        with patch.object(jw, "_on_tick", lambda job: entered.set()):
            thread = threading.Thread(target=lambda: outcome.update(state=self.turn(model, workspace, "wait")))
            thread.start()
            self.assertTrue(entered.wait(20))
            started = time.monotonic()
            raise_in_thread(thread)
            thread.join(20)
        self.assertLess(time.monotonic() - started, 5)
        return outcome["state"]

    def test_ac12_ctrl_c_ends_the_wait_not_the_job_and_the_session_is_usable(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        pid = self.process.started()
        state = self.interrupted_by_ctrl_c(workspace, job_id, Script([self.first(job_id)]))
        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        self.assertEqual(workspace.jobs.waits(), [])
        self.assertTrue(alive(pid))
        # Resume: closed with facts, not needs_recovery; nothing waits or starts by itself.
        model = Script([{"tool_calls": [wait_call("w2", job_id)]}, {"content": "still running"}])
        state = self.turn(model, workspace, None)
        self.assertEqual((state.status, state.blocked_tool_calls), (RunStatus.COMPLETED, []))
        closed = tool_data(model.seen[0], "w1")
        self.assertEqual((closed["outcome"], closed["job"]["status"]), ("wait_stopped", "running"))
        self.assertEqual(tool_data(model.seen[1], "w2")["outcome"], "held")
        # The next user input lifts it.
        self.ticks({3: lambda: self.finish(workspace)})
        model = Script([{"content": "ok", "tool_calls": [wait_call("w3", job_id, still_needed="user asked")]},
                        {"content": "done"}])
        state = self.turn(model, workspace, "please wait for it")
        self.assertEqual(tool_data(model.seen[1], "w3")["outcome"], "finished")
        self.assertEqual((self.process.starts(), pid), (1, self.process.started()))

    def test_held_is_never_the_effect_of_an_ordinary_interjection(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.ticks({2: lambda: self.inbox.put(USER, "a?"), 5: lambda: self.inbox.put(USER, "b?"),
                    8: lambda: self.finish(workspace)})
        answer = lambda n: {"content": f"answer {n}", "tool_calls": [wait_call(f"w{n}", job_id, still_needed="yes")]}
        model = Script([self.first(job_id, call_id="w0"), answer(1), answer(2), {"content": "done"}])
        state = self.turn(model, workspace, "wait")
        self.assertEqual([tool_data(state.messages, f"w{n}")["outcome"] for n in range(3)],
                         ["interrupted", "interrupted", "finished"])
        self.assertEqual([text for _, text in self.published], ["answer 1", "answer 2"])

    def test_ac12_a_durable_cancel_request_ends_the_wait_and_keeps_the_job(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.ticks({3: lambda: self.store.request_cancellation("s1")})
        state = self.turn(Script([self.first(job_id)]), workspace, "wait")
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(tool_data(state.messages, "w1")["outcome"], "cancelled")
        self.assertTrue(self.process.alive())

    def test_ac12_a_crash_in_the_wait_is_closed_by_a_new_process_with_facts(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)

        class Died(BaseException):
            pass

        def die():
            raise Died()
        self.ticks({3: die})
        with self.assertRaises(Died):
            self.turn(Script([self.first(job_id)]), workspace, "wait")
        self.finish(workspace)                                 # it ended while nobody was watching
        model = Script([{"content": "it ended"}])
        state = self.turn(model, self.own(self.workspace()), None, inbox=Inbox())
        self.assertEqual(state.status, RunStatus.COMPLETED)
        data = tool_data(model.seen[0], "w1")
        self.assertEqual((data["outcome"], data["job"]["exit_code"]), ("finished", 0))
        self.assertNotIn(f"- {job_id} exited", text_of(model.seen[0]))
        self.assertEqual(self.process.starts(), 1)

    def test_other_tools_that_were_cut_off_still_need_reconciliation(self):
        workspace = self.own(self.workspace())

        def boom(*args, **kwargs):
            raise KeyboardInterrupt()
        with patch.object(JobManager, "start", boom):
            state = self.turn(Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": self.process.argv})]}]), workspace)
        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        state = self.turn(Script([{"content": "never"}]), workspace, None)
        self.assertEqual((state.status, state.blocked_tool_calls), (RunStatus.NEEDS_RECOVERY, ["c1"]))
        self.assertEqual(self.process.starts(), 0)

    def test_ac12_session_shutdown_ends_the_wait_without_another_model_call(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace)
        self.ticks({4: workspace.jobs.close})
        model = Script([self.first(job_id), {"content": "never"}])
        state = self.turn(model, workspace, "wait")
        self.assertEqual((state.status, len(model.seen)), (RunStatus.CANCELLED, 1))
        self.assertEqual(tool_data(state.messages, "w1")["outcome"], "cancelled")
        self.assertTrue(wait_until(lambda: not self.process.alive()))           # it ends with the session, as before
        model = Script([{"content": "it was stopped"}])
        self.turn(model, self.own(self.workspace()), None, inbox=Inbox())
        self.assertIn(f"- {job_id} killed", model.seen[0][-1]["content"])       # the end is not lost

    def test_ac11_compaction_keeps_the_association_and_restart_too(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace, purpose="measure accuracy", expect="one number", next_step="compare to 0.9")
        settings = {"compaction_max_chars": 1500, "compaction_keep_turns": 1,
                    "runtime_config": {"compaction": {"min_tail_items": 1, "tail_chars": 10}}}
        for index in range(8):
            self.turn(Script([{"content": f"reply {index} " + "x" * 200}]), workspace, f"question {index} " + "y" * 200,
                      **settings)
        state = self.store.load("s1")
        self.assertTrue(state.history_summary)
        self.assertNotIn(job_id, json.dumps(state.history_summary))            # the summary alone would lose it
        model = Script([{"content": "ok"}])
        self.turn(model, workspace, "what is open?", **settings)
        request = text_of(model.seen[0])
        self.assertIn("Historical summary reference data", request)
        for fact in (job_id, "measure accuracy", "one number", "compare to 0.9", '"responsible": "s1"', '"goal": "active"',
                     '"user_inputs_since_confirmed": 9'):
            self.assertIn(fact, request)
        # Same session in a new process: the job ended meanwhile; association, result and log are found again.
        self.finish(workspace)
        restarted = self.own(self.workspace())
        model = Script([{"tool_calls": [tool_call("o", "job_output", {"job_id": job_id})]}, {"content": "0.91 > 0.9"}])
        self.turn(model, restarted, "and?", inbox=Inbox(), **settings)
        notice = model.seen[0][-1]["content"]
        for fact in (f"- {job_id} exited", "measure accuracy", "compare to 0.9", "the user said more"):
            self.assertIn(fact, notice)
        self.assertIn("output of the job", tool_data(model.seen[1], "o")["content"])
        self.assertEqual(self.process.starts(), 1)

    def test_ac11_a_damaged_association_is_reported_as_missing(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace, purpose="measure accuracy")
        (self.root / ".baseagent" / "jobs" / f"{job_id}.link.json").write_text("{broken", encoding="utf-8")
        restarted = self.own(self.workspace())
        model = Script([{"content": "?"}])
        self.turn(model, restarted, "hi", inbox=Inbox())
        reference = next(str(m["content"]) for m in model.seen[0] if REFERENCE_HEADER in str(m.get("content")))
        self.assertIn('"purpose": null', reference)
        self.assertNotIn("measure accuracy", reference)


class JointScenarioTests(WaitCase):
    """The cross-check scenario, with side conversations simulated as separate agents on the same manager."""

    def test_move_side_answer_wait_side_close_result_to_the_responsible_agent(self):
        workspace = self.own(self.workspace())
        answers, side_seen, facts = self.gui(), [], {}
        token = CancellationToken()
        settings = {"compaction_max_chars": 1200, "compaction_keep_turns": 1,
                    "runtime_config": {"compaction": {"min_tail_items": 1, "tail_chars": 10}}}
        # Earlier turns, so compaction has something to compact before the job is started.
        for index in range(10):
            self.turn(Script([{"content": "r" * 1500}]), workspace, f"old question {index} " + "y" * 900, **settings)
        main = InboxMiddleware(self.inbox, jobs=workspace.jobs, settles=True)

        def open_side():
            job_id = self.jid(workspace)
            facts["frozen"] = main.reference(self.store.load("s1"))
            model = Script([{"content": "SIDE ANSWER"}])
            side = InterimAnswers(lambda answer: (500, False))
            state = self.turn(model, workspace, "side question", session_id="side-1", answers=side, inbox=Inbox())
            side_seen.append((model.seen[0], state.total_tokens, state.model_calls))
            answers.mark_shown([500])                           # the side panel showing its own answer
            facts["phase_after_side"] = workspace.jobs.waits()[0]["phase"]
            facts["notified_after_side"] = self.record(job_id)["notified"]

        def close_side():
            token.cancel()                                      # stopping the side conversation's own run
            facts["phase_after_close"] = workspace.jobs.waits()[0]["phase"]

        def show():
            answers.mark_shown([1])
            answers.mark_shown([1])                             # reconnect: confirming twice changes nothing
        actions = {6: open_side, 10: lambda: facts.update(unshown=workspace.jobs.waits()[0]["phase"]), 12: show,
                   15: lambda: facts.update(waiting=workspace.jobs.waits()[0]["phase"]), 18: close_side,
                   22: lambda: self.finish(workspace, 4)}
        self.ticks(actions)
        # 1) a real command, moved to the background by the user's line; 2) answer + wait; 3) result analysed.
        self.type_when_running(self.inbox, (USER, "现在到哪一步了？", STEER))
        pid_file = self.process
        model = Script([
            {"tool_calls": [tool_call("c1", "run_command", {"argv": self.process.argv, "timeout": 50})]},
            lambda messages: {"content": "还在第一步。", "tool_calls": [
                wait_call("w1", self.jid(workspace), still_needed="结果还要用", purpose="验证 A", next_step="汇报退出码")]},
            lambda messages: {"tool_calls": [tool_call("h", "job_settle", {"job_id": self.jid(workspace), "state": "handled"})]},
            {"content": "退出码 4，失败。"}])
        self.marker = self.process.marker                       # the typist watches this process
        state = self.turn(model, workspace, "运行 A 并汇报", answers=answers, **settings)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        job_id = self.jid(workspace)
        moved = tool_data(state.messages, "c1")
        self.assertTrue(moved["moved_to_background"])
        # Shown before waiting; the side conversation's output and its stop change neither.
        self.assertEqual([facts[key] for key in ("phase_after_side", "unshown", "waiting", "phase_after_close")],
                         ["showing_answer", "showing_answer", "waiting", "waiting"])
        waited = tool_data(state.messages, "w1")
        self.assertEqual((waited["outcome"], waited["job"]["exit_code"], waited["job_succeeded"]), ("finished", 4, False))
        self.assertLessEqual(waited["answer_shown_at"], waited["waiting_since"])
        self.assertEqual([text for _, text in self.published], ["还在第一步。"])
        # The side conversation got no notice and no part of the main line; the main line none of the side.
        (side_request, _, side_calls), = side_seen
        self.assertNotIn(job_id, text_of(side_request))
        self.assertEqual((side_calls, facts["notified_after_side"]), (1, False))
        self.assertNotIn("SIDE ANSWER", text_of(state.messages))
        self.assertIn('"status": "running"', facts["frozen"])
        # One process, one start, one result, exactly four main calls: nothing polled, nothing unaccounted.
        self.assertEqual((pid_file.starts(), moved["pid"], len(model.seen), state.model_calls),
                         (1, waited["job"]["pid"], 4, 4))
        self.assertEqual(text_of(state.messages).count(f"- {job_id} "), 0)
        self.assertEqual(self.link(job_id)["delivery"]["settled"]["as"], "handled")
        self.assertTrue(self.store.load("s1").history_summary)
        self.assertTrue(all(tool_pairs_closed(seen) for seen in model.seen))

    def test_compaction_and_a_side_conversation_between_two_waits(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace, purpose="validate A", expect="exit 0", next_step="report")
        settings = {"compaction_max_chars": 1500, "compaction_keep_turns": 1,
                    "runtime_config": {"compaction": {"min_tail_items": 1, "tail_chars": 10}}}
        self.ticks({3: lambda: self.inbox.put(USER, "something else first")})
        self.turn(Script([self.first(job_id), {"content": "sure"}]), workspace, "wait", **settings)
        for index in range(8):
            self.turn(Script([{"content": "r" * 250}]), workspace, f"other topic {index} " + "y" * 250, **settings)
        self.assertNotIn(job_id, json.dumps(self.store.load("s1").history_summary))
        side = Script([{"content": "SIDE"}])
        self.turn(side, workspace, "side", session_id="side-1", inbox=Inbox(), **settings)
        answers, phases = self.gui(), []
        ticks = Ticks({4: lambda: phases.append(workspace.jobs.waits()[0]["phase"]), 5: lambda: answers.mark_shown([1]),
                       8: lambda: phases.append(workspace.jobs.waits()[0]["phase"]), 10: lambda: self.finish(workspace, 0)})
        model = Script([lambda messages: {"content": "still running, waiting on", "tool_calls": [
                            wait_call("w9", job_id, still_needed="A is still wanted")]},
                        {"tool_calls": [tool_call("h", "job_settle", {"job_id": job_id, "state": "handled"})]},
                        {"content": "A is valid"}])
        with patch.object(jw, "_on_tick", ticks):
            state = self.turn(model, workspace, "back to A, please wait", answers=answers, **settings)
        self.assertEqual(phases, ["showing_answer", "waiting"])
        for fact in (job_id, "validate A", "exit 0", '"next_step": "report"'):
            self.assertIn(fact, text_of(model.seen[0]))                       # from the link, the summary lost it
        data = tool_data(state.messages, "w9")
        self.assertEqual((data["outcome"], data["brief"]["purpose"]), ("finished", "validate A"))
        self.assertNotIn(job_id, text_of(side.seen[0]))
        self.assertEqual((self.process.starts(), len(model.seen), len(side.seen)), (1, 3, 1))
        self.assertEqual(self.link(job_id)["delivery"]["settled"]["as"], "handled")

    def test_stop_in_the_middle_then_the_result_still_reaches_the_main_line(self):
        workspace = self.workspace()
        job_id = self.started_job(workspace, purpose="validate", next_step="report")
        token = CancellationToken()
        self.ticks({3: token.cancel})
        state = self.turn(Script([self.first(job_id)]), workspace, "wait", cancellation=token)
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertTrue(self.process.alive())
        side = Script([{"content": "side"}])
        self.turn(side, workspace, "side", session_id="side-1", inbox=Inbox())
        self.finish(workspace)
        restarted = self.own(self.workspace())
        model = Script([{"content": "exit 0: valid"}])
        self.turn(model, restarted, None, inbox=Inbox())
        self.assertIn(f"- {job_id} exited", model.seen[0][-1]["content"])
        self.assertNotIn(job_id, text_of(side.seen[0]))
        self.assertEqual(restarted.jobs.overview()[0]["delivery"], "received")       # "report" is still open


if __name__ == "__main__":
    unittest.main()
