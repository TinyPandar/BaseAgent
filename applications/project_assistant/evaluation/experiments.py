"""Offline experiment metrics and tune-only no-answer threshold selection."""

import math
from dataclasses import fields

from applications.project_assistant.rag.retrieval import Evidence

from .retrieval import question_metrics


def score_distribution(scores):
    """Linear-interpolated quartiles of available scores; retain missing counts."""
    values = sorted(score for score in scores if score is not None)

    def percentile(fraction):
        if not values:
            return None
        position = (len(values) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    return {"count": len(values), "missing_count": len(scores) - len(values),
            **{label: percentile(fraction) for label, fraction in
               (("min", 0), ("p25", 0.25), ("median", 0.5), ("p75", 0.75), ("max", 1))}}


def threshold_metrics(rows, threshold):
    """Answerable iff top relevance >= threshold; missing results always abstain."""
    correct = {True: [], False: []}
    for row in rows:
        answerable = row["category"] != "no_answer"
        prediction = row["top_score"] is not None and row["top_score"] >= threshold
        correct[answerable].append(prediction == answerable)
    rates = [sum(values) / len(values) for values in correct.values() if values]
    values = correct[True] + correct[False]
    return {"balanced_accuracy": sum(rates) / len(rates) if rates else None,
            "accuracy": sum(values) / len(values) if values else None}


def select_threshold(rows):
    """Maximize balanced accuracy on tune; ties choose the smaller threshold."""
    if not rows or any(row["split"] != "tune" for row in rows):
        raise ValueError("threshold selection requires nonempty tune-only rows")
    scores = sorted({row["top_score"] for row in rows if row["top_score"] is not None})
    candidates = scores + ([math.nextafter(scores[-1], math.inf)] if scores else [0.0])
    best = max(candidates, key=lambda t: (threshold_metrics(rows, t)["balanced_accuracy"], -t))
    return {"threshold": best, **threshold_metrics(rows, best)}


def evaluate_ranking(questions, search, *, split, threshold=None, top_k=5):
    """Evaluate a single config once on the requested split, with relevance scores."""
    rows = []
    names = {field.name for field in fields(Evidence)}
    for question in questions:
        if question["split"] != split:
            continue
        results = search(question["question"])[:top_k]
        evidence = [Evidence(**{k: v for k, v in {
            **row["metadata"], "doc_id": row["doc_id"], "content": row["content"],
            "score": row["score"], "line_range": (row["metadata"]["line_start"],
                                                     row["metadata"]["line_end"]),
        }.items() if k in names}) for row in results]
        rows.append({"id": question["id"], "split": split, "category": question["category"],
                     **question_metrics(question["expected_evidence"], evidence),
                     "top_score": evidence[0].score if evidence else None,
                     "retrieved": [{"citation": item.citation, "score": item.score}
                                   for item in evidence]})
    answerable = [row for row in rows if row["recall"] is not None]
    metrics = {"mrr": sum(row["reciprocal_rank"] for row in answerable) / len(answerable)
               if answerable else 0.0,
               "recall": {str(k): sum(row["recall"][str(k)] for row in answerable)
                          / len(answerable) if answerable else 0.0 for k in (1, 3, 5)}}
    analysis = {"top1_score_distribution": {
        label: [row["top_score"] for row in rows
                if (row["category"] == "no_answer") == (label == "no_answer")]
        for label in ("answerable", "no_answer")}}
    analysis["top1_score_summary"] = {
        label: score_distribution(scores)
        for label, scores in analysis["top1_score_distribution"].items()}
    if split == "tune":
        analysis.update(select_threshold(rows))
    else:
        if threshold is None or not math.isfinite(threshold):
            raise ValueError("test requires an explicit finite tune-selected threshold")
        analysis.update({"threshold": threshold, **threshold_metrics(rows, threshold)})
    return {"metrics": metrics, "questions": rows, "no_answer": analysis,
            "score_kind": "relevance_higher_is_better", "top_k": top_k, "mrr_cutoff": top_k}
