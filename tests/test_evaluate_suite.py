"""Unified offline suite. Injected runners keep this test free of harness and corpus walks."""

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from applications.project_assistant.evaluation.answers import evaluate_answers, synthetic_answers
from applications.project_assistant.evaluation.suite import (
    PUBLISHED_RETRIEVAL,
    exit_code,
    main,
    run_offline,
)


def questions():
    return {"corpus_version": "frozen", "questions": [
        {"id": "a", "split": "tune", "category": "concept", "question": "a",
         "answer_summary": "see", "expected_evidence": ["src/a.py:1-1"]},
        {"id": "b", "split": "test", "category": "no_answer", "question": "b",
         "answer_summary": "abstain", "expected_evidence": []},
    ]}


class SuiteTests(unittest.TestCase):
    def test_offline_report_keeps_published_mrr_and_zero_cost(self):
        import tempfile
        dataset = questions()

        def harness():
            return {"passed": True, "success_rate": 1.0, "passed_count": 6, "case_count": 6,
                    "usage_kind": "synthetic", "mean_total_tokens": 15, "cost_cny": 0,
                    "cases": [{"name": "coding", "passed": True}]}

        def answers(data, directory):
            return evaluate_answers(data, synthetic_answers(data))

        def retrieval(data, root):
            return {"published": PUBLISHED_RETRIEVAL, "repro": {"status": "skipped",
                    "reason": "corpus_version_mismatch", "note": "not rebound"},
                    "cost_cny": 0, "provider_calls": 0}

        with tempfile.TemporaryDirectory() as folder:
            result = run_offline(root=Path(folder), questions=dataset, report_dir=Path(folder),
                                 harness_runner=harness, answer_runner=answers, retrieval_runner=retrieval)
            self.assertEqual(result["exit_code"], 0)
            text = next(Path(folder).glob("eval-*.md")).read_text(encoding="utf-8")
            self.assertIn("0.845", text)
            self.assertIn("0 CNY", text)
            self.assertIn("authorization_required", json.loads(next(Path(folder).glob("eval-*.json")).read_text())["live_answers"]["status"])
            self.assertNotIn("api_key", text.lower())
            self.assertNotRegex(text, r"[A-Za-z]:\\Users")

    def test_missing_live_is_not_a_failure_unless_required(self):
        summary = {"coding_harness": {"passed": True, "success_rate": 1.0, "ran": True},
                   "answers": {"live_executed": False, "splits": {"test": {"citation_support_rate": 0.5}}}}
        self.assertEqual(exit_code(summary, {})[0], 0)
        self.assertEqual(exit_code(summary, {"require_live_answers": True})[0], 1)
        self.assertEqual(exit_code(summary, {"min_citation_support": 0.9})[0], 1)
        failed = {"coding_harness": {"passed": False, "success_rate": 0.5, "ran": True}, "answers": {"live_executed": False, "splits": {}}}
        self.assertEqual(exit_code(failed, {})[0], 1)

    def test_cli_paid_commands_stop(self):
        smoke = io.StringIO()
        with redirect_stdout(smoke):
            self.assertEqual(main(["m2-smoke"]), 0)
        payload = json.loads(smoke.getvalue())
        self.assertFalse(payload["executed"])
        self.assertEqual(payload["budget_cny"], "0.20")
        self.assertEqual(payload["cost_cny"], 0)
        live = io.StringIO()
        with redirect_stdout(live):
            self.assertEqual(main(["live-answers", "--budget-cny", "2"]), 0)
        self.assertEqual(json.loads(live.getvalue())["provider_calls"], 0)


if __name__ == "__main__":
    unittest.main()
