"""Offline answer scoring. No network and no provider client."""

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from applications.project_assistant.evaluation.answers import (
    consume_budget,
    evaluate_answers,
    human_review_sample,
    is_refusal,
    planned_live,
    score_record,
    synthetic_answers,
)
from applications.project_assistant.evaluation.answers_cli import main as answers_main
from applications.project_assistant.evaluation.retrieval import QUESTION_SET


def question(identifier, split, category, evidence):
    return {"id": identifier, "split": split, "category": category, "question": identifier,
            "answer_summary": "summary", "expected_evidence": evidence}


class AnswerScoringTests(unittest.TestCase):
    def test_citation_overlap_and_plain_no_are_not_refusals(self):
        gold = question("q", "tune", "exact_identifier", ["src/a.py:1-3"])
        covered = score_record(gold, {"answer": "No. See src/a.py:2-2", "citations": []})
        missed = score_record(gold, {"answer": "No. See other.md:1-1", "citations": ["other.md:1-1"]})
        self.assertTrue(covered["citation_supported"])
        self.assertFalse(covered["refused"])
        self.assertTrue(covered["rule_correct"])
        self.assertFalse(missed["citation_supported"])
        self.assertFalse(is_refusal("No. Built-ins remain coding."))

    def test_no_answer_rules(self):
        item = question("n", "test", "no_answer", [])
        refused = score_record(item, {"answer": "The fact is not established by this checkout."})
        invented = score_record(item, {"answer": "The fact is not established.", "citations": ["src/a.py:1-1"]})
        answered = score_record(item, {"answer": "It cost 12 dollars."})
        self.assertTrue(refused["rule_correct"])
        self.assertFalse(invented["rule_correct"])
        self.assertFalse(answered["rule_correct"])

    def test_splits_stay_separate_and_test_cannot_tune(self):
        dataset = {"questions": [
            question("a", "tune", "concept", ["src/a.py:1-1"]),
            question("b", "test", "no_answer", []),
        ]}
        records = [
            {"id": "a", "answer": "src/a.py:1-1", "citations": ["src/a.py:1-1"], "source": "fixture"},
            {"id": "b", "answer": "not established", "source": "fixture"},
        ]
        report = evaluate_answers(dataset, records)
        self.assertEqual(report["splits"]["tune"]["count"], 1)
        self.assertEqual(report["splits"]["test"]["count"], 1)
        self.assertIsNone(report["splits"]["tune"]["refusal_rate"])
        self.assertEqual(report["splits"]["test"]["citation_support_rate"], None)
        self.assertIsNone(report["judge_cross_table"])
        with self.assertRaises(ValueError):
            evaluate_answers(dataset, records, judge="tune_on_test")

    def test_judge_cross_table_and_human_sample(self):
        dataset = {"questions": [question(f"q{i:02d}", "tune" if i < 5 else "test", "concept", ["src/a.py:1-1"])
                                 for i in range(10)]}
        records = [{"id": item["id"], "answer": "src/a.py:1-1", "citations": ["src/a.py:1-1"]}
                   for item in dataset["questions"]]
        judge = {item["id"]: {"correct": item["id"] != "q00"} for item in dataset["questions"]}
        report = evaluate_answers(dataset, records, judge=judge)
        self.assertEqual(report["splits"]["tune"]["accuracy_source"], "llm_judge")
        self.assertEqual(report["judge_cross_table"]["judge_incorrect_rule_correct"], 1)
        self.assertEqual(report["judge_cross_table"]["judge_correct_rule_correct"], 9)
        sample = human_review_sample(dataset["questions"])
        self.assertEqual(len(sample), 2)
        self.assertTrue(all(item["reviewed"] is None for item in sample))

    def test_real_question_set_synthetic_pipeline_is_free(self):
        dataset = json.loads(QUESTION_SET.read_text(encoding="utf-8"))
        report = evaluate_answers(dataset, synthetic_answers(dataset))
        self.assertEqual(report["splits"]["tune"]["accuracy"], 1)
        self.assertEqual(report["splits"]["test"]["accuracy"], 1)
        self.assertEqual(report["splits"]["tune"]["citation_support_rate"], 1)
        self.assertEqual(report["splits"]["test"]["refusal_rate"], 1)
        self.assertEqual(report["cost_cny"], 0)
        self.assertEqual(report["provider_calls"], 0)
        self.assertFalse(report["live_executed"])
        self.assertEqual(len(report["human_review"]), 16)
        tune_ids = {row["id"] for row in report["questions"] if row["split"] == "tune"}
        test_ids = {row["id"] for row in report["questions"] if row["split"] == "test"}
        self.assertFalse(tune_ids & test_ids)
        self.assertEqual(len(tune_ids) + len(test_ids), 80)

    def test_live_command_does_not_dispatch_and_budget_stops(self):
        plan = planned_live("1.5")
        self.assertFalse(plan["executed"])
        self.assertEqual(plan["cost_cny"], 0)
        self.assertIn("--budget-cny 1.5", plan["command"])
        calls = []

        def ask(question):
            calls.append(question["id"])
            return {"id": question["id"], "cost_cny": "0.8"}

        stopped = consume_budget([{"id": "a"}, {"id": "b"}, {"id": "c"}], ask, "1.0")
        self.assertEqual(calls, ["a", "b"])
        self.assertTrue(stopped["stopped_for_budget"])
        self.assertEqual(stopped["provider_calls"], 2)

    def test_cli_synthetic_and_live_write_no_secrets(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = answers_main(["--synthetic", "--report-dir", str(directory), "--split", "test"])
            self.assertEqual(code, 0)
            written = list(directory.glob("answers-*.json"))
            self.assertEqual(len(written), 1)
            text = written[0].read_text(encoding="utf-8")
            self.assertNotIn("tune", json.loads(text)["splits"])
            self.assertNotIn("api_key", text.lower())
            self.assertNotIn("sk-", text.lower())
            live = io.StringIO()
            with redirect_stdout(live):
                self.assertEqual(answers_main(["--live", "--budget-cny", "2"]), 0)
            self.assertIn("authorization_required", live.getvalue())
            self.assertNotIn("provider.invalid", live.getvalue())


if __name__ == "__main__":
    unittest.main()
