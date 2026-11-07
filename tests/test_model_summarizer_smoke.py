"""Offline coverage for the opt-in ModelSummarizer smoke. No network calls."""

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from unittest.mock import patch

from applications.project_assistant.evaluation.metering import MeteredModel
from baseagent.llm.response import ModelResponse, TokenUsage
from scripts import smoke_model_summarizer as smoke


class FakeClient:
    def __init__(self, content="Compacted summary: first goal; first step.", usage=None, fail=False):
        self.content = content
        self.usage = usage or TokenUsage(100, 40, 140)
        self.fail = fail
        self.calls = 0
        self.prompts = []

    def complete(self, messages, tools=None):
        self.calls += 1
        self.prompts.append(messages)
        if self.fail:
            raise RuntimeError("private provider diagnostic")
        return ModelResponse({"content": self.content}, self.usage)


class ModelSummarizerSmokeTests(unittest.TestCase):
    def test_happy_path_fires_context_compacted_with_one_call(self):
        client = FakeClient()
        meter = MeteredModel(client, budget_cny="0.5", max_calls=1)
        report = smoke.run_smoke(model=meter, budget_cny="0.5")
        self.assertTrue(report["success"])
        self.assertEqual(report["events"], ["context_compacted"])
        self.assertFalse(report["stopped_for_budget"])
        self.assertFalse(report["trimmed"])
        self.assertEqual(report["provider_calls"], 1)
        self.assertEqual(client.calls, 1)
        self.assertIn("Compacted summary", report["summary_excerpt"])
        self.assertEqual(len(client.prompts), 1)
        self.assertEqual(client.prompts[0][0]["role"], "system")

    def test_prefix_over_input_limit_trims_once_then_succeeds(self):
        client = FakeClient()
        meter = MeteredModel(client, budget_cny="0.5", max_calls=1)
        report = smoke.run_smoke(model=meter, budget_cny="0.5", max_summary_input_chars=100)
        self.assertTrue(report["success"])
        self.assertTrue(report["trimmed"])
        self.assertIn("context_compaction_input_trimmed", report["events"])
        self.assertEqual(report["events"][-1], "context_compacted")
        self.assertEqual(client.calls, 1)

    def test_budget_crossing_call_is_billed_then_stops(self):
        client = FakeClient(usage=TokenUsage(1000, 100, 1100))
        # 1000 input at 0.8/M + 100 output at 2/M = 0.00102 CNY.
        meter = MeteredModel(client, budget_cny="0.0005", max_calls=1)
        report = smoke.run_smoke(model=meter, budget_cny="0.0005")
        self.assertTrue(report["success"])
        self.assertTrue(report["stopped_for_budget"])
        self.assertEqual(report["provider_calls"], 1)
        self.assertEqual(client.calls, 1)
        self.assertGreater(Decimal(report["cost_cny"]), Decimal("0.0005"))

    def test_failure_is_a_single_paid_attempt_not_a_three_failure_loop(self):
        client = FakeClient(fail=True)
        meter = MeteredModel(client, budget_cny="0.5", max_calls=1)
        report = smoke.run_smoke(model=meter, budget_cny="0.5")
        self.assertFalse(report["success"])
        self.assertEqual(report["events"], ["context_compaction_failed"])
        self.assertEqual(report["provider_calls"], 1)
        self.assertEqual(client.calls, 1)
        self.assertFalse(report["stopped_for_budget"])

    def test_prefix_too_short_is_not_success_and_makes_no_call(self):
        client = FakeClient()
        meter = MeteredModel(client, budget_cny="0.5", max_calls=1)
        report = smoke.run_smoke(model=meter, budget_cny="0.5", min_prefix_chars=10**9)
        self.assertFalse(report["success"])
        self.assertEqual(report["events"], ["context_compaction_skipped"])
        self.assertEqual(client.calls, 0)

    def test_missing_budget_exits_without_provider_configuration(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            smoke.main([])
        self.assertEqual(raised.exception.code, 2)

    def test_missing_llm_configuration_makes_no_provider_calls(self):
        stdout = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(smoke, "initialize_model") as initialize, \
                redirect_stdout(stdout):
            code = smoke.main(["--budget-cny", "0.5"])
        self.assertEqual(code, 2)
        self.assertIn("pending", stdout.getvalue())
        self.assertIn("BASEAGENT_LLM_*", stdout.getvalue())
        initialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
