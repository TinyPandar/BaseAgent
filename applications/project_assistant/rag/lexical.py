"""Deterministic, code-aware BM25 using only the standard library."""

import math
import re
from collections import Counter

IDENTIFIER = re.compile(r"[A-Za-z0-9_]+(?:[./\\-][A-Za-z0-9_]+)*")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]+")
PART = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+")


def tokenize(text):
    """Keep full identifiers and their snake/camel/path components; CJK 1/2-grams."""
    tokens = []
    for match in IDENTIFIER.finditer(text):
        identifier = match.group()
        variants = [identifier.lower()]
        for component in re.split(r"[. /\\-]+", identifier):
            variants.append(component.lower())
            for word in component.split("_"):
                variants.append(word.lower())
                variants.extend(part.lower() for part in PART.findall(word))
        tokens.extend(dict.fromkeys(token for token in variants if token))
    for match in CJK.finditer(text):
        run = match.group()
        tokens.extend(run)
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def chunk_result(chunk):
    """Use the same result shape as VectorIndex.search."""
    return {"doc_id": chunk.doc_id, "content": chunk.content, "score": 0.0,
            "metadata": {"file_path": chunk.file_path, "line_start": chunk.line_start,
                         "line_end": chunk.line_end, "chunk_type": chunk.chunk_type,
                         "function_name": chunk.function_name, "class_name": chunk.class_name,
                         "parent_section": chunk.parent_section, "content_hash": chunk.content_hash}}


class BM25Index:
    """Positive BM25 relevance, with deterministic document-ID tie breaking."""

    def __init__(self, chunks, k1=1.5, b=0.75):
        if not math.isfinite(k1) or k1 <= 0 or not 0 <= b <= 1:
            raise ValueError("BM25 requires k1 > 0 and 0 <= b <= 1")
        self.k1, self.b = k1, b
        self.documents = sorted({c.doc_id: chunk_result(c) for c in chunks}.values(),
                                key=lambda row: row["doc_id"])
        self.terms = [Counter(tokenize(row["metadata"]["file_path"] + " " + row["content"]))
                      for row in self.documents]
        self.lengths = [sum(terms.values()) for terms in self.terms]
        self.average_length = sum(self.lengths) / len(self.lengths) if self.lengths else 1
        self.frequencies = Counter(term for terms in self.terms for term in terms)

    def search(self, query, top_k=5):
        if top_k <= 0:
            return []
        scores = []
        for row, terms, length in zip(self.documents, self.terms, self.lengths):
            score = 0.0
            for term in sorted(set(tokenize(query))):
                frequency = terms[term]
                if not frequency:
                    continue
                df = self.frequencies[term]
                idf = math.log1p((len(self.documents) - df + 0.5) / (df + 0.5))
                norm = self.k1 * (1 - self.b + self.b * length / (self.average_length or 1))
                score += idf * frequency * (self.k1 + 1) / (frequency + norm)
            scores.append({**row, "score": score})
        return sorted(scores, key=lambda row: (-row["score"], row["doc_id"]))[:top_k]
