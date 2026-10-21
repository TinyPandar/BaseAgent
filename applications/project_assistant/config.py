"""Configuration for the project assistant application."""

import os
from dataclasses import dataclass


@dataclass
class RAGConfig:
    """Configuration for RAG components."""

    # Embedding configuration
    embedding_provider: str = "openai"  # "openai" or "mock"
    openai_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int | None = None
    embedding_base_url: str | None = None
    embedding_batch_size: int = 10

    # Index configuration
    index_persist_dir: str | None = ".baseagent/rag_index"
    collection_name: str = "baseagent_docs"

    # Chunking configuration
    max_lines_per_chunk: int = 50
    chunk_overlap_lines: int = 5

    # Retrieval configuration
    default_top_k: int = 5
    min_similarity_score: float | None = None

    # Corpus configuration
    corpus_root: str = "."
    exclude_patterns: list = None

    retrieval_mode: str = "hybrid"
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    rrf_k: float = 30
    vector_weight: float = 1.0
    bm25_weight: float = 2.0
    candidate_depth: int = 20

    def __post_init__(self):
        import math

        if self.retrieval_mode not in {"vector", "bm25", "hybrid"}:
            raise ValueError(
                f"invalid RAG_RETRIEVAL_MODE/retrieval_mode {self.retrieval_mode!r}; "
                "must be vector, bm25, or hybrid"
            )
        if (not math.isfinite(self.bm25_k1) or self.bm25_k1 <= 0
                or not 0 <= self.bm25_b <= 1
                or type(self.candidate_depth) is not int or self.candidate_depth <= 0
                or not math.isfinite(self.rrf_k) or self.rrf_k < 0
                or any(not math.isfinite(w) or w < 0 for w in
                       (self.vector_weight, self.bm25_weight))
                or self.vector_weight + self.bm25_weight <= 0):
            raise ValueError("invalid retrieval parameters")

        if self.embedding_dimension is not None and (
            type(self.embedding_dimension) is not int or self.embedding_dimension <= 0
        ):
            raise ValueError("embedding dimension must be a positive integer")
        if type(self.embedding_batch_size) is not int or self.embedding_batch_size <= 0:
            raise ValueError("embedding batch size must be a positive integer")
        if self.exclude_patterns is None:
            self.exclude_patterns = [
                ".git",
                ".baseagent",
                "__pycache__",
                ".venv",
                "venv",
                ".pytest_cache",
                "tests",
                "applications/project_assistant/evaluation",
                "*.egg-info",
            ]

        # Load API key from environment if not provided
        if self.embedding_provider == "openai" and self.openai_api_key is None:
            self.openai_api_key = (os.getenv("RAG_EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY"))

    @classmethod
    def from_env(cls) -> "RAGConfig":
        """Create configuration from environment variables."""
        dimensions = os.getenv("RAG_EMBEDDING_DIMENSIONS", os.getenv("RAG_EMBEDDING_DIMENSION"))
        return cls(
            retrieval_mode=os.getenv("RAG_RETRIEVAL_MODE", "hybrid"),
            bm25_k1=float(os.getenv("RAG_BM25_K1", "1.5")),
            bm25_b=float(os.getenv("RAG_BM25_B", "0.75")),
            rrf_k=float(os.getenv("RAG_RRF_K", "30")),
            vector_weight=float(os.getenv("RAG_VECTOR_WEIGHT", "1")),
            bm25_weight=float(os.getenv("RAG_BM25_WEIGHT", "2")),
            candidate_depth=int(os.getenv("RAG_CANDIDATE_DEPTH", "20")),
            embedding_provider=os.getenv("RAG_EMBEDDING_PROVIDER", "openai"),
            openai_api_key=(os.getenv("RAG_EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")),
            embedding_model=os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small"),
            embedding_dimension=int(dimensions) if dimensions is not None else None,
            embedding_batch_size=int(os.getenv("RAG_EMBEDDING_BATCH_SIZE", "10")),
            embedding_base_url=os.getenv("RAG_EMBEDDING_BASE_URL"),
            index_persist_dir=os.getenv("RAG_INDEX_DIR", ".baseagent/rag_index"),
            collection_name=os.getenv("RAG_COLLECTION_NAME", "baseagent_docs"),
        )
