"""CLI for answer scoring. --live generates through a metered provider; --plan stops."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from applications.project_assistant.evaluation.answers import (  # noqa: E402
    DEFAULT_LINE_RECALL_MIN,
    DEFAULT_NLI_BASE,
    DEFAULT_SUMMARY_F1_MIN,
    DEFAULT_SUMMARY_ROUGE_L_MIN,
    LocalDebertaNLI,
    attach_judge,
    evaluate_answers,
    load_answers,
    planned_live,
    synthetic_answers,
    write_answer_report,
)
from applications.project_assistant.evaluation.live_answers import (  # noqa: E402
    DEFAULT_INDEX_DIR,
    DEFAULT_TOP_K,
    EMBEDDING_CNY_PER_MILLION,
    LiveSetupError,
    prepare_live_retrieval,
    run_live_answers,
)
from applications.project_assistant.evaluation.metering import MeteredModel  # noqa: E402
from applications.project_assistant.evaluation.retrieval import QUESTION_SET  # noqa: E402
from baseagent.main import initialize_model, model_configuration  # noqa: E402


def _positive_decimal(value, name):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite positive amount") from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be a finite positive amount")
    return number


def _run_live(args, dataset):
    try:
        budget = _positive_decimal(args.budget_cny, "live budget")
        rate = _positive_decimal(args.embedding_cny_per_million, "embedding rate")
    except ValueError as exc:
        print(json.dumps({"status": "pending", "reason": str(exc)}, ensure_ascii=False))
        return 2
    try:
        model_configuration(generic_only=True)
    except ValueError:
        print("pending: complete BASEAGENT_LLM_* configuration required; no provider calls made.")
        return 2
    try:
        # Construct the adapter before paid embeddings, so a bad client fails free.
        with initialize_model(generic_only=True, timeout=30,
                              max_completion_tokens=args.max_completion_tokens,
                              generation={"temperature": 0}) as client:
            try:
                setup = prepare_live_retrieval(
                    dataset, budget_cny=budget, embedding_rate=rate,
                    index_dir=args.index_dir, top_k=args.top_k)
            except LiveSetupError as exc:
                print(json.dumps({"status": "pending", "reason": str(exc)}, ensure_ascii=False))
                return 1
            except Exception as exc:  # noqa: BLE001 -- never expose provider diagnostics
                print(json.dumps({"status": "pending", "error": type(exc).__name__},
                                 ensure_ascii=False))
                return 1
            remaining = budget - setup["embedding_cost_cny"]
            if remaining > 0:
                meter = MeteredModel(client, budget_cny=remaining,
                                     max_calls=len(dataset["questions"]) + 1)
                result = run_live_answers(
                    dataset, retriever=setup["retriever"], meter=meter, budget_cny=budget,
                    embedding_cost_cny=setup["embedding_cost_cny"],
                    embedding_usage=setup["embedding_usage"], report_dir=args.report_dir)
            else:
                result = run_live_answers(
                    dataset, retriever=setup["retriever"], meter=None, budget_cny=budget,
                    embedding_cost_cny=setup["embedding_cost_cny"],
                    embedding_usage=setup["embedding_usage"], report_dir=args.report_dir)
    except Exception as exc:  # noqa: BLE001 -- never expose credentials or provider payloads
        print(json.dumps({"status": "pending", "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=QUESTION_SET)
    parser.add_argument("--answers", type=Path, help="Fixture or recorded answers JSON")
    parser.add_argument("--synthetic", action="store_true", help="Score gold-shaped fake answers; cost 0")
    parser.add_argument("--judge", type=Path,
                        help="Optional local JSON {id: {faithful, equivalent, reason}}; no network and no model")
    parser.add_argument("--split", choices=("tune", "test", "all"), default="all")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--corpus-root", type=Path, default=ROOT)
    parser.add_argument("--line-recall-min", type=float, default=DEFAULT_LINE_RECALL_MIN)
    parser.add_argument("--summary-f1-min", type=float, default=DEFAULT_SUMMARY_F1_MIN)
    parser.add_argument("--summary-rouge-min", type=float, default=DEFAULT_SUMMARY_ROUGE_L_MIN)
    parser.add_argument("--nli", action="store_true",
                        help="Optional local NLI faithfulness. Off by default; never downloads weights")
    parser.add_argument("--nli-model", default="base",
                        help="base, large, or a local model id. base and large map to the documented DeBERTa-v3 ids")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true",
                      help="Print the paid generation command and stop; no provider calls")
    mode.add_argument("--live", action="store_true",
                      help="Generate answers through a metered provider, then score offline")
    parser.add_argument("--budget-cny", default="2")
    parser.add_argument("--embedding-cny-per-million", default=str(EMBEDDING_CNY_PER_MILLION))
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX_DIR,
                        help="Live index directory; must be gitignored or outside the repository")
    parser.add_argument("--max-completion-tokens", type=int, default=512)
    args = parser.parse_args(argv)
    if args.plan:
        print(json.dumps(planned_live(args.budget_cny), ensure_ascii=False, indent=2))
        return 0
    dataset = json.loads(args.questions.read_text(encoding="utf-8"))
    if args.live:
        if args.synthetic or args.answers:
            parser.error("--live generates answers; do not combine it with --synthetic or --answers")
        if args.split != "all":
            parser.error("--live always reports tune and test separately; do not use --split")
        return _run_live(args, dataset)
    if args.synthetic == bool(args.answers):
        parser.error("choose exactly one of --synthetic or --answers")
    if args.split != "all":
        dataset = {**dataset, "questions": [q for q in dataset["questions"] if q["split"] == args.split]}
    records = synthetic_answers(dataset) if args.synthetic else load_answers(args.answers)
    judge = None
    if args.judge:
        payload = json.loads(args.judge.read_text(encoding="utf-8"))
        judge = payload.get("judgments", payload)
    if judge:
        records = attach_judge(records, judge)
    nli = None
    nli_skip_reason = "nli disabled"
    if args.nli:
        try:
            nli = LocalDebertaNLI(args.nli_model)
            nli.load()
        except Exception as exc:
            # Missing weights, a missing package, or a local-files failure must not
            # discard the citation and lexical layers, and must not download a model.
            nli = None
            nli_skip_reason = "nli model unavailable: " + type(exc).__name__
    report = evaluate_answers(
        dataset, records, judge=judge, nli=nli, corpus_root=args.corpus_root,
        nli_skip_reason=nli_skip_reason, line_recall_min=args.line_recall_min,
        summary_f1_min=args.summary_f1_min, summary_rouge_l_min=args.summary_rouge_min)
    path = write_answer_report(report, args.report_dir)
    print(json.dumps({"report": path.name, "splits": list(report["splits"]),
                      "cost_cny": report["cost_cny"], "provider_calls": 0,
                      "live_executed": False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
