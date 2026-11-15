"""Turn budgets across resumes: /continue, another message, restarts and --resume never reset spend.

Fake models only; every model request is counted, so "no new request" is checked directly.
Costs use the qwen-plus rates of --budget-cny: 1000 input tokens = 0.0008 CNY.
"""

import json
import time
import unittest
from types import SimpleNamespace

from test_main_agent import WAIT, Duplex, MainAgentTestCase, delegate, tool_call

from baseagent.agent.errors import BudgetExceeded
from baseagent.agent.state import RunStatus, State
from baseagent.agent.workers import BUDGET_TURNS, SharedBudgetMiddleware, TurnBudget, WorkerRecord, seed_turn_usage
from baseagent.llm.response import ModelResponse, TokenUsage


def usage(prompt_tokens):
    return TokenUsage(prompt_tokens, 0, prompt_tokens)


class Counting:
    """A single-agent fake model that records every request it receives."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = 0

    def complete_with_control(self, messages, tools, *, cancellation=None, timeout=None):
        self.requests += 1
        return next(self.responses)


class TurnBudgetUnitTests(unittest.TestCase):
    def test_seed_and_raise_limits_keep_used_counts(self):
        budget = TurnBudget(8, 8, budget_cny="0.01", reserve=0)
        budget.seed(model_calls=3, steps=4, prompt_tokens=1000)
        budget.seed(model_calls=2, steps=2, prompt_tokens=1000)
        snap = budget.snapshot()
        self.assertEqual((snap["model_calls"], snap["steps"], snap["cost_cny"]), (5, 6, "0.001600"))
        budget.raise_limits(model_calls=4, steps=4)
        self.assertEqual((budget.max_model_calls, budget.max_steps), (9, 10))
        budget.raise_limits(model_calls=1, steps=1)  # never lowers
        self.assertEqual((budget.max_model_calls, budget.max_steps), (9, 10))

    def test_spent_cap_refuses_before_the_step_and_the_request(self):
        budget = TurnBudget(None, None, budget_cny="0.001", reserve=0)
        budget.seed(prompt_tokens=2000)
        layer = SharedBudgetMiddleware(budget, worker=False)
        state = State(messages=[{"role": "system", "content": "x"}])
        with self.assertRaises(BudgetExceeded) as caught:
            layer.before_model(state)
        self.assertEqual(caught.exception.status, RunStatus.MAX_TOKENS_EXCEEDED)
        self.assertEqual(budget.steps, 0)

        def handler(request):
            raise AssertionError("no request may be sent once the cap is spent")

        with self.assertRaises(BudgetExceeded):
            layer.wrap_model_call(SimpleNamespace(state=state), handler)
        self.assertEqual(budget.model_calls, 0)

    def test_unresolved_usage_in_a_seeded_turn_blocks_spend(self):
        budget = TurnBudget(None, None, budget_cny="1", reserve=0)
        budget.seed(model_calls=1, unknown=True)
        self.assertEqual(budget.admit_model(worker=False), "budget_cny")

    def test_seed_counts_main_and_the_workers_of_that_turn_only(self):
        class Store:
            sessions = {
                "s-w1": State(messages=[], model_calls=2, step=2, prompt_tokens=1000),
                "s-w2": State(messages=[], model_calls=5, step=5, prompt_tokens=9000),
            }

            def load(self, session_id):
                return self.sessions.get(session_id)

        main = State(messages=[{"role": "system", "content": "x"}, {"role": "user", "content": "a"},
                               {"role": "user", "content": "b"}],
                     model_calls=1, step=1, prompt_tokens=1000)
        records = [WorkerRecord("w1", "s-w1", "t", False, main_turn=2), WorkerRecord("w2", "s-w2", "t", False, main_turn=1)]
        budget = seed_turn_usage(TurnBudget(16, 16, budget_cny="1"), Store(), main, records, 2)
        snap = budget.snapshot()
        self.assertEqual((snap["model_calls"], snap["steps"], snap["cost_cny"], budget.turn), (3, 3, "0.001600", 2))
        # An earlier turn: the main agent's part comes from its saved record; none (old session) counts as zero.
        older = seed_turn_usage(TurnBudget(16, 16, budget_cny="1"), Store(), main, records, 1)
        self.assertEqual(older.snapshot()["cost_cny"], "0.007200")
        main.metadata[BUDGET_TURNS] = {"1": {"main": {"model_calls": 1, "steps": 1, "prompt_tokens": 500,
                                                      "completion_tokens": 0, "unknown": False}}}
        older = seed_turn_usage(TurnBudget(16, 16, budget_cny="1"), Store(), main, records, 1)
        self.assertEqual(older.snapshot()["cost_cny"], "0.007600")


class MainAgentResumeTests(MainAgentTestCase):
    """The main agent and a worker spend 0.0032 of a 0.003 cap in one turn, then stop unfinished."""

    def spend_turn(self):
        def after_worker(messages):
            deadline = time.monotonic() + WAIT
            while (self.worker() is None or self.worker().status != "completed"):
                self.assertLess(time.monotonic(), deadline, "worker never finished")
                time.sleep(0.01)
            return ModelResponse({"tool_calls": [tool_call("m2", "message_worker", {"task_id": "w1", "text": "more"})]},
                                 usage(2000))

        return Duplex(main=[ModelResponse(delegate("d1", "A"), usage(1000)), after_worker],
                      by_task={"A": [ModelResponse({"content": "A done"}, usage(1000))]})

    def test_continue_message_and_restart_keep_spend_and_send_no_request(self):
        model = self.spend_turn()
        code, stdout, stderr = self.invoke(["--session", "demo", "--budget-cny", "0.003"], model,
                                           ["一个问题", "/wait", "/continue", "再试一次", "/status", "/exit"])
        self.assertEqual(code, 0)
        self.assertEqual(len(model.main_seen), 2)  # the third main request was refused, also after /continue
        self.assertEqual(len(model.worker_seen), 1)
        main_state = self.store().load("demo")
        self.assertEqual(main_state.status, RunStatus.MAX_TOKENS_EXCEEDED)
        self.assertEqual(main_state.metadata["shared_budget_exhausted"]["cost_cny"], "0.003200")
        self.assertIn("CNY 0.003200/0.003", stdout + stderr)
        self.assertNotIn("CNY 0.000000", stdout + stderr)
        self.assertIn("--budget-cny <larger>", stdout + stderr)
        steps = main_state.step

        # Restart: the same cap, no cap given (the turn keeps its cap), /continue and a plain message.
        for argv in (["--budget-cny", "0.003"], []):
            idle = Duplex()  # any request raises StopIteration
            code, stdout, stderr = self.invoke(["--session", "demo", *argv], idle, ["/status", "/continue", "继续", "/exit"])
            self.assertEqual(code, 0)
            self.assertEqual((idle.main_seen, idle.worker_seen), ([], []))
            self.assertIn("CNY 0.003200/0.003", stdout)
        self.assertEqual(self.store().load("demo").step, steps)  # refused before a step was taken

        # Only an explicitly larger cap continues, from what was already spent.
        more = Duplex(main=[ModelResponse({"content": "done"}, usage(1000))])
        code, stdout, _ = self.invoke(["--session", "demo", "--budget-cny", "0.01"], more, ["/continue", "/status", "/exit"])
        self.assertEqual(code, 0)
        self.assertEqual(len(more.main_seen), 1)
        self.assertEqual(self.store().load("demo").status, RunStatus.COMPLETED)
        self.assertIn("CNY 0.004000/0.01", stdout)

    def test_resume_counts_worker_steps_against_the_shared_step_limit(self):
        # The main agent and w1 share --max-steps 4; the turn stops when the pool is used up. A restart
        # must not hand the main agent the steps w1 already took.
        def after_worker(messages):
            deadline = time.monotonic() + WAIT
            while self.worker() is None or self.worker().status != "completed":
                self.assertLess(time.monotonic(), deadline, "worker never finished")
                time.sleep(0.01)
            return {"tool_calls": [tool_call("m2", "message_worker", {"task_id": "w1", "text": "more"})]}

        model = Duplex(main=[delegate("d1", "A"), after_worker],
                       by_task={"A": [{"tool_calls": [tool_call("g1", "glob", {"pattern": "*.md"})]}, {"content": "A done"}]})
        self.invoke(["--session", "demo", "--max-steps", "4"], model, ["一个问题", "/wait", "/exit"])
        main_state = self.store().load("demo")
        self.assertEqual(main_state.status, RunStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(main_state.step + self.worker().step, 4)
        idle = Duplex()
        code, _, _ = self.invoke(["--session", "demo", "--max-steps", "4"], idle, ["继续", "/exit"])
        self.assertEqual(code, 0)
        self.assertEqual(idle.main_seen, [])
        # /continue 2 gives the turn exactly two more shared steps.
        more = Duplex(main=[{"tool_calls": [tool_call("m3", "message_worker", {"task_id": "w1", "text": "x"})]},
                            {"content": "done"}])
        self.invoke(["--session", "demo", "--max-steps", "4"], more, ["/continue 2", "/exit"])
        self.assertEqual(len(more.main_seen), 2)
        main_state = self.store().load("demo")
        self.assertEqual(main_state.status, RunStatus.COMPLETED)
        self.assertEqual(main_state.step + self.worker().step, 6)

    def test_parked_worker_resumed_after_restart_keeps_its_turn_spend(self):
        # w1 asks for approval after spending; the turn's cap is used up when the main agent answers.
        model = Duplex(main=[ModelResponse(delegate("d1", "Create out.txt", allow_writes=True), usage(1000)),
                             ModelResponse({"content": "ok"}, usage(1000))],
                       worker=[ModelResponse({"tool_calls": [tool_call("w1c", "write_file", self.write_request())]},
                                             usage(2000))])
        options = ["--session", "demo", "--allow-write", "--tool-policy", self.policy(["write_file"]), "--budget-cny", "0.003"]
        code, _, _ = self.invoke(options, model, ["create out.txt", "/wait", "/exit"], approvals=[EOFError()])
        self.assertEqual(code, 0)
        self.assertEqual(self.worker().status, RunStatus.AWAITING_APPROVAL)
        # After a restart and the approval, the write runs but w1 gets no new request: turn 1 spent 0.0032.
        resumed = Duplex(worker=[{"content": "Created out.txt."}])
        code, stdout, _ = self.invoke(options, resumed, ["/wait", "/exit"], approvals=["y"])
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "out.txt").read_text(), "hello")
        self.assertEqual(resumed.worker_seen, [])
        self.assertEqual(self.worker().metadata["shared_budget_exhausted"]["cost_cny"], "0.003200")

    def test_old_session_without_budget_record_still_resumes(self):
        model = self.spend_turn()
        self.invoke(["--session", "demo", "--budget-cny", "0.003"], model, ["一个问题", "/wait", "/exit"])
        store = self.store()
        state = store.load("demo")
        state.metadata.pop(BUDGET_TURNS)
        store.save(state)
        idle = Duplex()
        code, stdout, _ = self.invoke(["--session", "demo", "--budget-cny", "0.003"], idle, ["/continue", "/status", "/exit"])
        self.assertEqual(code, 0)
        self.assertEqual(idle.main_seen, [])  # spend is rebuilt from the session counters, not the record
        self.assertIn("CNY 0.003200/0.003", stdout)


class OneShotBudgetTests(MainAgentTestCase):
    def run_once(self, argv, model):
        import contextlib
        import io

        from baseagent.main import main
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--root", str(self.root), "--db", str(self.db), *argv], model_factory=lambda args: model)
        return code, stdout.getvalue(), stderr.getvalue()

    def looping(self, tokens):
        counter = iter(range(1, 100))
        while True:
            index = next(counter)
            yield ModelResponse({"tool_calls": [tool_call(f"g{index}", "glob", {"pattern": f"*{index}*"})]}, usage(tokens))

    def test_one_shot_budget_cny_stops_before_the_next_request(self):
        model = Counting(self.looping(1000))
        code, stdout, stderr = self.run_once(["--session", "one", "--budget-cny", "0.002", "--max-steps", "20",
                                              "--max-model-calls", "20", "look around"], model)
        self.assertNotEqual(code, 0)
        state = self.store().load("one")
        self.assertEqual(state.status, RunStatus.MAX_TOKENS_EXCEEDED)
        self.assertEqual(model.requests, 3)  # 0.0008, 0.0016, 0.0024 >= 0.002: the fourth is never sent
        self.assertEqual(state.metadata["shared_budget_exhausted"]["cost_cny"], "0.002400")
        self.assertIn("--budget-cny exhausted for this turn: CNY 0.002400/0.002", stdout + stderr)
        self.assertNotIn("main agent + workers", stdout + stderr)
        self.assertNotIn("session_mode", state.metadata)

        # --resume keeps the spend and the cap: no request. A larger cap continues from 0.0024.
        idle = Counting([])
        self.run_once(["--session", "one", "--resume"], idle)
        self.assertEqual(idle.requests, 0)
        self.run_once(["--session", "one", "--resume", "--budget-cny", "0.002"], idle)
        self.assertEqual(idle.requests, 0)
        more = Counting([ModelResponse({"content": "done"}, usage(1000))])
        code, _, _ = self.run_once(["--session", "one", "--resume", "--budget-cny", "0.01"], more)
        self.assertEqual((code, more.requests), (0, 1))
        state = self.store().load("one")
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.metadata[BUDGET_TURNS]["1"]["main"]["prompt_tokens"], 4000)

    def test_one_shot_without_budget_is_unchanged(self):
        model = Counting([{"content": "hi"}])
        code, _, _ = self.run_once(["--session", "plain", "hello"], model)
        self.assertEqual(code, 0)
        self.assertNotIn(BUDGET_TURNS, self.store().load("plain").metadata)

    def test_a_new_one_shot_turn_starts_a_fresh_pool(self):
        self.run_once(["--session", "one", "--budget-cny", "0.001", "first"], Counting([ModelResponse({"content": "a"}, usage(2000))]))
        second = Counting([ModelResponse({"content": "b"}, usage(1000))])
        code, _, _ = self.run_once(["--session", "one", "--budget-cny", "0.001", "second"], second)
        self.assertEqual((code, second.requests), (0, 1))
        self.assertEqual(json.loads(json.dumps(self.store().load("one").metadata[BUDGET_TURNS]))["2"]["budget_cny"], "0.001")


class WorkerBypassTests(MainAgentTestCase):
    """Review findings: resuming workers must not open a second pool, skip the main turn's pool, or lose the cap."""

    def wait_for(self, predicate, what):
        deadline = time.monotonic() + WAIT
        while not predicate():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.01)

    def status_of(self, worker_id):
        state = self.worker(worker_id)
        return state.status if state is not None else None

    def park_two_workers(self, cap):
        """Turn 1: main 0.0008 + w1 0.0008 + w2 0.0008 = 0.0024; both workers park at a glob approval."""
        def after_parked(messages):
            self.wait_for(lambda: all(self.status_of(w) == RunStatus.AWAITING_APPROVAL for w in ("w1", "w2")),
                          "workers never parked")
            return ModelResponse({"content": "两个都在等审批"}, usage(0))

        model = Duplex(main=[ModelResponse({"tool_calls": [delegate("d1", "A")["tool_calls"][0],
                                                           delegate("d2", "B")["tool_calls"][0]]}, usage(1000)),
                             after_parked, ModelResponse({"content": "第二轮"}, usage(0))],
                       by_task={"A": [ModelResponse({"tool_calls": [tool_call("a1", "glob", {"pattern": "*.md"})]}, usage(1000))],
                                "B": [ModelResponse({"tool_calls": [tool_call("b1", "glob", {"pattern": "*.txt"})]}, usage(1000))]})
        options = ["--session", "demo", "--tool-policy", self.policy(["glob"]), "--budget-cny", cap]
        # A later turn, so the parked workers belong to an older turn than the current one.
        code, _, _ = self.invoke(options, model, ["两个任务", "/wait", "第二个问题", "/exit"],
                                 approvals=[EOFError(), EOFError(), EOFError(), EOFError()])
        self.assertEqual(code, 0)
        self.assertEqual([self.status_of(w) for w in ("w1", "w2")], [RunStatus.AWAITING_APPROVAL] * 2)
        return options

    def test_workers_of_one_turn_share_one_pool_after_a_restart(self):
        options = self.park_two_workers("0.003")

        def approve_after_w1(prompt):
            self.wait_for(lambda: self.status_of("w1") not in (RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL),
                          "w1 never finished")
            return "y"

        # w1 spends 0.0008 more (0.0032 >= 0.003); w2 must then be refused, not given its own 0.003.
        resumed = Duplex(by_task={"A": [ModelResponse({"content": "A done"}, usage(1000))],
                                  "B": [ModelResponse({"content": "B done"}, usage(1000))]})
        code, _, _ = self.invoke(options, resumed, ["/wait", "/wait", "/exit"], approvals=["y", approve_after_w1])
        self.assertEqual(code, 0)
        self.assertEqual(len(resumed.worker_seen), 1)
        self.assertEqual(self.worker("w2").metadata["shared_budget_exhausted"]["cost_cny"], "0.003200")

    def test_restored_workers_of_an_older_turn_get_one_pool(self):
        # Two workers of turn 1 approved close together after a restart, while turn 2 is current:
        # both must draw from one pool, or each could spend the whole remaining cap.
        from baseagent.agent.workers import WorkerManager
        built = []

        def factory(**kwargs):
            built.append(kwargs)
            return TurnBudget(16, 16, budget_cny="0.003", turn=kwargs.get("turn"))

        from baseagent.tools.registry import ToolRegistry
        manager = WorkerManager(ToolRegistry(), system_prompt="x", store=None, runner=None, budget_factory=factory)
        manager.budget = TurnBudget(16, 16, budget_cny="0.003", turn=2)
        first, second = (WorkerRecord(f"w{n}", f"s-w{n}", "t", False, main_turn=1) for n in (1, 2))
        manager.records = {"w1": first, "w2": second}
        self.assertIs(manager.pool_for(first), manager.pool_for(second))
        self.assertEqual(built, [{"turn": 1}])

    def test_resuming_a_worker_session_directly_uses_the_main_turn_pool(self):
        # w1 runs out of the shared cap; the main turn stops with it.
        def after_worker(messages):
            self.wait_for(lambda: self.status_of("w1") not in (None, RunStatus.RUNNING), "w1 never stopped")
            return ModelResponse({"content": "never"}, usage(0))

        model = Duplex(main=[ModelResponse(delegate("d1", "A"), usage(1000)), after_worker],
                       by_task={"A": [ModelResponse({"tool_calls": [tool_call("a1", "glob", {"pattern": "*.md"})]}, usage(2000))]})
        self.invoke(["--session", "demo", "--budget-cny", "0.002"], model, ["一个问题", "/wait", "/exit"])
        self.assertEqual(self.worker().metadata["shared_budget_exhausted"]["reason"], "budget_cny")
        steps = self.worker().step
        for extra in ([], ["--max-steps", "50", "--max-model-calls", "50"], ["--budget-cny", "0.002"]):
            idle = Counting([])
            OneShotBudgetTests.run_once(self, ["--session", "demo-w1", "--resume", "--accept-config-changes", *extra], idle)
            self.assertEqual(idle.requests, 0, extra)
        self.assertEqual(self.worker().step, steps)
        self.assertEqual(self.worker().metadata["shared_budget_exhausted"]["cost_cny"], "0.002400")
        # Only an explicitly larger cap lets the worker go on, from the turn's 0.0024.
        more = Counting([ModelResponse({"content": "A done"}, usage(1000))])
        code, out, err = OneShotBudgetTests.run_once(self, ["--session", "demo-w1", "--resume", "--accept-config-changes", "--budget-cny", "0.01"], more)
        self.assertEqual(more.requests, 1, out + err)
        self.assertEqual(more.requests, 1)
        self.assertEqual(self.worker().status, RunStatus.COMPLETED)

    def drop_turn_record(self, turn="1"):
        store = self.store()
        state = store.load("demo")
        state.metadata[BUDGET_TURNS].pop(turn)  # as in a session from before the record
        store.save(state)

    def approve_after_w1(self, prompt):
        self.wait_for(lambda: self.status_of("w1") not in (RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL),
                      "w1 never finished")
        return "y"

    def test_missing_turn_record_blocks_instead_of_dropping_the_cap(self):
        options = self.park_two_workers("0.003")
        self.drop_turn_record()
        resumed = Duplex(by_task={"A": [ModelResponse({"content": "A done"}, usage(2000))],
                                  "B": [ModelResponse({"content": "B done"}, usage(1000))]})
        # No --budget-cny on the restart and the main agent's turn-1 spend is unknown: nothing is sent.
        # Before the fix the cap was gone (the workers then ran without one).
        no_cap = [option for option in options if option not in ("--budget-cny", "0.003")]
        self.invoke(no_cap, resumed, ["/wait", "/wait", "/exit"], approvals=["y", self.approve_after_w1])
        self.assertEqual(resumed.worker_seen, [])
        for worker in ("w1", "w2"):
            shared = self.worker(worker).metadata["shared_budget_exhausted"]
            self.assertEqual((shared["reason"], shared["unrecovered"]), ("budget_unknown", "main_turn_record_missing"))

    def test_missing_turn_record_with_an_explicit_cap_counts_the_known_usage(self):
        options = self.park_two_workers("0.003")
        self.drop_turn_record()
        resumed = Duplex(by_task={"A": [ModelResponse({"content": "A done"}, usage(2000))],
                                  "B": [ModelResponse({"content": "B done"}, usage(1000))]})
        self.invoke(options, resumed, ["/wait", "/wait", "/exit"], approvals=["y", self.approve_after_w1])
        # Only the workers' 0.0016 is known; w1 brings it to 0.0032, so w2 is refused.
        self.assertEqual(len(resumed.worker_seen), 1)
        self.assertEqual(self.worker("w2").metadata["shared_budget_exhausted"]["cost_cny"], "0.003200")

    def test_records_of_turns_with_unfinished_workers_are_never_pruned(self):
        state = State(messages=[{"role": "system", "content": "x"}, {"role": "user", "content": "a"}])
        state.metadata["workers"] = {"w1": {"id": "w1", "session_id": "s-w1", "task": "t", "allow_writes": False,
                                            "status": "awaiting_approval", "main_turn": 1},
                                     "w2": {"id": "w2", "session_id": "s-w2", "task": "t", "allow_writes": False,
                                            "status": "completed", "main_turn": 2}}
        layer = SharedBudgetMiddleware(TurnBudget(16, 16, budget_cny="1"), worker=False)
        for turn in range(1, 120):
            layer.budget.turn = turn
            layer._record(state)
        kept = state.metadata[BUDGET_TURNS]
        self.assertIn("1", kept)        # w1 can still resume
        self.assertNotIn("2", kept)     # w2 finished: prunable
        self.assertEqual(len(kept), 51)


class LegacyWorkerAndConcurrencyTests(MainAgentTestCase):
    """Second review: a worker session without main_turn, and concurrent first rebuilds of one turn."""

    wait_for, status_of = WorkerBypassTests.wait_for, WorkerBypassTests.status_of

    def spend_cap_in_w1(self):
        """Turn 1 of "demo": main 0.0008 + w1 0.0016 = 0.0024 of a 0.002 cap; w1 stops on the cap."""
        def after_worker(messages):
            self.wait_for(lambda: self.status_of("w1") not in (None, RunStatus.RUNNING), "w1 never stopped")
            return ModelResponse({"content": "never"}, usage(0))

        model = Duplex(main=[ModelResponse(delegate("d1", "A"), usage(1000)), after_worker],
                       by_task={"A": [ModelResponse({"tool_calls": [tool_call("a1", "glob", {"pattern": "*.md"})]},
                                                    usage(2000))]})
        self.invoke(["--session", "demo", "--budget-cny", "0.002"], model, ["一个问题", "/wait", "/exit"])
        self.assertEqual(self.worker().metadata["shared_budget_exhausted"]["reason"], "budget_cny")

    def strip_worker_tags(self, registry_too=False):
        """Make demo-w1 look like a session from before main_turn and turn_budget were tagged."""
        store = self.store()
        worker = store.load("demo-w1")
        worker.metadata.pop("main_turn")
        worker.metadata.pop("turn_budget", None)
        store.save(worker)
        if registry_too:
            main_state = store.load("demo")
            main_state.metadata["workers"]["w1"].pop("main_turn")
            store.save(main_state)

    def resume_w1(self, extra, model):
        return OneShotBudgetTests.run_once(
            self, ["--session", "demo-w1", "--resume", "--accept-config-changes", *extra], model)

    def test_worker_without_main_turn_finds_its_turn_in_the_parent_registry(self):
        self.spend_cap_in_w1()
        self.strip_worker_tags()
        steps = self.worker().step
        for extra in ([], ["--max-steps", "50", "--max-model-calls", "50"], ["--budget-cny", "0.002"]):
            idle = Counting([])
            self.resume_w1(extra, idle)
            self.assertEqual(idle.requests, 0, extra)  # before the fix: one request, the parent spend ignored
        self.assertEqual(self.worker().step, steps)
        # The parent's 0.0008 is counted: the turn spent 0.0024, not just w1's 0.0016.
        self.assertEqual(self.worker().metadata["shared_budget_exhausted"]["cost_cny"], "0.002400")
        more = Counting([ModelResponse({"content": "A done"}, usage(1000))])
        self.resume_w1(["--budget-cny", "0.01"], more)
        self.assertEqual(more.requests, 1)
        self.assertEqual(self.worker().status, RunStatus.COMPLETED)

    def test_worker_whose_turn_cannot_be_found_is_blocked_until_a_cap_is_given(self):
        self.spend_cap_in_w1()
        self.strip_worker_tags(registry_too=True)
        idle = Counting([])
        code, out, err = self.resume_w1([], idle)
        self.assertEqual(idle.requests, 0)
        self.assertNotEqual(code, 0)
        shared = self.worker().metadata["shared_budget_exhausted"]
        self.assertEqual((shared["reason"], shared["unrecovered"]), ("budget_unknown", "worker_turn_unknown"))
        self.assertIn("cannot be recovered", out + err)
        self.assertIn("--budget-cny <cap>", out + err)
        # An explicit cap is the user's decision; it counts the usage still known (w1's own 0.0016).
        idle = Counting([])
        self.resume_w1(["--budget-cny", "0.0016"], idle)
        self.assertEqual(idle.requests, 0)
        more = Counting([ModelResponse({"content": "A done"}, usage(1000))])
        self.resume_w1(["--budget-cny", "0.01"], more)
        self.assertEqual(more.requests, 1)

    def test_pools_of_different_turns_stay_apart(self):
        from baseagent.agent.workers import WorkerManager
        from baseagent.tools.registry import ToolRegistry
        manager = WorkerManager(ToolRegistry(), system_prompt="x", store=None, runner=None)
        first = manager.turn_pool(1, lambda: TurnBudget(16, 16, budget_cny="1"))
        self.assertIs(manager.turn_pool(1, lambda: self.fail("turn 1 is cached")), first)
        second = manager.turn_pool(2, lambda: TurnBudget(16, 16, budget_cny="1"))
        self.assertIsNot(first, second)
        self.assertEqual((first.turn, second.turn), (1, 2))
        # A new turn always starts fresh, even if a pool was cached under its number (a rejected attempt).
        third = manager.turn_pool(2, lambda: TurnBudget(16, 16, budget_cny="1"), replace=True)
        self.assertIsNot(third, second)

    def probe_concurrent_rebuilds(self, first):
        """Restart the chat and, from inside it, rebuild turn 1's pool from three threads at once: the main
        agent (resuming its turn) and the two parked workers. ``first`` starts and is held inside its rebuild
        until both others are blocked waiting for it; no sleeps, only events."""
        import threading
        from unittest.mock import patch

        import baseagent.agent.workers as workers_module
        from baseagent.agent.workers import WorkerManager

        # A probe path "arrives" when it waits for the lock held by the rebuild in progress, or (the bug)
        # starts a rebuild of its own.
        arrived = threading.Semaphore(0)

        class Watched:
            """The manager's RLock, reporting when a probe thread has to wait for it."""

            def __init__(self):
                self.inner = threading.RLock()

            def acquire(self, blocking=True, timeout=-1):
                if self.inner.acquire(False):
                    return True
                if not blocking:
                    return False
                if threading.current_thread().name.startswith("probe-"):
                    arrived.release()
                return self.inner.acquire(True, timeout)

            def release(self):
                self.inner.release()

            __enter__ = acquire

            def __exit__(self, *exc):
                self.release()

            def _is_owned(self):
                return self.inner._is_owned()

            def _release_save(self):
                return self.inner._release_save()

            def _acquire_restore(self, saved):
                self.inner._acquire_restore(saved)

        managers, original_init, original_seed = [], WorkerManager.__init__, workers_module.seed_turn_usage
        building, go, builds = threading.Event(), threading.Event(), []

        def init(manager, *args, **kwargs):
            original_init(manager, *args, **kwargs)
            manager._lock = Watched()
            manager._changed = threading.Condition(manager._lock)
            managers.append(manager)

        def seed(budget, *args, **kwargs):
            if threading.current_thread().name.startswith("probe-"):
                builds.append(threading.current_thread().name)
                if building.is_set():
                    arrived.release()
                building.set()
                go.wait(WAIT)
            return original_seed(budget, *args, **kwargs)

        got = {}

        def probe(prompt):
            manager = managers[-1]
            paths = {"probe-main": lambda: manager.budget_factory(resuming=True),
                     "probe-w1": lambda: manager.pool_for(manager.records["w1"]),
                     "probe-w2": lambda: manager.pool_for(manager.records["w2"])}
            threads = {name: threading.Thread(target=lambda name=name: got.__setitem__(name, paths[name]()), name=name)
                       for name in paths}
            threads[first].start()
            self.assertTrue(building.wait(WAIT), "the first path never started its rebuild")
            for name, thread in threads.items():
                if name != first:
                    thread.start()
            for _ in range(2):  # both others are now waiting for the rebuild in progress (or rebuilding too)
                self.assertTrue(arrived.acquire(timeout=WAIT), "a path never reached its pool lookup")
            go.set()
            for thread in threads.values():
                thread.join(WAIT)
            return "/exit"

        options = ["--session", "demo", "--tool-policy", self.policy(["glob"]), "--budget-cny", "0.003"]
        with patch.object(WorkerManager, "__init__", init), patch.object(workers_module, "seed_turn_usage", seed):
            code, _, _ = self.invoke(options, Duplex(), [probe], approvals=[EOFError()] * 4)
        self.assertEqual(code, 0)
        return got, builds

    def park_in_current_turn(self):
        """Turn 1 (still the current turn): main 0.0008 + w1 0.0008 + w2 0.0008; both workers parked."""
        def after_parked(messages):
            self.wait_for(lambda: all(self.status_of(w) == RunStatus.AWAITING_APPROVAL for w in ("w1", "w2")),
                          "workers never parked")
            return ModelResponse({"content": "两个都在等审批"}, usage(0))

        model = Duplex(main=[ModelResponse({"tool_calls": [delegate("d1", "A")["tool_calls"][0],
                                                           delegate("d2", "B")["tool_calls"][0]]}, usage(1000)),
                             after_parked],
                       by_task={"A": [ModelResponse({"tool_calls": [tool_call("a1", "glob", {"pattern": "*.md"})]}, usage(1000))],
                                "B": [ModelResponse({"tool_calls": [tool_call("b1", "glob", {"pattern": "*.txt"})]}, usage(1000))]})
        options = ["--session", "demo", "--tool-policy", self.policy(["glob"]), "--budget-cny", "0.003"]
        self.invoke(options, model, ["两个任务", "/wait", "/exit"], approvals=[EOFError()] * 4)
        self.assertEqual([self.status_of(w) for w in ("w1", "w2")], [RunStatus.AWAITING_APPROVAL] * 2)

    def test_concurrent_first_rebuilds_of_one_turn_share_one_pool(self):
        self.park_in_current_turn()
        for first in ("probe-main", "probe-w1", "probe-w2"):
            with self.subTest(first=first):
                got, builds = self.probe_concurrent_rebuilds(first)
                self.assertEqual(builds, [first])  # one rebuild; the others waited and reused it
                pools = list(got.values())
                self.assertEqual(len(pools), 3)
                self.assertTrue(all(pool is pools[0] for pool in pools))
                self.assertEqual(pools[0].snapshot()["cost_cny"], "0.002400")
                got["probe-w1"].charge(1000, 0)  # any path spends; every path sees the same total
                self.assertEqual({pool.snapshot()["cost_cny"] for pool in got.values()}, {"0.003200"})
                self.assertEqual(got["probe-main"].admit_model(worker=False), "budget_cny")
                self.assertEqual(got["probe-w2"].admit_model(worker=True), "budget_cny")


if __name__ == "__main__":
    unittest.main()
