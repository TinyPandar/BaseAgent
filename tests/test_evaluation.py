import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from baseagent.evaluation import evaluate, EvaluationFailure
from baseagent.session import SessionStore


class EvaluationTests(unittest.TestCase):
    def test_all_offline_cases(self):
        report = evaluate()
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["case_count"], 6)
        self.assertEqual(report["passed_count"], 6)
        self.assertEqual(report["model_kind"], "scripted_offline")
        self.assertEqual(report["usage_kind"], "synthetic")
        for case in report["cases"]:
            self.assertTrue(case["checks"])
            self.assertTrue(all(case["checks"].values()))
        delegation = next(case for case in report["cases"] if case["name"] == "delegation")
        self.assertEqual(delegation["metrics"]["nodes"], 2)
        self.assertIn("child_answer_cannot_replace_root_verification", delegation["checks"])

    def test_failure_report_is_machine_readable_and_omits_exception_body(self):
        def fail(root, evidence):
            raise RuntimeError("private exception body")
        with patch.dict("baseagent.evaluation.SCENARIOS", {"coding": fail}):
            report = evaluate(cases=["coding"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["cases"][0]["error_type"], "RuntimeError")
        self.assertNotIn("private exception body", json.dumps(report))

    def test_failed_check_is_identified_and_other_cases_continue(self):
        def fail(root, evidence):
            evidence["controlled_gate"] = False
            raise EvaluationFailure("controlled_gate")
        with patch.dict("baseagent.evaluation.SCENARIOS", {"coding": fail}):
            report = evaluate(cases=["coding", "approval"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["passed_count"], 1)
        self.assertEqual(report["cases"][0]["failed_check"], "controlled_gate")

    def test_report_publication_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            report = evaluate(cases=["approval"], report_path=target)
            self.assertEqual(json.loads(target.read_text()), report)
            original = target.read_bytes()
            with self.assertRaises(FileExistsError):
                evaluate(cases=["approval"], report_path=target)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob(".evaluation-*")), [])

    def test_cli_report_without_provider_and_without_user_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "report.json"
            result = subprocess.run([sys.executable, "-m", "baseagent", "--eval-harness", "--eval-case", "approval", "--eval-report", str(target)],
                                    cwd=root, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report, json.loads(target.read_text()))
            self.assertTrue(report["passed"])
            self.assertFalse((root / ".baseagent").exists())

    def test_invalid_case_selection(self):
        for cases in [[], ["bad"], ["approval", "approval"]]:
            with self.assertRaises(ValueError):
                evaluate(cases=cases)

    def test_delegation_report_fails_if_node_reconciliation_does_not_commit(self):
        with patch.object(SessionStore, "resolve_task_call", return_value=None):
            report = evaluate(cases=["delegation"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["cases"][0]["failed_check"], "root_budget_blocks_descendant_model")
        self.assertTrue(report["cases"][0]["checks"]["child_terminal_evidence_independently_verified"])

    def test_delegation_cli_case_is_available_without_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "delegation.json"
            result = subprocess.run([sys.executable, "-m", "baseagent", "--eval-harness", "--eval-case", "delegation", "--eval-report", str(target)],
                                    cwd=root, capture_output=True, timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report, json.loads(target.read_text()))
            self.assertEqual([case["name"] for case in report["cases"]], ["delegation"])
            self.assertTrue(report["passed"])
            self.assertFalse((root / ".baseagent").exists())


if __name__ == "__main__":
    unittest.main()
