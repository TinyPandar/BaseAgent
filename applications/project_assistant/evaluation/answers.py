"""Rule scoring for generated answers. The LLM judge is optional and off by default.

Synthetic answers prove the report pipeline. They are not a model-quality claim.
Live provider calls are printed as a command and are not dispatched here.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from .retrieval import parse_reference

REFUSAL_PATTERNS = (
    r"not established",
    r"\babstain\b",
    r"no evidence",
    r"insufficient evidence",
    r"cannot answer",
    r"can't answer",
    r"unable to answer",
    r"i don't know",
    r"i do not know",
    r"not in (the |this )?(corpus|checkout|repository)",
    r"没有证据",
    r"未找到",
    r"无法回答",
    r"无法确定",
)
CITATION_RE = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|md)):([1-9][0-9]*)-([1-9][0-9]*)")
REPORTS_DIR = Path(__file__).parent / "reports"
LIVE_COMMAND = (
    "python scripts/evaluate_answers.py --live --budget-cny 2 "
    "--questions applications/project_assistant/evaluation/questions.json"
)


def is_refusal(answer):
    text = answer or ""
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in REFUSAL_PATTERNS)


def extract_citations(answer, explicit=None):
    found = []
    for item in list(explicit or []) + CITATION_RE.findall(answer or ""):
        if isinstance(item, tuple):
            citation = f"{item[0]}:{item[1]}-{item[2]}"
        else:
            citation = str(item)
        if citation not in found:
            found.append(citation)
    return found


def _ranges(citations):
    ranges = []
    for citation in citations:
        try:
            ranges.append(parse_reference(citation))
        except ValueError:
            continue
    return ranges


def citation_coverage(gold, citations):
    """Fraction of gold ranges overlapped by a same-path citation. Empty gold is None."""
    if not gold:
        return None
    cited = _ranges(citations)
    covered = 0
    for path, start, end in (parse_reference(item) for item in gold):
        if any(other == path and other_start <= end and start <= other_end
               for other, other_start, other_end in cited):
            covered += 1
    return covered / len(gold)


def score_record(question, record):
    if question.get("category") == "no_answer":
        if question.get("expected_evidence"):
            raise ValueError("no_answer questions cannot carry expected evidence")
        answerable = False
    else:
        if not question.get("expected_evidence"):
            raise ValueError("answerable questions require expected evidence")
        answerable = True
    answer = record.get("answer") or ""
    citations = extract_citations(answer, record.get("citations"))
    coverage = citation_coverage(question.get("expected_evidence") or [], citations)
    refused = is_refusal(answer)
    has_citations = bool(citations)
    citation_supported = coverage == 1.0 if answerable else not has_citations
    rule_correct = (citation_supported and not refused) if answerable else (refused and not has_citations)
    judge = record.get("judge")
    return {
        "id": question["id"],
        "split": question["split"],
        "category": question["category"],
        "answerable": answerable,
        "refused": refused,
        "has_citations": has_citations,
        "citation_coverage": coverage,
        "citation_supported": citation_supported,
        "rule_correct": rule_correct,
        "judge_correct": None if not judge else bool(judge.get("correct")),
        "latency_ms": _number(record.get("latency_ms"), 0),
        "total_tokens": _number(record.get("total_tokens"), 0),
        "cost_cny": _number(record.get("cost_cny"), 0),
        "source": record.get("source", "fixture"),
    }


def _number(value, default):
    if value is None:
        return default
    number = float(value)
    if number < 0:
        raise ValueError("latency, tokens and cost must be nonnegative")
    return number


def _mean(values):
    return sum(values) / len(values) if values else None


def aggregate(rows, split):
    selected = [row for row in rows if row["split"] == split]
    answerable = [row for row in selected if row["answerable"]]
    unanswered = [row for row in selected if not row["answerable"]]
    judged = [row for row in selected if row["judge_correct"] is not None]
    accuracy_source = "llm_judge" if judged and len(judged) == len(selected) else "rules"
    correct_key = "judge_correct" if accuracy_source == "llm_judge" else "rule_correct"
    return {
        "count": len(selected),
        "answerable_count": len(answerable),
        "no_answer_count": len(unanswered),
        "accuracy": _mean([row[correct_key] for row in selected]) if selected else None,
        "accuracy_source": accuracy_source if selected else None,
        "rule_accuracy": _mean([row["rule_correct"] for row in selected]) if selected else None,
        "citation_support_rate": _mean([row["citation_supported"] for row in answerable]) if answerable else None,
        "refusal_rate": _mean([row["refused"] for row in unanswered]) if unanswered else None,
        "answerable_false_refusal_rate": _mean([row["refused"] for row in answerable]) if answerable else None,
        "latency_ms_mean": _mean([row["latency_ms"] for row in selected]) if selected else None,
        "total_tokens": sum(row["total_tokens"] for row in selected),
        "mean_total_tokens": _mean([row["total_tokens"] for row in selected]) if selected else None,
        "cost_cny": sum(row["cost_cny"] for row in selected),
    }


def human_review_sample(questions, fraction=0.2):
    """Deterministic ~20% sample. reviewed stays null; this does not wait for a person."""
    if not 0 < fraction <= 1:
        raise ValueError("human review fraction must be in (0, 1]")
    ordered = sorted(questions, key=lambda question: question["id"])
    if not ordered:
        return []
    count = min(len(ordered), max(1, round(len(ordered) * fraction)))
    if count == len(ordered):
        picked = ordered
    else:
        step = len(ordered) / count
        indexes = []
        for offset in range(count):
            index = min(len(ordered) - 1, int(offset * step))
            if indexes and index <= indexes[-1]:
                index = indexes[-1] + 1
            indexes.append(index)
        picked = [ordered[index] for index in indexes]
    return [{"id": question["id"], "split": question["split"], "reviewed": None,
             "reviewer": None, "notes": None} for question in picked]


def cross_table(rows):
    judged = [row for row in rows if row["judge_correct"] is not None]
    if not judged:
        return None
    table = {"judge_correct_rule_correct": 0, "judge_correct_rule_incorrect": 0,
             "judge_incorrect_rule_correct": 0, "judge_incorrect_rule_incorrect": 0}
    for row in judged:
        key = ("judge_correct_" if row["judge_correct"] else "judge_incorrect_")
        key += "rule_correct" if row["rule_correct"] else "rule_incorrect"
        table[key] += 1
    table["judged_count"] = len(judged)
    return table


def synthetic_answers(dataset):
    """Gold-shaped answers for pipeline proof. source is synthetic, cost is zero."""
    records = []
    for question in dataset["questions"]:
        if question["category"] == "no_answer":
            answer, citations = "The requested fact is not established by this checkout.", []
        else:
            citations = list(question["expected_evidence"])
            answer = question["answer_summary"] + " " + " ".join(citations)
            if is_refusal(answer):
                answer = "Supported by " + " ".join(citations)
        records.append({"id": question["id"], "answer": answer, "citations": citations,
                        "latency_ms": 0, "total_tokens": 0, "cost_cny": 0, "source": "synthetic"})
    return records


def load_answers(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload["answers"] if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("answers must be a list")
    return rows


def attach_judge(records, judgments):
    by_id = {row["id"]: row for row in records}
    judged = []
    for record in records:
        row = dict(record)
        if record["id"] in judgments:
            verdict = judgments[record["id"]]
            if "correct" not in verdict:
                raise ValueError("judge rows require correct")
            row["judge"] = {"correct": bool(verdict["correct"]), "reason": verdict.get("reason", "")}
        judged.append(row)
    missing = sorted(set(judgments) - set(by_id))
    if missing:
        raise ValueError("judge ids missing from answers: " + ", ".join(missing))
    return judged


def evaluate_answers(dataset, records, *, judge=None):
    """Score tune and test separately. Test rows are never used to fit a threshold."""
    if judge == "tune_on_test":
        raise ValueError("test split cannot be used for tuning")
    by_id = {}
    for record in records:
        if record["id"] in by_id:
            raise ValueError("duplicate answer id")
        by_id[record["id"]] = record
    rows = []
    started = time.perf_counter()
    for question in dataset["questions"]:
        record = by_id.get(question["id"], {"answer": "", "source": "missing"})
        if judge and question["id"] in judge:
            record = {**record, "judge": judge[question["id"]]}
        rows.append(score_record(question, record))
    splits = {split: aggregate(rows, split) for split in ("tune", "test")}
    present = {row["split"] for row in rows}
    splits = {split: metrics for split, metrics in splits.items() if split in present}
    cost = sum(metrics["cost_cny"] for metrics in splits.values())
    tokens = sum(metrics["total_tokens"] for metrics in splits.values())
    return {
        "format": "baseagent.answer_evaluation",
        "version": 1,
        "model_kind": rows[0]["source"] if rows else "none",
        "usage_kind": "synthetic" if rows and all(row["source"] == "synthetic" for row in rows) else "recorded",
        "live_executed": False,
        "provider_calls": 0,
        "cost_cny": cost,
        "total_tokens": tokens,
        "latency_ms_mean": _mean([row["latency_ms"] for row in rows]),
        "scoring_duration_seconds": time.perf_counter() - started,
        "splits": splits,
        "judge_cross_table": cross_table(rows),
        "human_review": human_review_sample(dataset["questions"]),
        "questions": rows,
        "question_set_sha256": sha256(json.dumps(dataset["questions"], sort_keys=True).encode()).hexdigest(),
    }


def planned_live(budget_cny="2"):
    budget = Decimal(str(budget_cny))
    if not budget.is_finite() or budget <= 0:
        raise ValueError("live budget must be a positive finite CNY amount")
    return {
        "executed": False,
        "provider_calls": 0,
        "cost_cny": 0,
        "status": "authorization_required",
        "budget_cny": str(budget),
        "command": LIVE_COMMAND.replace("--budget-cny 2", f"--budget-cny {budget}"),
        "note": "Real 80-question generation is not dispatched. Stop and wait for Frank.",
    }


def consume_budget(questions, ask_fn, budget_cny):
    """Stop once accumulated cost reaches the budget. Used by tests; CLI does not call providers."""
    budget = Decimal(str(budget_cny))
    if not budget.is_finite() or budget <= 0:
        raise ValueError("budget must be positive")
    spent = Decimal(0)
    rows = []
    stopped = False
    for question in questions:
        if spent >= budget:
            stopped = True
            break
        result = ask_fn(question)
        cost = Decimal(str(result.get("cost_cny", 0)))
        if not cost.is_finite() or cost < 0:
            raise ValueError("answer cost must be nonnegative")
        spent += cost
        rows.append(result)
        if spent >= budget:
            stopped = True
            break
    return {"executed": True, "answers": rows, "spent_cny": format(spent, "f"),
            "stopped_for_budget": stopped, "provider_calls": len(rows)}


def render_markdown(report):
    lines = ["# Generated-answer evaluation", "",
             f"Source: `{report['model_kind']}`. Provider calls: {report['provider_calls']}. "
             f"Cost (CNY): {report['cost_cny']}.",
             "",
             "Tune and test are scored separately. This run does not fit anything on test.",
             "",
             "| Split | N | Accuracy | Source | Citation support | Refusal | False refusal | Mean latency ms | Tokens | Cost CNY |",
             "| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for split, metrics in report["splits"].items():
        lines.append("| {split} | {count} | {accuracy} | {accuracy_source} | {citation} | {refusal} | {false} | {latency} | {tokens} | {cost} |".format(
            split=split, count=metrics["count"],
            accuracy=_fmt(metrics["accuracy"]), accuracy_source=metrics["accuracy_source"],
            citation=_fmt(metrics["citation_support_rate"]), refusal=_fmt(metrics["refusal_rate"]),
            false=_fmt(metrics["answerable_false_refusal_rate"]),
            latency=_fmt(metrics["latency_ms_mean"]), tokens=metrics["total_tokens"],
            cost=_fmt(metrics["cost_cny"])))
    lines += ["", "## Human review sample", "",
              "About 20% of question ids. `reviewed` is null; nobody is waiting.", ""]
    for item in report["human_review"]:
        lines.append(f"- `{item['id']}` ({item['split']}) reviewed={item['reviewed']}")
    if report["judge_cross_table"]:
        table = report["judge_cross_table"]
        lines += ["", "## Judge vs rules", "",
                  "| | Rule correct | Rule incorrect |",
                  "| --- | ---: | ---: |",
                  f"| Judge correct | {table['judge_correct_rule_correct']} | {table['judge_correct_rule_incorrect']} |",
                  f"| Judge incorrect | {table['judge_incorrect_rule_correct']} | {table['judge_incorrect_rule_incorrect']} |",
                  ""]
    else:
        lines += ["", "LLM judge: not run.", ""]
    if report["model_kind"] == "synthetic":
        lines.append("Synthetic rows only prove scoring and report formatting. They are not an accuracy claim about qwen.")
    lines.append("")
    return "\n".join(lines)


def _fmt(value):
    return "n/a" if value is None else f"{value:.3f}"


def write_answer_report(report, directory=None):
    directory = Path(directory or REPORTS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC)
    payload = {**report, "timestamp": timestamp.isoformat()}
    path = directory / f"answers-{timestamp:%Y%m%dT%H%M%S%fZ}-{uuid4().hex}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    _reject_secrets(text)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
    path.with_suffix(".md").write_text(render_markdown(payload), encoding="utf-8")
    return path


def _reject_secrets(text):
    lowered = text.lower()
    for marker in ("api_key", "authorization:", "sk-", "bearer "):
        if marker in lowered:
            raise ValueError("answer report contains a secret marker")
