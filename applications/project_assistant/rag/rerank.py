"""Opt-in rerankers. Endpoint credentials are never included in diagnostics."""

import json
import math
import os
import urllib.request
from abc import ABC, abstractmethod

from .lexical import IDENTIFIER, tokenize


class Reranker(ABC):
    """Rerank candidate dictionaries, returning higher-is-better scores."""

    api_calls = 0
    total_tokens = 0
    estimated_tokens = 0
    usage_estimated = False

    @abstractmethod
    def rerank(self, query, documents, top_n=5):
        """Return up to top_n ranked candidates."""


class HeuristicReranker(Reranker):
    """Term coverage plus exact code identifier and path matches."""

    def rerank(self, query, documents, top_n=5):
        query_terms = set(tokenize(query))
        identifiers = {m.group().lower() for m in IDENTIFIER.finditer(query)
                       if any(c in m.group() for c in "_./\\")
                       or any(c.isupper() for c in m.group()[1:])}
        ranked = []
        for row in documents:
            text = row["metadata"]["file_path"] + " " + row["content"]
            coverage = len(query_terms & set(tokenize(text))) / max(1, len(query_terms))
            exact = {m.group().lower() for m in IDENTIFIER.finditer(text)}
            score = coverage + 2 * len(identifiers & exact)
            ranked.append({**row, "score": score})
        return sorted(ranked, key=lambda row: (-row["score"], row["doc_id"]))[:max(0, top_n)]


def derive_rerank_base_url(embedding_base):
    """Bailian rerank uses compatible-api rather than compatible-mode."""
    base = (embedding_base or "").rstrip("/")
    suffix = "/compatible-mode/v1"
    return base[:-len(suffix)] + "/compatible-api/v1" if base.endswith(suffix) else base


class QwenRerank(Reranker):
    """Explicitly budgeted Bailian rerank requests, with injectable offline transport.

    The guard reserves max(actual usage, chars/2 estimate) after every call.
    Provider billing can differ from estimates; no client-side estimate guarantees
    an exact billing cap.
    """

    def __init__(self, *, max_tokens, allow_api=False, base_url=None, api_key=None,
                 transport=None):
        if not allow_api:
            raise ValueError("Qwen reranking requires explicit API opt-in")
        if type(max_tokens) is not int or max_tokens < 0:
            raise ValueError("a nonnegative rerank token budget is required")
        self._base = (base_url or os.getenv("RAG_RERANK_BASE_URL") or
                      derive_rerank_base_url(os.getenv("RAG_EMBEDDING_BASE_URL"))).rstrip("/")
        self._key = api_key or os.getenv("RAG_RERANK_API_KEY") or os.getenv("RAG_EMBEDDING_API_KEY")
        if not self._base or not self._key:
            raise ValueError("rerank endpoint and credentials must be configured")
        self.max_tokens = max_tokens
        self.api_calls = self.total_tokens = self.estimated_tokens = self._reserved = 0
        self.usage_estimated = False
        self._transport = transport or self._post

    def _post(self, payload):
        request = urllib.request.Request(
            self._base + "/reranks", data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def rerank(self, query, documents, top_n=5):
        if not documents or top_n <= 0:
            return []
        texts = [row["content"][:6000] for row in documents]
        # Reserve repeated query tokens conservatively for pairwise provider accounting.
        estimate = sum(math.ceil((len(query) + len(text)) / 2) for text in texts)
        if self._reserved + estimate > self.max_tokens:
            raise ValueError("rerank token budget exceeded before request")
        payload = {"model": "qwen3-rerank", "query": query, "documents": texts,
                   "top_n": min(top_n, len(texts))}
        self.api_calls += 1
        self._reserved += estimate
        self.estimated_tokens += estimate
        try:
            response = self._transport(payload)
        except Exception:  # noqa: BLE001 -- Never expose transport credentials or endpoint.
            raise RuntimeError("rerank request failed") from None
        usage = (response.get("usage") or {}).get("total_tokens")
        if usage is None:
            usage = estimate
            self.usage_estimated = True
        if not isinstance(usage, (int, float)) or not math.isfinite(usage) or usage < 0:
            raise ValueError("invalid rerank token usage")
        self.total_tokens += usage
        self._reserved += max(0, usage - estimate)
        ranked, seen = [], set()
        for result in response.get("results", []):
            index = result["index"]
            score = float(result["relevance_score"])
            if type(index) is not int or not 0 <= index < len(documents) or not math.isfinite(score):
                raise ValueError("invalid rerank result")
            if index not in seen:
                ranked.append({**documents[index], "score": score})
                seen.add(index)
        return sorted(ranked, key=lambda row: (-row["score"], row["doc_id"]))[:top_n]
