"""Retrieval interface with evidence aggregation."""

import logging
from dataclasses import dataclass
from functools import lru_cache

from .indexing import VectorIndex


@lru_cache(maxsize=2)
def _warn_fallback(mode):
    """Log each hybrid fallback direction once per process, without source data."""
    unavailable = "BM25" if mode == "vector" else "Vector"
    logging.getLogger(__name__).warning(
        "%s index unavailable; hybrid retrieval falling back to %s", unavailable, mode
    )


@dataclass
class Evidence:
    """A piece of evidence retrieved from the knowledge base."""

    doc_id: str
    file_path: str
    line_range: tuple[int, int]
    content: str
    score: float
    chunk_type: str
    parent_section: str | None = None
    function_name: str | None = None
    class_name: str | None = None
    content_hash: str | None = None

    @property
    def citation(self) -> str:
        """Human-readable citation format."""
        start, end = self.line_range
        return f"{self.file_path}:{start}-{end}"

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "doc_id": self.doc_id,
            "file_path": self.file_path,
            "line_start": self.line_range[0],
            "line_end": self.line_range[1],
            "content": self.content,
            "score": self.score,
            "chunk_type": self.chunk_type,
            "citation": self.citation,
            "parent_section": self.parent_section,
            "function_name": self.function_name,
            "class_name": self.class_name,
            "content_hash": self.content_hash,
        }


def retrieve(
    query: str,
    index: VectorIndex | None,
    top_k: int = 5,
    min_score: float | None = None,
    *, config=None, lexical_index=None, reranker=None, diagnostics=None,
) -> list[Evidence]:
    """
    Retrieve relevant evidence from the knowledge base.

    Args:
        query: Search query
        index: Vector index to search
        top_k: Maximum number of results
        min_score: Distance ceiling for vector; relevance floor for BM25/hybrid/rerank
        diagnostics: Optional dict populated with effective retrieval_mode and score_kind

    Returns:
        List of Evidence objects, sorted by relevance
    """
    from applications.project_assistant.config import RAGConfig

    from .chunking import Chunk
    from .hybrid import reciprocal_rank_fusion
    from .lexical import BM25Index

    if top_k <= 0:
        return []
    config = config or RAGConfig.from_env()
    mode = config.retrieval_mode
    if mode == "vector":
        results = index.search(query, top_k=top_k if reranker is None else config.candidate_depth)
    else:
        depth = config.candidate_depth
        try:
            if lexical_index is None:
                data = index.collection.get(include=["documents", "metadatas"])
                chunks = [Chunk(content=text, **{k: v for k, v in metadata.items()
                                                if k != "citation"})
                          for text, metadata in zip(data["documents"], data["metadatas"])]
                lexical_index = BM25Index(chunks, config.bm25_k1, config.bm25_b)
            if mode == "hybrid" and not lexical_index.documents:
                raise ValueError("BM25 corpus is empty")
            results = lexical_index.search(query, top_k=depth)
        except Exception:
            if mode != "hybrid":
                raise
            _warn_fallback("vector")
            mode = "vector"
            try:
                count = index.count() if index is not None else 0
                results = (index.search(query, top_k=min(count, top_k if reranker is None else depth))
                           if count else [])
            except Exception as exc:
                raise RuntimeError("Neither BM25 nor vector retrieval is available") from exc
        if mode == "hybrid":
            try:
                count = index.count() if index is not None else 0
                if not count:
                    raise ValueError("Vector index is empty or unavailable")
                vector_results = index.search(query, top_k=min(depth, count))
                if not vector_results:
                    raise ValueError("Vector index returned no candidates")
            except Exception:  # noqa: BLE001 - optional vector backend failures degrade to BM25
                _warn_fallback("bm25")
                mode = "bm25"
            else:
                results = reciprocal_rank_fusion(
                    [vector_results, results],
                    rrf_k=config.rrf_k, weights=[config.vector_weight, config.bm25_weight],
                    candidate_depth=depth)
    if diagnostics is not None:
        diagnostics.update(retrieval_mode=mode, score_kind=(
            "distance_lower_is_better" if mode == "vector" and reranker is None
            else "relevance_higher_is_better"))
    if reranker is not None:
        results = reranker.rerank(query, results[:config.candidate_depth], top_n=top_k)
    results = results[:top_k]

    evidence_list = []
    for result in results:
        metadata = result["metadata"]

        # ChromaDB returns distance (lower is better)
        score = result["score"]
        if min_score is not None and (
            score > min_score if mode == "vector" and reranker is None
            else score < min_score
        ):
            continue

        evidence = Evidence(
            doc_id=result["doc_id"],
            file_path=metadata["file_path"],
            line_range=(metadata["line_start"], metadata["line_end"]),
            content=result["content"],
            score=score,
            chunk_type=metadata["chunk_type"],
            parent_section=metadata.get("parent_section") or None,
            function_name=metadata.get("function_name") or None,
            class_name=metadata.get("class_name") or None,
            content_hash=metadata.get("content_hash"),
        )
        evidence_list.append(evidence)

    return evidence_list
