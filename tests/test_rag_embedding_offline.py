"""OpenAI adapter tests with a fake SDK client, never a network connection."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from applications.project_assistant.config import RAGConfig
from applications.project_assistant.rag.embedding import OpenAIEmbedding
from applications.project_assistant.rag.indexing import VectorIndex


class OpenAIEmbeddingOfflineTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = Mock()
        self.factory = Mock(return_value=self.client)
        self.sdk = patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=self.factory)})
        self.sdk.start()
        self.addCleanup(self.sdk.stop)

    def response(self, vectors):
        self.client.embeddings.create.return_value = SimpleNamespace(
            data=[SimpleNamespace(index=i, embedding=vector) for i, vector in enumerate(vectors)],
            usage=SimpleNamespace(total_tokens=12),
        )

    def test_unknown_model_infers_dimension_without_probe(self):
        provider = OpenAIEmbedding(api_key="fake", model="compatible-model")
        self.assertIsNone(provider.dimension)
        self.client.embeddings.create.assert_not_called()
        self.assertEqual(provider.embed([]), [])
        self.response([[1.0, 2.0, 3.0]])
        self.assertEqual(provider.embed_query("hello"), [1.0, 2.0, 3.0])
        self.assertEqual(provider.dimension, 3)
        self.assertEqual(provider.api_calls, 1)
        self.assertEqual(provider.total_tokens, 12)
        self.client.embeddings.create.assert_called_once_with(input=["hello"], model="compatible-model")

    def test_known_defaults_are_replaced_by_first_response(self):
        for model, default in (("text-embedding-3-small", 1536), ("text-embedding-3-large", 3072)):
            with self.subTest(model=model):
                provider = OpenAIEmbedding(api_key="fake", model=model)
                self.assertEqual(provider.dimension, default)
                self.response([[0.0, 1.0]])
                provider.embed(["hello"])
                self.assertEqual(provider.dimension, 2)

    def test_constructor_configuration_overrides_environment(self):
        with patch.dict(os.environ, {"RAG_EMBEDDING_DIMENSION": "8",
                                     "RAG_EMBEDDING_BASE_URL": "https://env.invalid/v1"}):
            provider = OpenAIEmbedding(api_key="fake", dimension=2, base_url="https://arg.invalid/v1")
        self.factory.assert_called_once_with(api_key="fake", base_url="https://arg.invalid/v1")
        self.response([[0.0, 1.0]])
        provider.embed_query("hello")
        self.client.embeddings.create.assert_called_once_with(
            input=["hello"], model="text-embedding-3-small", dimensions=2)

    def test_environment_flows_through_config(self):
        with patch.dict(os.environ, {"RAG_EMBEDDING_DIMENSION": "2",
                                     "RAG_EMBEDDING_BASE_URL": "https://env.invalid/v1"}):
            config = RAGConfig.from_env()
            provider = OpenAIEmbedding(api_key="fake")
        self.assertEqual(config.embedding_dimension, 2)
        self.assertEqual(config.embedding_base_url, provider.base_url)
        self.assertEqual(provider.dimension, 2)
        self.factory.assert_called_once_with(api_key="fake", base_url=config.embedding_base_url)

    def test_invalid_dimensions_and_response_shapes(self):
        for dimension in (0, -1, True, 1.5):
            with self.subTest(dimension=dimension), self.assertRaises(ValueError):
                OpenAIEmbedding(api_key="fake", dimension=dimension)
        for vectors in ([], [[]], [[1.0], [1.0, 2.0]], [[1.0, 2.0]]):
            with self.subTest(vectors=vectors):
                provider = OpenAIEmbedding(api_key="fake", dimension=1)
                self.response(vectors)
                with self.assertRaises(ValueError):
                    provider.embed(["a", "b"] if len(vectors) == 2 else ["a"])
        provider = OpenAIEmbedding(api_key="fake", model="custom")
        self.response([[1.0]])
        provider.embed_query("a")
        self.response([[1.0, 2.0]])
        with self.assertRaises(ValueError):
            provider.embed_query("b")

    def test_out_of_order_response_restores_input_order(self):
        provider = OpenAIEmbedding(api_key="fake", model="custom")
        self.response([[1.0], [2.0]])
        self.client.embeddings.create.return_value.data.reverse()
        self.assertEqual(provider.embed(["a", "b"]), [[1.0], [2.0]])

    def test_key_precedence_and_fallback(self):
        for rag_key, expected in (("embedding-secret", "embedding-secret"), ("", "openai-secret")):
            with self.subTest(rag_key=rag_key), patch.dict(os.environ, {
                "RAG_EMBEDDING_API_KEY": rag_key, "OPENAI_API_KEY": "openai-secret",
            }):
                self.assertEqual(RAGConfig.from_env().openai_api_key, expected)
                self.assertEqual(RAGConfig().openai_api_key, expected)
                self.assertEqual(OpenAIEmbedding().api_key, expected)
                self.factory.assert_called_with(api_key=expected)
                self.assertEqual(OpenAIEmbedding(api_key="explicit").api_key, "explicit")
        with self.assertRaises(ValueError):
            OpenAIEmbedding()

    def test_plural_dimensions_precede_alias(self):
        for values, expected in (({"RAG_EMBEDDING_DIMENSIONS": "2"}, 2),
                                 ({"RAG_EMBEDDING_DIMENSION": "3"}, 3),
                                 ({"RAG_EMBEDDING_DIMENSIONS": "2",
                                   "RAG_EMBEDDING_DIMENSION": "3"}, 2)):
            with self.subTest(values=values), patch.dict(os.environ, values):
                self.assertEqual(RAGConfig.from_env().embedding_dimension, expected)
                provider = OpenAIEmbedding(api_key="fake")
                self.response([[0.0] * expected])
                provider.embed_query("hello")
                self.assertEqual(self.client.embeddings.create.call_args.kwargs["dimensions"], expected)

    def test_bailian_batches_preserve_order_and_count_usage(self):
        def create(input, model, dimensions):
            self.assertLessEqual(len(input), 10)
            self.assertEqual(model, "text-embedding-v4")
            self.assertEqual(dimensions, 1)
            return SimpleNamespace(
                data=[SimpleNamespace(index=i, embedding=[float(text)])
                      for i, text in reversed(list(enumerate(input)))],
                usage=SimpleNamespace(total_tokens=len(input) * 2),
            )
        self.client.embeddings.create.side_effect = create
        with patch.dict(os.environ, {"RAG_EMBEDDING_BATCH_SIZE": "10"}):
            config = RAGConfig.from_env()
            provider = OpenAIEmbedding(api_key="fake", model="text-embedding-v4", dimension=1)
        self.assertEqual(config.embedding_batch_size, 10)
        self.assertEqual(provider.embed([str(i) for i in range(23)]), [[float(i)] for i in range(23)])
        self.assertEqual([len(c.kwargs["input"]) for c in self.client.embeddings.create.call_args_list],
                         [10, 10, 3])
        self.assertEqual(provider.api_calls, 3)
        self.assertEqual(provider.total_tokens, 46)
        provider.embed([])
        self.assertEqual(provider.api_calls, 3)

    def test_default_batch_size_and_explicit_override(self):
        self.assertEqual(OpenAIEmbedding(api_key="fake").batch_size, 10)
        self.assertEqual(RAGConfig.from_env().embedding_batch_size, 10)
        with patch.dict(os.environ, {"RAG_EMBEDDING_BATCH_SIZE": "10"}):
            self.assertEqual(OpenAIEmbedding(api_key="fake", batch_size=3).batch_size, 3)
        for size in (0, -1, True, 1.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                OpenAIEmbedding(api_key="fake", batch_size=size)
            with self.assertRaises(ValueError):
                RAGConfig(embedding_batch_size=size)
        with patch.dict(os.environ, {"RAG_EMBEDDING_BATCH_SIZE": "0"}):
            with self.assertRaises(ValueError):
                RAGConfig.from_env()
            with self.assertRaises(ValueError):
                OpenAIEmbedding(api_key="fake")

    def test_failed_batch_counts_attempt_without_inventing_usage(self):
        provider = OpenAIEmbedding(api_key="fake", batch_size=1)
        self.response([[1.0]])
        self.client.embeddings.create.side_effect = [self.client.embeddings.create.return_value,
                                                   RuntimeError("fake failure")]
        with self.assertRaises(RuntimeError):
            provider.embed(["one", "two", "three"])
        self.assertEqual(provider.api_calls, 2)
        self.assertEqual(provider.total_tokens, 12)

    def test_index_accepts_unknown_dimension_and_records_inference(self):
        from applications.project_assistant.rag.chunking import Chunk

        provider = OpenAIEmbedding(api_key="fake", model="custom")
        collection = Mock()
        client = Mock()
        client.get_or_create_collection.return_value = collection
        with patch.dict(sys.modules, {"chromadb": SimpleNamespace(Client=lambda: client)}):
            index = VectorIndex(provider)
            client.get_or_create_collection.assert_called_once_with(name="project_docs", metadata=None)
            self.response([[1.0, 2.0]])
            index.add_chunks([Chunk("hello", "a.md", 1, 1, "text_block")])
        collection.modify.assert_called_once_with(metadata={"dimension": 2})


if __name__ == "__main__":
    unittest.main()
