"""Script to build the BaseAgent knowledge base index."""

# ruff: noqa: E402

import argparse
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from applications.project_assistant.config import RAGConfig
from applications.project_assistant.corpus import (
    collect_corpus,
    compute_corpus_version,
    save_corpus_metadata,
)
from applications.project_assistant.rag.embedding import OpenAIEmbedding, MockEmbedding
from applications.project_assistant.rag.indexing import VectorIndex


def main():
    parser = argparse.ArgumentParser(description="Build the BaseAgent knowledge base index")
    parser.add_argument(
        "--corpus-root",
        default=".",
        help="Root directory of the corpus (default: current directory)",
    )
    parser.add_argument(
        "--mock-embedding",
        action="store_true",
        help="Use mock embedding provider (no API calls)",
    )
    parser.add_argument(
        "--output-dir",
        default=".baseagent/rag_index",
        help="Directory to persist the index",
    )
    parser.add_argument(
        "--metadata-file",
        default=".baseagent/corpus_metadata.json",
        help="Path to save corpus metadata",
    )

    args = parser.parse_args()

    # Load configuration
    config = RAGConfig.from_env()
    config.corpus_root = args.corpus_root
    config.index_persist_dir = args.output_dir

    print(f"Collecting corpus from: {args.corpus_root}")
    chunks = collect_corpus(
        Path(args.corpus_root),
        exclude_patterns=config.exclude_patterns,
    )

    if not chunks:
        print("ERROR: No chunks collected. Check the corpus root directory.")
        return 1

    print(f"Collected {len(chunks)} chunks from {len(set(c.file_path for c in chunks))} files")

    # Compute and save metadata
    corpus_version = compute_corpus_version(chunks)
    print(f"Corpus version: {corpus_version}")

    metadata_path = Path(args.metadata_file)
    save_corpus_metadata(chunks, metadata_path, corpus_version)
    print(f"Saved corpus metadata to: {metadata_path}")

    # Initialize embedding provider
    if args.mock_embedding or config.embedding_provider == "mock":
        print("Using mock embedding provider (no API calls)")
        embedding = MockEmbedding()
    else:
        print(f"Using OpenAI embedding: {config.embedding_model}")
        try:
            embedding = OpenAIEmbedding(
                api_key=config.openai_api_key,
                model=config.embedding_model,
                dimension=config.embedding_dimension,
                base_url=config.embedding_base_url,
                batch_size=config.embedding_batch_size,
            )
        except ValueError as e:
            print(f"ERROR: {e}")
            print("Use --mock-embedding to build without API calls, or set RAG_EMBEDDING_API_KEY / OPENAI_API_KEY")
            return 1

    # Build index
    print(f"Building vector index at: {args.output_dir}")
    index = VectorIndex(
        embedding_provider=embedding,
        persist_directory=args.output_dir,
        collection_name=config.collection_name,
    )

    # Clear existing index
    index.clear()

    # Add all chunks
    print("Indexing chunks...")
    # Process in batches to show progress
    batch_size = 50
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i+batch_size]
        index.add_chunks(batch)
        print(f"  Indexed {min(i+batch_size, len(chunks))}/{len(chunks)} chunks")

    final_count = index.count()
    print("\nIndex built successfully!")
    print(f"  Total chunks indexed: {final_count}")
    print(f"  Persist directory: {args.output_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
