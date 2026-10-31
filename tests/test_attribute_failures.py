import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.attribute_failures import classify_run, load_runs, summarize  # noqa: E402


class AttributionTests(unittest.TestCase):
    def test_real_cassettes_are_consistent_and_deterministic(self):
        runs = load_runs()
        summary = summarize(runs)
        self.assertEqual(sum(v["count"] for v in summary["categories"].values()), summary["failed_runs"])
        first = subprocess.check_output([sys.executable, "scripts/attribute_failures.py", "--json"], cwd=ROOT)
        second = subprocess.check_output([sys.executable, "scripts/attribute_failures.py", "--json"], cwd=ROOT)
        self.assertEqual(json.loads(first), json.loads(second))

    def test_primary_category_synthetic_cases(self):
        failed = {"external_acceptance_passed": False, "status": "failed"}
        rr = [{"name": "read_file", "arguments": {"path": "a"}, "output_chars": 0}]
        repeated = rr + [{**rr[0]}]
        self.assertEqual(classify_run(failed, repeated, [])['primary_category'], 'repeated_file_reads')
        self.assertEqual(classify_run(failed, [], [])['primary_category'], 'other')
        self.assertEqual(classify_run({**failed, "status": "max_steps_exceeded"}, [], [])['primary_category'], 'step_limit_exhausted')
        huge = [{"name": "read_file", "arguments": {}, "output_chars": 8001}]
        self.assertEqual(classify_run(failed, huge, [])['primary_category'], 'tool_output_context_blowup')
        self.assertEqual(classify_run(failed, [{"name": "read_file", "arguments": {}, "output_chars": 0}] * 3, [])['primary_category'], 'verify_never_called')
        verified = [{"name": "verify_command", "arguments": {}, "output_chars": 0,
                     "output": '{"ok":true,"data":{"exit_code":0}}'}]
        self.assertEqual(classify_run(failed, verified, [])['primary_category'], 'other')

    def test_stalled_answers_and_permission_denied_are_diagnostics(self):
        denial = '{"ok":false,"error":{"code":"permission_denied"}}'
        messages = [
            {"role": "system", "content": "Harness completion requirements are unmet"},
            {"role": "assistant", "content": "I could not finish"},
            {"role": "system", "content": "Harness completion requirements are unmet"},
            {"role": "assistant", "content": "I could not finish"},
        ]
        calls = [{"name": "edit_file", "arguments": {"path": "sample.py"}, "output": denial, "output_chars": len(denial)}]
        d = classify_run({"external_acceptance_passed": False, "status": "failed"}, calls, [], messages)
        self.assertEqual(d["permission_denied_count"], 1)
        self.assertEqual(d["permission_denied_tools"], ["edit_file"])
        self.assertEqual(d["completion_rejections"], 2)
        self.assertEqual(d["stalled_final_answers"], 2)
        self.assertFalse(d["required_edit_succeeded"])


if __name__ == "__main__":
    unittest.main()
