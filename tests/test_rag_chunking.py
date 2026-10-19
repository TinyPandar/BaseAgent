"""Tests for RAG chunking functionality."""

import textwrap
import unittest

from applications.project_assistant.rag.chunking import (
    chunk_document,
    chunk_python_code,
    Chunk,
)


class TestChunking(unittest.TestCase):
    """Test document and code chunking."""

    def test_chunk_markdown_by_headers(self):
        """Markdown documents should be chunked by section headers."""
        content = textwrap.dedent("""
            # Introduction
            This is the introduction.

            ## Setup
            Installation instructions here.

            ## Usage
            Usage examples here.

            ### Advanced Usage
            Advanced examples.
        """).strip()

        chunks = chunk_document(content, "README.md", max_lines=20)

        # Should have 4 chunks (one per header section)
        self.assertGreaterEqual(len(chunks), 3)

        # Check first chunk
        self.assertIn("Introduction", chunks[0].content)
        self.assertEqual(chunks[0].file_path, "README.md")
        self.assertEqual(chunks[0].chunk_type, "markdown_section")
        self.assertEqual(chunks[0].line_start, 1)

        # Check citations
        for chunk in chunks:
            citation = chunk.citation
            self.assertIn("README.md:", citation)
            self.assertIn("-", citation)

    def test_chunk_markdown_fixed_size_fallback(self):
        """Documents without headers should use fixed-size chunking."""
        content = "\n".join([f"Line {i}" for i in range(100)])

        chunks = chunk_document(content, "plain.txt", max_lines=20, overlap_lines=5)

        # Should have multiple chunks
        self.assertGreater(len(chunks), 1)

        # Check overlap
        self.assertIn("Line 15", chunks[0].content)
        self.assertIn("Line 15", chunks[1].content)  # Overlapping line

    def test_chunk_python_functions(self):
        """Python code should be chunked by function and class definitions."""
        content = textwrap.dedent('''
            """Module docstring."""

            def helper():
                """Helper function."""
                return 42

            class Example:
                """Example class."""

                def method(self):
                    """Method docstring."""
                    pass
        ''').strip()

        chunks = chunk_python_code(content, "src/example.py")

        # Should have module docstring, helper function, and Example class
        self.assertGreaterEqual(len(chunks), 2)

        # Find function chunk
        func_chunks = [c for c in chunks if c.chunk_type == "python_function"]
        self.assertGreater(len(func_chunks), 0)
        self.assertEqual(func_chunks[0].function_name, "helper")

        # Find class chunk
        class_chunks = [c for c in chunks if c.chunk_type == "python_class"]
        self.assertGreater(len(class_chunks), 0)
        self.assertEqual(class_chunks[0].class_name, "Example")

    def test_chunk_metadata_preserved(self):
        """Chunks should preserve file path and line range metadata."""
        content = "# Test\nContent here."
        chunks = chunk_document(content, "test.md")

        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]

        self.assertEqual(chunk.file_path, "test.md")
        self.assertGreater(chunk.line_start, 0)
        self.assertGreaterEqual(chunk.line_end, chunk.line_start)
        self.assertIsNotNone(chunk.content_hash)
        self.assertIsNotNone(chunk.doc_id)

    def test_chunk_doc_id_unique(self):
        """Each chunk should have a unique doc_id."""
        content = "# Section 1\nText.\n\n# Section 2\nMore text."
        chunks = chunk_document(content, "doc.md")

        doc_ids = [c.doc_id for c in chunks]
        self.assertEqual(len(doc_ids), len(set(doc_ids)))  # All unique


if __name__ == "__main__":
    unittest.main()
