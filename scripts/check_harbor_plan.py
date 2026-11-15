"""Validate the --plan job config with Harbor itself. Runs no trials and calls no model.

Run in a Python environment that has harbor==0.26.0 (see docs/terminal_bench.md):

    PYTHONPATH=src python scripts/check_harbor_plan.py [--config FILE]

Steps, all Harbor 0.26.0 code: JobConfig parsing, ``validate_job_config``, the agent
preflight, ``validate_local_sources`` (dataset metadata, no task download), and the
dataset's task list compared with ``terminal_bench_subset.json``. These are the checks of
``harbor run --dry-run`` except the environment preflight, which needs Docker; when Docker
is available, also run ``harbor run -c FILE --dry-run``.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from applications.project_assistant.evaluation.terminal_bench import (  # noqa: E402
    docker_status, load_subset, plan_job,
)


async def check(config_path):
    from harbor.agents.factory import AgentFactory
    from harbor.cli.config_validation import print_validation_report, validate_job_config, validate_local_sources
    from harbor.models.job.config import JobConfig

    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    config = JobConfig.model_validate(data)
    print_validation_report(await validate_job_config(config))
    for agent in config.agents:
        AgentFactory.run_preflight(agent, skip_credential_checks=config.skip_credential_checks)
    report = await validate_local_sources(config)
    print_validation_report(report)
    subset = load_subset()
    tasks = await config.datasets[0].model_copy(deep=True).get_task_configs(disable_verification=False)
    names = sorted(task.path.name if task.path else task.name for task in tasks)
    commits = sorted({task.git_commit_id for task in tasks})
    result = {"harbor_checks": "passed", "resolved_tasks": len(names),
              "matches_subset": names == sorted(subset["task_ids"]),
              "dataset_commits": commits, "matches_subset_commit": commits == [subset["dataset_commit"]]}
    available, reason = docker_status()
    result["environment_preflight"] = ("run `harbor run -c FILE --dry-run` too" if available
                                       else f"not run here: {reason}")
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="job config written by evaluate_terminal_bench.py --plan")
    args = parser.parse_args(argv)
    path = args.config
    if path is None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(plan_job()["job_config"], handle)
        handle.close()
        path = Path(handle.name)
    result = asyncio.run(check(path))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["matches_subset"] and result["matches_subset_commit"] else 1


if __name__ == "__main__":
    sys.exit(main())
