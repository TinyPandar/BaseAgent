"""Recover paid embeddings from a local Chroma index without embedding requests."""

import argparse
import math
import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from applications.project_assistant.rag.cache import DiskCache, embedding_key


def seed_collection(collection, cache, *, provider, model, dimensions, batch_size=100):
    """Page existing documents/vectors; never instantiate an embedding function."""
    if dimensions <= 0 or batch_size <= 0:
        raise ValueError("dimensions and batch size must be positive")
    written = 0
    for offset in range(0, collection.count(), batch_size):
        rows = collection.get(include=["documents", "embeddings"],
                              limit=batch_size, offset=offset)
        documents, vectors = rows["documents"], rows["embeddings"]
        if documents is None or vectors is None or len(documents) != len(vectors):
            raise ValueError("Chroma documents/embeddings are missing or inconsistent")
        for text, vector in zip(documents, vectors):
            values = [float(x) for x in vector]
            if (not isinstance(text, str) or len(values) != dimensions
                    or any(not math.isfinite(x) for x in values)):
                raise ValueError("invalid Chroma document/vector or dimensions")
            cache.put("embedding", embedding_key(provider, model, dimensions, text), values)
            written += 1
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-dir", type=Path, required=True)
    parser.add_argument("--collection-name", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dimensions", type=int, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--batch-size", type=int, default=100, help="Local Chroma read page size")
    args = parser.parse_args(argv)
    if args.dimensions <= 0 or args.batch_size <= 0:
        parser.error("dimensions and batch size must be positive")
    if not (args.index_dir / "chroma.sqlite3").is_file():
        parser.error("--index-dir must contain an existing Chroma index")
    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    import chromadb
    from chromadb.config import Settings

    collection = chromadb.PersistentClient(
        path=str(args.index_dir), settings=Settings(anonymized_telemetry=False)
    ).get_collection(args.collection_name, embedding_function=None)
    count = seed_collection(collection, DiskCache(args.cache_dir), provider=args.provider,
                            model=args.model, dimensions=args.dimensions,
                            batch_size=args.batch_size)
    print(f"Seeded {count} embeddings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
