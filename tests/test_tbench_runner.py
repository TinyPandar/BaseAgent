"""The Harbor-facing run loop with a fake environment and scripted models. No Docker, no provider."""

import asyncio
import json
import os
import threading
import time
import traceback
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from tbench_helpers import ShellEnvironment, temporary_workdir

from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.tbench import BaseAgentInfrastructureError, BudgetExceededError
from baseagent.tbench.runner import SUMMARY_FILE, RunConfig, run_in_environment
from baseagent.tbench.scripted import ScriptedFixtureModel
from baseagent.tbench.settings import AgentSettings

SECRET = "sk-test-secret-must-stay-on-host"


def _call(name, arguments, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _context():
    return SimpleNamespace(n_input_tokens=None, n_output_tokens=None, n_cache_tokens=None, cost_usd=None, metadata=None)


class CountingModel:
    """Each call returns the next scripted message with 100 input / 10 output tokens.

    ``gate`` (call number -> threading.Event) blocks that call until the event is set.
    """

    def __init__(self, messages, *, gate=None):
        self.messages = list(messages)
        self.calls = 0
        self.gate = gate or {}
        self.entered = threading.Event()

    def complete(self, messages, tools):
        self.calls += 1
        number = self.calls
        if number in self.gate:
            self.entered.set()
            self.gate[number].wait(10)
        message = json.loads(json.dumps(self.messages[min(number, len(self.messages)) - 1]))
        for call in message.get("tool_calls") or []:
            call["id"] = f"{call['id']}-{number}"      # call IDs stay unique within the turn
        return ModelResponse(message, TokenUsage(100, 10, 110))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.workdir = temporary_workdir(self)
        self.logs = self.workdir.parent / "logs"
        self.env = ShellEnvironment(self.workdir)

    def run_task(self, model, config=None, *, timeout=None, context=None):
        context = context or _context()

        async def main():
            work = run_in_environment("Create answer.txt containing ok.", self.env, context, model=model,
                                      config=config or RunConfig(), logs_dir=self.logs)
            if timeout is None:
                return await work
            return await asyncio.wait_for(work, timeout)

        return asyncio.run(main()), context

    def test_scripted_fixture_writes_through_exec_and_costs_nothing(self):
        with mock.patch.dict(os.environ, {"BASEAGENT_LLM_API_KEY": SECRET, "DEEPSEEK_API_KEY": SECRET}):
            reason, context = self.run_task(ScriptedFixtureModel("correct"),
                                            RunConfig(provider_calls_are_billed=False))
        self.assertEqual(reason, "completed")
        self.assertEqual((self.workdir / "answer.txt").read_text(), "ok\n")
        # Only the model's own file exists in the task workdir: no framework files.
        self.assertEqual([path.name for path in self.workdir.iterdir()], ["answer.txt"])
        self.assertEqual(context.metadata["cost_cny"], "0.000000")
        self.assertEqual(context.metadata["provider_calls"], 0)
        self.assertIsNone(context.cost_usd)
        self.assertEqual(context.metadata["workdir"], str(self.workdir))
        self.assertTrue(all(call["env"] is None for call in self.env.calls))
        self.assertFalse(any(SECRET in call["command"] for call in self.env.calls))
        self.assertTrue((self.logs / SUMMARY_FILE).exists())

    def test_scripted_wrong_answer_is_a_completed_run(self):
        reason, context = self.run_task(ScriptedFixtureModel("wrong"), RunConfig(provider_calls_are_billed=False))
        self.assertEqual(reason, "completed")      # Harbor's verifier, not the agent, decides it is wrong
        self.assertEqual((self.workdir / "answer.txt").read_text(), "not ok\n")
        self.assertEqual(context.n_input_tokens, 0)

    def test_provider_key_never_reaches_the_container(self):
        model = CountingModel([{"role": "assistant", "content": None,
                                "tool_calls": [_call("run_command", {"argv": ["env"]}, "env")]},
                               {"role": "assistant", "content": "done"}])
        with mock.patch.dict(os.environ, {"BASEAGENT_LLM_API_KEY": SECRET}):
            reason, context = self.run_task(model, RunConfig(budget_cny=Decimal("1")))
        self.assertEqual(reason, "completed")
        self.assertFalse(any(SECRET in call["command"] or call["env"] for call in self.env.calls))
        self.assertNotIn(SECRET, json.dumps(context.metadata))
        self.assertNotIn(SECRET, (self.logs / SUMMARY_FILE).read_text())

    def test_error_text_never_carries_credentials(self):
        stray = "sk-unlisted0123456789abcdef"

        class LeakyModel:
            calls = 0

            def complete(self, messages, tools):
                raise RuntimeError(f"401 Unauthorized: key {SECRET} rejected; also {stray}; Bearer {SECRET}x")

        def check(error, context, reason):
            texts = [str(error), "".join(traceback.format_exception(error)), json.dumps(context.metadata),
                     (self.logs / SUMMARY_FILE).read_text()]
            for text in texts:
                self.assertNotIn(SECRET, text)
                self.assertNotIn(stray, text)
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__ or error.__context__ is None)
            self.assertIn(reason, str(error))      # the reason itself survives

        with mock.patch.dict(os.environ, {"BASEAGENT_LLM_API_KEY": SECRET}):
            context = _context()
            with self.assertRaises(BaseAgentInfrastructureError) as caught:
                self.run_task(LeakyModel(), RunConfig(budget_cny=Decimal("1")), context=context)
            check(caught.exception, context, "401 Unauthorized")
            # A failure before the loop starts goes through the same scrubbing.
            self.env.fail_with = RuntimeError(f"docker: auth header Bearer {SECRET} refused")
            context = _context()
            with self.assertRaises(BaseAgentInfrastructureError) as caught:
                self.run_task(CountingModel([]), RunConfig(budget_cny=Decimal("1")), context=context)
            check(caught.exception, context, "auth header")

    def test_model_held_key_is_scrubbed_even_when_not_in_the_environment(self):
        from baseagent.tbench.runner import model_secrets, redact
        model = SimpleNamespace(client=SimpleNamespace(api_key="held-only-in-the-client-123"))
        self.assertEqual(model_secrets(model), ["held-only-in-the-client-123"])
        self.assertNotIn("held-only-in-the-client-123", redact("bad key held-only-in-the-client-123", model_secrets(model)))

    def test_outer_timeout_during_a_command_stops_new_model_requests(self):
        model = CountingModel([{"role": "assistant", "content": None, "tool_calls": [
            _call("run_command", {"argv": ["sh", "-c", "sleep 2; touch late"], "timeout": 30}, "slow")]},
            {"role": "assistant", "content": "should never be requested"}])
        context = _context()
        with self.assertRaises(asyncio.TimeoutError):
            self.run_task(model, RunConfig(budget_cny=Decimal("1")), timeout=1.0, context=context)
        time.sleep(3)
        self.assertEqual(model.calls, 1, "a model request was sent after the outer timeout")
        self.assertEqual(context.n_input_tokens, 100)      # counted before the timeout, never more
        self.assertEqual(context.metadata["stop_reason"], "timeout")
        self.assertFalse((self.workdir / "late").exists(), "the command kept running after cancellation")

    def test_outer_timeout_during_a_model_call_freezes_counted_tokens(self):
        release = threading.Event()
        model = CountingModel([{"role": "assistant", "content": None,
                                "tool_calls": [_call("run_command", {"argv": ["true"]}, "first")]},
                               {"role": "assistant", "content": None,
                                "tool_calls": [_call("run_command", {"argv": ["touch", "after"]}, "second")]},
                               {"role": "assistant", "content": "third"}], gate={2: release})
        context = _context()

        async def main():
            task = asyncio.ensure_future(run_in_environment("t", self.env, context, model=model,
                                                            config=RunConfig(budget_cny=Decimal("1")),
                                                            logs_dir=self.logs))
            while not model.entered.is_set():
                await asyncio.sleep(0.02)
            counted_before_cancel = 100   # call 1 returned; call 2 is in flight
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return counted_before_cancel

        counted_before_cancel = asyncio.run(main())
        release.set()           # the in-flight call now returns with more usage
        time.sleep(1)
        self.assertEqual(model.calls, 2, "a new model request was sent after cancellation")
        self.assertLessEqual(context.n_input_tokens, counted_before_cancel)
        self.assertEqual(context.metadata["stop_reason"], "timeout")
        self.assertEqual(context.metadata["in_flight_model_calls_at_stop"], 1)
        self.assertFalse((self.workdir / "after").exists())

    def test_budget_stop_raises_budget_exceeded_error(self):
        # Rates make each call cost 1 CNY; the cap of 1.5 CNY admits two calls and refuses the third.
        loop = {"role": "assistant", "content": None, "tool_calls": [_call("run_command", {"argv": ["true"]}, "x")]}
        model = CountingModel([loop])
        config = RunConfig(budget_cny=Decimal("1.5"), input_cny_per_million=Decimal(10_000),
                           output_cny_per_million=Decimal(0))
        with self.assertRaises(BudgetExceededError) as caught:
            self.run_task(model, config)
        self.assertEqual(type(caught.exception).__name__, "BudgetExceededError")
        self.assertEqual(model.calls, 2)
        summary = json.loads((self.logs / SUMMARY_FILE).read_text())
        self.assertEqual(summary["stop_reason"], "budget_stopped")
        self.assertEqual(summary["cost_cny"], "2.000000")

    def test_environment_failure_is_infrastructure(self):
        model = CountingModel([{"role": "assistant", "content": None,
                                "tool_calls": [_call("run_command", {"argv": ["true"]}, "x")]}])

        async def fail_after_discovery(*args, **kwargs):
            raise RuntimeError("container disappeared")

        original = self.env.exec

        async def exec_once(command, **kwargs):
            if command == "pwd -P":
                return await original(command, **kwargs)
            return await fail_after_discovery()

        self.env.exec = exec_once
        with self.assertRaises(BaseAgentInfrastructureError) as caught:
            self.run_task(model, RunConfig(budget_cny=Decimal("1")))
        self.assertIn("EnvironmentUnavailable", str(caught.exception))
        self.assertEqual(model.calls, 1)
        self.env.exec = original
        self.env.fail_with = RuntimeError("no such image")
        with self.assertRaises(BaseAgentInfrastructureError):
            self.run_task(CountingModel([]), RunConfig(budget_cny=Decimal("1")))

    def test_step_limit_is_a_graded_stop(self):
        loop = {"role": "assistant", "content": None, "tool_calls": [_call("run_command", {"argv": ["true"]}, "x")]}
        reason, context = self.run_task(CountingModel([loop]), RunConfig(max_steps=2, budget_cny=Decimal("1")))
        self.assertEqual(reason, "steps_exhausted")
        self.assertEqual(context.n_input_tokens, 200)

    def test_nothing_is_written_after_the_wall_clock_deadline(self):
        # 17 s of agent time minus the 15 s margin leaves about 2 s; the command asks for 30 s.
        model = CountingModel([{"role": "assistant", "content": None, "tool_calls": [
            _call("run_command", {"argv": ["sh", "-c", "sleep 4; touch late"], "timeout": 30}, "slow")]},
            {"role": "assistant", "content": "done"}])
        config = RunConfig(max_command_timeout_sec=60, agent_timeout_sec=17, safety_margin_sec=15,
                           budget_cny=Decimal("1"))
        started = time.monotonic()
        reason, context = self.run_task(model, config)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(reason, "timeout")
        time.sleep(4)
        self.assertFalse((self.workdir / "late").exists(), "the command wrote after the wall-clock deadline")

    def test_background_children_never_write_after_the_deadline(self):
        # The first command leaves a child behind and exits at once; the second runs into the deadline.
        model = CountingModel([
            {"role": "assistant", "content": None, "tool_calls": [
                _call("run_command", {"argv": ["sh", "-c", "(sleep 3; touch late) >/dev/null 2>&1 & exit 0"],
                                      "timeout": 30}, "bg")]},
            {"role": "assistant", "content": None, "tool_calls": [
                _call("run_command", {"argv": ["sh", "-c", "sleep 30"], "timeout": 30}, "slow")]},
            {"role": "assistant", "content": "done"}])
        config = RunConfig(max_command_timeout_sec=60, agent_timeout_sec=17, safety_margin_sec=15,
                           budget_cny=Decimal("1"))
        reason, context = self.run_task(model, config)
        self.assertEqual(reason, "timeout")
        time.sleep(4)
        self.assertFalse((self.workdir / "late").exists(), "a background child wrote after the wall-clock deadline")
        self.assertIs(context.metadata["agent_processes_stopped"], True)

    def test_a_stop_that_cannot_kill_ends_the_run_as_infrastructure(self):
        # A kill builtin that accepts nothing, or one that reports success and does nothing: the
        # command cannot be stopped at the deadline and ends by itself a second later.
        for shim in ("sh", "sh-noop-kill"):
            with self.subTest(shim):
                self.env = ShellEnvironment(self.workdir, shims=("setsid", shim))
                model = CountingModel([{"role": "assistant", "content": None, "tool_calls": [
                    _call("run_command", {"argv": ["sh", "-c", "sleep 3; touch late"], "timeout": 30}, "slow")]},
                    {"role": "assistant", "content": "done"}])
                config = RunConfig(max_command_timeout_sec=60, agent_timeout_sec=17, safety_margin_sec=15,
                                   budget_cny=Decimal("1"))
                context = _context()
                with self.assertRaises(BaseAgentInfrastructureError):
                    self.run_task(model, config, context=context)
                self.assertEqual(context.metadata["stop_reason"], "infrastructure")
                self.assertIs(context.metadata["agent_processes_stopped"], False)

    def test_command_timeout_never_exceeds_the_remaining_wall_clock(self):
        model = CountingModel([{"role": "assistant", "content": None,
                                "tool_calls": [_call("run_command", {"argv": ["true"], "timeout": 600}, "x")]},
                               {"role": "assistant", "content": "done"}])
        config = RunConfig(max_command_timeout_sec=600, agent_timeout_sec=40, safety_margin_sec=15,
                           budget_cny=Decimal("1"))
        reason, context = self.run_task(model, config)
        self.assertEqual(reason, "completed")
        command = next(call for call in self.env.calls if call["command"].endswith("true"))
        # Channel deadline = the effective command timeout (<= 25 s left) plus Harbor's 30 s backstop.
        self.assertLessEqual(command["timeout_sec"], 25 + 30)
        self.assertLessEqual(context.metadata["max_duration_sec"], 25)


class SettingsTests(unittest.TestCase):
    def test_real_model_requires_a_budget_and_kwargs_are_validated(self):
        with self.assertRaises(ValueError):
            AgentSettings.from_kwargs({})
        with self.assertRaises(ValueError):
            AgentSettings.from_kwargs({"scripted": "sometimes"})
        with self.assertRaises(ValueError):
            AgentSettings.from_kwargs({"budget_cny": "1", "max_command_timeout_sec": "0"})
        with self.assertRaises(ValueError):
            AgentSettings.from_kwargs({"budget_cny": "1", "surprise": "1"})
        settings = AgentSettings.from_kwargs({"budget_cny": "0.8333", "max_command_timeout_sec": "600"})
        self.assertEqual(settings.max_command_timeout_sec, 600)
        self.assertEqual(settings.budget_cny, Decimal("0.8333"))
        self.assertIsNone(settings.usd_per_cny)
        self.assertEqual(AgentSettings.from_kwargs({"scripted": "correct"}).max_command_timeout_sec, 60)


if __name__ == "__main__":
    unittest.main()
