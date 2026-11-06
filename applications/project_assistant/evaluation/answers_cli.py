"""CLI for answer scoring. --live prints the paid command and does not call a provider."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from applications.project_assistant.evaluation.answers import (  # noqa: E402
    attach_judge,
    evaluate_answers,
    load_answers,
    planned_live,
    synthetic_answers,
    write_answer_report,
)
from applications.project_assistant.evaluation.retrieval import QUESTION_SET  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=QUESTION_SET)
    parser.add_argument("--answers", type=Path, help="Fixture or recorded answers JSON")
    parser.add_argument("--synthetic", action="store_true", help="Score gold-shaped fake answers; cost 0")
    parser.add_argument("--judge", type=Path, help="Optional local judge JSON {id: {correct, reason}}; no network")
    parser.add_argument("--split", choices=("tune", "test", "all"), default="all")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--live", action="store_true", help="Print the paid command and stop")
    parser.add_argument("--budget-cny", default="2")
    args = parser.parse_args(argv)
    if args.live:
        print(json.dumps(planned_live(args.budget_cny), ensure_ascii=False, indent=2))
        return 0
    if args.synthetic == bool(args.answers):
        parser.error("choose exactly one of --synthetic or --answers")
    dataset = json.loads(args.questions.read_text(encoding="utf-8"))
    if args.split != "all":
        dataset = {**dataset, "questions": [q for q in dataset["questions"] if q["split"] == args.split]}
    records = synthetic_answers(dataset) if args.synthetic else load_answers(args.answers)
    judge = None
    if args.judge:
        payload = json.loads(args.judge.read_text(encoding="utf-8"))
        judge = payload.get("judgments", payload)
    if judge:
        records = attach_judge(records, judge)
    report = evaluate_answers(dataset, records, judge=judge)
    path = write_answer_report(report, args.report_dir)
    print(json.dumps({"report": path.name, "splits": list(report["splits"]),
                      "cost_cny": report["cost_cny"], "provider_calls": 0,
                      "live_executed": False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
