"""Offline metric fixtures and verification of every checked-in gold citation."""

import json
import tempfile
import unittest
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from applications.project_assistant.corpus import collect_corpus, compute_corpus_version
from applications.project_assistant.evaluation.retrieval import (
    CATEGORIES,
    QUESTION_SET,
    evaluate,
    parse_reference,
    question_metrics,
    validate_question_set,
    write_report,
)
from applications.project_assistant.rag.chunking import Chunk
from applications.project_assistant.rag.retrieval import Evidence


def evidence(path="a.py", start=10, end=20, score=0.2):
    return Evidence("id", path, (start, end), "source", score, "python_function")


class RetrievalMetricsTests(unittest.TestCase):
    def test_partial_cross_file_recall_and_first_hit_rank(self):
        results = [evidence("wrong.py"), evidence(), evidence(), evidence("b.py", 30, 35)]
        metrics = question_metrics(["a.py:20-25", "b.py:32-40"], results)
        self.assertEqual(metrics["recall"], {"1": 0, "3": 0.5, "5": 1})
        self.assertEqual(metrics["reciprocal_rank"], 0.5)

    def test_overlap_is_inclusive_and_path_must_match(self):
        self.assertEqual(question_metrics(["a.py:20-25"], [evidence()])["recall"]["1"], 1)
        for results in ([], [evidence("b.py")], [evidence(end=19)]):
            self.assertEqual(question_metrics(["a.py:20-25"], results)["reciprocal_rank"], 0)

    def test_no_answer_has_no_metrics_and_reports_top_distance(self):
        self.assertEqual(question_metrics([], [evidence(score=0.7)]),
                         {"recall": None, "reciprocal_rank": None, "top_score": 0.7})
        self.assertIsNone(question_metrics([], [])["top_score"])

    def test_splits_exclude_no_answer_from_denominator(self):
        dataset = {"questions": [
            {"id": "yes", "split": "tune", "category": "concept", "question": "yes",
             "expected_evidence": ["a.py:10-20"]},
            {"id": "no", "split": "tune", "category": "no_answer", "question": "no",
             "expected_evidence": []},
            {"id": "miss", "split": "test", "category": "stale", "question": "miss",
             "expected_evidence": ["missing.py:1-2"]},
        ]}
        class Index:
            def count(self):
                return 1

        with patch("applications.project_assistant.evaluation.retrieval.retrieve", return_value=[evidence()]):
            report = evaluate(dataset, Index())
        self.assertEqual(report["splits"]["tune"]["mrr"], 1)
        self.assertEqual(report["splits"]["tune"]["answerable_count"], 1)
        self.assertEqual(report["splits"]["tune"]["no_answer"]["count"], 1)
        self.assertEqual(report["splits"]["test"]["mrr"], 0)
        self.assertEqual(report["splits"]["test"]["no_answer"]["count"], 0)
        with self.assertRaises(ValueError):
            evaluate(dataset, Index(), top_k=3)

    def test_empty_splits_do_not_divide_by_zero(self):
        report = evaluate({"questions": []}, None)
        self.assertIsNone(report["splits"]["test"]["mrr"])
        self.assertIsNone(report["splits"]["tune"]["recall"]["5"])

    def test_reports_never_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("applications.project_assistant.evaluation.retrieval.REPORTS_DIR", Path(directory)):
                first = write_report({"marker": 1})
                second = write_report({"marker": 2})
            self.assertNotEqual(first, second)
            self.assertEqual(json.loads(first.read_text())["marker"], 1)
            self.assertEqual(json.loads(second.read_text())["marker"], 2)
            self.assertIn("timestamp", json.loads(first.read_text()))

    def test_invalid_references_rejected(self):
        for reference in ("../a.py:1-2", "/a.py:1-2", "a.py:0-2", "a.py:4-2", "a.py:1", "a\\b.py:1-2"):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                parse_reference(reference)


class EvidenceValidationTests(unittest.TestCase):
    def test_all_gold_evidence_matches_real_ingested_files_and_lines(self):
        root = Path(__file__).resolve().parent.parent
        dataset = json.loads(QUESTION_SET.read_text(encoding="utf-8"))
        chunks = collect_corpus(root)
        self.assertGreaterEqual(len(dataset["questions"]), 30)
        self.assertLessEqual(len(dataset["questions"]), 50)
        for split in ("tune", "test"):
            self.assertEqual({q["category"] for q in dataset["questions"] if q["split"] == split}, CATEGORIES)
        # Validation checks the corpus hash, line existence, source text digests,
        # and that every gold range overlaps an actual ingested chunk.
        validate_question_set(dataset, root, chunks)

    def test_validation_rejects_drift_and_uningested_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.md").write_text("hello\nworld\n", encoding="utf-8")
            chunks = [Chunk("hello\nworld", "a.md", 1, 2, "text_block")]
            dataset = {"corpus_version": compute_corpus_version(chunks),
                       "evidence_sha256": {"a.md:1-2": sha256(b"hello\nworld").hexdigest()},
                       "questions": [{"id": "q", "split": "test", "category": "concept",
                                      "question": "hello?", "expected_evidence": ["a.md:1-2"]}]}
            validate_question_set(dataset, root, chunks)
            for field, value in (("corpus_version", "wrong"), ("evidence_sha256", {})):
                changed = deepcopy(dataset)
                changed[field] = value
                with self.assertRaises(ValueError):
                    validate_question_set(changed, root, chunks)
            (root / "a.md").write_text("hello\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_question_set(dataset, root, chunks)
            (root / "a.md").write_text("hello\nworld\n", encoding="utf-8")
            hidden = [Chunk("source", "other.py", 1, 1, "python_module")]
            dataset["corpus_version"] = compute_corpus_version(hidden)
            with self.assertRaises(ValueError):
                validate_question_set(dataset, root, hidden)


if __name__ == "__main__":
    unittest.main()
