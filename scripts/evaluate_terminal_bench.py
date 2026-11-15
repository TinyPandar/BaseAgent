"""Terminal-Bench 2.0 through Harbor: plan the paid subset, grade the local fixture, import results.

  --plan       print the harbor run commands and job config for the 18-task subset; runs nothing
  --fixture    grade the local fixture with Harbor (oracle, scripted correct, scripted wrong);
               needs harbor==0.26.0 and Docker, makes no provider calls
  --import     read Harbor result.json files (job directories) into the report
  --synthetic  report smoke over synthetic records; not a Terminal-Bench score
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from applications.project_assistant.evaluation.terminal_bench import (  # noqa: E402
    import_results, load_subset, plan_job, run_fixture, score_records,
)

SYNTHETIC_RECORDS = [
    {"id": "synthetic-passed", "outcome": "passed", "steps": 3, "tokens": 0, "cost_cny": 0},
    {"id": "synthetic-wrong", "outcome": "wrong_answer", "steps": 5, "tokens": 0, "cost_cny": 0},
    {"id": "synthetic-timeout", "outcome": "timeout", "steps": 9, "tokens": 0, "cost_cny": 0},
    {"id": "synthetic-steps", "outcome": "steps_exhausted", "steps": 60, "tokens": 0, "cost_cny": 0},
    {"id": "synthetic-permission", "outcome": "permission_blocked", "steps": 2, "tokens": 0, "cost_cny": 0},
    {"id": "synthetic-budget", "outcome": "budget_stopped", "steps": 4, "tokens": 0, "cost_cny": 0},
    {"id": "synthetic-infrastructure", "outcome": "infrastructure", "steps": 0, "tokens": 0, "cost_cny": 0},
]


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="Print the paid subset job and stop")
    mode.add_argument("--fixture", action="store_true", help="Grade the local fixture with Harbor (needs Docker)")
    mode.add_argument("--import", dest="import_paths", nargs="+", type=Path, metavar="PATH",
                      help="Harbor job directories or result.json files")
    mode.add_argument("--synthetic", action="store_true", help="Report smoke over synthetic records")
    parser.add_argument("--budget-cny", default="15", help="--plan: total CNY cap for the subset")
    parser.add_argument("--model", default="deepseek-chat", help="--plan: model name passed to the agent")
    parser.add_argument("--max-command-timeout", type=int, default=600,
                        help="--plan: per-command cap in seconds; the task's remaining time always caps it too")
    parser.add_argument("--write-config", type=Path, help="--plan: also write the job config JSON here")
    parser.add_argument("--harbor", default="harbor", help="--fixture: harbor executable")
    parser.add_argument("--jobs-dir", type=Path, default=Path("jobs/terminal-bench-fixture"))
    args = parser.parse_args(argv)
    if args.plan:
        plan = plan_job(args.budget_cny, model=args.model, max_command_timeout_sec=args.max_command_timeout)
        if args.write_config:
            args.write_config.write_text(json.dumps(plan["job_config"], ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
            plan["config_file"] = str(args.write_config)
            plan["commands"] = {key: value.replace("terminal-bench-job.json", str(args.write_config))
                                for key, value in plan["commands"].items()}
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.synthetic:
        report = score_records(SYNTHETIC_RECORDS)
        report.update(synthetic=True, subset_size=len(load_subset()["task_ids"]), provider_calls=0,
                      note="Synthetic records for report plumbing only. This is not a Terminal-Bench score.")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.import_paths:
        report = import_results(*args.import_paths)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["tasks"] else 1
    result = run_fixture(args.harbor, args.jobs_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["jobs"]:
        print(f"Fixture NOT verified: {result['reason']}", file=sys.stderr)
        return 2
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
