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
        runtime = policy_for("RuntimeError", config.override_error_policy["RuntimeError"])
        self.assertEqual((runtime.should_grade, runtime.has_valid_reward), (False, False))

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

    def test_harbor_records_no_credentials_from_error_text(self):
        import os
        from unittest import mock
        from baseagent.tbench.harbor_agent import BaseAgentHarborAgent
        secret = "sk-harbor-test-secret-0123456789"

        class LeakyModel:
            def complete(self, messages, tools):
                raise RuntimeError(f"provider said: invalid api key {secret}")

        workdir = temporary_workdir(self)
        with mock.patch.dict(os.environ, {"BASEAGENT_LLM_API_KEY": secret}):
            agent = BaseAgentHarborAgent(workdir.parent / "logs", "x", budget_cny="1", agent_timeout_sec="300")
            agent._model = lambda: LeakyModel()
            context = AgentContext()
            try:
                asyncio.run(agent.run("t", ShellEnvironment(workdir), context))
            except Exception as exc:  # noqa: BLE001 -- recorded the way Harbor's trial does
                info = ExceptionInfo.from_exception(exc)
        self.assertEqual(info.exception_type, "BaseAgentInfrastructureError")
        for text in (info.exception_message, info.exception_traceback, context.model_dump_json(),
                     (workdir.parent / "logs" / "baseagent-summary.json").read_text()):
            self.assertNotIn(secret, text)
        self.assertIn("invalid api key", info.exception_message)

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


@unittest.skipUnless(HARBOR, "harbor==0.26.0 is not installed in this environment")
class HarborFailureRunTests(unittest.TestCase):
    """Real ``harbor run`` trials whose environment fails on purpose. No container, no model."""

    def run_job(self, directory, name, environment):
        import shutil
        import subprocess
        import sys
        harbor = Path(sys.executable).parent / "harbor"
        if not harbor.exists():
            harbor = Path(shutil.which("harbor") or "harbor")
        config = plan_job("15")["job_config"]
        job = {"job_name": name, "jobs_dir": str(Path(directory) / "jobs"), "n_concurrent_trials": 1,
               "timeout_multiplier": 1.0, "retry": {"max_retries": 0},
               "override_error_policy": config["override_error_policy"],
               "environment": {"import_path": f"harbor_support.broken_environments:{environment}"},
               "agents": [{"import_path": config["agents"][0]["import_path"], "kwargs": {"scripted": "correct"}}],
               "tasks": [{"path": str(FIXTURE)}]}
        path = Path(directory) / f"{name}.json"
        path.write_text(json.dumps(job))
        root = Path(__file__).resolve().parents[1]
        env = {**__import__("os").environ,
               "PYTHONPATH": f"{root / 'src'}:{root / 'tests'}:{root}"}
        completed = subprocess.run([str(harbor), "run", "-c", str(path), "-y", "-q"], cwd=directory, env=env,
                                   capture_output=True, text=True, timeout=300)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return Path(directory) / "jobs" / name

    def test_missing_image_and_failing_backend_are_infrastructure(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = self.run_job(directory, "missing-image", "MissingImageEnvironment")
            failing = self.run_job(directory, "failing-exec", "FailingExecEnvironment")
            report = import_results(missing, failing)
            raw = [json.loads(path.read_text()) for path in sorted(failing.glob("*/result.json"))]
        self.assertEqual([row["outcome"] for row in report["tasks"]], ["infrastructure", "infrastructure"])
        self.assertEqual(report["model_tasks"], 0)
        self.assertIsNone(report["success_rate"])
        self.assertEqual(sorted(name for row in report["tasks"] for name in row["exception_types"]),
                         ["BaseAgentInfrastructureError", "RuntimeError"])
        info = raw[0]["exception_info"]
        self.assertEqual(info["policy"], {"should_retry": False, "should_grade": False, "has_valid_reward": False})
        self.assertIsNone(raw[0]["verifier_result"])


if __name__ == "__main__":
    unittest.main()
