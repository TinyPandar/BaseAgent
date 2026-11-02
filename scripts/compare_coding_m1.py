"""Compare M1 spill/guard coding runs with an existing baseline report.

Live recording is opt-in and uses the same bounded, redacted cassette machinery
as record_coding_scenario. --dry-run is fully offline.
"""
import argparse
import json
import sys
import tempfile
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
    values = []
    for run in runs:
        try:
            values.append(float(run[key]))
        except (KeyError, TypeError, ValueError):
            pass
    return sum(values) / len(values) if values else None


def finalize_cassette(path, configuration, key="", url=""):
    """Stamp the live scenario policy into the header so replay uses v3, then scan.

    The rewrite is flushed and re-read before scan_cassette so a failed stamp cannot
    be mistaken for a successful finalize (replay defaults unversioned headers to v1).
    """
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    header["base_url"] = "<redacted-endpoint>"
    header["scenario_policy_version"] = 3
    if "extra_body" in configuration:
        header["extra_body"] = configuration["extra_body"]
    lines[0] = json.dumps(header, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")
    stamped = json.loads(path.read_text().splitlines()[0])
    if stamped.get("scenario_policy_version") != 3:
        raise RuntimeError("cassette header rewrite did not persist scenario_policy_version=3")
    scan_cassette(path, key, url)


def comparison_summary(before_runs, after_runs):
    def summarize(runs):
        passed = [bool(run.get("external_acceptance_passed")) for run in runs]
        return {"runs": len(runs), "success_rate": sum(passed) / len(passed) if passed else None,
                **{key: _mean(runs, key) for key in ("total_tokens", "prompt_tokens", "completion_tokens", "cost_cny")}}
    before, after = summarize(before_runs), summarize(after_runs)
    base_tokens, new_tokens = before["total_tokens"], after["total_tokens"]
    reduction = (base_tokens - new_tokens) / base_tokens if base_tokens and new_tokens is not None else None
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
    status = None
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
                name = f"run_{number}.json"
                state, result, observed, consistent = None, {"external_acceptance_passed": False}, None, False
                reason = None
                try:
                    # Record outside the repo so scenario_workspace's snapshot assert cannot fire
                    # when CassetteModel.close() publishes the cassette (see record_coding_scenario).
                    with tempfile.TemporaryDirectory(prefix="coding-m1-") as directory:
                        path = Path(directory) / name
                        with scenario_workspace() as root:
                            model = CassetteModel(path, meter, mode="record",
                                                  redactor=cassette_redactor(root, key, url))
                            try:
                                result, state, ledger = run_scenario(model, root, runtime_config=runtime_config)
                                observed = outcome(result, state, ledger, root)
                                reason = meter.reason or state.error
                            except Exception as exc:  # noqa: BLE001 -- record the failure, keep going
                                result, observed, reason = {"external_acceptance_passed": False}, None, type(exc).__name__
                            finally:
                                model.close()
                        if path.is_file():
                            finalize_cassette(path, configuration, key, url)
                            try:
                                consistent = (observed is not None
                                              and replay(path, details=True, runtime_config=runtime_config) == observed)
                            except Exception as exc:  # noqa: BLE001 -- replay failure is a per-run result
                                reason = reason or f"replay failed: {type(exc).__name__}"
                            target = args.output_dir / name
                            with target.open("xb") as stream:
                                stream.write(path.read_bytes())
                except Exception as exc:  # noqa: BLE001 -- one bad run must not abort the loop
                    reason = f"run failed: {type(exc).__name__}"
                results.append({"run": number, **signature(result),
                                # Failed runs fall back to metered usage so partial cost/tokens stay visible.
                                "total_tokens": meter.prompt_tokens + meter.completion_tokens if state is None else state.total_tokens,
                                "prompt_tokens": meter.prompt_tokens if state is None else state.prompt_tokens,
                                "completion_tokens": meter.completion_tokens if state is None else state.completion_tokens,
                                "cost_cny": str(meter.run_cost_cny), "reason": reason,
                                "replay_consistent": consistent, "outcome": observed})
    except Exception as exc:  # noqa: BLE001 -- still write whatever runs completed
        status = f"recording/check failed ({type(exc).__name__})"
        print(f"pending: {status}.")
    report = {"runs": results, "cost_cny": str(meter.cost_cny), "budget_cny": str(meter.budget),
              "runtime_config": runtime_config, "partial": len(results) < args.runs or status is not None,
              "error": status}
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
    return 0 if status is None and len(results) == args.runs and all(summary["passed"].values()) else 1

if __name__ == "__main__":
    raise SystemExit(main())
