"""Tests for RAG embedding and indexing."""

import tempfile
import unittest
from pathlib import Path

from applications.project_assistant.rag.chunking import Chunk
from applications.project_assistant.rag.embedding import MockEmbedding, EmbeddingProvider
from applications.project_assistant.rag.indexing import VectorIndex
from applications.project_assistant.rag.retrieval import retrieve


class TestEmbedding(unittest.TestCase):
    """Test embedding providers."""

    def test_mock_embedding_deterministic(self):
        """Mock embeddings should be deterministic for the same text."""
        provider = MockEmbedding(dimension=128)

        text = "This is a test document."
        emb1 = provider.embed_query(text)
        emb2 = provider.embed_query(text)

        self.assertEqual(emb1, emb2)
        self.assertEqual(len(emb1), 128)

    def test_mock_embedding_batch(self):
        """Mock embedding should support batch processing."""
        provider = MockEmbedding(dimension=64)

        texts = ["First document", "Second document", "Third document"]
        embeddings = provider.embed(texts)

        self.assertEqual(len(embeddings), 3)
        self.assertEqual(len(embeddings[0]), 64)

        # Different texts should have different embeddings
        self.assertNotEqual(embeddings[0], embeddings[1])


class TestVectorIndex(unittest.TestCase):
    """Test vector indexing with ChromaDB."""

    def setUp(self):
        """Set up a temporary index for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.embedding = MockEmbedding(dimension=128)

    def test_index_add_and_count(self):
        """Index should correctly add chunks and count them."""
        index = VectorIndex(
            embedding_provider=self.embedding,
            persist_directory=None,  # In-memory for tests
            collection_name="test_collection",
        )

        chunks = [
            Chunk(
                content="Python is a programming language.",
                file_path="intro.md",
                line_start=1,
                line_end=1,
                chunk_type="markdown_section",
            ),
            Chunk(
                content="Rust is a systems programming language.",
                file_path="intro.md",
                line_start=3,
                line_end=3,
                chunk_type="markdown_section",
            ),
        ]

        index.add_chunks(chunks)
        self.assertEqual(index.count(), 2)

    def test_index_search_returns_results(self):
        """Index search should return relevant chunks."""
        index = VectorIndex(
            embedding_provider=self.embedding,
            persist_directory=None,
            collection_name="test_search",
        )

        chunks = [
            Chunk(
                content="The kernel module handles model and tool execution.",
                file_path="architecture.md",
                line_start=10,
                line_end=10,
                chunk_type="markdown_section",
                parent_section="Architecture",
            ),
            Chunk(
                content="The harness provides session persistence and recovery.",
                file_path="architecture.md",
                line_start=15,
                line_end=15,
                chunk_type="markdown_section",
                parent_section="Architecture",
            ),
        ]

        index.add_chunks(chunks)

        results = index.search("kernel execution", top_k=2)

        # Should return results (mock embedding won't guarantee exact relevance)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["metadata"]["file_path"], "architecture.md")

        # At least one result should mention either kernel or harness
        contents = [r["content"].lower() for r in results]
        has_relevant = any("kernel" in c or "harness" in c for c in contents)
        self.assertTrue(has_relevant)

    def test_retrieve_with_evidence(self):
        """Retrieve function should return Evidence objects."""
        index = VectorIndex(
            embedding_provider=self.embedding,
            persist_directory=None,
            collection_name="test_retrieve",
        )

        chunks = [
            Chunk(
                content="def run_agent(model, prompt): pass",
                file_path="agent.py",
                line_start=5,
                line_end=5,
                chunk_type="python_function",
                function_name="run_agent",
            ),
        ]

        index.add_chunks(chunks)

        evidence_list = retrieve("run_agent function", index, top_k=1)

        self.assertEqual(len(evidence_list), 1)
        evidence = evidence_list[0]

        self.assertEqual(evidence.file_path, "agent.py")
        self.assertEqual(evidence.line_range, (5, 5))
        self.assertEqual(evidence.chunk_type, "python_function")
        self.assertEqual(evidence.citation, "agent.py:5-5")
        self.assertIsNotNone(evidence.score)

    def test_index_clear(self):
        """Index should support clearing all documents."""
        index = VectorIndex(
            embedding_provider=self.embedding,
            persist_directory=None,
            collection_name="test_clear",
        )

        chunks = [
            Chunk(
                content="Test content",
                file_path="test.md",
                line_start=1,
                line_end=1,
                chunk_type="text_block",
            ),
        ]

        index.add_chunks(chunks)
        self.assertEqual(index.count(), 1)

        index.clear()
        self.assertEqual(index.count(), 0)


if __name__ == "__main__":
    unittest.main()
