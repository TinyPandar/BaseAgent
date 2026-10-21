"""Exercise the evaluation CLI with a fake SDK and index, without network calls."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from applications.project_assistant.rag.chunking import Chunk
from scripts import evaluate_retrieval as cli


class EvaluationCLITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.questions = self.directory / "questions.json"
        self.questions.write_text(json.dumps({"questions": [
            {"id": "q", "question": "hello?", "category": "no_answer", "split": "test",
             "expected_evidence": []},
        ]}))
        self.stack.enter_context(patch.dict(os.environ, {
            "RAG_CACHE_DIR": str(self.directory / "cache"),
            "RAG_EMBEDDING_PROVIDER": "openai", "RAG_EMBEDDING_API_KEY": "fake-private-key",
            "RAG_EMBEDDING_MODEL": "text-embedding-v4", "RAG_EMBEDDING_BATCH_SIZE": "10",
            "RAG_EMBEDDING_DIMENSIONS": "2",
            "RAG_EMBEDDING_BASE_URL": "https://user:private-password@provider.invalid/v1",
        }, clear=True))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.chunks = [Chunk(str(i), "a.md", i + 1, i + 1, "text_block") for i in range(23)]
        self.stack.enter_context(patch.object(cli, "collect_corpus", return_value=self.chunks))
        self.stack.enter_context(patch.object(cli, "validate_question_set"))
        self.client = Mock()
        self.client.embeddings.create.side_effect = lambda **kw: SimpleNamespace(
            data=[SimpleNamespace(index=i, embedding=[1.0, 0.0]) for i in range(len(kw["input"]))],
            usage=SimpleNamespace(total_tokens=len(kw["input"]) * 2),
        )
        self.sdk = Mock(return_value=self.client)
        self.stack.enter_context(patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=self.sdk)}))
        self.index = Mock()
        self.index.count.return_value = 23
        self.index.search.return_value = []
        self.index_factory = self.stack.enter_context(patch.object(cli, "VectorIndex", return_value=self.index))
        self.index.add_chunks.side_effect = lambda chunks: self.index_factory.call_args.args[0].embed(
            [chunk.content for chunk in chunks])
        self.index.search.side_effect = self.search
        self.writer = self.stack.enter_context(patch.object(cli, "write_report", return_value="report.json"))

    def search(self, query, **kwargs):
        self.index_factory.call_args.args[0].embed_query(query)
        return []

    def run_cli(self, *args):
        return cli.main(["--questions", str(self.questions), *args])

    def test_real_provider_from_env_batches_persists_and_omits_secrets(self):
        self.assertEqual(self.run_cli("--index-dir", str(self.directory / "index"), "--max-tokens", "26"), 0)
        self.assertEqual(self.index_factory.call_args.kwargs["persist_directory"], str(self.directory / "index"))
        report = self.writer.call_args.args[0]
        self.assertEqual(report["api_calls"], 4)  # Three corpus batches and one query.
        self.assertEqual(report["total_tokens"], 48)
        self.assertEqual(report["embedding_dimension"], 2)
        self.assertEqual(report["estimated_tokens"], 26)
        serialized = json.dumps(report)
        for secret in ("fake-private-key", "private-password", "base_url", "provider.invalid", "api_key"):
            self.assertNotIn(secret, serialized)
        self.sdk.assert_called_once_with(api_key="fake-private-key", base_url=os.environ["RAG_EMBEDDING_BASE_URL"])

    def test_second_run_zero_budget_hits_chunks_and_queries_without_sdk(self):
        self.run_cli()
        self.sdk.reset_mock()
        self.client.embeddings.create.reset_mock()
        self.run_cli("--max-tokens", "0")
        self.sdk.assert_not_called()
        self.client.embeddings.create.assert_not_called()
        usage = self.writer.call_args.args[0]["embedding_usage"]
        self.assertEqual(usage["cache_hits"], 24)
        self.assertEqual(usage["cache_misses"], 0)
        self.assertEqual(usage["estimated_tokens"], 0)

    def test_partial_cache_budget_counts_only_unsent_text(self):
        from applications.project_assistant.rag.cache import DiskCache, embedding_key

        cache = DiskCache()
        cache.put("embedding", embedding_key("openai", "text-embedding-v4", 2, "0"), [1., 0.])
        self.run_cli("--max-tokens", "25")
        sent = [text for call in self.client.embeddings.create.call_args_list
                for text in call.kwargs["input"]]
        self.assertNotIn("0", sent)
        self.assertEqual(len(sent), 23)
        self.assertEqual(self.writer.call_args.args[0]["embedding_usage"]["cache_hits"], 1)

    def test_no_cache_sends_all_again_and_budget_counts_all(self):
        self.run_cli()
        with self.assertRaises(SystemExit):
            self.run_cli("--no-cache", "--max-tokens", "0")
        self.client.embeddings.create.reset_mock()
        self.run_cli("--no-cache", "--max-tokens", "26")
        self.assertEqual(self.client.embeddings.create.call_count, 4)
        self.assertEqual(self.writer.call_args.args[0]["embedding_usage"]["cache_hits"], 0)

    def test_budget_aborts_before_client_or_index_creation(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_cli("--max-tokens", "25")
        self.assertEqual(raised.exception.code, 2)
        self.sdk.assert_not_called()
        self.index_factory.assert_not_called()
        self.writer.assert_not_called()

    def test_real_default_index_is_gitignored(self):
        self.run_cli()
        directory = Path(self.index_factory.call_args.kwargs["persist_directory"])
        self.assertEqual(directory, cli.project_root / ".baseagent/retrieval_evaluation")

    def test_real_index_rejects_nonignored_repo_directory(self):
        with self.assertRaises(SystemExit):
            self.run_cli("--index-dir", str(cli.project_root / "public-eval-index"))
        self.sdk.assert_not_called()
        self.index_factory.assert_not_called()

    def test_mock_from_env_does_not_construct_sdk_or_persist(self):
        with patch.dict(os.environ, {"RAG_EMBEDDING_PROVIDER": "mock"}):
            self.run_cli("--max-tokens", "0")
        self.sdk.assert_not_called()
        self.assertIsNone(self.index_factory.call_args.kwargs["persist_directory"])
        self.assertEqual(self.writer.call_args.args[0]["total_tokens"], 0)

    def test_explicit_provider_flags_override_environment(self):
        self.run_cli("--mock-embedding")
        self.sdk.assert_not_called()
        with patch.dict(os.environ, {"RAG_EMBEDDING_PROVIDER": "mock"}):
            self.run_cli("--openai-embedding")
        self.sdk.assert_called_once()


if __name__ == "__main__":
    unittest.main()
