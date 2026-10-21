"""Candidate retrieval, rerank protocol and threshold tests; no network clients."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from applications.project_assistant.config import RAGConfig
from applications.project_assistant.evaluation.experiments import (
    evaluate_ranking,
    score_distribution,
    select_threshold,
    threshold_metrics,
)
from applications.project_assistant.rag.chunking import Chunk
from applications.project_assistant.rag.hybrid import reciprocal_rank_fusion
from applications.project_assistant.rag.lexical import BM25Index, chunk_result, tokenize
from applications.project_assistant.rag.rerank import (
    HeuristicReranker,
    QwenRerank,
    derive_rerank_base_url,
)
from applications.project_assistant.rag.retrieval import retrieve
from scripts import run_retrieval_experiments as cli


def document(text="hello", path="a.py"):
    return chunk_result(Chunk(text, path, 1, 2, "text_block"))


class CandidateTests(unittest.TestCase):
    def test_score_quartiles_include_zero_and_track_missing_results(self):
        self.assertEqual(score_distribution([None, 30, 0, 10, 20]), {
            "count": 4, "missing_count": 1, "min": 0, "p25": 7.5,
            "median": 15, "p75": 22.5, "max": 30,
        })
        self.assertIsNone(score_distribution([None])["median"])
        self.assertEqual(score_distribution([7])["p75"], 7)

    def test_tokenizer_full_identifiers_and_parts(self):
        tokens = tokenize("pkg/foo_bar.HTTPClient42 getHTTPResponse snake_case")
        self.assertTrue({"pkg/foo_bar.httpclient42", "pkg", "foo_bar", "foo", "bar", "http",
                         "httpclient42", "client", "42", "gethttpresponse", "get", "response",
                         "snake_case", "snake", "case"}.issubset(tokens))
        self.assertEqual(tokenize("AbC_123"), tokenize("AbC_123"))
        self.assertFalse(any(t in tokens for t in ("HTTP", "Client")))

    def test_cjk_and_ascii_boundaries(self):
        self.assertEqual(tokenize("知识库 abc 中文"),
                         ["abc", "知", "识", "库", "知识", "识库", "中", "文", "中文"])
        self.assertNotIn("库中", tokenize("知识库 中文"))
        self.assertEqual(tokenize(""), [])

    def test_bm25_ranks_and_ties_deterministically(self):
        chunks = [Chunk("other", "b.py", 1, 2, "text_block"),
                  Chunk("retrieve_docs retrieves documents", "a.py", 1, 2, "text_block")]
        index = BM25Index(chunks)
        self.assertEqual(index.search("retrieve_docs")[0]["doc_id"], chunks[1].doc_id)
        self.assertGreater(index.search("retrieve_docs")[0]["score"], 0)
        self.assertEqual(index.search("absent"), BM25Index(reversed(chunks)).search("absent"))
        self.assertEqual(BM25Index([]).search("query"), [])
        for kwargs in ({"k1": 0}, {"b": 2}):
            with self.assertRaises(ValueError):
                BM25Index(chunks, **kwargs)

    def test_rrf_math_weights_depth_and_duplicates(self):
        a, b, c = document(path="a"), document(path="b"), document(path="c")
        result = reciprocal_rank_fusion([[a, b, a], [b, c]], rrf_k=10,
                                        weights=[2, 1], candidate_depth=2)
        scores = {row["doc_id"]: row["score"] for row in result}
        self.assertAlmostEqual(scores[a["doc_id"]], 2 / 11)
        self.assertAlmostEqual(scores[b["doc_id"]], 2 / 12 + 1 / 11)
        self.assertAlmostEqual(scores[c["doc_id"]], 1 / 12)
        self.assertEqual(result[0]["doc_id"], b["doc_id"])
        self.assertEqual(reciprocal_rank_fusion([[a, a]], rrf_k=0)[0]["score"], 1)
        with self.assertRaises(ValueError):
            reciprocal_rank_fusion([[a]], weights=[-1])

    def test_default_vector_behavior_and_distance_filter(self):
        index = Mock()
        index.search.return_value = [document() | {"score": 0.2}, document(path="b") | {"score": 0.8}]
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(RAGConfig.from_env().retrieval_mode, "vector")
            result = retrieve("hello", index, top_k=2, min_score=0.5)
        index.search.assert_called_once_with("hello", top_k=2)
        index.collection.get.assert_not_called()
        self.assertEqual([row.score for row in result], [0.2])

    def test_bm25_env_does_not_embed_and_hybrid_opt_in(self):
        chunk = Chunk("hello identifier", "a.py", 1, 2, "text_block")
        index = Mock()
        index.collection.get.return_value = {"documents": [chunk.content],
                                             "metadatas": [chunk_result(chunk)["metadata"]]}
        index.count.return_value = 1
        index.search.return_value = [chunk_result(chunk)]
        with patch.dict(os.environ, {"RAG_RETRIEVAL_MODE": "bm25"}):
            self.assertEqual(retrieve("identifier", index)[0].doc_id, chunk.doc_id)
        index.search.assert_not_called()
        result = retrieve("identifier", index, config=RAGConfig(retrieval_mode="hybrid"))
        self.assertAlmostEqual(result[0].score, 1 / 31 + 2 / 31)

    def test_heuristic_identifier_path_and_coverage(self):
        exact = document("def loadConfig(): pass", "pkg/retrieval.py")
        other = document("load the configuration retrieval", "b.py")
        reranker = HeuristicReranker()
        self.assertEqual(reranker.rerank("loadConfig pkg/retrieval.py", [other, exact])[0]["doc_id"],
                         exact["doc_id"])
        self.assertEqual(reranker.rerank("configuration", [exact, other])[0]["doc_id"], other["doc_id"])


class RerankTests(unittest.TestCase):
    def make_reranker(self, **kwargs):
        return QwenRerank(allow_api=True, max_tokens=kwargs.pop("max_tokens", 10000),
                          base_url="https://private.invalid/v1", api_key="secret", **kwargs)

    def test_base_derivation_and_env_fallbacks(self):
        self.assertEqual(derive_rerank_base_url("https://host/compatible-mode/v1/"),
                         "https://host/compatible-api/v1")
        self.assertEqual(derive_rerank_base_url("https://host/v1"), "https://host/v1")
        with patch.dict(os.environ, {"RAG_EMBEDDING_BASE_URL": "https://host/compatible-mode/v1",
                                     "RAG_EMBEDDING_API_KEY": "embedding-secret"}, clear=True):
            reranker = QwenRerank(allow_api=True, max_tokens=10)
            self.assertEqual(reranker._base, "https://host/compatible-api/v1")
            self.assertEqual(reranker._key, "embedding-secret")
            with patch.dict(os.environ, {"RAG_RERANK_BASE_URL": "https://override/v1",
                                         "RAG_RERANK_API_KEY": "override"}):
                other = QwenRerank(allow_api=True, max_tokens=10)
                self.assertEqual(other._base, "https://override/v1")
                self.assertEqual(other._key, "override")

    def test_request_shape_auth_truncation_and_actual_usage(self):
        response = io.BytesIO(json.dumps({"results": [{"index": 0, "relevance_score": 0.9}],
                                         "usage": {"total_tokens": 12}}).encode())
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            reranker = self.make_reranker()
            result = reranker.rerank("hello", [document("x" * 7000)], top_n=3)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://private.invalid/v1/reranks")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
        self.assertEqual(json.loads(request.data), {"model": "qwen3-rerank", "query": "hello",
                                                   "documents": ["x" * 6000], "top_n": 1})
        self.assertEqual(result[0]["score"], 0.9)
        self.assertEqual((reranker.api_calls, reranker.total_tokens), (1, 12))
        self.assertFalse(reranker.usage_estimated)

    def test_budget_before_request_and_cumulative_estimated_usage(self):
        transport = Mock(return_value={"results": []})
        reranker = self.make_reranker(max_tokens=4, transport=transport)
        reranker.rerank("hi", [document("abc")])  # ceil(5 / 2) = 3
        self.assertEqual(reranker.total_tokens, 3)
        self.assertTrue(reranker.usage_estimated)
        with self.assertRaisesRegex(ValueError, "budget"):
            reranker.rerank("hi", [document("abc")])
        self.assertEqual(transport.call_count, 1)
        with self.assertRaises(ValueError):
            QwenRerank(max_tokens=100)

    def test_actual_usage_reduces_remaining_budget(self):
        transport = Mock(return_value={"results": [], "usage": {"total_tokens": 10}})
        reranker = self.make_reranker(max_tokens=10, transport=transport)
        reranker.rerank("hi", [document("abc")])
        with self.assertRaisesRegex(ValueError, "budget"):
            reranker.rerank("hi", [document("abc")])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(reranker.total_tokens, 10)

    def test_transport_errors_hide_secrets(self):
        reranker = self.make_reranker(transport=Mock(side_effect=RuntimeError("private.invalid secret")))
        with self.assertRaisesRegex(RuntimeError, "^rerank request failed$"):
            reranker.rerank("hello", [document()])
        self.assertEqual(reranker.api_calls, 1)


class ThresholdTests(unittest.TestCase):
    def row(self, score, no_answer=False, split="tune"):
        return {"top_score": score, "split": split, "category": "no_answer" if no_answer else "concept"}

    def test_threshold_selection_balances_classes(self):
        rows = [self.row(0.9), self.row(0.8), self.row(0.1, True), self.row(None, True)]
        self.assertEqual(select_threshold(rows), {"threshold": 0.8, "accuracy": 1, "balanced_accuracy": 1})
        applied = threshold_metrics([self.row(0.7), self.row(0.1, True)], 0.8)
        self.assertEqual(applied["accuracy"], 0.5)
        with self.assertRaises(ValueError):
            select_threshold([self.row(1, split="test")])

    def test_missing_and_tied_scores(self):
        rows = [self.row(None), self.row(None, True)]
        self.assertEqual(select_threshold(rows)["balanced_accuracy"], 0.5)
        tied = [self.row(0.1), self.row(0.1, True)]
        self.assertEqual(select_threshold(tied)["threshold"], 0.1)

    def test_evaluate_only_requested_split_and_apply_fixed_threshold(self):
        questions = [{"id": split, "question": split, "split": split, "category": "concept",
                      "expected_evidence": ["a.py:1-2"]} for split in ("tune", "test")]
        search = Mock(return_value=[document()])
        result = evaluate_ranking(questions, search, split="test", threshold=0.1)
        search.assert_called_once_with("test")
        self.assertEqual(result["metrics"]["mrr"], 1)
        self.assertEqual(result["no_answer"]["threshold"], 0.1)
        self.assertEqual(result["no_answer"]["accuracy"], 0)


class ExperimentCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.chunk = Chunk("hello", "a.py", 1, 2, "text_block")
        self.questions = [{"id": split + category, "split": split, "category": category,
                           "question": split + category,
                           "expected_evidence": [] if category == "no_answer" else ["a.py:1-2"]}
                          for split in ("tune", "test") for category in ("concept", "no_answer")]
        self.path = self.root / "questions.json"
        self.path.write_text(json.dumps({"questions": self.questions}))
        self.collection = Mock()
        self.collection.count.return_value = 1
        self.collection.query.return_value = {"ids": [[self.chunk.doc_id]],
                                               "documents": [[self.chunk.content]],
                                               "metadatas": [[chunk_result(self.chunk)["metadata"]]],
                                               "distances": [[0.2]]}

    def invoke(self, *args):
        with (patch.dict(os.environ, {}, clear=True),
              patch.object(cli, "collect_corpus", return_value=[self.chunk]),
              patch.object(cli, "validate_question_set"),
              patch.object(cli, "open_frozen_index", return_value=self.collection),
              patch.object(cli, "OpenAIEmbedding", side_effect=AssertionError("no client")),
              patch.object(cli, "write_report", return_value="report.json") as writer,
              redirect_stdout(io.StringIO())):
            cli.main(["--mock-embedding", "--questions", str(self.path), "--query-cache",
                      str(self.root / "cache.json"), *args])
        writer.assert_called_once()
        return writer.call_args.args[0]

    def test_tune_grid_caches_all_queries_and_never_evaluates_test(self):
        report = self.invoke("--split", "tune")
        self.assertEqual(len(report["configs"]), 22)  # 18 grid + 4 named configs
        self.assertTrue((self.root / "cache.json" / "rag.sqlite3").is_file())
        self.assertEqual(self.collection.query.call_count, 2)
        self.assertTrue(all(row["split"] == "tune" for config in report["configs"].values()
                            for row in config["questions"]))
        self.assertEqual(report["api_calls"], 0)
        report2 = self.invoke("--split", "tune")
        self.assertEqual(report2["embedding_usage"]["cache_hits"], 4)

    def test_final_requires_fixed_inputs_writes_once_and_only_evaluates_test(self):
        tune = self.invoke()
        self.collection.query.reset_mock()
        report = self.invoke("--final", "--hybrid-params", json.dumps(tune["best_hybrid_params"]),
                             "--thresholds", json.dumps(tune["final_thresholds"]))
        self.assertEqual(len(report["configs"]), 4)
        self.assertEqual(self.collection.query.call_count, 2)
        self.assertTrue(all(row["split"] == "test" for config in report["configs"].values()
                            for row in config["questions"]))
        self.assertNotIn("private", json.dumps(report))
        with (redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()),
              self.assertRaises(SystemExit)):
            self.invoke("--final")

    def test_cache_hits_do_not_create_embedding_client(self):
        config = RAGConfig(embedding_dimension=2)
        path = self.root / "vectors.json"
        with patch.object(cli, "OpenAIEmbedding") as provider:
            provider.return_value.embed.return_value = [[1, 2]] * 4
            provider.return_value.api_calls = 1
            provider.return_value.total_tokens = 20
            first, usage = cli.query_vectors(self.questions, path, config)
        self.assertEqual(usage["api_calls"], 1)
        with patch.object(cli, "OpenAIEmbedding", side_effect=AssertionError("no client")):
            second, usage = cli.query_vectors(self.questions, path, config)
        self.assertEqual(first, second)
        self.assertEqual(usage["api_calls"], 0)
        # A different dimension has its own namespace and cannot reuse old vectors.
        with patch.object(cli, "OpenAIEmbedding") as provider:
            provider.return_value.embed.return_value = [[1, 2, 3]] * 4
            _, usage = cli.query_vectors(self.questions, path, RAGConfig(embedding_dimension=3))
            self.assertEqual(usage["cache_hits"], 0)

    def test_mismatch_aborts_before_query_embedding_or_report(self):
        with (patch.object(cli, "collect_corpus", return_value=[self.chunk]),
              patch.object(cli, "validate_question_set"),
              patch.object(cli, "open_frozen_index", side_effect=ValueError("index mismatch")),
              patch.object(cli, "query_vectors") as vectors,
              patch.object(cli, "write_report") as writer,
              patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit)):
            cli.main(["--questions", str(self.path)])
        vectors.assert_not_called()
        writer.assert_not_called()

    def test_rerank_requires_opt_in_and_budget_before_any_work(self):
        for flags in (["--configs", "hybrid+qwen"], ["--allow-rerank-api"]):
            with (patch.object(cli, "collect_corpus") as corpus,
                  patch.object(cli, "QwenRerank") as reranker,
                  patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit)):
                cli.main(flags)
            corpus.assert_not_called()
            reranker.assert_not_called()

    def test_final_qwen_runs_once_per_test_query_with_fake_transport(self):
        tune = self.invoke()
        reranker = QwenRerank(allow_api=True, max_tokens=10000,
                             base_url="https://private.invalid/v1", api_key="secret",
                             transport=Mock(return_value={
                                 "results": [{"index": 0, "relevance_score": 0.8}],
                                 "usage": {"total_tokens": 5}}))
        with patch.object(cli, "QwenRerank", return_value=reranker):
            report = self.invoke("--final", "--configs", "hybrid+qwen", "--allow-rerank-api",
                                 "--max-rerank-tokens", "10000", "--hybrid-params",
                                 json.dumps(tune["best_hybrid_params"]), "--thresholds",
                                 json.dumps({"hybrid+qwen": 0.5}))
        self.assertEqual(reranker.api_calls, 2)
        self.assertEqual(report["api_calls"], 2)
        self.assertEqual(report["total_tokens"], 10)
        for forbidden in ("secret", "private.invalid", "base_url", "api_key"):
            self.assertNotIn(forbidden, json.dumps(report))

    def test_snapshot_count_ids_version_content_checks(self):
        (self.root / "chroma.sqlite3").touch()
        self.collection.metadata = {}
        data = {"ids": [self.chunk.doc_id], "documents": [self.chunk.content],
                "metadatas": [chunk_result(self.chunk)["metadata"]]}
        self.collection.get.return_value = data
        client = Mock()
        client.get_collection.return_value = self.collection
        with patch.dict("sys.modules", {"chromadb": Mock(PersistentClient=Mock(return_value=client))}):
            self.assertIs(cli.open_frozen_index(self.root, "docs", [self.chunk], "v"), self.collection)
            for bad in ({"ids": ["wrong"]}, {"documents": ["drift"]}):
                self.collection.get.return_value = {**data, **bad}
                with self.assertRaises(ValueError):
                    cli.open_frozen_index(self.root, "docs", [self.chunk], "v")
            self.collection.get.return_value = data
            self.collection.metadata = {"corpus_version": "old"}
            with self.assertRaises(ValueError):
                cli.open_frozen_index(self.root, "docs", [self.chunk], "v")
            self.collection.count.return_value = 2
            with self.assertRaises(ValueError):
                cli.open_frozen_index(self.root, "docs", [self.chunk], "v")
        client.get_or_create_collection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
