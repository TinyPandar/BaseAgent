"""Compare M1 spill/guard coding runs with an existing baseline report.

Live recording is opt-in and uses the same bounded, redacted cassette machinery
as record_coding_scenario. --dry-run is fully offline.
"""
import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from applications.project_assistant.evaluation.coding import run_scenario, scenario_workspace
from baseagent.main import initialize_model, model_configuration
from baseagent.testing import CassetteModel
from scripts.record_coding_scenario import (
    BoundedModel, REPORTS_DIR, cassette_redactor, outcome, replay, scan_cassette, signature,
)


def _mean(runs, key):
    values = [float(run[key]) for run in runs if isinstance(run.get(key), (int, float))]
    return sum(values) / len(values) if values else None


def comparison_summary(before_runs, after_runs):
    def summarize(runs):
        passed = [bool(run.get("external_acceptance_passed")) for run in runs]
        return {"runs": len(runs), "success_rate": sum(passed) / len(passed) if passed else None,
                **{key: _mean(runs, key) for key in ("total_tokens", "prompt_tokens", "completion_tokens", "cost_cny")}}
    before, after = summarize(before_runs), summarize(after_runs)
    base_tokens, new_tokens = before["total_tokens"], after["total_tokens"]
    reduction = (base_tokens - new_tokens) / base_tokens if base_tokens else None
    return {"before": before, "after": after, "total_tokens_reduction": reduction,
            "targets": {"success_rate": 0.8, "total_tokens_reduction": 0.3},
            "passed": {"success_rate": after["success_rate"] is not None and after["success_rate"] >= 0.8,
                       "total_tokens_reduction": reduction is not None and reduction >= 0.3}}


def _report_markdown(summary):
    before, after = summary["before"], summary["after"]
    reduction = summary["total_tokens_reduction"]
    def fmt(value):
        return "n/a" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)
    lines = ["# M1 coding scenario comparison", "", "| Metric | Baseline | M1 |", "|---|---:|---:|"]
    for label, key in (("Success rate", "success_rate"), ("Mean total tokens", "total_tokens"),
                       ("Mean prompt tokens", "prompt_tokens"), ("Mean completion tokens", "completion_tokens"),
                       ("Mean cost (CNY)", "cost_cny")):
        lines.append(f"| {label} | {fmt(before[key])} | {fmt(after[key])} |")
    lines += ["", f"Total token reduction: {fmt(reduction)} (target >= 0.300).",
              f"Success target >= 0.800: {'PASS' if summary['passed']['success_rate'] else 'FAIL'}.",
              f"Token reduction target >= 0.300: {'PASS' if summary['passed']['total_tokens_reduction'] else 'FAIL'}.", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--budget-cny", type=Decimal)
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("tests/cassettes/coding_m1"))
    parser.add_argument("--report-dir", type=Path, default=REPORTS_DIR)
    parser.add_argument("--spill-threshold-tokens", type=int, default=2000)
    parser.add_argument("--wrapup-hint-steps", type=int, default=3)
    parser.add_argument("--repeat-call-guard", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dedupe-reads", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.spill_threshold_tokens < 0 or args.wrapup_hint_steps < 0:
        parser.error("thresholds must be nonnegative")
    try:
        baseline_report = json.loads(args.baseline_report.read_text(encoding="utf-8"))
        baseline_runs = baseline_report["runs"]
        if not isinstance(baseline_runs, list):
            raise ValueError("runs must be a list")
    except (OSError, ValueError, KeyError, TypeError):
        parser.error("baseline report must exist, be readable JSON, and contain a runs list")
    runtime_config = {"spill": {"threshold_tokens": args.spill_threshold_tokens},
                      "guards": {"wrapup_hint_steps": args.wrapup_hint_steps,
                                 "repeat_call_guard": args.repeat_call_guard, "dedupe_reads": args.dedupe_reads}}
    if args.dry_run:
        print(json.dumps({"planned_live_command": f"python scripts/compare_coding_m1.py --runs {args.runs} --budget-cny {args.budget_cny if args.budget_cny is not None else '<CNY>'} --baseline-report {args.baseline_report}",
                          "runtime_config": runtime_config}, ensure_ascii=False))
        return 0
    if args.budget_cny is None:
        parser.error("--budget-cny is required for live mode")
    try:
        configuration = model_configuration(generic_only=True)
    except ValueError:
        print("pending: complete BASEAGENT_LLM_* configuration required; no model calls made.")
        return 2
    key, url = configuration["api_key"], configuration["base_url"]
    meter = BoundedModel(None, budget_cny=args.budget_cny)
    results = []
    if any((args.output_dir / f"run_{number}.json").exists() for number in range(1, args.runs + 1)):
        print("pending: refusing to overwrite an existing cassette.")
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with initialize_model(generic_only=True, timeout=30, max_completion_tokens=1024,
                              generation={"temperature": 0}) as client:
            meter.client = client
            meter.model_name, meter.generation_params = client.model_name, client.generation_params
            meter.max_completion_tokens = client.max_completion_tokens
            for number in range(1, args.runs + 1):
                if meter.stopped:
                    break
                meter.start_run()
                cassette_path = args.output_dir / f"run_{number}.json"
                with scenario_workspace() as root:
                    model = CassetteModel(cassette_path, meter, mode="record", redactor=cassette_redactor(root, key, url))
                    state = None
                    try:
                        result, state, ledger = run_scenario(model, root, runtime_config=runtime_config)
                        observed = outcome(result, state, ledger, root)
                        reason = meter.reason or state.error
                    except Exception as exc:
                        result, observed, reason = {"external_acceptance_passed": False}, None, type(exc).__name__
                    finally:
                        model.close()
                scan_cassette(cassette_path, key, url)
                consistent = (observed is not None and cassette_path.is_file()
                              and replay(cassette_path, details=True, runtime_config=runtime_config) == observed)
                results.append({"run": number, **signature(result),
                                "total_tokens": None if state is None else state.total_tokens,
                                "prompt_tokens": None if state is None else state.prompt_tokens,
                                "completion_tokens": None if state is None else state.completion_tokens,
                                "cost_cny": str(meter.run_cost_cny), "reason": reason,
                                "replay_consistent": consistent, "outcome": observed})
        report = {"runs": results, "cost_cny": str(meter.cost_cny), "budget_cny": str(meter.budget),
                  "runtime_config": runtime_config}
        args.report_dir.mkdir(parents=True, exist_ok=True)
        docs_reports = Path("docs/reports")
        docs_reports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
        report_path = args.report_dir / f"coding-m1-{stamp}.json"
        summary = comparison_summary(baseline_runs, results)
        report["comparison"] = summary
        report_path.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
        (docs_reports / f"m1_coding_comparison-{stamp}.md").write_text(_report_markdown(summary), encoding="utf-8")
        print(json.dumps({"runs_saved": len(results), "cost_cny": str(meter.cost_cny),
                          "report": str(report_path), "comparison_md": str(docs_reports / f"m1_coding_comparison-{stamp}.md")},
                         ensure_ascii=False))
        return 0 if len(results) == args.runs and all(summary["passed"].values()) else 1
    except Exception as exc:
        print(f"pending: recording/check failed ({type(exc).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
