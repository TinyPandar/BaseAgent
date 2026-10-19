"""Corpus collection and version management for BaseAgent knowledge base."""

import hashlib
import json
from pathlib import Path
from typing import List, Set

from .rag.chunking import chunk_document, chunk_python_code, Chunk


def collect_corpus(
    root_dir: Path,
    include_patterns: List[str] = None,
    exclude_patterns: List[str] = None,
) -> List[Chunk]:
    """
    Collect and chunk documents from the BaseAgent repository.

    Args:
        root_dir: Root directory of the repository
        include_patterns: File patterns to include (e.g., ["*.md", "*.py"])
        exclude_patterns: Directory patterns to exclude (e.g., [".git", ".baseagent"])

    Returns:
        List of all chunks from the corpus
    """
    if include_patterns is None:
        include_patterns = ["*.md", "*.py"]

    if exclude_patterns is None:
        exclude_patterns = [
            ".git",
            ".baseagent",
            "__pycache__",
            ".venv",
            "venv",
            ".pytest_cache",
            "tests/cassettes_local",
            "*.egg-info",
        ]

    root_dir = Path(root_dir)
    all_chunks = []

    # Collect markdown files
    for pattern in ["*.md"]:
        for file_path in root_dir.rglob(pattern):
            if _should_exclude(file_path, root_dir, exclude_patterns):
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
                relative_path = file_path.relative_to(root_dir).as_posix()
                chunks = chunk_document(content, relative_path)
                all_chunks.extend(chunks)
            except (UnicodeDecodeError, OSError):
                # Skip files that can't be read
                continue

    # Collect Python source files
    for pattern in ["*.py"]:
        for file_path in root_dir.rglob(pattern):
            if _should_exclude(file_path, root_dir, exclude_patterns):
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
                relative_path = file_path.relative_to(root_dir).as_posix()
                chunks = chunk_python_code(content, relative_path)
                all_chunks.extend(chunks)
            except (UnicodeDecodeError, OSError, SyntaxError):
                # Skip files that can't be read or parsed
                continue

    return all_chunks


def _should_exclude(file_path: Path, root_dir: Path, exclude_patterns: List[str]) -> bool:
    """Check if a file should be excluded based on patterns."""
    relative_path = file_path.relative_to(root_dir)
    parts = relative_path.parts

    for pattern in exclude_patterns:
        # Check if any part of the path matches the exclude pattern
        for part in parts:
            if part.startswith(pattern.rstrip("/*")):
                return True

    return False


def compute_corpus_version(chunks: List[Chunk]) -> str:
    """
    Compute a version hash for the corpus based on all chunk hashes.

    Args:
        chunks: List of chunks in the corpus

    Returns:
        SHA256 hash (first 16 chars) representing the corpus version
    """
    # Sort chunks by file path and line number for deterministic ordering
    sorted_chunks = sorted(chunks, key=lambda c: (c.file_path, c.line_start))

    # Concatenate all chunk hashes
    combined = "".join(c.content_hash for c in sorted_chunks)

    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def save_corpus_metadata(
    chunks: List[Chunk],
    output_path: Path,
    corpus_version: str = None,
) -> None:
    """
    Save corpus metadata (file list, chunk count, version) to JSON.

    Args:
        chunks: List of chunks in the corpus
        output_path: Path to save metadata JSON
        corpus_version: Optional pre-computed version hash
    """
    if corpus_version is None:
        corpus_version = compute_corpus_version(chunks)

    # Group chunks by file
    files = {}
    for chunk in chunks:
        if chunk.file_path not in files:
            files[chunk.file_path] = []
        files[chunk.file_path].append({
            "line_range": f"{chunk.line_start}-{chunk.line_end}",
            "chunk_type": chunk.chunk_type,
        })

    metadata = {
        "version": corpus_version,
        "total_chunks": len(chunks),
        "total_files": len(files),
        "files": files,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
