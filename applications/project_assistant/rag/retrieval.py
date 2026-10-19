"""Retrieval interface with evidence aggregation."""

from dataclasses import dataclass
from typing import List, Optional

from .indexing import VectorIndex


@dataclass
class Evidence:
    """A piece of evidence retrieved from the knowledge base."""

    doc_id: str
    file_path: str
    line_range: tuple[int, int]
    content: str
    score: float
    chunk_type: str
    parent_section: Optional[str] = None
    function_name: Optional[str] = None
    class_name: Optional[str] = None

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
        }


def retrieve(
    query: str,
    index: VectorIndex,
    top_k: int = 5,
    min_score: Optional[float] = None,
) -> List[Evidence]:
    """
    Retrieve relevant evidence from the knowledge base.

    Args:
        query: Search query
        index: Vector index to search
        top_k: Maximum number of results
        min_score: Minimum similarity score threshold (lower distance is better)

    Returns:
        List of Evidence objects, sorted by relevance
    """
    results = index.search(query, top_k=top_k)

    evidence_list = []
    for result in results:
        metadata = result["metadata"]

        # ChromaDB returns distance (lower is better)
        score = result["score"]
        if min_score is not None and score > min_score:
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
        )
        evidence_list.append(evidence)

    return evidence_list
