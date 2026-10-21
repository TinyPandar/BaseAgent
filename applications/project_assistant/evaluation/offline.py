"""BM25-only evaluation and chars/3 estimates, with network access disabled."""

import argparse
import json
import math
import socket
import subprocess
from collections import Counter
from hashlib import sha256
from pathlib import Path

from applications.project_assistant.corpus import collect_corpus, compute_corpus_version
from applications.project_assistant.evaluation.experiments import (
    evaluate_ranking,
    score_distribution,
)
from applications.project_assistant.evaluation.retrieval import (
    QUESTION_SET,
    parse_reference,
    validate_question_set,
)
from applications.project_assistant.rag.lexical import BM25Index


def estimate(text):
    return math.ceil(len(text) / 3)


def blocked_network(*args, **kwargs):
    raise RuntimeError("network access is disabled for this offline evaluation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True, help="Commit containing the original gold set")
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path")
    args = parser.parse_args()
    socket.socket.connect = blocked_network
    socket.create_connection = blocked_network
    root = Path(__file__).resolve().parents[3]
    dataset = json.loads(QUESTION_SET.read_text(encoding="utf-8"))
    chunks = collect_corpus(root)
    validate_question_set(dataset, root, chunks)
    unique = list({chunk.doc_id: chunk for chunk in chunks}.values())
    index = BM25Index(unique, k1=1.5, b=0.75)
    candidates = {q["question"]: index.search(q["question"], top_k=20)
                  for q in dataset["questions"]}
    search = candidates.__getitem__
    tune = evaluate_ranking(dataset["questions"], search, split="tune")
    test = evaluate_ranking(dataset["questions"], search, split="test",
                            threshold=tune["no_answer"]["threshold"])
    estimates = []
    for q in dataset["questions"]:
        full_texts = [row["content"] for row in candidates[q["question"]]]
        texts = [text[:6000] for text in full_texts]
        estimates.append({
            "id": q["id"], "split": q["split"], "query_chars": len(q["question"]),
            "query_tokens": estimate(q["question"]), "candidate_count": len(texts),
            "document_chars": sum(map(len, texts)),
            "query_plus_20_documents_tokens": estimate(q["question"]) + sum(map(estimate, texts)),
            "query_plus_20_full_documents_tokens": estimate(q["question"]) + sum(map(estimate, full_texts)),
            "repeated_query_pairwise_tokens": sum(estimate(q["question"] + text) for text in texts),
            "candidates": [row["metadata"]["file_path"] + ":"
                           + str(row["metadata"]["line_start"]) + "-"
                           + str(row["metadata"]["line_end"]) for row in candidates[q["question"]]],
        })
    baseline = json.loads(subprocess.check_output(
        ["git", "show", f"{args.baseline_ref}:applications/project_assistant/evaluation/questions.json"],
        cwd=root, text=True))
    current = {q["id"]: q for q in dataset["questions"]}
    corrections = [{"id": old["id"], "before": old, "after": current[old["id"]]}
                   for old in baseline["questions"] if old != current[old["id"]]]
    evidence = []
    for reference in dataset["evidence_sha256"]:
        path, start, end = parse_reference(reference)
        lines = (root / path).read_text(encoding="utf-8").splitlines()
        evidence.append({"reference": reference, "line_count": end - start + 1,
                         "sha256": sha256("\n".join(lines[start - 1:end]).encode()).hexdigest()})
    rows = tune["questions"] + test["questions"]
    report = {
        "corpus_version": compute_corpus_version(chunks),
        "corpus_version_method": dataset["corpus_version_method"],
        "question_set_sha256": sha256(QUESTION_SET.read_bytes()).hexdigest(),
        "baseline_ref": args.baseline_ref,
        "statistics": {"total": len(current),
                       "splits": dict(Counter(q["split"] for q in current.values())),
                       "categories": dict(Counter(q["category"] for q in current.values())),
                       "categories_by_split": {
                           split: dict(Counter(q["category"] for q in current.values()
                                               if q["split"] == split)) for split in ("tune", "test")},
                       "no_answer": sum(q["category"] == "no_answer" for q in current.values())},
        "bm25": {"k1": 1.5, "b": 0.75, "top_k": 5, "candidate_depth": 20,
                 "splits": {"tune": tune, "test": test},
                 "overall_top1_score_summary": {
                     label: score_distribution([r["top_score"] for r in rows
                                                if (r["category"] == "no_answer") == (label == "no_answer")])
                     for label in ("answerable", "no_answer")}},
        "tokens": {"method": "ceil(len(text)/3) per text, Unicode characters; no tokenizer/API call",
                   "chunk_count_all": len(chunks), "chunk_count_unique": len(unique),
                   "corpus_chars_all": sum(len(c.content) for c in chunks),
                   "corpus_tokens_all": sum(estimate(c.content) for c in chunks),
                   "corpus_chars_unique": sum(len(c.content) for c in unique),
                   "corpus_tokens_unique": sum(estimate(c.content) for c in unique),
                   "query_chars": sum(r["query_chars"] for r in estimates),
                   "query_tokens": sum(r["query_tokens"] for r in estimates),
                   "rerank_method": "BM25 top20 content, 6000 chars/document cap; query once plus documents; pairwise repeated-query estimate also recorded; metadata/protocol overhead excluded",
                   "rerank_per_question": estimates,
                   "rerank_total": sum(r["query_plus_20_documents_tokens"] for r in estimates),
                   "rerank_full_documents_total": sum(r["query_plus_20_full_documents_tokens"] for r in estimates),
                   "rerank_full_documents_distribution": score_distribution([
                       r["query_plus_20_full_documents_tokens"] for r in estimates]),
                   "rerank_distribution": score_distribution([
                       r["query_plus_20_documents_tokens"] for r in estimates]),
                   "rerank_pairwise_total": sum(r["repeated_query_pairwise_tokens"] for r in estimates)},
        "original_question_corrections": corrections,
        "evidence_audit": evidence,
        "validation": {"validate_question_set": "passed", "evidence_review": "Every distinct gold range opened with source line numbers and reviewed; digests checked afterwards",
                       "no_answer_review": "Reviewed source and default-corpus term search; implementation/configuration and requested operational facts are absent",
                       "api_calls": 0, "network_connections": 0, "cost_usd": 0},
    }
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"statistics": report["statistics"],
                      "metrics": {split: value["metrics"] for split, value in
                                  report["bm25"]["splits"].items()},
                      "scores": report["bm25"]["overall_top1_score_summary"],
                      "tokens": {k: v for k, v in report["tokens"].items()
                                 if k != "rerank_per_question"}}, indent=2))


if __name__ == "__main__":
    main()
