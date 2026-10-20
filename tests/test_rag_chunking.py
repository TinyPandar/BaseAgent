"""Tests for RAG chunking functionality."""

import textwrap
import unittest

from applications.project_assistant.rag.chunking import (
    chunk_document,
    chunk_python_code,
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

        # Include the module docstring, helper, and whole class.
        self.assertEqual(len(chunks), 3)

        # Find function chunk
        func_chunks = [c for c in chunks if c.chunk_type == "python_function"]
        self.assertEqual(len(func_chunks), 1)
        self.assertEqual(func_chunks[0].function_name, "helper")

        # The method stays inside the single class chunk.
        class_chunks = [c for c in chunks if c.chunk_type == "python_class"]
        self.assertEqual(len(class_chunks), 1)
        self.assertEqual(class_chunks[0].class_name, "Example")
        self.assertEqual((class_chunks[0].line_start, class_chunks[0].line_end), (7, 12))
        self.assertIn("def method(self):", class_chunks[0].content)

    def test_oversized_python_definitions(self):
        for prefix, signature, class_name in (
            ("", "def large():", None),
            ("class Example:\n", "    async def large(self):", "Example"),
        ):
            with self.subTest(class_name=class_name):
                indent = "    " if class_name is None else "        "
                content = prefix + signature + "\n" + "\n".join(
                    f"{indent}value = {i}" for i in range(250))
                chunks = chunk_python_code(content, "large.py")
                functions = [c for c in chunks if c.chunk_type == "python_function"]
                self.assertGreater(len(functions), 1)
                if class_name:
                    preamble = [c for c in chunks if c.chunk_type == "python_class"]
                    self.assertEqual(len(preamble), 1)
                    self.assertEqual(preamble[0].content, "class Example:")
                    self.assertEqual(preamble[0].class_name, class_name)
                for chunk in functions:
                    self.assertEqual(chunk.function_name, "large")
                    self.assertEqual(chunk.class_name, class_name)
                    self.assertLessEqual(chunk.line_end - chunk.line_start + 1, 200)
                    self.assertEqual(chunk.content, "\n".join(
                        content.splitlines()[chunk.line_start - 1:chunk.line_end]))
                for previous, current in zip(functions, functions[1:]):
                    self.assertEqual(current.line_start, previous.line_end - 4)

    def test_medium_python_function_stays_whole(self):
        content = "def medium():\n" + "\n".join(
            f"    value = {i}" for i in range(100))
        chunks = chunk_python_code(content, "medium.py")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].content, content)
        self.assertEqual((chunks[0].line_start, chunks[0].line_end), (1, 101))
        self.assertEqual(chunks[0].function_name, "medium")

    def test_class_size_boundary_includes_decorators(self):
        content = ("@decorate\nclass Example:\n    label = 'example'\n"
                   "    @decorate\n    def method(self):\n        pass")
        whole = chunk_python_code(content, "boundary.py", max_lines=6)
        self.assertEqual(len(whole), 1)
        self.assertEqual(whole[0].chunk_type, "python_class")
        self.assertEqual(whole[0].content, content)
        self.assertEqual((whole[0].line_start, whole[0].line_end), (1, 6))

        split = chunk_python_code(content, "boundary.py", max_lines=5, overlap_lines=1)
        self.assertEqual([c.chunk_type for c in split], ["python_class", "python_function"])
        self.assertEqual([(c.line_start, c.line_end) for c in split], [(1, 3), (4, 6)])
        self.assertEqual(split[1].function_name, "method")
        self.assertEqual(split[1].class_name, "Example")

    def test_oversized_class_preamble_splits(self):
        content = ("class Example:\n" + "\n".join(
            f"    value_{i} = {i}" for i in range(210))
            + "\n    def method(self):\n        pass")
        chunks = chunk_python_code(content, "preamble.py")
        preamble = [c for c in chunks if c.chunk_type == "python_class"]
        self.assertEqual([(c.line_start, c.line_end) for c in preamble], [(1, 200), (196, 211)])
        self.assertTrue(all(c.class_name == "Example" for c in chunks))
        self.assertEqual(chunks[-1].function_name, "method")
        self.assertEqual((chunks[-1].line_start, chunks[-1].line_end), (212, 213))

    def test_oversized_class_with_small_methods(self):
        content = "class Example:\n" + "\n".join(
            f"    def method_{i}(self):\n        return {i}" for i in range(100))
        chunks = chunk_python_code(content, "methods.py")
        self.assertEqual(chunks[0].content, "class Example:")
        self.assertEqual(chunks[0].chunk_type, "python_class")
        self.assertEqual(len(chunks), 101)
        self.assertEqual([c.function_name for c in chunks[1:]],
                         [f"method_{i}" for i in range(100)])
        self.assertTrue(all(c.class_name == "Example" for c in chunks))

    def test_nested_definitions_are_not_duplicated(self):
        content = ("class Example:\n"
                   "    def method(self):\n"
                   "        def inner():\n"
                   "            pass\n"
                   "        return inner()\n")
        chunks = chunk_python_code(content, "nested.py")
        methods = [c for c in chunks if c.chunk_type == "python_function"]
        self.assertEqual(methods, [])
        self.assertEqual(len(chunks), 1)
        self.assertIn("def inner():", chunks[0].content)

        # Splitting a larger class still emits only direct methods.
        split = chunk_python_code(content, "nested.py", max_lines=4, overlap_lines=1)
        self.assertEqual([c.function_name for c in split], [None, "method"])

        function = "def outer():\n    def inner():\n        pass\n    return inner()"
        chunks = chunk_python_code(function, "nested_function.py")
        self.assertEqual([c.function_name for c in chunks], ["outer"])

    def test_python_module_statements(self):
        content = '\n'.join(['"""Module."""', 'LIMIT = 5', '', 'def helper():', '    pass'])
        chunks = chunk_python_code(content, "module.py")
        module = next(c for c in chunks if c.chunk_type == "python_module")
        self.assertEqual((module.line_start, module.line_end), (1, 2))
        self.assertIn("LIMIT = 5", module.content)
        self.assertFalse(any(c.chunk_type == "python_module" for c in
                             chunk_python_code(content, "module.py", include_module_level=False)))

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
