"""Tests for corpus collection."""

import tempfile
import unittest
from pathlib import Path

from applications.project_assistant.corpus import (
    collect_corpus,
    compute_corpus_version,
    save_corpus_metadata,
)


class TestCorpusCollection(unittest.TestCase):
    """Test corpus collection and versioning."""

    def setUp(self):
        """Create a temporary repository structure."""
        self.temp_dir = Path(tempfile.mkdtemp())

        # Create test files
        (self.temp_dir / "README.md").write_text("# Test Project\nDescription here.")
        (self.temp_dir / "src").mkdir()
        (self.temp_dir / "src" / "main.py").write_text("def main(): pass")
        (self.temp_dir / ".git").mkdir()
        (self.temp_dir / ".git" / "config").write_text("git config")

    def test_collect_corpus_includes_markdown_and_python(self):
        """Corpus collection should include .md and .py files."""
        chunks = collect_corpus(self.temp_dir)

        # Should have chunks from README.md and main.py
        file_paths = {c.file_path for c in chunks}

        self.assertIn("README.md", file_paths)
        self.assertIn("src/main.py", file_paths)

    def test_collect_corpus_excludes_git_directory(self):
        """Corpus collection should exclude .git directory."""
        chunks = collect_corpus(self.temp_dir)

        file_paths = {c.file_path for c in chunks}

        # Should not include files from .git
        self.assertFalse(any(".git" in path for path in file_paths))

    def test_corpus_version_deterministic(self):
        """Corpus version should be deterministic for the same content."""
        chunks = collect_corpus(self.temp_dir)

        version1 = compute_corpus_version(chunks)
        version2 = compute_corpus_version(chunks)

        self.assertEqual(version1, version2)
        self.assertEqual(len(version1), 16)  # First 16 chars of SHA256

    def test_save_corpus_metadata(self):
        """Corpus metadata should be saved to JSON."""
        chunks = collect_corpus(self.temp_dir)
        output_path = self.temp_dir / "corpus_metadata.json"

        save_corpus_metadata(chunks, output_path)

        self.assertTrue(output_path.exists())

        import json
        metadata = json.loads(output_path.read_text())

        self.assertIn("version", metadata)
        self.assertIn("total_chunks", metadata)
        self.assertIn("total_files", metadata)
        self.assertIn("files", metadata)

        self.assertEqual(metadata["total_chunks"], len(chunks))


if __name__ == "__main__":
    unittest.main()
