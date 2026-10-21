"""Transactional, local caches shared by retrieval evaluation and recovery tools."""

import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .embedding import EmbeddingProvider


def cache_directory():
    return Path(os.environ.get("RAG_CACHE_DIR", Path(__file__).resolve().parents[3] / ".baseagent/cache"))


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embedding_key(provider, model, dimensions, text):
    return [provider, model, dimensions, text_hash(text)]


class DiskCache:
    """Each write commits a complete JSON value with SQLite crash recovery."""

    def __init__(self, directory=None):
        directory = Path(directory) if directory is not None else cache_directory()
        if directory.is_file():
            raise ValueError("cache location must be a directory; legacy query JSON files are unsupported")
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "rag.sqlite3"
        with closing(self._connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS cache (kind TEXT, key TEXT, value TEXT, "
                       "PRIMARY KEY(kind, key))")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def get(self, kind, key):
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM cache WHERE kind=? AND key=?",
                             (kind, json.dumps(key, ensure_ascii=False))).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, key, value):
        encoded = json.dumps(value, allow_nan=False)
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                       (kind, json.dumps(key, ensure_ascii=False), encoded))


def usage_stats(hits=0, misses=0, api_calls=0, estimated_tokens=0, total_tokens=0):
    return {"cache_hits": hits, "cache_misses": misses,
            "cache_hit_rate": hits / (hits + misses) if hits + misses else 0.0,
            "api_calls": api_calls, "estimated_tokens": estimated_tokens,
            "total_tokens": total_tokens}


class CachedEmbedding(EmbeddingProvider):
    """Lazy provider creation; only unique misses are sent, one durable batch at a time."""

    def __init__(self, factory, *, provider, model, dimensions, batch_size=10,
                 cache=None, max_tokens=None):
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("embedding batch size must be a positive integer")
        self.factory, self.provider, self.model = factory, provider, model
        self.batch_size, self.cache, self.max_tokens = batch_size, cache, max_tokens
        self.auto_dimension = dimensions is None
        # Auto mode remembers the model's observed default, while every vector
        # is still stored under its concrete dimension, compatible with seeds.
        if dimensions is None and cache:
            dimensions = cache.get("embedding_default_dimensions", [provider, model])
        if dimensions is not None and (type(dimensions) is not int or dimensions <= 0):
            raise ValueError("embedding dimension must be a positive integer")
        self._dimension = self.key_dimension = dimensions
        self.client = None
        self.cache_hits = self.cache_misses = self.estimated_tokens = 0

    def key(self, text):
        return embedding_key(self.provider, self.model, self.key_dimension, text)

    def lookup(self, text):
        vector = (self.cache.get("embedding", self.key(text))
                  if self.cache and self.key_dimension is not None else None)
        if vector is not None and (
            not vector or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector)
            or (self._dimension is not None and len(vector) != self._dimension)
        ):
            raise ValueError("invalid cached embedding vector or dimensions")
        return vector

    def estimate_missing(self, texts):
        return sum(math.ceil(len(t) / 2) for t in dict.fromkeys(texts) if self.lookup(t) is None)

    def embed(self, texts):
        vectors = {t: self.lookup(t) for t in dict.fromkeys(texts)}
        missing = [t for t, v in vectors.items() if v is None]
        estimate = sum(math.ceil(len(t) / 2) for t in missing)
        if self.max_tokens is not None and self.estimated_tokens + estimate > self.max_tokens:
            raise ValueError("embedding token budget exceeded before request")
        self.cache_hits += sum(vectors[t] is not None for t in texts)
        self.cache_misses += sum(vectors[t] is None for t in texts)
        for offset in range(0, len(missing), self.batch_size):
            batch = missing[offset:offset + self.batch_size]
            try:
                if self.client is None:
                    self.client = self.factory()
                self.estimated_tokens += sum(math.ceil(len(t) / 2) for t in batch)
                embedded = self.client.embed(batch)
            except Exception:  # noqa: BLE001 -- SDK errors may contain credentials or endpoint URLs.
                raise RuntimeError("embedding request failed") from None
            if len(embedded) != len(batch):
                raise ValueError("embedding response count mismatch")
            for text, vector in zip(batch, embedded):
                if (not vector or any(not isinstance(x, (int, float)) or not math.isfinite(x)
                                      for x in vector)
                        or (self._dimension is not None and len(vector) != self._dimension)):
                    raise ValueError("invalid embedding vector or dimensions")
                self._dimension = self.key_dimension = len(vector)
                vectors[text] = vector
                if self.cache:
                    self.cache.put("embedding", self.key(text), vector)
                    if self.auto_dimension:
                        self.cache.put("embedding_default_dimensions", [self.provider, self.model],
                                       self.key_dimension)
        for vector in vectors.values():
            self._dimension = len(vector)
        return [vectors[t] for t in texts]

    def embed_query(self, query):
        return self.embed([query])[0]

    @property
    def dimension(self):
        return self._dimension

    @property
    def api_calls(self):
        return getattr(self.client, "api_calls", 0)

    @property
    def total_tokens(self):
        return getattr(self.client, "total_tokens", 0)

    def usage(self):
        return usage_stats(self.cache_hits, self.cache_misses, self.api_calls,
                           self.estimated_tokens if self.provider != "mock" else 0, self.total_tokens)
