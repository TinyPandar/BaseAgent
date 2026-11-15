"""Terminal-Bench 2.0 through Harbor: job plans, the local Harbor fixture, and result import.

Harbor (pinned 0.26.0) owns the containers, ``/app``, same-container grading, timeouts and
``result.json``. Nothing here runs tasks or grades them. This module

* builds the ``harbor run`` job config for the fixed 18-task subset (``plan_job``);
* builds the ``harbor run`` commands that grade the local fixture task with Harbor
  (``fixture_jobs``);
* imports Harbor ``result.json`` files into the existing report (``import_results``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from baseagent.tbench.errors import BaseAgentInfrastructureError, BudgetExceededError
from baseagent.tbench.settings import AGENT_IMPORT_PATH, HARBOR_VERSION

EVALUATION_DIR = Path(__file__).parent
SUBSET_FILE = EVALUATION_DIR / "terminal_bench_subset.json"
FIXTURE = EVALUATION_DIR / "fixtures" / "terminal_bench" / "local-echo"
INFRASTRUCTURE_ERROR = BaseAgentInfrastructureError.__name__
BUDGET_ERROR = BudgetExceededError.__name__
# Infrastructure: never graded, never a valid reward, never retried (a retry could spend money).
INFRASTRUCTURE_POLICY = {"should_retry": False, "should_grade": False, "has_valid_reward": False}
# Harbor's docker environment reports start, build and exec failures as plain RuntimeError, and
# Harbor's default for an unlisted name is "retry, grade, reward valid". The agent wraps every failure
# of its own, so in these jobs a RuntimeError always comes from Harbor or the environment.
INFRASTRUCTURE_NAMES = (INFRASTRUCTURE_ERROR, "RuntimeError")
# Budget stop: graded, reward invalid, not retried. Same as Harbor 0.26.0's default for this name,
# stated explicitly so an upgrade cannot change it silently.
BUDGET_POLICY = {"should_retry": False, "should_grade": True, "has_valid_reward": False}

OUTCOMES = ("passed", "wrong_answer", "timeout", "steps_exhausted", "permission_blocked", "budget_stopped",
            "infrastructure")
FAILURES = set(OUTCOMES) - {"passed"}
# Exceptions that describe the model's own run. Every other exception type is infrastructure,
# so an unknown error never turns into a wrong answer.
MODEL_SIDE_EXCEPTIONS = {"AgentTimeoutError", BUDGET_ERROR, "ContextWindowExceededError", "ContextLengthExceededError",
                         "OutputTokenExceededError", "OutputLengthExceededError", "AgentSafetyRefusalError",
                         "ContentPolicyViolationError"}
STOP_REASON_OUTCOMES = {"timeout": "timeout", "steps_exhausted": "steps_exhausted",
                        "permission_blocked": "permission_blocked", "budget_stopped": "budget_stopped",
                        "infrastructure": "infrastructure"}


def load_subset(path=None):
    data = json.loads(Path(path or SUBSET_FILE).read_text(encoding="utf-8"))
    if data.get("dataset") != "terminal-bench@2.0" or not data.get("task_ids"):
        raise ValueError("subset file must name terminal-bench@2.0 task ids")
    return data


# ---------------------------------------------------------------------------------------------
# Report


def classify(record):
    """Outcome of one report record. Infrastructure failures are not model-quality failures."""
    if record.get("outcome") in OUTCOMES:
        return record["outcome"]
    if record.get("infrastructure"):
        return "infrastructure"
    if record.get("budget_stopped"):
        return "budget_stopped"
    if record.get("permission_blocked") or record.get("sandbox_blocked"):
        return "permission_blocked"
    if record.get("steps_exhausted"):
        return "steps_exhausted"
    if record.get("timeout"):
        return "timeout"
    if record.get("passed"):
        return "passed"
    return "wrong_answer"


def score_records(records):
    rows = []
    for record in records:
        kind = classify(record)
        if kind not in OUTCOMES:
            raise ValueError("unknown outcome")
        rows.append({**record, "outcome": kind, "counts_as_model": kind != "infrastructure"})
    model_rows = [row for row in rows if row["counts_as_model"]]
    passed = sum(row["outcome"] == "passed" for row in model_rows)
    costs = [Decimal(str(row.get("cost_cny") or 0)) for row in rows]
    return {"tasks": rows, "model_tasks": len(model_rows), "passed": passed,
            "success_rate": passed / len(model_rows) if model_rows else None,
            "outcomes": {name: sum(row["outcome"] == name for row in rows) for name in OUTCOMES},
            "infrastructure": sum(row["outcome"] == "infrastructure" for row in rows),
            "infrastructure_tasks": [row.get("id") for row in rows if row["outcome"] == "infrastructure"],
            "tokens": sum(row.get("tokens") or 0 for row in rows),
            "cost_cny": float(sum(costs, Decimal(0))),
            "provider_calls": sum(row.get("provider_calls") or 0 for row in rows)}


def _reward(result):
    verifier = result.get("verifier_result") or {}
    rewards = verifier.get("rewards") or {}
    if "reward" in rewards:
        return rewards["reward"]
    if len(rewards) == 1:
        return next(iter(rewards.values()))
    return None


def _exceptions(result):
    found = [result["exception_info"]] if result.get("exception_info") else []
    found += [step["exception_info"] for step in result.get("step_results") or [] if step.get("exception_info")]
    return found


def classify_trial(result: dict) -> dict:
    """One Harbor ``TrialResult`` (parsed result.json) as a report record."""
    exceptions = _exceptions(result)
    types = [item.get("exception_type") for item in exceptions]
    agent = result.get("agent_result") or {}
    metadata = agent.get("metadata") or {}
    reward = _reward(result)
    policies = [item.get("policy") or {} for item in exceptions]
    reward_valid = reward is not None and all(policy.get("has_valid_reward", True) for policy in policies)
    stop = metadata.get("stop_reason")
    if any(name not in MODEL_SIDE_EXCEPTIONS for name in types) or stop == "infrastructure":
        outcome = "infrastructure"
    elif BUDGET_ERROR in types or stop == "budget_stopped":
        outcome = "budget_stopped"
    elif reward is None or not reward_valid:
        # Graded nothing usable without a model-side reason: the grading itself failed.
        outcome = "infrastructure"
    elif float(reward) >= 1:
        outcome = "passed"
    elif "AgentTimeoutError" in types:
        outcome = "timeout"
    else:
        outcome = STOP_REASON_OUTCOMES.get(stop, "wrong_answer")
    tokens = (agent.get("n_input_tokens") or 0) + (agent.get("n_output_tokens") or 0)
    return {"id": result.get("task_name"), "trial": result.get("trial_name"), "outcome": outcome,
            "reward": reward, "exception_types": types, "stop_reason": stop,
            "steps": metadata.get("steps"), "tokens": tokens,
            "cost_cny": metadata.get("cost_cny") if metadata.get("cost_cny") is not None else 0,
            "cost_usd": agent.get("cost_usd"), "provider_calls": metadata.get("provider_calls") or 0,
            "model": metadata.get("model")}


def trial_results(path):
    """Trial ``result.json`` files under a Harbor job directory (or one result.json)."""
    path = Path(path)
    if path.is_file():
        return [json.loads(path.read_text(encoding="utf-8"))]
    found = []
    for candidate in sorted(path.rglob("result.json")):
        data = json.loads(candidate.read_text(encoding="utf-8"))
        if "trial_name" in data and "task_name" in data:   # skip the job-level result.json
            found.append(data)
    return found


def import_results(*paths):
    records = [classify_trial(result) for path in paths for result in trial_results(path)]
    report = score_records(records)
    report["source"] = "harbor result.json"
    return report


# ---------------------------------------------------------------------------------------------
# Job plans


def per_task_budget(budget_cny, tasks):
    total = Decimal(str(budget_cny))
    if not total > 0:
        raise ValueError("budget_cny must be positive")
    return (total / tasks).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def plan_job(budget_cny="15", *, model="deepseek-chat", max_command_timeout_sec=600, max_steps=60,
             jobs_dir="jobs/terminal-bench", job_name="baseagent-tb2-subset18", subset=None):
    """The JobConfig for the paid subset run. Printed by ``--plan``; never executed here.

    Task timeouts and resources stay exactly as the tasks define them: timeout_multiplier is 1.0
    and no override, maximum or resource field is set. Trials run one at a time (-n 1) and each
    task gets an equal share of the CNY budget as its own hard cap, so the job total stays within
    ``budget_cny`` without a shared ledger.
    """
    subset = subset or load_subset()
    tasks = subset["task_ids"]
    share = per_task_budget(budget_cny, len(tasks))
    registry = subset["source"].replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/")
    name, _, version = subset["dataset"].partition("@")
    config = {
        "job_name": job_name,
        "jobs_dir": jobs_dir,
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "timeout_multiplier": 1.0,
        "retry": {"max_retries": 0},
        "override_error_policy": {**{name: dict(INFRASTRUCTURE_POLICY) for name in INFRASTRUCTURE_NAMES},
                                  BUDGET_ERROR: dict(BUDGET_POLICY)},
        "environment": {"type": "docker"},
        "agents": [{"import_path": AGENT_IMPORT_PATH, "model_name": model,
                    "kwargs": {"budget_cny": str(share), "max_command_timeout_sec": max_command_timeout_sec,
                               "max_steps": max_steps}}],
        "datasets": [{"name": name, "version": version, "registry_url": registry, "task_names": list(tasks)}],
    }
    return {"executed": False, "provider_calls": 0, "cost_cny": 0, "status": "authorization_required",
            "harbor_version": HARBOR_VERSION, "budget_cny": str(Decimal(str(budget_cny))),
            "budget_cny_per_task": str(share), "tasks": len(tasks),
            "commands": {"validate": "harbor run -c terminal-bench-job.json --dry-run",
                         "run": "harbor run -c terminal-bench-job.json"},
            "environment": "PYTHONPATH=src in a Python environment with harbor==0.26.0; provider keys only on the host",
            "note": ("Paid. Needs a separate authorization; this command only prints. Per-task budget caps are checked "
                     "before each model call, so one call already in flight can exceed a task's share."),
            "job_config": config}


# ---------------------------------------------------------------------------------------------
# Local fixture graded by Harbor


FIXTURE_AGENTS = {
    "oracle": {"args": ["-a", "oracle"], "expect": 1.0},
    "scripted-correct": {"args": ["-a", AGENT_IMPORT_PATH, "--ak", "scripted=correct"], "expect": 1.0},
    "scripted-wrong": {"args": ["-a", AGENT_IMPORT_PATH, "--ak", "scripted=wrong"], "expect": 0.0},
}


def fixture_jobs(harbor="harbor", jobs_dir="jobs/terminal-bench-fixture", task_dir=None):
    task_dir = str(Path(task_dir or FIXTURE).resolve())
    return {name: [harbor, "run", "-p", task_dir, *spec["args"], "-n", "1", "--job-name", f"fixture-{name}",
                   "-o", str(jobs_dir), "-y", "-q"] for name, spec in FIXTURE_AGENTS.items()}


def docker_status():
    """(available, reason). Harbor's docker environment needs a running Docker daemon."""
    if shutil.which("docker") is None:
        return False, "docker is not installed or not on PATH"
    try:
        completed = subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker info failed: {type(exc).__name__}"
    if completed.returncode != 0:
        return False, "docker daemon is not running"
    return True, "docker available"


def run_fixture(harbor="harbor", jobs_dir="jobs/terminal-bench-fixture", *, env=None, runner=None):
    """Grade the fixture with Harbor: official oracle, scripted correct, scripted wrong.

    Returns the imported report plus whether each job got the expected reward. Requires Docker;
    without it the result is ``verified: False`` and nothing is claimed.
    """
    if runner is None:
        available, reason = docker_status()
        if not available:
            return {"verified": False, "reason": reason, "jobs": {}}
        if shutil.which(harbor) is None:
            return {"verified": False, "reason": f"{harbor} is not installed (pip install harbor=={HARBOR_VERSION})",
                    "jobs": {}}
        runner = subprocess.run
    else:
        reason = "injected runner (test)"
    if env is None:
        # Harbor imports the adapter from this checkout.
        source = str(EVALUATION_DIR.parents[2] / "src")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [source, os.environ.get("PYTHONPATH")]))}
    jobs = {}
    for name, argv in fixture_jobs(harbor, jobs_dir).items():
        completed = runner(argv, env=env, check=False)
        job_dir = Path(jobs_dir) / f"fixture-{name}"
        report = import_results(job_dir) if job_dir.exists() else {"tasks": []}
        rewards = [row["reward"] for row in report["tasks"]]
        jobs[name] = {"exit_code": completed.returncode, "rewards": rewards,
                      "outcomes": [row["outcome"] for row in report["tasks"]],
                      "cost_cny": report.get("cost_cny"), "provider_calls": report.get("provider_calls"),
                      "as_expected": rewards == [FIXTURE_AGENTS[name]["expect"]]}
    return {"verified": all(job["as_expected"] for job in jobs.values()), "reason": reason, "jobs": jobs}
