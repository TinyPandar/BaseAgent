"""Score a local Terminal-Bench fixture, or print the paid subset command and stop."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from applications.project_assistant.evaluation.terminal_bench import (  # noqa: E402
    load_subset, planned_live, run_fixture, score_records,
)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", help="Score the local fake task; no model and no dataset download")
    parser.add_argument("--live", action="store_true", help="Print the paid subset command and stop")
    parser.add_argument("--budget-cny", default="15")
    args = parser.parse_args(argv)
    if args.live:
        print(json.dumps(planned_live(args.budget_cny), ensure_ascii=False, indent=2))
        return 0
    if not args.fixture:
        parser.error("choose --fixture for the offline pipe, or --live to print the paid command")
    subset = load_subset()

    def executor(argv):
        return {"passed": True, "steps": 1, "tokens": 0, "cost_cny": 0, "backend": "fixture"}

    report = run_fixture(executor=executor)
    report["subset_size"] = len(subset["task_ids"])
    report["live_executed"] = False
    report["provider_calls"] = 0
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["success_rate"] != 1:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
