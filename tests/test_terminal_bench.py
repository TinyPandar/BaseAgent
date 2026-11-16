"""Terminal-Bench plan, fixture and result import. No dataset download, no model, no Docker."""

import io
import json
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from applications.project_assistant.evaluation import terminal_bench as tb
from applications.project_assistant.evaluation.terminal_bench import (
    FIXTURE, classify, classify_trial, import_results, load_subset, plan_job, run_fixture, score_records,
)
from scripts import evaluate_terminal_bench


def trial(name, reward, *, error=None, policy=None, stop="completed", tokens=(0, 0), cost="0", provider_calls=0,
          agent=None):
    """A parsed Harbor 0.26.0 TrialResult with the fields the importer reads."""
    exception = None
    if error is not None:
        exception = {"exception_type": error, "exception_message": "x", "exception_traceback": "",
                     "occurred_at": "2026-11-15T00:00:00Z",
                     "policy": policy or {"should_retry": True, "should_grade": True, "has_valid_reward": True}}
    return {"task_name": name, "trial_name": name + "__abc", "exception_info": exception,
            "config": {"agent": agent or {"name": "oracle"}},
            "verifier_result": None if reward is None else {"rewards": {"reward": reward}},
            "agent_result": {"n_input_tokens": tokens[0], "n_output_tokens": tokens[1], "cost_usd": None,
                             "metadata": {"stop_reason": stop, "cost_cny": cost, "provider_calls": provider_calls,
                                          "steps": 3}}}


INFRA_POLICY = {"should_retry": False, "should_grade": False, "has_valid_reward": False}
BUDGET_POLICY = {"should_retry": False, "should_grade": True, "has_valid_reward": False}


class PlanTests(unittest.TestCase):
    def test_plan_only_prints_and_keeps_task_timeouts_and_resources(self):
        subset = load_subset()
        self.assertEqual(len(subset["task_ids"]), 18)
        plan = plan_job("15")
        self.assertFalse(plan["executed"])
        self.assertEqual(plan["provider_calls"], 0)
        config = plan["job_config"]
        self.assertEqual(config["timeout_multiplier"], 1.0)
        forbidden = {"agent_timeout_multiplier", "verifier_timeout_multiplier", "agent_setup_timeout_multiplier",
                     "environment_build_timeout_multiplier", "verifier"}
        self.assertFalse(forbidden & set(config))
        self.assertEqual(set(config["environment"]), {"type"})
        agent = config["agents"][0]
        self.assertFalse({"override_timeout_sec", "max_timeout_sec", "override_setup_timeout_sec"} & set(agent))
        self.assertEqual(agent["import_path"], "baseagent.tbench.harbor_agent:BaseAgentHarborAgent")
        policy = config["override_error_policy"]
        self.assertEqual(policy["BaseAgentInfrastructureError"],
                         {"should_retry": False, "should_grade": False, "has_valid_reward": False})
        self.assertEqual(policy["BudgetExceededError"],
                         {"should_retry": False, "should_grade": True, "has_valid_reward": False})
        dataset = config["datasets"][0]
        self.assertEqual(dataset["task_names"], subset["task_ids"])
        self.assertIn("7367e809aeed7cc01060e6d23245fb46c3310089", dataset["registry_url"])
        self.assertEqual(config["n_concurrent_trials"], 1)
        self.assertEqual(config["retry"], {"max_retries": 0})
        # Equal per-task caps never add up to more than the total.
        self.assertLessEqual(float(plan["budget_cny_per_task"]) * 18, 15)
        self.assertNotIn("api_key", json.dumps(plan).lower())

    def test_cli_plan_writes_the_config_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("subprocess.run") as run:
            target = Path(directory) / "job.json"
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(evaluate_terminal_bench.main(["--plan", "--write-config", str(target)]), 0)
            run.assert_not_called()
            self.assertEqual(json.loads(target.read_text()), json.loads(out.getvalue())["job_config"])
            self.assertIn(str(target), json.loads(out.getvalue())["commands"]["validate"])


class FixtureTests(unittest.TestCase):
    def test_fixture_is_a_harbor_task_graded_in_the_same_container(self):
        config = tomllib.loads((FIXTURE / "task.toml").read_text())
        self.assertEqual(config["verifier"]["environment_mode"], "shared")
        self.assertIn("timeout_sec", config["agent"])
        for path in ("instruction.md", "environment/Dockerfile", "solution/solve.sh", "tests/test.sh"):
            self.assertTrue((FIXTURE / path).is_file(), path)
        test = (FIXTURE / "tests/test.sh").read_text()
        self.assertIn("/logs/verifier/reward.txt", test)
        self.assertIn("echo 0", test)
        self.assertIn("/app/answer.txt", (FIXTURE / "solution/solve.sh").read_text())

    def test_fixture_without_docker_is_not_verified(self):
        err = io.StringIO()
        with mock.patch.object(tb, "docker_status", return_value=(False, "docker is not installed or not on PATH")), \
                mock.patch("subprocess.run") as run, redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(evaluate_terminal_bench.main(["--fixture"]), 2)
        run.assert_not_called()
        self.assertIn("NOT verified", err.getvalue())

    @staticmethod
    def harbor_writing(rewards, *, exit_code=0, agents=None):
        """A stand-in for ``harbor run`` that writes one trial result per job, like Harbor does."""
        def run(argv, env=None, check=False):
            job = argv[argv.index("--job-name") + 1]
            name = next(key for key in tb.FIXTURE_AGENTS if job.startswith(f"fixture-{key}-"))
            if name in rewards:
                spec = tb.FIXTURE_AGENTS[name]["agent"]
                agent = (agents or {}).get(name) or (
                    {"name": "oracle"} if "name" in spec else
                    # The shape `harbor run -a module:Class --ak scripted=...` records (checked with --print-config).
                    {"name": spec["import_path"], "import_path": None, "kwargs": {"scripted": spec["scripted"]}})
                path = Path(argv[argv.index("-o") + 1]) / job / "local-echo__x" / "result.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(trial("local-echo", rewards[name], agent=agent)))
            return SimpleNamespace(returncode=exit_code)
        return run

    EXPECTED = {"oracle": 1.0, "scripted-correct": 1.0, "scripted-wrong": 0.0}

    def test_fixture_grading_comes_from_harbor_results(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_fixture("harbor", Path(directory), runner=self.harbor_writing(self.EXPECTED))
            self.assertTrue(result["verified"], result)
            self.assertEqual(result["jobs"]["scripted-wrong"]["outcomes"], ["wrong_answer"])
            self.assertEqual(result["jobs"]["scripted-correct"]["outcomes"], ["passed"])
            self.assertEqual(result["jobs"]["oracle"]["cost_cny"], 0)
        with tempfile.TemporaryDirectory() as directory:      # a grader that passes everything is caught
            everything = dict.fromkeys(self.EXPECTED, 1.0)
            self.assertFalse(run_fixture("harbor", Path(directory), runner=self.harbor_writing(everything))["verified"])

    def test_old_results_never_count_for_a_failed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            # Passing results from earlier runs, under the old fixed names and under a reused run id.
            for name, reward in self.EXPECTED.items():
                for job in (f"fixture-{name}", tb.fixture_job_name(name, "old")):
                    path = jobs / job / "t" / "result.json"
                    path.parent.mkdir(parents=True)
                    path.write_text(json.dumps(trial("local-echo", reward)))
            failed = run_fixture("harbor", jobs, runner=lambda argv, env=None, check=False: SimpleNamespace(returncode=1))
            self.assertFalse(failed["verified"])
            self.assertTrue(all(job["rewards"] == [] for job in failed["jobs"].values()))
            reused = run_fixture("harbor", jobs, runner=self.harbor_writing(self.EXPECTED), run_id="old")
            self.assertFalse(reused["verified"])
            self.assertIn("job directory existed before this run", reused["jobs"]["oracle"]["problems"])

    def test_nonzero_exit_or_wrong_agent_is_not_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            crashed = run_fixture("harbor", Path(directory), runner=self.harbor_writing(self.EXPECTED, exit_code=1))
            self.assertFalse(crashed["verified"])
        with tempfile.TemporaryDirectory() as directory:
            swapped = self.harbor_writing(self.EXPECTED, agents={"scripted-wrong": {"name": "oracle"}})
            result = run_fixture("harbor", Path(directory), runner=swapped)
            self.assertFalse(result["verified"])
            self.assertIn("trial result belongs to a different agent", result["jobs"]["scripted-wrong"]["problems"])

    def test_no_hardcoded_pass_remains(self):
        source = Path(evaluate_terminal_bench.__file__).read_text() + Path(tb.__file__).read_text()
        self.assertNotIn('"passed": True', source)
        self.assertNotIn("def executor", source)


class ImportTests(unittest.TestCase):
    def test_trial_outcomes(self):
        cases = {
            "passed": trial("a", 1.0, tokens=(120, 30), cost="0.0002", provider_calls=2),
            "wrong_answer": trial("b", 0.0),
            "timeout": trial("c", 0.0, error="AgentTimeoutError"),
            "steps_exhausted": trial("d", 0.0, stop="steps_exhausted"),
            "permission_blocked": trial("e", 0.0, stop="permission_blocked"),
            "budget_stopped": trial("f", 0.0, error="BudgetExceededError", policy=BUDGET_POLICY, stop="budget_stopped"),
            "infrastructure": trial("g", None, error="BaseAgentInfrastructureError", policy=INFRA_POLICY,
                                    stop="infrastructure"),
        }
        for expected, result in cases.items():
            self.assertEqual(classify_trial(result)["outcome"], expected, expected)
        # A pass is a pass even if the agent ran out of steps or time afterwards.
        self.assertEqual(classify_trial(trial("h", 1.0, stop="steps_exhausted"))["outcome"], "passed")
        self.assertEqual(classify_trial(trial("i", 1.0, error="AgentTimeoutError"))["outcome"], "passed")

    def test_infrastructure_failures_are_never_wrong_answers(self):
        # Missing image, a failing backend, an unknown exception, and a grader that produced no reward.
        for result in (trial("image", None, error="SandboxBuildFailedError", policy=INFRA_POLICY | {"should_retry": True}),
                       trial("docker", None, error="RuntimeError"),
                       trial("unknown", 0.0, error="KeyError"),
                       trial("backend", None, error="BaseAgentInfrastructureError", policy=INFRA_POLICY),
                       trial("verifier", None, error="RewardFileNotFoundError",
                             policy={"should_retry": False, "should_grade": True, "has_valid_reward": False}),
                       trial("nothing", None)):
            self.assertEqual(classify_trial(result)["outcome"], "infrastructure", result["task_name"])

    def test_report_excludes_infrastructure_from_the_success_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory) / "job"
            rows = [trial("pass", 1.0, tokens=(100, 10), cost="0.0001", provider_calls=1), trial("wrong", 0.0),
                    trial("infra", None, error="BaseAgentInfrastructureError", policy=INFRA_POLICY),
                    trial("image", None, error="SandboxBuildFailedError", policy=INFRA_POLICY)]
            for row in rows:
                path = job / row["trial_name"] / "result.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(row))
            (job / "result.json").write_text(json.dumps({"id": "job-level", "stats": {}}))
            report = import_results(job)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(evaluate_terminal_bench.main(["--import", str(job)]), 0)
        self.assertEqual(len(report["tasks"]), 4)
        self.assertEqual(report["model_tasks"], 2)
        self.assertEqual(report["success_rate"], 0.5)
        self.assertEqual(report["infrastructure"], 2)
        self.assertEqual(sorted(report["infrastructure_tasks"]), ["image", "infra"])
        self.assertEqual(report["tokens"], 110)
        self.assertAlmostEqual(report["cost_cny"], 0.0001)
        self.assertEqual(json.loads(out.getvalue())["success_rate"], 0.5)

    def test_legacy_record_flags_still_classify(self):
        self.assertEqual(classify({"infrastructure": True, "passed": False}), "infrastructure")
        self.assertEqual(classify({"sandbox_blocked": True}), "permission_blocked")
        mixed = score_records([{"id": "ok", "passed": True}, {"id": "bad"}, {"id": "infra", "infrastructure": True}])
        self.assertEqual((mixed["model_tasks"], mixed["passed"], mixed["infrastructure"]), (2, 1, 1))

    def test_synthetic_report_is_labeled(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(evaluate_terminal_bench.main(["--synthetic"]), 0)
        report = json.loads(out.getvalue())
        self.assertTrue(report["synthetic"])
        self.assertIn("not a Terminal-Bench score", report["note"])
        self.assertEqual(report["provider_calls"], 0)
        self.assertEqual(set(report["outcomes"]), set(tb.OUTCOMES))


if __name__ == "__main__":
    unittest.main()
