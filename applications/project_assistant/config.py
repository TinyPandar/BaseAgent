"""Configuration for the project assistant application."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class RAGConfig:
    """Configuration for RAG components."""

    # Embedding configuration
    embedding_provider: str = "openai"  # "openai" or "mock"
    openai_api_key: Optional[str] = None
    embedding_model: str = "text-embedding-3-small"

    # Index configuration
    index_persist_dir: Optional[str] = ".baseagent/rag_index"
    collection_name: str = "baseagent_docs"

    # Chunking configuration
    max_lines_per_chunk: int = 50
    chunk_overlap_lines: int = 5

    # Retrieval configuration
    default_top_k: int = 5
    min_similarity_score: Optional[float] = None

    # Corpus configuration
    corpus_root: str = "."
    exclude_patterns: list = None

    def __post_init__(self):
        if self.exclude_patterns is None:
            self.exclude_patterns = [
                ".git",
                ".baseagent",
                "__pycache__",
                ".venv",
                "venv",
                ".pytest_cache",
                "tests/cassettes_local",
                "*.egg-info",
            ]

        # Load API key from environment if not provided
        if self.embedding_provider == "openai" and self.openai_api_key is None:
            self.openai_api_key = os.getenv("OPENAI_API_KEY")

    @classmethod
    def from_env(cls) -> "RAGConfig":
        """Create configuration from environment variables."""
        return cls(
            embedding_provider=os.getenv("RAG_EMBEDDING_PROVIDER", "openai"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            embedding_model=os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small"),
            index_persist_dir=os.getenv("RAG_INDEX_DIR", ".baseagent/rag_index"),
            collection_name=os.getenv("RAG_COLLECTION_NAME", "baseagent_docs"),
        )
