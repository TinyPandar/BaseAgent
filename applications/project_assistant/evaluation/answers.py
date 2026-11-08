"""Layered scoring for generated answers. The LLM judge and NLI are optional and off.

Layers stay separate. Nothing here is an answer-accuracy number.
Synthetic answers prove the report pipeline. They are not a model-quality claim.
This module never dispatches provider calls; the CLI does that through a meter.
Faithfulness uses an injected backend. The default local DeBERTa loader never downloads.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from contextlib import contextmanager
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
# English words and code identifiers. Underscores and dotted names stay intact.
_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*|\d+(?:\.\d+)?|[\u4e00-\u9fff]"
)
REPORTS_DIR = Path(__file__).parent / "reports"
LIVE_COMMAND = (
    "python scripts/evaluate_answers.py --live --budget-cny 2 "
    "--questions applications/project_assistant/evaluation/questions.json"
)
DEFAULT_LINE_RECALL_MIN = 0.3
DEFAULT_SUMMARY_F1_MIN = 0.25
DEFAULT_SUMMARY_ROUGE_L_MIN = 0.3
# A common MNLI fine-tune of DeBERTa-v3. `--nli-model base|large` selects these ids.
DEFAULT_NLI_BASE = "cross-encoder/nli-deberta-v3-base"
DEFAULT_NLI_LARGE = "cross-encoder/nli-deberta-v3-large"
NLI_MODEL_ALIASES = {"base": DEFAULT_NLI_BASE, "large": DEFAULT_NLI_LARGE}
# pipeline(..., local_files_only=True) does not reach cached_file on this release.
SUPPORTED_TRANSFORMERS = "4.57.1"
JUDGE_QUESTIONS = (
    "Is the answer faithful to the cited source?",
    "Is the answer equivalent to answer_summary?",
)
_ENTAILED_LABELS = {"entailment", "entailed"}


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


def _line_index(references):
    """Union of inclusive line numbers per file. Invalid references are skipped."""
    lines = {}
    spans = []
    for reference in references or []:
        try:
            path, start, end = parse_reference(reference)
        except (TypeError, ValueError):
            continue
        spans.append((path, start, end))
        lines.setdefault(path, set()).update(range(start, end + 1))
    return lines, spans


def citation_line_stats(gold, citations):
    """Return line recall, line precision, and whether every gold span is hit.

    Recall and precision are computed on line-number sets per file. A span hits
    when at least one of its lines is cited. Empty gold yields None. One overlapping
    line is not reported as coverage 1.
    """
    gold_lines, gold_spans = _line_index(gold)
    cited_lines, _ = _line_index(citations)
    if not gold_spans:
        return None, None, None
    gold_count = sum(len(values) for values in gold_lines.values())
    cited_count = sum(len(values) for values in cited_lines.values())
    overlap = sum(len(values & cited_lines.get(path, set())) for path, values in gold_lines.items())
    recall = overlap / gold_count if gold_count else None
    precision = overlap / cited_count if cited_count else None
    span_hit = all(
        any(line in cited_lines.get(path, ()) for line in range(start, end + 1))
        for path, start, end in gold_spans
    )
    return recall, precision, span_hit


def citation_coverage(gold, citations):
    """Deprecated alias of citation line recall. It is not whole-span coverage."""
    recall, _, _ = citation_line_stats(gold, citations)
    return recall


def tokenize(text):
    """Lowercased English words, numbers, and CJK characters. Identifiers stay whole."""
    return [token.lower() for token in _TOKEN_RE.findall(text or "")]


def prose_for_overlap(answer):
    """Drop citation syntax so the lexical layer does not rescore the citation layer."""
    return CITATION_RE.sub(" ", answer or "")


def _f1(overlap, predicted, gold):
    if not predicted and not gold:
        return 1.0
    if not predicted or not gold or overlap == 0:
        return 0.0
    precision = overlap / predicted
    recall = overlap / gold
    return 2 * precision * recall / (precision + recall)


def summary_token_f1(answer, summary):
    predicted = tokenize(prose_for_overlap(answer))
    gold = tokenize(summary)
    if not predicted and not gold:
        return 1.0
    overlap = sum((Counter(predicted) & Counter(gold)).values())
    return _f1(overlap, len(predicted), len(gold))


def _lcs_length(left, right):
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for token in left:
        current = [0]
        for index, other in enumerate(right, start=1):
            if token == other:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return previous[-1]


def summary_rouge_l(answer, summary):
    """ROUGE-L F1. Lexical overlap with answer_summary, not semantic correctness."""
    predicted = tokenize(prose_for_overlap(answer))
    gold = tokenize(summary)
    if not predicted and not gold:
        return 1.0
    if not predicted or not gold:
        return 0.0
    return _f1(_lcs_length(predicted, gold), len(predicted), len(gold))


def split_sentences(answer):
    """Claims left after citation markers are removed. Citation-only text yields none."""
    text = (answer or "").strip()
    if not text:
        return []
    sentences = []
    for part in re.split(r"(?<=[.!?。！？])\s+", text):
        claim = re.sub(r"\s+", " ", CITATION_RE.sub(" ", part)).strip(" \t\"'`*-")
        if claim and re.search(r"[A-Za-z0-9\u4e00-\u9fff]", claim):
            sentences.append(claim)
    return sentences


def resolve_nli_model(model_name):
    """Map the base and large aliases to the documented model ids."""
    text = str(model_name or "").strip()
    return NLI_MODEL_ALIASES.get(text.lower(), text)


def nli_variant(model_name):
    """Keep base and large results apart. A name that contains neither is custom."""
    lowered = resolve_nli_model(model_name).lower()
    if "large" in lowered:
        return "large"
    if "base" in lowered:
        return "base"
    return "custom"


@contextmanager
def _local_hub_only(tf_hub, hub_constants):
    """Force cache-only hub lookups. Restores the previous flags on exit.

    transformers 4.57.1 snapshots HF_HUB_OFFLINE into transformers.utils.hub at
    import, and pipeline() resolves config.json before model_kwargs apply.
    Setting both flags makes cached_file force local_files_only.
    """
    import os
    names = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    saved_env = {name: os.environ.get(name) for name in names}
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    had_tf = hasattr(tf_hub, "_is_offline_mode")
    saved_tf = getattr(tf_hub, "_is_offline_mode", None)
    tf_hub._is_offline_mode = True
    saved_hub = getattr(hub_constants, "HF_HUB_OFFLINE", None) if hub_constants is not None else None
    if hub_constants is not None:
        hub_constants.HF_HUB_OFFLINE = True
    try:
        yield
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if had_tf:
            tf_hub._is_offline_mode = saved_tf
        elif hasattr(tf_hub, "_is_offline_mode"):
            delattr(tf_hub, "_is_offline_mode")
        if hub_constants is not None:
            hub_constants.HF_HUB_OFFLINE = saved_hub


def _load_local_nli(model_name):
    """Load config, tokenizer, and weights from the local cache only.

    Supported target is transformers 4.57.1. A top-level
    pipeline(..., local_files_only=True) does not reach cached_file and can
    still resolve the repo over the network. Offline flags are set first, then
    each piece is loaded with local_files_only=True. pipeline() receives the
    loaded objects, not a repo id.
    """
    try:
        from transformers import (
            AutoConfig,
            AutoModelForSequenceClassification,
            AutoTokenizer,
            pipeline,
        )
        import transformers.utils.hub as tf_hub
    except ImportError as exc:
        raise RuntimeError("transformers is not installed") from exc
    try:
        import huggingface_hub.constants as hub_constants
    except ImportError:
        hub_constants = None
    local = {"local_files_only": True}
    try:
        with _local_hub_only(tf_hub, hub_constants):
            config = AutoConfig.from_pretrained(model_name, **local)
            tokenizer = AutoTokenizer.from_pretrained(model_name, **local)
            model = AutoModelForSequenceClassification.from_pretrained(model_name, **local)
            return pipeline("text-classification", model=model, tokenizer=tokenizer, config=config)
    except Exception as exc:
        raise RuntimeError(f"local NLI weights unavailable for {model_name}") from exc


class LocalDebertaNLI:
    """Injectable local MNLI backend. Construction does not load or download weights."""

    def __init__(self, model_name=DEFAULT_NLI_BASE):
        resolved = resolve_nli_model(model_name)
        if not resolved:
            raise ValueError("nli model name is required")
        self.model_name = resolved
        self.variant = nli_variant(resolved)
        self._pipe = None

    def load(self):
        if self._pipe is None:
            self._pipe = _load_local_nli(self.model_name)
        return self

    def label(self, premise, hypothesis):
        self.load()
        output = self._pipe({"text": premise or "", "text_pair": hypothesis or ""}, truncation=True)
        if isinstance(output, list):
            output = output[0]
        return output["label"]


def _is_entailed(label):
    if label is True:
        return True
    return str(label).strip().lower() in _ENTAILED_LABELS


def make_source_reader(root):
    """Read cited lines from a checkout. Paths may not escape the root."""
    root = Path(root).resolve()

    def read_source(path, start, end):
        relative = Path(str(path))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("citation path escapes corpus root")
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError("citation path is not a corpus file")
        lines = target.read_text(encoding="utf-8").splitlines()
        if end > len(lines):
            raise ValueError("citation lines out of range")
        return "\n".join(lines[start - 1:end])

    return read_source


def cited_premise(citations, read_source):
    parts = []
    for citation in citations:
        try:
            path, start, end = parse_reference(citation)
        except (TypeError, ValueError):
            continue
        try:
            text = read_source(path, start, end) if read_source else ""
        except (OSError, TypeError, ValueError):
            continue
        if text and str(text).strip():
            parts.append(str(text).strip())
    return "\n".join(parts)


def faithfulness_score(answer, citations, backend, read_source):
    """Fraction of answer sentences the backend labels entailed. None when skipped."""
    if backend is None:
        return None
    sentences = split_sentences(answer)
    if not sentences:
        return None
    premise = cited_premise(citations, read_source)
    entailed = 0
    for sentence in sentences:
        if _is_entailed(backend.label(premise, sentence)):
            entailed += 1
    return entailed / len(sentences)


def _threshold(name, value):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number in [0, 1]") from exc
    if math.isnan(number) or number < 0 or number > 1:
        raise ValueError(f"{name} must be a number in [0, 1]")
    return number


def _meets(value, threshold):
    return value is not None and value + 1e-12 >= threshold


def score_record(question, record, *, line_recall_min=DEFAULT_LINE_RECALL_MIN,
                 summary_f1_min=DEFAULT_SUMMARY_F1_MIN,
                 summary_rouge_l_min=DEFAULT_SUMMARY_ROUGE_L_MIN,
                 nli=None, read_source=None):
    line_recall_min = _threshold("line_recall_min", line_recall_min)
    summary_f1_min = _threshold("summary_f1_min", summary_f1_min)
    summary_rouge_l_min = _threshold("summary_rouge_l_min", summary_rouge_l_min)
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
    recall, precision, span_hit = citation_line_stats(question.get("expected_evidence") or [], citations)
    refused = is_refusal(answer)
    has_citations = bool(citations)
    if answerable:
        citation_supported = bool(span_hit) and _meets(recall, line_recall_min)
        # Debug only: citation gate and not a refusal. This is not answer correctness.
        citation_and_refusal_pass = citation_supported and not refused
        token_f1 = summary_token_f1(answer, question.get("answer_summary") or "")
        rouge_l = summary_rouge_l(answer, question.get("answer_summary") or "")
        summary_match = _meets(token_f1, summary_f1_min) or _meets(rouge_l, summary_rouge_l_min)
    else:
        # Unanswerable items pass on refusal with no citations. Summary F1 is not a pass.
        citation_supported = refused and not has_citations
        citation_and_refusal_pass = citation_supported
        token_f1 = None
        rouge_l = None
        summary_match = None
    judge = record.get("judge") or {}
    faithful = faithfulness_score(answer, citations, nli, read_source)
    variant = getattr(nli, "variant", None) if nli is not None else None
    row = {
        "id": question["id"],
        "split": question["split"],
        "category": question["category"],
        "answerable": answerable,
        "refused": refused,
        "has_citations": has_citations,
        "citation_span_hit": span_hit,
        "citation_line_recall": recall,
        "citation_line_precision": precision,
        "citation_coverage": recall,
        "citation_supported": citation_supported,
        "citation_and_refusal_pass": citation_and_refusal_pass,
        "summary_token_f1": token_f1,
        "summary_rouge_l": rouge_l,
        "summary_match": summary_match,
        "judge_correct": None if "correct" not in judge else bool(judge.get("correct")),
        "judge_faithful": None if "faithful" not in judge else bool(judge.get("faithful")),
        "judge_equivalent": None if "equivalent" not in judge else bool(judge.get("equivalent")),
        "latency_ms": _number(record.get("latency_ms"), 0),
        "total_tokens": _number(record.get("total_tokens"), 0),
        "cost_cny": _number(record.get("cost_cny"), 0),
        "source": record.get("source", "fixture"),
    }
    if nli is not None:
        row["faithfulness"] = faithful
        row["faithfulness_base"] = faithful if variant == "base" else None
        row["faithfulness_large"] = faithful if variant == "large" else None
        row["nli_variant"] = variant
    return row


def _number(value, default):
    if value is None:
        return default
    number = float(value)
    if number < 0:
        raise ValueError("latency, tokens and cost must be nonnegative")
    return number


def _mean(values):
    return sum(values) / len(values) if values else None


def _mean_present(rows, key):
    values = [row[key] for row in rows if row.get(key) is not None]
    return _mean(values) if values else None


def aggregate(rows, split, *, nli_variant=None):
    selected = [row for row in rows if row["split"] == split]
    answerable = [row for row in selected if row["answerable"]]
    unanswered = [row for row in selected if not row["answerable"]]
    metrics = {
        "count": len(selected),
        "answerable_count": len(answerable),
        "no_answer_count": len(unanswered),
        "span_hit_rate": _mean_present(answerable, "citation_span_hit"),
        "line_recall": _mean_present(answerable, "citation_line_recall"),
        "line_precision": _mean_present(answerable, "citation_line_precision"),
        "refusal_accuracy": _mean([bool(row["refused"] and not row["has_citations"]) for row in unanswered])
        if unanswered else None,
        "summary_token_f1": _mean_present(answerable, "summary_token_f1"),
        "summary_rouge_l": _mean_present(answerable, "summary_rouge_l"),
        "summary_match_rate": _mean_present(answerable, "summary_match"),
        "citation_support_rate": _mean([row["citation_supported"] for row in answerable]) if answerable else None,
        "refusal_rate": _mean([row["refused"] for row in unanswered]) if unanswered else None,
        "answerable_false_refusal_rate": _mean([row["refused"] for row in answerable]) if answerable else None,
        "latency_ms_mean": _mean([row["latency_ms"] for row in selected]) if selected else None,
        "total_tokens": sum(row["total_tokens"] for row in selected),
        "mean_total_tokens": _mean([row["total_tokens"] for row in selected]) if selected else None,
        "cost_cny": sum(row["cost_cny"] for row in selected),
    }
    if nli_variant:
        score = _mean_present(selected, "faithfulness")
        metrics["faithfulness"] = score
        metrics["nli_variant"] = nli_variant
        if nli_variant == "base":
            metrics["faithfulness_base"] = score
        elif nli_variant == "large":
            metrics["faithfulness_large"] = score
    faithful = _mean_present(selected, "judge_faithful")
    equivalent = _mean_present(selected, "judge_equivalent")
    if any(row["judge_faithful"] is not None for row in selected):
        metrics["judge_faithful_rate"] = faithful
    if any(row["judge_equivalent"] is not None for row in selected):
        metrics["judge_equivalent_rate"] = equivalent
    return metrics


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
    """Debug comparison of a legacy judge `correct` flag and the citation/refusal gate."""
    judged = [row for row in rows if row["judge_correct"] is not None]
    if not judged:
        return None
    table = {"judge_correct_citation_pass": 0, "judge_correct_citation_fail": 0,
             "judge_incorrect_citation_pass": 0, "judge_incorrect_citation_fail": 0}
    for row in judged:
        key = "judge_correct_" if row["judge_correct"] else "judge_incorrect_"
        key += "citation_pass" if row["citation_and_refusal_pass"] else "citation_fail"
        table[key] += 1
    table["judged_count"] = len(judged)
    table["note"] = "Debug only. citation_and_refusal_pass is not answer correctness."
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
            if not any(key in verdict for key in ("correct", "faithful", "equivalent")):
                raise ValueError("judge rows require faithful, equivalent, or legacy correct")
            row["judge"] = {
                "reason": verdict.get("reason", ""),
                **{key: bool(verdict[key]) for key in ("correct", "faithful", "equivalent") if key in verdict},
            }
        judged.append(row)
    missing = sorted(set(judgments) - set(by_id))
    if missing:
        raise ValueError("judge ids missing from answers: " + ", ".join(missing))
    return judged


def _nli_report(rows, backend, skip_reason):
    if backend is None:
        return {"status": "skipped", "reason": skip_reason or "nli disabled", "model": None,
                "variant": None, "base": None, "large": None}
    variant = getattr(backend, "variant", None) or "custom"
    scores = [row["faithfulness"] for row in rows if row["faithfulness"] is not None]
    slot = {"model": getattr(backend, "model_name", None), "variant": variant,
            "faithfulness": _mean(scores) if scores else None, "scored_count": len(scores)}
    report = {"status": "ran", "reason": None, "model": slot["model"], "variant": variant,
              "base": slot if variant == "base" else None,
              "large": slot if variant == "large" else None}
    if variant not in {"base", "large"}:
        report["custom"] = slot
    return report


def evaluate_answers(dataset, records, *, judge=None, nli=None, corpus_root=None,
                     read_source=None, nli_skip_reason=None,
                     line_recall_min=DEFAULT_LINE_RECALL_MIN,
                     summary_f1_min=DEFAULT_SUMMARY_F1_MIN,
                     summary_rouge_l_min=DEFAULT_SUMMARY_ROUGE_L_MIN):
    """Score tune and test separately. Test rows are never used to fit a threshold."""
    if judge == "tune_on_test":
        raise ValueError("test split cannot be used for tuning")
    line_recall_min = _threshold("line_recall_min", line_recall_min)
    summary_f1_min = _threshold("summary_f1_min", summary_f1_min)
    summary_rouge_l_min = _threshold("summary_rouge_l_min", summary_rouge_l_min)
    by_id = {}
    for record in records:
        if record["id"] in by_id:
            raise ValueError("duplicate answer id")
        by_id[record["id"]] = record
    if nli is not None and read_source is None and corpus_root is not None:
        read_source = make_source_reader(corpus_root)
    rows = []
    started = time.perf_counter()
    for question in dataset["questions"]:
        record = by_id.get(question["id"], {"answer": "", "source": "missing"})
        if judge and question["id"] in judge:
            record = {**record, "judge": judge[question["id"]]}
        rows.append(score_record(
            question, record, line_recall_min=line_recall_min, summary_f1_min=summary_f1_min,
            summary_rouge_l_min=summary_rouge_l_min, nli=nli, read_source=read_source))
    variant = getattr(nli, "variant", None) if nli is not None else None
    splits = {split: aggregate(rows, split, nli_variant=variant) for split in ("tune", "test")}
    present = {row["split"] for row in rows}
    splits = {split: metrics for split, metrics in splits.items() if split in present}
    cost = sum(metrics["cost_cny"] for metrics in splits.values())
    tokens = sum(metrics["total_tokens"] for metrics in splits.values())
    judge_ran = any(row["judge_correct"] is not None or row["judge_faithful"] is not None
                    or row["judge_equivalent"] is not None for row in rows)
    return {
        "format": "baseagent.answer_evaluation",
        "version": 2,
        "model_kind": rows[0]["source"] if rows else "none",
        "usage_kind": "synthetic" if rows and all(row["source"] == "synthetic" for row in rows) else "recorded",
        "live_executed": False,
        "provider_calls": 0,
        "cost_cny": cost,
        "total_tokens": tokens,
        "latency_ms_mean": _mean([row["latency_ms"] for row in rows]),
        "scoring_duration_seconds": time.perf_counter() - started,
        "thresholds": {
            "line_recall_min": line_recall_min,
            "summary_f1_min": summary_f1_min,
            "summary_rouge_l_min": summary_rouge_l_min,
            "summary_match": "token F1 >= summary_f1_min OR ROUGE-L >= summary_rouge_l_min; "
                             "lexical overlap, not semantic correctness",
            "citation_supported": "answerable: span hit AND line recall >= line_recall_min; "
                                  "unanswerable: refusal AND no citations",
            "citation_coverage": "deprecated alias of citation_line_recall; not whole-span coverage",
        },
        "splits": splits,
        "nli": _nli_report(rows, nli, nli_skip_reason),
        "llm_judge": {"ran": judge_ran, "questions": list(JUDGE_QUESTIONS),
                      "note": "Optional and off by default. Not an accuracy score."},
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
        "note": "Plan only. The real command is dispatched by --live after authorization.",
    }


def consume_budget(questions, ask_fn, budget_cny):
    """Stop once accumulated cost reaches the budget. The live CLI drives this loop."""
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


def _table_row(cells):
    return "| " + " | ".join(cells) + " |"


def render_markdown(report):
    nli = report.get("nli") or {}
    judge = report.get("llm_judge") or {}
    lines = ["# Generated-answer evaluation", "",
             f"Source: `{report['model_kind']}`. Provider calls: {report['provider_calls']}. "
             f"Cost (CNY): {report['cost_cny']}.",
             "",
             "Layers are separate. summary_token_f1 and summary_rouge_l are lexical overlap "
             "with answer_summary, not semantic correctness.",
             "citation_and_refusal_pass is a debug field. It is not answer accuracy.",
             "citation_coverage is a deprecated alias of citation_line_recall, not whole-span coverage.",
             "",
             "Tune and test are scored separately. This run does not fit anything on test.",
             ""]
    show_nli = nli.get("status") == "ran"
    header = ["Split", "N", "Span hit", "Line recall", "Line precision", "Refusal accuracy",
              "summary_token_f1", "summary_rouge_l"]
    if show_nli:
        header.append(f"Faithfulness ({nli.get('variant')})")
    show_faithful = any("judge_faithful_rate" in metrics for metrics in report["splits"].values())
    show_equivalent = any("judge_equivalent_rate" in metrics for metrics in report["splits"].values())
    if show_faithful:
        header.append("Judge faithful")
    if show_equivalent:
        header.append("Judge equivalent")
    header += ["Mean latency ms", "Tokens", "Cost CNY"]
    lines.append(_table_row(header))
    lines.append(_table_row(["---"] + ["---:"] * (len(header) - 1)))
    for split, metrics in report["splits"].items():
        cells = [split, str(metrics["count"]), _fmt(metrics.get("span_hit_rate")),
                 _fmt(metrics.get("line_recall")), _fmt(metrics.get("line_precision")),
                 _fmt(metrics.get("refusal_accuracy")), _fmt(metrics.get("summary_token_f1")),
                 _fmt(metrics.get("summary_rouge_l"))]
        if show_nli:
            cells.append(_fmt(metrics.get("faithfulness")))
        if show_faithful:
            cells.append(_fmt(metrics.get("judge_faithful_rate")))
        if show_equivalent:
            cells.append(_fmt(metrics.get("judge_equivalent_rate")))
        cells += [_fmt(metrics.get("latency_ms_mean")), str(metrics.get("total_tokens")),
                  _fmt(metrics.get("cost_cny"))]
        lines.append(_table_row(cells))
    thresholds = report.get("thresholds") or {}
    lines += ["",
              "Answerable citation_supported requires a span hit and "
              f"line recall >= {thresholds.get('line_recall_min', DEFAULT_LINE_RECALL_MIN)}. "
              "Unanswerable items pass only on a refusal with no citations.",
              "Line precision averages only items that cite at least one line. "
              "Items with no citations have precision null and stay out of the average, "
              "so 1.000 does not mean every item cited the gold lines.",
              "summary_match is lexical: token F1 >= "
              f"{thresholds.get('summary_f1_min', DEFAULT_SUMMARY_F1_MIN)} or ROUGE-L >= "
              f"{thresholds.get('summary_rouge_l_min', DEFAULT_SUMMARY_ROUGE_L_MIN)}. "
              "It is not semantic correctness. Unanswerable items do not use it.",
              ""]
    if show_nli:
        slot = "large" if nli.get("variant") == "large" else "base" if nli.get("variant") == "base" else "custom"
        lines.append(f"Faithfulness/NLI ran with `{nli.get('model')}` ({slot}). "
                     "Base and large scores are stored separately and are not averaged together.")
    else:
        lines.append(f"Faithfulness/NLI: skipped ({nli.get('reason') or 'nli disabled'}).")
    lines += ["", "## Human review sample", "",
              "About 20% of question ids. `reviewed` is null; nobody is waiting.", ""]
    for item in report["human_review"]:
        lines.append(f"- `{item['id']}` ({item['split']}) reviewed={item['reviewed']}")
    if report.get("judge_cross_table"):
        table = report["judge_cross_table"]
        lines += ["", "## Judge vs citation/refusal debug", "",
                  "This table does not score answer correctness.",
                  "",
                  "| | Citation/refusal pass | Citation/refusal fail |",
                  "| --- | ---: | ---: |",
                  f"| Judge legacy correct | {table['judge_correct_citation_pass']} | {table['judge_correct_citation_fail']} |",
                  f"| Judge legacy incorrect | {table['judge_incorrect_citation_pass']} | {table['judge_incorrect_citation_fail']} |",
                  ""]
    if judge.get("ran"):
        lines.append("LLM judge file was applied. It is not an accuracy score. "
                     "Questions: " + " ".join(JUDGE_QUESTIONS))
    else:
        lines.append("LLM judge: not run. After a later authorization it would only ask: "
                     + " ".join(JUDGE_QUESTIONS))
    lines.append("")
    if report.get("model_kind") == "synthetic" or report.get("usage_kind") == "synthetic":
        lines.append("Synthetic rows only prove scoring and report formatting. They are not model scores.")
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


class SecretMarkerError(ValueError):
    """Encoded report or checkpoint text is shaped like it contains a credential."""


# Env-var and identifier names end in _API_KEY / _api_key and are not secrets.
# A bare lowercase api_key field is. An sk- token is a secret only at a word
# boundary, so "ask-first" and "disk-cache" are allowed but "sk-proj-..." is not.
_API_KEY_RE = re.compile(r"api_key", re.IGNORECASE)
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]", re.IGNORECASE)


def _reject_secrets(text):
    text = text or ""
    lowered = text.lower()
    if "authorization:" in lowered or "bearer " in lowered:
        raise SecretMarkerError("answer report contains a secret marker")
    if _SK_TOKEN_RE.search(text):
        raise SecretMarkerError("answer report contains a secret marker")
    for match in _API_KEY_RE.finditer(text):
        if match.group(0) == "API_KEY":
            # Upper-case env style, for example API_KEY on its own.
            continue
        if match.start() > 0 and text[match.start() - 1] == "_":
            # Part of a longer identifier such as OPENAI_API_KEY.
            continue
        raise SecretMarkerError("answer report contains a secret marker")
