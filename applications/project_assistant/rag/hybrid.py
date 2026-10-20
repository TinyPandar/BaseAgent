"""Weighted reciprocal rank fusion. All scores here are higher-is-better."""

import math


def reciprocal_rank_fusion(rankings, *, rrf_k=60, weights=None, candidate_depth=50):
    """Fuse ranked result lists; each document contributes once per source."""
    if not math.isfinite(rrf_k) or rrf_k < 0 or candidate_depth <= 0:
        raise ValueError("rrf_k must be nonnegative and candidate_depth positive")
    weights = [1.0] * len(rankings) if weights is None else weights
    if (len(weights) != len(rankings) or not any(weights)
            or any(not math.isfinite(w) or w < 0 for w in weights)):
        raise ValueError("provide one finite nonnegative weight per source, with positive sum")
    documents, scores = {}, {}
    for ranking, weight in zip(rankings, weights):
        seen = set()
        for rank, row in enumerate(ranking[:candidate_depth], 1):
            key = row["doc_id"]
            if key in seen or not weight:
                continue
            seen.add(key)
            documents.setdefault(key, row)
            scores[key] = scores.get(key, 0.0) + weight / (rrf_k + rank)
    return [{**documents[key], "score": score} for key, score in
            sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))]
