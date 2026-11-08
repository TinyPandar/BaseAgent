"""Offline coverage for the metered live-answer path. No network calls."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from applications.project_assistant.config import RAGConfig
from applications.project_assistant.evaluation import answers_cli, live_answers
from applications.project_assistant.evaluation.live_answers import (
    LiveSetupError,
    prepare_checkpoint,
    retriever_for,
    run_live_answers,
)
from applications.project_assistant.evaluation.metering import MeteredModel
from baseagent.llm.response import ModelResponse, TokenUsage


def dataset():
    return {"corpus_version": "frozen", "questions": [
        {"id": "a", "split": "tune", "category": "concept", "question": "alpha",
         "answer_summary": "answer a", "expected_evidence": ["src/a.py:1-1"]},
        {"id": "b", "split": "test", "category": "concept", "question": "beta",
         "answer_summary": "answer b", "expected_evidence": ["src/b.py:2-2"]},
        {"id": "c", "split": "tune", "category": "concept", "question": "gamma",
         "answer_summary": "answer c", "expected_evidence": ["src/c.py:3-3"]},
        {"id": "d", "split": "test", "category": "concept", "question": "delta",
         "answer_summary": "answer d", "expected_evidence": ["src/d.py:4-4"]},
    ]}


class FakeClient:
    def __init__(self, usage=TokenUsage(100, 50, 150)):
        self.usage = usage
        self.calls = 0
        self.prompts = []

    def complete(self, messages, tools=None):
        self.calls += 1
        self.prompts.append(messages)
        question = messages[-1]["content"].splitlines()[1]
        return ModelResponse({"content": f"Answer for {question}. src/a.py:1-1"}, self.usage)


class LiveAnswerTests(unittest.TestCase):
    def test_plan_still_does_not_dispatch(self):
        stdout = io.StringIO()
        with patch.object(answers_cli, "prepare_live_retrieval") as prepare, \
                patch.object(answers_cli, "initialize_model") as initialize, \
                redirect_stdout(stdout):
            self.assertEqual(answers_cli.main(["--plan", "--budget-cny", "1.5"]), 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "authorization_required")
        self.assertFalse(payload["executed"])
        prepare.assert_not_called()
        initialize.assert_not_called()

    def test_live_without_llm_configuration_makes_no_provider_calls(self):
        stdout = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(answers_cli, "prepare_live_retrieval") as prepare, \
                patch.object(answers_cli, "initialize_model") as initialize, \
                redirect_stdout(stdout):
            code = answers_cli.main(["--live", "--budget-cny", "2"])
        self.assertNotEqual(code, 0)
        self.assertIn("pending", stdout.getvalue())
        self.assertIn("BASEAGENT_LLM_*", stdout.getvalue())
        prepare.assert_not_called()
        initialize.assert_not_called()

    def test_retriever_uses_hybrid_without_reranker(self):
        calls = []

        def fake_retrieve(query, index, top_k=5, **kwargs):
            calls.append((query, index, top_k, kwargs))
            return [SimpleNamespace(citation="src/a.py:1-1", content="evidence")]

        config = RAGConfig(embedding_provider="mock", retrieval_mode="hybrid")
        with patch.object(live_answers, "retrieve", fake_retrieve):
            retrieve_for = retriever_for("index", config, top_k=3)
            evidence = retrieve_for("alpha")
        self.assertEqual(evidence[0].citation, "src/a.py:1-1")
        query, index, top_k, kwargs = calls[0]
        self.assertEqual((query, index, top_k), ("alpha", "index", 3))
        self.assertIsNone(kwargs["reranker"])
        self.assertEqual(kwargs["config"].retrieval_mode, "hybrid")

    def test_generation_loop_is_metered_and_scores_tune_and_test_separately(self):
        calls = []

        def fake_retrieve(query, index, top_k=5, **kwargs):
            calls.append(query)
            return [SimpleNamespace(citation="src/a.py:1-1", content="evidence " + query)]

        config = RAGConfig(embedding_provider="mock", retrieval_mode="hybrid")
        with patch.object(live_answers, "retrieve", fake_retrieve):
            retriever = retriever_for("index", config, top_k=5)
            client = FakeClient()
            meter = MeteredModel(client, budget_cny=Decimal("0.0003"),
                                 input_rate=1, output_rate=1, reasoning_rate=1)
            with tempfile.TemporaryDirectory() as folder:
                result = run_live_answers(
                    dataset(), retriever=retriever, meter=meter,
                    budget_cny="0.00035", embedding_cost_cny="0.00005",
                    embedding_usage={"cache_hits": 0, "cache_misses": 0,
                                     "api_calls": 0, "total_tokens": 0},
                    report_dir=Path(folder))
                report = result["report"]
                json_text = result["path"].read_text(encoding="utf-8")
                markdown_text = result["path"].with_suffix(".md").read_text(encoding="utf-8")

        # Embeddings leave 0.0003 CNY, and each call costs 0.00015 CNY.
        self.assertEqual(client.calls, 2)
        self.assertEqual(calls, ["alpha", "beta"])
        self.assertEqual(result["summary"]["stopped_for_budget"], True)
        self.assertTrue(report["live_executed"])
        self.assertEqual(report["provider_calls"], 2)
        self.assertAlmostEqual(report["cost_cny"], 0.00035)
        self.assertAlmostEqual(report["embedding_cost_cny"], 0.00005)
        self.assertEqual(list(report["splits"]), ["tune", "test"])
        # Scores keep both splits; unanswered questions remain missing rows.
        self.assertEqual(report["splits"]["tune"]["count"], 2)
        self.assertEqual(report["splits"]["test"]["count"], 2)
        self.assertEqual(report["splits"]["tune"]["total_tokens"], 150)
        self.assertAlmostEqual(report["splits"]["test"]["cost_cny"], 0.00015)
        self.assertEqual(report["model_kind"], "live")
        self.assertEqual(len(report["questions"]), 4)
        for marker in ("api_key", "authorization:", "bearer ", "sk-"):
            self.assertNotIn(marker, json_text.lower())
            self.assertNotIn(marker, markdown_text.lower())

    def test_embedding_precheck_aborts_before_index_or_provider(self):
        class FakeEmbedding:
            dimension = 1024

            def __init__(self, *args, **kwargs):
                self.factory = args[0]

            def estimate_missing(self, texts):
                return 10_000_000

            def usage(self):
                return {"cache_hits": 0, "cache_misses": 0, "api_calls": 0, "total_tokens": 0}

        config = RAGConfig(
            embedding_provider="openai", openai_api_key="fake-private-key",
            embedding_model="text-embedding-v4", embedding_dimension=1024,
            embedding_batch_size=10, retrieval_mode="hybrid", exclude_patterns=[])
        with patch.object(live_answers.RAGConfig, "from_env", return_value=config), \
                patch.object(live_answers, "collect_corpus", return_value=[]), \
                patch.object(live_answers, "validate_question_set"), \
                patch.object(live_answers, "DiskCache", return_value=None), \
                patch.object(live_answers, "CachedEmbedding", FakeEmbedding), \
                patch.object(live_answers, "VectorIndex") as index:
            with self.assertRaises(LiveSetupError) as raised:
                live_answers.prepare_live_retrieval(
                    {"questions": []}, budget_cny="0.01", embedding_rate="0.5",
                    index_dir=Path(tempfile.gettempdir()) / "live-answer-index")
        self.assertIn("exceeds budget", str(raised.exception))
        self.assertNotIn("fake-private-key", str(raised.exception))
        index.assert_not_called()

    def test_usage_unavailable_is_recorded_and_later_questions_continue(self):
        class SelectiveClient:
            def __init__(self):
                self.calls = 0
                self.asked = []

            def complete(self, messages, tools=None):
                self.calls += 1
                question = messages[-1]["content"].splitlines()[1]
                self.asked.append(question)
                if question == "beta":
                    raise RuntimeError("private provider diagnostic")
                return ModelResponse(
                    {"content": f"Answer for {question}. src/a.py:1-1"},
                    TokenUsage(100, 50, 150))

        config = RAGConfig(embedding_provider="mock", retrieval_mode="hybrid")
        client = SelectiveClient()
        with patch.object(live_answers, "retrieve",
                          return_value=[SimpleNamespace(citation="src/a.py:1-1",
                                                        content="evidence")]):
            retriever = retriever_for("index", config, top_k=5)
            meter = MeteredModel(client, budget_cny=Decimal("1"),
                                 input_rate=1, output_rate=1, reasoning_rate=1)
            with tempfile.TemporaryDirectory() as folder:
                folder = Path(folder)
                checkpoint = folder / "checkpoint.json"
                result = run_live_answers(
                    dataset(), retriever=retriever, meter=meter, budget_cny="1",
                    embedding_cost_cny="0",
                    embedding_usage={"api_calls": 0, "total_tokens": 0},
                    report_dir=folder / "reports", checkpoint_path=checkpoint)
                saved = json.loads(checkpoint.read_text(encoding="utf-8"))
                self.assertTrue(result["path"].exists())

        self.assertEqual(client.asked, ["alpha", "beta", "gamma", "delta"])
        statuses = {row["id"]: (row["status"], row.get("error")) for row in saved["records"]}
        self.assertEqual(statuses["a"], ("ok", None))
        self.assertEqual(statuses["b"], ("failed", "UsageUnavailable"))
        self.assertEqual(statuses["c"], ("ok", None))
        self.assertEqual(statuses["d"], ("ok", None))
        self.assertEqual(result["summary"]["questions_ok"], 3)
        self.assertEqual(result["summary"]["questions_failed"], 1)
        self.assertEqual(result["summary"]["questions_remaining"], 0)
        for row in saved["records"]:
            self.assertNotIn("private provider diagnostic", json.dumps(row))

    def test_resume_skips_recorded_successes_and_failures(self):
        config = RAGConfig(embedding_provider="mock", retrieval_mode="hybrid")
        client = FakeClient()
        with patch.object(live_answers, "retrieve",
                          return_value=[SimpleNamespace(citation="src/a.py:1-1",
                                                        content="evidence")]):
            retriever = retriever_for("index", config, top_k=5)
            meter = MeteredModel(client, budget_cny=Decimal("1"),
                                 input_rate=1, output_rate=1, reasoning_rate=1)
            with tempfile.TemporaryDirectory() as folder:
                folder = Path(folder)
                checkpoint = folder / "checkpoint.json"
                checkpoint.write_text(json.dumps({
                    "schema_version": 1,
                    "embedding_cost_cny": "0.0001",
                    "embedding_usage": {"api_calls": 3, "total_tokens": 200},
                    "records": [
                        {"id": "a", "answer": "done a src/a.py:1-1", "citations": ["src/a.py:1-1"],
                         "latency_ms": 1, "total_tokens": 10, "cost_cny": 0.01,
                         "source": "live", "status": "ok", "error": None},
                        {"id": "b", "answer": "", "citations": [], "latency_ms": 1,
                         "total_tokens": 0, "cost_cny": 0, "source": "live",
                         "status": "failed", "error": "UsageUnavailable"},
                    ],
                }), encoding="utf-8")
                result = run_live_answers(
                    dataset(), retriever=retriever, meter=meter, budget_cny="1",
                    embedding_cost_cny="0",
                    embedding_usage={"api_calls": 0, "total_tokens": 0},
                    report_dir=folder / "reports", checkpoint_path=checkpoint,
                    resume=True)
                saved = json.loads(checkpoint.read_text(encoding="utf-8"))

        self.assertEqual(client.calls, 2)
        questions = [messages[-1]["content"].splitlines()[1] for messages in client.prompts]
        self.assertEqual(questions, ["gamma", "delta"])
        self.assertEqual([row["id"] for row in saved["records"]], ["a", "b", "c", "d"])
        self.assertEqual(saved["records"][1]["status"], "failed")
        self.assertEqual(result["summary"]["resumed"], True)
        self.assertEqual(result["summary"]["questions_ok"], 3)
        self.assertEqual(result["summary"]["questions_failed"], 1)
        self.assertAlmostEqual(float(saved["embedding_cost_cny"]), 0.0001)

    def test_existing_checkpoint_without_resume_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "checkpoint.json"
            original = json.dumps({
                "schema_version": 1,
                "embedding_cost_cny": "0.1",
                "embedding_usage": {},
                "records": [{"id": "a", "status": "ok"}],
            })
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(LiveSetupError) as raised:
                prepare_checkpoint(path, resume=False)
            self.assertIn("--resume", str(raised.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), original)

            stdout = io.StringIO()
            with patch.object(answers_cli, "initialize_model") as initialize,                     patch.object(answers_cli, "prepare_live_retrieval") as prepare,                     redirect_stdout(stdout):
                code = answers_cli.main([
                    "--live", "--budget-cny", "2", "--checkpoint", str(path),
                ])
            self.assertEqual(code, 2)
            self.assertIn("checkpoint already exists", stdout.getvalue())
            initialize.assert_not_called()
            prepare.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_resume_without_checkpoint_makes_no_provider_calls(self):
        missing = Path(tempfile.gettempdir()) / "baseagent-missing-checkpoint.json"
        if missing.exists():
            missing.unlink()
        stdout = io.StringIO()
        with patch.object(answers_cli, "initialize_model") as initialize,                 patch.object(answers_cli, "prepare_live_retrieval") as prepare,                 redirect_stdout(stdout):
            code = answers_cli.main([
                "--live", "--budget-cny", "2", "--resume", "--checkpoint", str(missing),
            ])
        self.assertEqual(code, 2)
        self.assertIn("existing --checkpoint", stdout.getvalue())
        initialize.assert_not_called()
        prepare.assert_not_called()

    def test_budget_stop_still_persists_earlier_questions(self):
        config = RAGConfig(embedding_provider="mock", retrieval_mode="hybrid")
        client = FakeClient()
        with patch.object(live_answers, "retrieve",
                          return_value=[SimpleNamespace(citation="src/a.py:1-1",
                                                        content="evidence")]):
            retriever = retriever_for("index", config, top_k=5)
            meter = MeteredModel(client, budget_cny=Decimal("0.0003"),
                                 input_rate=1, output_rate=1, reasoning_rate=1)
            with tempfile.TemporaryDirectory() as folder:
                folder = Path(folder)
                checkpoint = folder / "checkpoint.json"
                result = run_live_answers(
                    dataset(), retriever=retriever, meter=meter,
                    budget_cny="0.00035", embedding_cost_cny="0.00005",
                    embedding_usage={"api_calls": 0, "total_tokens": 0},
                    report_dir=folder / "reports", checkpoint_path=checkpoint)
                saved = json.loads(checkpoint.read_text(encoding="utf-8"))

        self.assertEqual(client.calls, 2)
        self.assertEqual([row["id"] for row in saved["records"]], ["a", "b"])
        self.assertTrue(all(row["status"] == "ok" for row in saved["records"]))
        self.assertEqual(result["summary"]["stopped_for_budget"], True)
        self.assertEqual(result["summary"]["questions_remaining"], 2)


if __name__ == "__main__":
    unittest.main()
