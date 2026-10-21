"""Embedding providers with OpenAI and local fallback support."""

import os
from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    """Abstract interface for text embedding providers."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a batch of texts into dense vectors.

        Args:
            texts: List of text strings to embed

        Returns:
            List of embedding vectors (each is a list of floats)
        """

    @abstractmethod
    def embed_query(self, query: str) -> list[float]:
        """
        Embed a single query text.

        Args:
            query: Query string

        Returns:
            Embedding vector
        """

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Return the dimension of the embedding vectors."""


class OpenAIEmbedding(EmbeddingProvider):
    """OpenAI-compatible embeddings with configurable or inferred dimensions."""

    def __init__(self, api_key: str | None = None, model: str = "text-embedding-3-small",
                 dimension: int | None = None, base_url: str | None = None, batch_size: int | None = None):
        """
        Initialize OpenAI embedding provider.

        Args:
            api_key: RAG_EMBEDDING_API_KEY, falling back to OPENAI_API_KEY.
            model: OpenAI embedding model name
            dimension: RAG_EMBEDDING_DIMENSIONS (alias RAG_EMBEDDING_DIMENSION).
            base_url: Compatible endpoint (defaults to RAG_EMBEDDING_BASE_URL).
            batch_size: Maximum inputs per request (RAG_EMBEDDING_BATCH_SIZE or 10).
        """
        self.model = model
        if dimension is None:
            configured = os.getenv("RAG_EMBEDDING_DIMENSIONS", os.getenv("RAG_EMBEDDING_DIMENSION"))
            dimension = int(configured) if configured is not None else None
        if dimension is not None and (type(dimension) is not int or dimension <= 0):
            raise ValueError("embedding dimension must be a positive integer")
        if batch_size is None:
            batch_size = int(os.getenv("RAG_EMBEDDING_BATCH_SIZE", "10"))
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("embedding batch size must be a positive integer")
        self.batch_size = batch_size
        self._requested_dimension = dimension
        self._dimension = dimension
        self.api_calls = 0
        self.total_tokens = 0
        self.base_url = base_url or os.getenv("RAG_EMBEDDING_BASE_URL")
        self.api_key = api_key or os.getenv("RAG_EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("Set RAG_EMBEDDING_API_KEY or OPENAI_API_KEY, or provide api_key")

        # Lazy import to avoid dependency when not using OpenAI
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "openai package is required for OpenAIEmbedding. "
                "Install it with: pip install openai"
            ) from e

        options = {"api_key": self.api_key}
        if self.base_url:
            options["base_url"] = self.base_url
        self.client = OpenAI(**options)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts using OpenAI API."""
        if not texts:
            return []

        embeddings = []
        for offset in range(0, len(texts), self.batch_size):
            batch = texts[offset:offset + self.batch_size]
            options = {"input": batch, "model": self.model}
            if self._requested_dimension is not None:
                options["dimensions"] = self._requested_dimension
            self.api_calls += 1
            response = self.client.embeddings.create(**options)
            usage = getattr(response, "usage", None)
            self.total_tokens += getattr(usage, "total_tokens", 0) or 0

            vectors = [item.embedding for item in sorted(response.data, key=lambda item: item.index)]
            if len(vectors) != len(batch) or not vectors or not vectors[0]:
                raise ValueError("embedding response must contain one nonempty vector per input")
            dimension = len(vectors[0])
            if any(len(vector) != dimension for vector in vectors):
                raise ValueError("embedding response contains inconsistent dimensions")
            if self._dimension is not None and self._dimension != dimension:
                raise ValueError("embedding response dimension changed or differs from configuration")
            self._dimension = dimension
            embeddings.extend(vectors)
        return embeddings

    def embed_query(self, query: str) -> list[float]:
        """Embed a single query text."""
        return self.embed([query])[0]

    @property
    def dimension(self) -> int | None:
        """Known default before embedding; the first response is authoritative."""
        defaults = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}
        return self._dimension if self._dimension is not None else defaults.get(self.model)


class MockEmbedding(EmbeddingProvider):
    """Mock embedding provider for testing without API calls."""

    def __init__(self, dimension: int = 1536):
        self._dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return deterministic mock embeddings based on text hash."""
        import hashlib

        embeddings = []
        for text in texts:
            # Use hash to generate deterministic but varied embeddings
            hash_bytes = hashlib.sha256(text.encode()).digest()
            # Convert first N bytes to floats in [-1, 1]
            values = [
                (b - 128) / 128.0
                for b in hash_bytes[:self._dimension]
            ]
            # Pad if needed
            while len(values) < self._dimension:
                values.append(0.0)
            embeddings.append(values[:self._dimension])

        return embeddings

    def embed_query(self, query: str) -> list[float]:
        """Embed a single query text."""
        return self.embed([query])[0]

    @property
    def dimension(self) -> int:
        return self._dimension
