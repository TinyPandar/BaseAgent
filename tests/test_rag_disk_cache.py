"""Offline cache regressions: fake providers/transports only, no HTTP allowed."""

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from applications.project_assistant.evaluation import retrieval
from applications.project_assistant.rag.cache import (
    CachedEmbedding,
    DiskCache,
    cache_directory,
    embedding_key,
)
from applications.project_assistant.rag.rerank import QwenRerank
from scripts import seed_embedding_cache_from_chroma as seed


class DiskCacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.cache = DiskCache(self.root)
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network")))
        self.client = Mock(api_calls=0, total_tokens=0)

        def embed(texts):
            self.client.api_calls += 1
            self.client.total_tokens += len(texts)
            return [[float(len(text)), 1.0] for text in texts]

        self.client.embed.side_effect = embed
        self.factory = Mock(return_value=self.client)

    def provider(self, **kwargs):
        return CachedEmbedding(self.factory, provider="openai", model="fake", dimensions=2,
                               batch_size=2, cache=self.cache, **kwargs)

    def test_hits_reuse_chunk_for_query_across_instances_without_client(self):
        first = self.provider()
        vector = first.embed(["chunk"])[0]
        self.factory.reset_mock()
        second = self.provider(max_tokens=0)
        self.assertEqual(second.embed_query("chunk"), vector)
        self.factory.assert_not_called()
        self.assertEqual(second.usage(), {"cache_hits": 1, "cache_misses": 0,
                                         "cache_hit_rate": 1.0, "api_calls": 0,
                                         "estimated_tokens": 0, "total_tokens": 0})

    def test_partial_hits_batch_only_unique_misses_and_restore_order(self):
        self.provider().embed(["hit"])
        self.client.embed.reset_mock()
        provider = self.provider(max_tokens=3)
        self.assertEqual(provider.estimate_missing(["hit", "a", "bb", "ccc", "a"]), 4)
        with self.assertRaises(ValueError):
            provider.embed(["hit", "a", "bb", "ccc", "a"])
        self.client.embed.assert_not_called()
        provider.max_tokens = 4
        vectors = provider.embed(["hit", "a", "bb", "ccc", "a"])
        self.assertEqual(vectors, [[3., 1.], [1., 1.], [2., 1.], [3., 1.], [1., 1.]])
        self.assertEqual([c.args[0] for c in self.client.embed.call_args_list], [["a", "bb"], ["ccc"]])
        self.assertEqual(provider.cache_hits, 1)
        self.assertEqual(provider.cache_misses, 4)
        self.assertEqual(provider.estimated_tokens, 4)

    def test_failed_later_batch_keeps_committed_first_batch(self):
        self.client.embed.side_effect = [[[1., 2.], [3., 4.]], RuntimeError("interrupted")]
        with self.assertRaises(RuntimeError):
            self.provider().embed(["a", "b", "c"])
        recovered = self.provider(max_tokens=0)
        self.assertEqual(recovered.embed(["a", "b"]), [[1., 2.], [3., 4.]])
        self.assertIsNone(self.cache.get("embedding", embedding_key("openai", "fake", 2, "c")))

    def test_key_isolation(self):
        self.cache.put("embedding", embedding_key("openai", "fake", 2, "a"), [1., 2.])
        for key in (embedding_key("other", "fake", 2, "a"),
                    embedding_key("openai", "other", 2, "a"),
                    embedding_key("openai", "fake", 3, "a"),
                    embedding_key("openai", "fake", 2, "b")):
            self.assertIsNone(self.cache.get("embedding", key))

    def test_inferred_dimensions_store_concrete_keys_and_resume(self):
        provider = CachedEmbedding(self.factory, provider="openai", model="auto",
                                   dimensions=None, cache=self.cache)
        provider.embed_query("auto text")
        self.assertEqual(self.cache.get("embedding", embedding_key("openai", "auto", 2, "auto text")),
                         [9., 1.])
        self.factory.reset_mock()
        resumed = CachedEmbedding(self.factory, provider="openai", model="auto",
                                  dimensions=None, cache=self.cache, max_tokens=0)
        self.assertEqual(resumed.embed_query("auto text"), [9., 1.])
        self.factory.assert_not_called()

    def test_no_cache_sends_again(self):
        provider = CachedEmbedding(self.factory, provider="openai", model="fake", dimensions=2)
        provider.embed_query("same")
        provider.embed_query("same")
        self.assertEqual(self.client.api_calls, 2)
        self.assertEqual(list(self.root.iterdir()), [self.cache.path])

    def test_rerank_hits_skip_transport_and_zero_budget_with_different_top_n(self):
        transport = Mock(return_value={"results": [
            {"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .2}],
            "usage": {"total_tokens": 8}})
        documents = [{"doc_id": "a", "content": "alpha"}, {"doc_id": "b", "content": "beta"}]
        first = QwenRerank(max_tokens=100, allow_api=True, base_url="https://fake.invalid",
                           api_key="fake", transport=transport, cache=self.cache)
        self.assertEqual(first.rerank("q", documents, top_n=1)[0]["doc_id"], "b")
        self.assertEqual(transport.call_args.args[0]["top_n"], 2)
        transport.reset_mock()
        second = QwenRerank(max_tokens=0, allow_api=True, base_url="https://fake.invalid",
                            api_key="fake", transport=transport, cache=DiskCache(self.root))
        self.assertEqual([r["doc_id"] for r in second.rerank("q", documents, top_n=2)], ["b", "a"])
        transport.assert_not_called()
        self.assertEqual(second.usage()["cache_hits"], 1)
        self.assertEqual(second.estimated_tokens, 0)
        with self.assertRaises(ValueError):
            second.rerank("changed query", documents)
        with self.assertRaises(ValueError):
            second.rerank("q", list(reversed(documents)))

    def test_seed_paged_vectors_reusable_by_query_and_idempotent(self):
        collection = Mock()
        collection.count.return_value = 3
        rows = [("a", [1., 2.]), ("bb", [2., 3.]), ("ccc", [3., 4.])]

        def get(include, offset, limit):
            self.assertEqual(include, ["documents", "embeddings"])
            selected = rows[offset:offset + limit]
            return {"documents": [r[0] for r in selected], "embeddings": [r[1] for r in selected]}

        collection.get.side_effect = get
        for _ in range(2):
            self.assertEqual(seed.seed_collection(collection, self.cache, provider="openai",
                                                 model="fake", dimensions=2, batch_size=2), 3)
        self.assertEqual(self.provider(max_tokens=0).embed_query("bb"), [2., 3.])
        self.factory.assert_not_called()
        with self.assertRaises(ValueError):
            seed.seed_collection(collection, self.cache, provider="openai", model="fake", dimensions=3)

    def test_seed_cli_uses_no_embedding_function(self):
        (self.root / "chroma.sqlite3").touch()
        collection = Mock()
        collection.count.return_value = 1
        collection.get.return_value = {"documents": ["seeded"], "embeddings": [[1., 2.]]}
        with patch("chromadb.PersistentClient") as client:
            client.return_value.get_collection.return_value = collection
            self.assertEqual(seed.main(["--index-dir", str(self.root), "--collection-name", "docs",
                                        "--provider", "openai", "--model", "fake", "--dimensions", "2",
                                        "--cache-dir", str(self.root)]), 0)
            client.return_value.get_collection.assert_called_once_with("docs", embedding_function=None)
        self.assertEqual(self.provider(max_tokens=0).embed_query("seeded"), [1., 2.])

    def test_report_json_and_markdown_have_usage(self):
        self.provider().embed_query("a")
        with patch.object(retrieval, "REPORTS_DIR", self.root):
            report = retrieval.write_report({"embedding_usage": self.provider().usage()})
        self.assertIn("cache_hits", json.loads(report.read_text())["embedding_usage"])
        self.assertIn("Sent token estimate", report.with_suffix(".md").read_text())

    def test_default_and_environment_directory(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cache_directory(), Path(__file__).resolve().parents[1] / ".baseagent/cache")
        with patch.dict(os.environ, {"RAG_CACHE_DIR": str(self.root)}):
            self.assertEqual(DiskCache().path, self.root / "rag.sqlite3")
