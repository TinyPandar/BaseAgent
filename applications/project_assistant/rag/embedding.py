"""Embedding providers with OpenAI and local fallback support."""

import os
from abc import ABC, abstractmethod
from typing import List


class EmbeddingProvider(ABC):
    """Abstract interface for text embedding providers."""

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """
        Embed a batch of texts into dense vectors.

        Args:
            texts: List of text strings to embed

        Returns:
            List of embedding vectors (each is a list of floats)
        """
        pass

    @abstractmethod
    def embed_query(self, query: str) -> List[float]:
        """
        Embed a single query text.

        Args:
            query: Query string

        Returns:
            Embedding vector
        """
        pass

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Return the dimension of the embedding vectors."""
        pass


class OpenAIEmbedding(EmbeddingProvider):
    """OpenAI text-embedding-3-small provider."""

    def __init__(self, api_key: str = None, model: str = "text-embedding-3-small"):
        """
        Initialize OpenAI embedding provider.

        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
            model: OpenAI embedding model name
        """
        self.model = model
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not provided and not found in environment")

        # Lazy import to avoid dependency when not using OpenAI
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "openai package is required for OpenAIEmbedding. "
                "Install it with: pip install openai"
            ) from e

        self.client = OpenAI(api_key=self.api_key)
        self._dimension = 1536  # text-embedding-3-small dimension

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts using OpenAI API."""
        if not texts:
            return []

        response = self.client.embeddings.create(
            input=texts,
            model=self.model,
        )

        return [item.embedding for item in response.data]

    def embed_query(self, query: str) -> List[float]:
        """Embed a single query text."""
        return self.embed([query])[0]

    @property
    def dimension(self) -> int:
        return self._dimension


class MockEmbedding(EmbeddingProvider):
    """Mock embedding provider for testing without API calls."""

    def __init__(self, dimension: int = 1536):
        self._dimension = dimension

    def embed(self, texts: List[str]) -> List[List[float]]:
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

    def embed_query(self, query: str) -> List[float]:
        """Embed a single query text."""
        return self.embed([query])[0]

    @property
    def dimension(self) -> int:
        return self._dimension
