"""Checks against Harbor 0.26.0's own models. Skipped where Harbor is not installed.

Run in a Python environment with ``harbor==0.26.0``::

    PYTHONPATH=src:tests:. python -B -m unittest tests/test_tbench_harbor.py -v
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tbench_helpers import ShellEnvironment, temporary_workdir

try:
    import harbor  # noqa: F401
    from harbor.models.agent.context import AgentContext
    from harbor.models.job.config import JobConfig
    from harbor.models.trial.config import AgentConfig, TaskConfig, TrialConfig
    from harbor.models.trial.error_policy import policy_for
    from harbor.models.trial.result import AgentInfo, ExceptionInfo, TrialResult
    from harbor.models.verifier.result import VerifierResult
    HARBOR = True
except ImportError:
    HARBOR = False

from applications.project_assistant.evaluation.terminal_bench import FIXTURE, import_results, load_subset, plan_job
from baseagent.tbench import BaseAgentInfrastructureError, BudgetExceededError


@unittest.skipUnless(HARBOR, "harbor==0.26.0 is not installed in this environment")
class HarborContractTests(unittest.TestCase):
    def test_plan_is_a_valid_job_config_with_untouched_timeouts_and_resources(self):
        config = JobConfig.model_validate(plan_job("15")["job_config"])
        self.assertEqual(config.timeout_multiplier, 1.0)
        for name in ("agent_timeout_multiplier", "verifier_timeout_multiplier", "agent_setup_timeout_multiplier",
                     "environment_build_timeout_multiplier"):
            self.assertIsNone(getattr(config, name))
        agent = config.agents[0]
        self.assertIsNone(agent.override_timeout_sec)
        self.assertIsNone(agent.max_timeout_sec)
        self.assertIsNone(config.verifier.override_timeout_sec)
        self.assertIsNone(config.verifier.max_timeout_sec)
        for name in ("override_cpus", "override_memory_mb", "override_storage_mb", "override_gpus"):
            self.assertIsNone(getattr(config.environment, name))
        self.assertEqual(config.datasets[0].task_names, load_subset()["task_ids"])
        self.assertEqual(config.n_concurrent_trials, 1)
        infra = policy_for("BaseAgentInfrastructureError", config.override_error_policy["BaseAgentInfrastructureError"])
        self.assertEqual((infra.should_grade, infra.has_valid_reward, infra.should_retry), (False, False, False))
        budget = policy_for("BudgetExceededError", config.override_error_policy["BudgetExceededError"])
        self.assertEqual((budget.should_grade, budget.has_valid_reward, budget.should_retry), (True, False, False))
        # Harbor's default for the name agrees; the explicit entry only pins it.
        default = policy_for("BudgetExceededError")
        self.assertEqual((default.should_grade, default.has_valid_reward, default.should_retry), (True, False, False))
        self.assertIn("BaseAgentInfrastructureError", config.retry.exclude_exceptions)

    def test_exception_names_resolve_to_the_configured_policies(self):
        overrides = JobConfig.model_validate(plan_job("15")["job_config"]).override_error_policy
        for error, grade in ((BudgetExceededError("stop"), True), (BaseAgentInfrastructureError("gone"), False)):
            info = ExceptionInfo.from_exception(error, override_error_policy=overrides)
            self.assertEqual(info.exception_type, type(error).__name__)
            self.assertEqual(info.policy.should_grade, grade)
            self.assertFalse(info.policy.has_valid_reward)

    def test_agent_timeout_is_read_from_the_trial_records(self):
        from baseagent.tbench.harbor_agent import agent_timeout_from_trial
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory) / "trial"
            (trial / "agent").mkdir(parents=True)
            config = TrialConfig(task=TaskConfig(path=FIXTURE), agent=AgentConfig(import_path="x.y:Z"))
            (trial / "config.json").write_text(config.model_dump_json())
            self.assertEqual(agent_timeout_from_trial(trial / "agent"), 300.0)
            self.assertIsNone(agent_timeout_from_trial(Path(directory) / "missing" / "agent"))

    def test_adapter_fills_a_real_agent_context(self):
        from baseagent.tbench.harbor_agent import BaseAgentHarborAgent
        workdir = temporary_workdir(self)
        agent = BaseAgentHarborAgent(workdir.parent / "agent-logs", "deepseek/deepseek-chat",
                                     scripted="correct", agent_timeout_sec="300")
        context = AgentContext()
        asyncio.run(agent.run("write ok", ShellEnvironment(workdir), context))
        self.assertEqual((workdir / "answer.txt").read_text(), "ok\n")
        dumped = json.loads(context.model_dump_json())
        self.assertEqual(dumped["metadata"]["stop_reason"], "completed")
        self.assertEqual(dumped["metadata"]["deadline_source"], "agent_kwarg")
        self.assertEqual(dumped["n_input_tokens"], 0)
        self.assertIsNone(dumped["cost_usd"])

    def test_import_reads_harbor_trial_results(self):
        overrides = JobConfig.model_validate(plan_job("15")["job_config"]).override_error_policy

        def trial(name, reward, error=None, stop="completed"):
            result = TrialResult(task_name=name, trial_name=name + "__1", trial_uri="file:///x",
                                 task_id={"path": "/x/" + name}, task_checksum="0",
                                 config=TrialConfig(task=TaskConfig(path=Path("/x") / name)),
                                 agent_info=AgentInfo(name="baseagent", version="0"),
                                 agent_result=AgentContext(n_input_tokens=0, n_output_tokens=0,
                                                           metadata={"stop_reason": stop, "cost_cny": "0"}),
                                 verifier_result=None if reward is None else VerifierResult(rewards={"reward": reward}))
            if error is not None:
                try:
                    raise error
                except Exception as exc:  # noqa: BLE001
                    result.exception_info = ExceptionInfo.from_exception(exc, override_error_policy=overrides)
            return json.loads(result.model_dump_json())

        class AgentTimeoutError(Exception):
            pass

        class SandboxBuildFailedError(Exception):
            pass

        with tempfile.TemporaryDirectory() as directory:
            rows = [trial("pass", 1.0), trial("wrong", 0.0), trial("slow", 0.0, AgentTimeoutError("t")),
                    trial("budget", 0.0, BudgetExceededError("b"), "budget_stopped"),
                    trial("infra", None, BaseAgentInfrastructureError("i"), "infrastructure"),
                    trial("image", None, SandboxBuildFailedError("no such image"))]
            for index, row in enumerate(rows):
                path = Path(directory) / f"t{index}" / "result.json"
                path.parent.mkdir()
                path.write_text(json.dumps(row))
            report = import_results(directory)
        outcomes = {row["id"]: row["outcome"] for row in report["tasks"]}
        self.assertEqual(outcomes, {"pass": "passed", "wrong": "wrong_answer", "slow": "timeout",
                                    "budget": "budget_stopped", "infra": "infrastructure", "image": "infrastructure"})
        self.assertEqual(report["model_tasks"], 4)
        self.assertEqual(report["success_rate"], 0.25)


if __name__ == "__main__":
    unittest.main()
