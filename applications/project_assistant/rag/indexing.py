"""Vector indexing with ChromaDB backend."""

import os
from typing import List, Optional, Dict, Any

from .chunking import Chunk
from .embedding import EmbeddingProvider


class VectorIndex:
    """Vector index for semantic search over document chunks."""

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        persist_directory: Optional[str] = None,
        collection_name: str = "project_docs",
    ):
        """
        Initialize vector index with ChromaDB backend.

        Args:
            embedding_provider: Provider for generating embeddings
            persist_directory: Directory to persist the index (None for in-memory)
            collection_name: Name of the ChromaDB collection
        """
        self.embedding_provider = embedding_provider
        self.persist_directory = persist_directory
        self.collection_name = collection_name

        # Lazy import ChromaDB
        try:
            import chromadb
        except ImportError as e:
            raise ImportError(
                "chromadb package is required for VectorIndex. "
                "Install it with: pip install chromadb"
            ) from e

        # Initialize ChromaDB client
        if persist_directory:
            os.makedirs(persist_directory, exist_ok=True)
            self.client = chromadb.PersistentClient(path=persist_directory)
        else:
            self.client = chromadb.Client()

        # Get or create collection
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata=self._embedding_metadata(),
        )

    def _embedding_metadata(self):
        dimension = self.embedding_provider.dimension
        return {"dimension": dimension} if dimension is not None else None

    def add_chunks(self, chunks: List[Chunk]) -> None:
        """
        Add document chunks to the index.

        Args:
            chunks: List of Chunk objects to index
        """
        if not chunks:
            return

        # Prepare data for ChromaDB
        ids = [chunk.doc_id for chunk in chunks]
        documents = [chunk.content for chunk in chunks]
        metadatas = [
            {
                "file_path": chunk.file_path,
                "line_start": chunk.line_start,
                "line_end": chunk.line_end,
                "chunk_type": chunk.chunk_type,
                "citation": chunk.citation,
                "parent_section": chunk.parent_section or "",
                "function_name": chunk.function_name or "",
                "class_name": chunk.class_name or "",
                "content_hash": chunk.content_hash,
            }
            for chunk in chunks
        ]

        # Generate embeddings
        embeddings = self.embedding_provider.embed(documents)

        # Add to collection
        self.collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
        self.collection.modify(metadata=self._embedding_metadata())

    def search(
        self,
        query: str,
        top_k: int = 5,
        filter_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search the index for relevant chunks.

        Args:
            query: Search query text
            top_k: Number of results to return
            filter_metadata: Optional metadata filters (e.g., {"chunk_type": "python_function"})

        Returns:
            List of results, each containing:
                - doc_id: Chunk identifier
                - content: Chunk text
                - score: Similarity score (distance)
                - metadata: Chunk metadata (file_path, line_start, etc.)
        """
        query_embedding = self.embedding_provider.embed_query(query)

        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=filter_metadata,
        )

        # Format results
        formatted = []
        for i in range(len(results["ids"][0])):
            formatted.append({
                "doc_id": results["ids"][0][i],
                "content": results["documents"][0][i],
                "score": results["distances"][0][i] if results["distances"] else 0.0,
                "metadata": results["metadatas"][0][i],
            })

        return formatted

    def clear(self) -> None:
        """Clear all documents from the index."""
        self.client.delete_collection(name=self.collection_name)
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata=self._embedding_metadata(),
        )

    def count(self) -> int:
        """Return the number of chunks in the index."""
        return self.collection.count()
