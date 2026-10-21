"""Evidence validation, retrieval metrics, and append-only JSON reports."""

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from uuid import uuid4

from applications.project_assistant.config import RAGConfig
from applications.project_assistant.corpus import compute_corpus_version
from applications.project_assistant.rag.retrieval import retrieve

EVALUATION_DIR = Path(__file__).parent
QUESTION_SET = EVALUATION_DIR / "questions.json"
REPORTS_DIR = EVALUATION_DIR / "reports"
CATEGORIES = {"exact_identifier", "concept", "cross_file", "no_answer", "stale"}


def parse_reference(reference):
    """Parse a canonical, repository-relative path and inclusive line range."""
    match = re.fullmatch(r"([^:]+):([1-9][0-9]*)-([1-9][0-9]*)", reference)
    if not match:
        raise ValueError(f"invalid evidence reference: {reference}")
    path, start, end = match.groups()
    relative = PurePosixPath(path)
    if (relative.is_absolute() or relative.as_posix() != path or ".." in relative.parts
            or "\\" in path or int(end) < int(start)):
        raise ValueError(f"invalid evidence reference: {reference}")
    return path, int(start), int(end)


def validate_question_set(dataset, root, chunks):
    """Reject stale corpus versions, unavailable evidence, and changed evidence text."""
    if dataset["corpus_version"] != compute_corpus_version(chunks):
        raise ValueError("question set corpus_version differs; review evidence and rebind explicitly")
    root = Path(root).resolve()
    ids = set()
    for question in dataset["questions"]:
        if question["id"] in ids:
            raise ValueError("duplicate question id")
        ids.add(question["id"])
        if (question["split"] not in {"tune", "test"} or question["category"] not in CATEGORIES
                or not question["question"].strip()):
            raise ValueError("invalid question split, category, or text")
        references = question["expected_evidence"]
        if bool(references) == (question["category"] == "no_answer"):
            raise ValueError("only no_answer questions may have empty evidence")
        for reference in references:
            path, start, end = parse_reference(reference)
            target = root / path
            if not target.resolve().is_relative_to(root):
                raise ValueError("evidence escapes corpus root")
            lines = target.read_text(encoding="utf-8").splitlines()
            if end > len(lines):
                raise ValueError(f"evidence refers to missing lines: {reference}")
            digest = sha256("\n".join(lines[start - 1:end]).encode()).hexdigest()
            if dataset["evidence_sha256"].get(reference) != digest:
                raise ValueError(f"evidence text changed: {reference}")
            if not any(c.file_path == path and c.line_start <= end and start <= c.line_end
                       for c in chunks):
                raise ValueError(f"evidence is outside ingested corpus: {reference}")


def question_metrics(references, results):
    """Recall is the fraction of gold ranges covered; MRR uses the first hit.

    A hit has the same path and inclusive overlapping lines. Each gold range
    counts once, even when several retrieved chunks overlap it. Empty gold is
    unanswerable and contributes no recall or reciprocal-rank observation.
    """
    if not references:
        return {"recall": None, "reciprocal_rank": None,
                "top_score": results[0].score if results else None}
    gold = [parse_reference(reference) for reference in references]

    def matches(result, evidence):
        path, start, end = evidence
        return (result.file_path == path and result.line_range[0] <= end
                and start <= result.line_range[1])

    recall = {str(k): sum(any(matches(result, evidence) for result in results[:k])
                         for evidence in gold) / len(gold) for k in (1, 3, 5)}
    first = next((rank for rank, result in enumerate(results, 1)
                  if any(matches(result, evidence) for evidence in gold)), None)
    return {"recall": recall, "reciprocal_rank": 1 / first if first else 0.0}


def evaluate(dataset, index, *, top_k=5):
    """Retrieve once per question and aggregate answerable questions by split."""
    if top_k < 5:
        raise ValueError("top_k must be at least 5 for Recall@5")
    rows = []
    config = RAGConfig.from_env()
    default_kind = ("distance_lower_is_better" if config.retrieval_mode == "vector"
                    else "relevance_higher_is_better")
    for question in dataset["questions"]:
        diagnostics = {"retrieval_mode": config.retrieval_mode, "score_kind": default_kind}
        results = retrieve(question["question"], index, top_k=top_k,
                           config=config, diagnostics=diagnostics)
        rows.append({"id": question["id"], "split": question["split"],
                     "category": question["category"],
                     **diagnostics,
                     **question_metrics(question["expected_evidence"], results),
                     "retrieved": [{"citation": result.citation, "score": result.score}
                                   for result in results]})
    splits = {}
    for split in ("tune", "test"):
        selected = [row for row in rows if row["split"] == split]
        answerable = [row for row in selected if row["recall"] is not None]
        unanswerable = [row for row in selected if row["recall"] is None]
        splits[split] = {
            "answerable_count": len(answerable),
            "recall": {str(k): sum(row["recall"][str(k)] for row in answerable) / len(answerable)
                       if answerable else None for k in (1, 3, 5)},
            "mrr": sum(row["reciprocal_rank"] for row in answerable) / len(answerable)
            if answerable else None,
            "no_answer": {"count": len(unanswerable), "top_scores": [
                {"id": row["id"], "score": row["top_score"]} for row in unanswerable]},
        }
    kinds = {row["score_kind"] for row in rows}
    return {"splits": splits, "questions": rows, "top_k": top_k,
            "retrieval_mode": config.retrieval_mode, "mrr_cutoff": top_k,
            "score_kind": next(iter(kinds), default_kind) if len(kinds) <= 1 else "mixed"}


def write_report(report):
    """Create a unique report exclusively; never overwrite a previous run."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC)
    report = {**report, "timestamp": timestamp.isoformat()}
    path = REPORTS_DIR / f"retrieval-{timestamp:%Y%m%dT%H%M%S%fZ}-{uuid4().hex}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    if "embedding_usage" in report or "rerank_usage" in report:
        lines = ["# Retrieval evaluation", "",
                 "| Cache | Hits | Misses | Hit rate | API calls | Sent token estimate |",
                 "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for name in ("embedding", "rerank"):
            usage = report.get(name + "_usage", {})
            lines.append(
                f"| {name} | {usage.get('cache_hits', 0)} | {usage.get('cache_misses', 0)} | "
                f"{usage.get('cache_hit_rate', 0):.2%} | {usage.get('api_calls', 0)} | "
                f"{usage.get('estimated_tokens', 0)} |")
        path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
