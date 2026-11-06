"""Offline Terminal-Bench scoring. No dataset download and no model."""

import json
import unittest

from applications.project_assistant.evaluation.terminal_bench import (
    classify, load_subset, planned_live, run_fixture, score_records,
)


class TerminalBenchTests(unittest.TestCase):
    def test_subset_is_fixed_and_live_does_not_run(self):
        subset = load_subset()
        self.assertEqual(len(subset["task_ids"]), 18)
        self.assertEqual(subset["task_ids"][0], "adaptive-rejection-sampler")
        plan = planned_live("15")
        self.assertFalse(plan["executed"])
        self.assertIn("--budget-cny 15", plan["command"])

    def test_fixture_scores_and_infrastructure_is_separate(self):
        report = run_fixture(executor=lambda argv: {"passed": True, "steps": 2, "tokens": 0, "cost_cny": 0})
        self.assertEqual(report["success_rate"], 1)
        self.assertEqual(report["cost_cny"], 0)
        mixed = score_records([
            {"id": "ok", "passed": True, "steps": 1, "tokens": 10, "cost_cny": 0},
            {"id": "bad", "passed": False, "steps": 4, "tokens": 3, "cost_cny": 0},
            {"id": "infra", "infrastructure": True, "steps": 0, "tokens": 0, "cost_cny": 0},
            {"id": "sandbox", "sandbox_blocked": True, "steps": 2, "tokens": 1, "cost_cny": 0},
            {"id": "steps", "steps_exhausted": True, "steps": 8, "tokens": 2, "cost_cny": 0},
        ])
        self.assertEqual(mixed["infrastructure"], 1)
        self.assertEqual(mixed["model_tasks"], 4)
        self.assertEqual(classify({"infrastructure": True, "passed": False}), "infrastructure")
        self.assertNotIn("api_key", json.dumps(mixed))


if __name__ == "__main__":
    unittest.main()
