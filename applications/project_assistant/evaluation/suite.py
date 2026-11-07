"""One offline entry for harness, retained retrieval numbers, and answer scoring.

Real generation, M2 smoke, and path-bound cassette replay are commands only.
Missing a paid run is not a failure unless --require-live-answers is set.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
SUITE_REPORTS = Path(__file__).resolve().parent / "reports" / "suite"
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from applications.project_assistant.evaluation.answers import (  # noqa: E402
    evaluate_answers,
    planned_live,
    render_markdown as render_answers,
    synthetic_answers,
    write_answer_report,
)
from applications.project_assistant.evaluation.retrieval import QUESTION_SET  # noqa: E402

PUBLISHED_RETRIEVAL = {
    "source": "applications/project_assistant/evaluation/reports/bailian-80q-summary-2026-10-21.md",
    "split": "test",
    "answerable": 28,
    "no_answer": 8,
    "vector_mrr": 0.678,
    "bm25_mrr": 0.759,
    "hybrid_mrr": 0.845,
    "hybrid_r_at_5": 0.782,
    "hybrid_rerank_mrr": 0.857,
    "hybrid_rerank_r_at_5": 0.770,
    "rerank_default": False,
    "rerank_reason": "R@5 drops from 0.782 to 0.770 and each query costs about 14k tokens",
    "cost_cny_this_run": 0,
    "note": "Retained measurement. This suite does not recompute hybrid MRR and does not rebind gold.",
}
PUBLISHED_CODING = {
    "source": "docs/reports/m1_coding_comparison-20261102T111829Z-b21f9bdd6a3745a4b572a5e3dcf309e1.md",
    "baseline_success_rate": 0.667,
    "success_rate": 1.0,
    "runs": 5,
    "mean_total_tokens_before": 80780.0,
    "mean_total_tokens_after": 36185.4,
    "token_reduction": 0.552,
    "mean_cost_cny_after": 0.030,
    "comparison_cost_cny_note": "five after-runs at 0.030 CNY average, about 0.15 CNY",
    "cost_cny_this_run": 0,
    "note": "Retained qwen-plus comparison. Not recomputed here.",
}
M2_SMOKE_BUDGET_CNY = "0.20"
M2_SMOKE_COMMANDS = (
    'python -m baseagent --stream --root "$PWD" --session demo-stream --follow-events 30 -- "Summarize README in one sentence"',
    'python -m baseagent --parallel-readonly-tools --root "$PWD" --session demo-par -- "Read README.md and pyproject.toml then compare their titles"',
    'python -m baseagent --root "$PWD" --session demo-resume --resume',
)
CASSETTE_REPLAY_COMMAND = "python scripts/record_coding_scenario.py --replay"


def load_questions(path=None):
    return json.loads(Path(path or QUESTION_SET).read_text(encoding="utf-8"))


def harness_summary(report):
    token_values = [case["metrics"]["total_tokens"] for case in report["cases"]
                    if case.get("metrics") and case["metrics"].get("total_tokens") is not None]
    return {
        "kind": "offline_scripted_harness",
        "passed": bool(report["passed"]),
        "success_rate": report["passed_count"] / report["case_count"] if report["case_count"] else None,
        "passed_count": report["passed_count"],
        "case_count": report["case_count"],
        "usage_kind": "synthetic",
        "mean_total_tokens": sum(token_values) / len(token_values) if token_values else None,
        "cost_cny": 0,
        "cases": [{"name": case["name"], "passed": case["passed"]} for case in report["cases"]],
    }


def default_harness():
    from baseagent.evaluation import evaluate
    return harness_summary(evaluate())


def default_answers(dataset, directory):
    report = evaluate_answers(dataset, synthetic_answers(dataset))
    path = write_answer_report(report, directory)
    report = {**report, "report": path.name}
    return report


def default_retrieval(dataset, root):
    from applications.project_assistant.corpus import collect_corpus, compute_corpus_version
    from applications.project_assistant.evaluation.retrieval import validate_question_set

    chunks = collect_corpus(root)
    current = compute_corpus_version(chunks)
    result = {"published": PUBLISHED_RETRIEVAL, "bound_corpus_version": dataset.get("corpus_version"),
              "current_corpus_version": current, "cost_cny": 0, "provider_calls": 0}
    if current != dataset.get("corpus_version"):
        result["repro"] = {"status": "skipped", "reason": "corpus_version_mismatch",
                           "note": "Gold set was not rebound. Published hybrid MRR stays 0.845."}
        return result
    from applications.project_assistant.evaluation.experiments import evaluate_ranking
    from applications.project_assistant.rag.lexical import BM25Index

    validate_question_set(dataset, root, chunks)
    index = BM25Index(list({chunk.doc_id: chunk for chunk in chunks}.values()), k1=1.5, b=0.75)

    def search(query):
        return index.search(query, top_k=5)

    tune = evaluate_ranking(dataset["questions"], search, split="tune")
    test = evaluate_ranking(dataset["questions"], search, split="test",
                            threshold=tune["no_answer"]["threshold"])
    result["repro"] = {"status": "ran", "kind": "offline_bm25_not_hybrid", "cost_cny": 0,
                       "tune_mrr": tune["metrics"]["mrr"], "test_mrr": test["metrics"]["mrr"],
                       "note": "BM25-only plumbing repro. Do not replace the retained hybrid MRR."}
    return result


def build_summary(*, coding, answers, retrieval, report_dir):
    live = planned_live()
    return {
        "format": "baseagent.eval_suite",
        "version": 1,
        "offline": True,
        "provider_calls": 0,
        "cost_cny": 0,
        "coding_harness": coding,
        "coding_retained": PUBLISHED_CODING,
        "retrieval": retrieval,
        "answers": {key: value for key, value in answers.items() if key != "questions"},
        "answer_preview": render_answers(answers).splitlines()[0],
        "live_answers": live,
        "m2_smoke": {"executed": False, "budget_cny": M2_SMOKE_BUDGET_CNY,
                     "commands": list(M2_SMOKE_COMMANDS), "cost_cny": 0},
        "cassette_replay": {"executed": False, "command": CASSETTE_REPLAY_COMMAND,
                            "note": "Path-bound cassettes are a separate subcommand, not a default failure.",
                            "cost_cny": 0},
        "report_dir": report_dir.name if isinstance(report_dir, Path) else str(report_dir),
    }


def render_suite(summary):
    coding = summary["coding_harness"]
    retained = summary["coding_retained"]
    retrieval = summary["retrieval"]["published"]
    answers = summary["answers"]
    lines = [
        "# Offline evaluation summary",
        "",
        "This run did not call a paid model. Cost: 0 CNY.",
        "",
        "## Coding",
        "",
        f"Offline harness success: {coding['passed_count']}/{coding['case_count']} "
        f"({_fmt(coding['success_rate'])}). Usage kind: {coding['usage_kind']}. "
        f"Mean synthetic total tokens: {_fmt(coding['mean_total_tokens'])}.",
        "",
        "Retained M1 qwen-plus comparison (not recomputed): "
        f"success {retained['baseline_success_rate']:.3f} -> {retained['success_rate']:.3f} "
        f"({retained['runs']}/5), mean tokens {retained['mean_total_tokens_before']:.0f} -> "
        f"{retained['mean_total_tokens_after']:.1f} ({retained['token_reduction']:.1%} reduction). "
        f"{retained['comparison_cost_cny_note']}. Source: `{retained['source']}`.",
        "",
        "## Retrieval",
        "",
        f"Retained test hybrid MRR: {retrieval['hybrid_mrr']:.3f} "
        f"(vector {retrieval['vector_mrr']:.3f}, BM25 {retrieval['bm25_mrr']:.3f}, "
        f"hybrid+rerank {retrieval['hybrid_rerank_mrr']:.3f}). "
        f"Rerank stays off: {retrieval['rerank_reason']}.",
        f"Source: `{retrieval['source']}`. This suite does not replace that number.",
        "",
    ]
    repro = summary["retrieval"].get("repro", {})
    if repro.get("status") == "skipped":
        lines.append(f"Offline BM25 repro skipped: {repro['reason']}. {repro['note']}")
    elif repro.get("status") == "ran":
        lines.append("Offline BM25 repro (not hybrid): "
                     f"tune MRR {_fmt(repro['tune_mrr'])}, test MRR {_fmt(repro['test_mrr'])}. {repro['note']}")
    lines += ["", "## Generated answers", ""]
    if answers.get("usage_kind") == "synthetic" or answers.get("model_kind") == "synthetic":
        lines.append("Synthetic answers prove the scoring pipeline. They are not model scores.")
    lines.append("Span hit, line recall, line precision, refusal accuracy, summary_token_f1 "
                 "and summary_rouge_l are separate. Lexical overlap is not semantic correctness.")
    lines.append("Line precision leaves out items with no citations (precision is null and "
                 "they do not enter the average), so 1.000 is not a claim that every item cited gold.")
    nli = answers.get("nli") or {}
    if nli.get("status") == "ran":
        lines.append(f"Faithfulness/NLI ran with `{nli.get('model')}` ({nli.get('variant')}). "
                     "Base and large results stay in separate fields.")
    else:
        lines.append(f"Faithfulness/NLI: skipped ({nli.get('reason') or 'nli disabled'}).")
    for split, metrics in answers.get("splits", {}).items():
        line = (
            f"- {split}: span hit {_fmt(metrics.get('span_hit_rate'))}, "
            f"line recall {_fmt(metrics.get('line_recall'))}, "
            f"line precision {_fmt(metrics.get('line_precision'))}, "
            f"refusal accuracy {_fmt(metrics.get('refusal_accuracy'))}, "
            f"summary_token_f1 {_fmt(metrics.get('summary_token_f1'))}, "
            f"summary_rouge_l {_fmt(metrics.get('summary_rouge_l'))}, "
            f"tokens {metrics.get('total_tokens')}, cost {_fmt(metrics.get('cost_cny'))} CNY, "
            f"mean latency {_fmt(metrics.get('latency_ms_mean'))} ms")
        if "faithfulness" in metrics:
            line += f", faithfulness/{metrics.get('nli_variant')} {_fmt(metrics.get('faithfulness'))}"
        if "judge_faithful_rate" in metrics or "judge_equivalent_rate" in metrics:
            line += (f", judge faithful {_fmt(metrics.get('judge_faithful_rate'))}, "
                     f"judge equivalent {_fmt(metrics.get('judge_equivalent_rate'))}")
        lines.append(line)
    lines += ["", "Human review ids (reviewed=null): "
              + ", ".join(item["id"] for item in answers.get("human_review", [])),
              "",
              "Live 80-question generation was not run. That is not a failure.",
              f"Command, do not run without Frank: `{summary['live_answers']['command']}`",
              "",
              "## Not run",
              "",
              f"- M2 smoke suggested cap {summary['m2_smoke']['budget_cny']} CNY. "
              "The coding CLI has no --budget-cny switch, so these commands are not started. Commands:"]
    for command in summary["m2_smoke"]["commands"]:
        lines.append(f"  - `{command}`")
    lines += [f"- Cassette replay: `{summary['cassette_replay']['command']}`",
              "",
              "Exit policy: a failed offline harness fails the process. "
              "Skipping live generation does not, unless `--require-live-answers` is set.",
              ""]
    return "\n".join(lines)


def _fmt(value):
    return "n/a" if value is None else f"{value:.3f}"


def exit_code(summary, thresholds):
    reasons = []
    harness = summary["coding_harness"]
    if harness.get("ran", True) and harness.get("passed") is False and not thresholds.get("ignore_harness_failure"):
        reasons.append("harness_failed")
    if thresholds.get("require_live_answers") and not summary["answers"].get("live_executed"):
        reasons.append("live_answers_required")
    minimum = thresholds.get("min_coding_success")
    if minimum is not None and (harness.get("success_rate") is None or harness["success_rate"] < minimum):
        reasons.append("coding_success")
    support = thresholds.get("min_citation_support")
    if support is not None:
        for split, metrics in summary["answers"].get("splits", {}).items():
            rate = metrics.get("citation_support_rate")
            if rate is None or rate < support:
                reasons.append("citation_support:" + split)
    return (1 if reasons else 0), reasons


def write_suite_report(summary, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC)
    summary = {**summary, "timestamp": timestamp.isoformat()}
    stem = f"eval-{timestamp:%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:8]}"
    markdown = directory / f"{stem}.md"
    payload = directory / f"{stem}.json"
    text = render_suite(summary)
    data = json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    for marker in ("api_key", "authorization:", "bearer ", "sk-"):
        if marker in data.lower() or marker in text.lower():
            raise ValueError("suite report contains a secret marker")
    with markdown.open("x", encoding="utf-8") as handle:
        handle.write(text)
    with payload.open("x", encoding="utf-8") as handle:
        handle.write(data)
    return markdown


def run_offline(*, root=ROOT, questions=None, report_dir=None, harness_runner=None,
                answer_runner=None, retrieval_runner=None, thresholds=None):
    dataset = questions if questions is not None else load_questions()
    # Default stays outside the ingested corpus so a local run does not force a gold rebind.
    report_dir = Path(report_dir or SUITE_REPORTS)
    answer_dir = report_dir / "answers"
    coding = harness_runner() if harness_runner else default_harness()
    answers = answer_runner(dataset, answer_dir) if answer_runner else default_answers(dataset, answer_dir)
    retrieval = retrieval_runner(dataset, root) if retrieval_runner else default_retrieval(dataset, root)
    summary = build_summary(coding=coding, answers=answers, retrieval=retrieval, report_dir=report_dir)
    path = write_suite_report(summary, report_dir)
    code, reasons = exit_code(summary, thresholds or {})
    return {"exit_code": code, "reasons": reasons, "report": str(path.relative_to(root)) if path.is_relative_to(root) else path.name,
            "summary": summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    offline = commands.add_parser("offline", help="Harness + synthetic answers + retained MRR; no provider calls")
    offline.add_argument("--report-dir", type=Path, default=SUITE_REPORTS)
    offline.add_argument("--questions", type=Path, default=QUESTION_SET)
    offline.add_argument("--min-coding-success", type=float)
    offline.add_argument("--min-citation-support", type=float)
    offline.add_argument("--require-live-answers", action="store_true")
    live = commands.add_parser("live-answers", help="Print the paid generation command and stop")
    live.add_argument("--budget-cny", default="2")
    commands.add_parser("m2-smoke", help="Print M2 smoke commands and stop")
    commands.add_parser("cassette-replay", help="Replay recorded coding cassettes; no new provider calls")
    bench = commands.add_parser("terminal-bench", help="Score the local fixture, or print the paid subset command and stop")
    bench.add_argument("--fixture", action="store_true")
    bench.add_argument("--live", action="store_true")
    bench.add_argument("--budget-cny", default="15")
    args = parser.parse_args(argv)
    if args.command == "live-answers":
        print(json.dumps(planned_live(args.budget_cny), ensure_ascii=False, indent=2))
        return 0
    if args.command == "m2-smoke":
        print(json.dumps({"executed": False, "budget_cny": M2_SMOKE_BUDGET_CNY,
                          "commands": list(M2_SMOKE_COMMANDS), "cost_cny": 0,
                          "note": "Stop. Do not run until Frank authorizes."}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "cassette-replay":
        from scripts.record_coding_scenario import main as replay_main
        return replay_main(["--replay"])
    if args.command == "terminal-bench":
        from scripts.evaluate_terminal_bench import main as bench_main
        argv = []
        if args.fixture:
            argv.append("--fixture")
        if args.live:
            argv.extend(["--live", "--budget-cny", args.budget_cny])
        return bench_main(argv or ["--fixture"])
    result = run_offline(
        questions=load_questions(args.questions), report_dir=args.report_dir,
        thresholds={"min_coding_success": args.min_coding_success,
                    "min_citation_support": args.min_citation_support,
                    "require_live_answers": args.require_live_answers})
    print(json.dumps({"report": result["report"], "exit_code": result["exit_code"],
                      "reasons": result["reasons"], "cost_cny": 0, "provider_calls": 0},
                     ensure_ascii=False, indent=2))
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
