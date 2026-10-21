"""Build a fresh corpus index and write a versioned retrieval evaluation report."""

import argparse
import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from applications.project_assistant.config import RAGConfig
from applications.project_assistant.corpus import collect_corpus, compute_corpus_version
from applications.project_assistant.evaluation.retrieval import (
    QUESTION_SET,
    evaluate,
    validate_question_set,
    write_report,
)
from applications.project_assistant.rag.cache import (
    CachedEmbedding,
    DiskCache,
    usage_stats,
)
from applications.project_assistant.rag.embedding import MockEmbedding, OpenAIEmbedding
from applications.project_assistant.rag.indexing import VectorIndex


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    provider = parser.add_mutually_exclusive_group()
    provider.add_argument("--mock-embedding", action="store_true", help="Offline hash baseline")
    provider.add_argument("--openai-embedding", action="store_true", help="Opt in to paid API requests")
    parser.add_argument("--corpus-root", type=Path, default=project_root)
    parser.add_argument("--questions", type=Path, default=QUESTION_SET)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--index-dir", type=Path,
                        default=project_root / ".baseagent/retrieval_evaluation",
                        help="Real index directory; must be gitignored or outside the repo")
    parser.add_argument("--max-tokens", type=int,
                        help="Abort before API calls if estimated corpus + query tokens exceed this")
    parser.add_argument("--no-cache", action="store_true", help="Disable disk cache reads and writes")
    args = parser.parse_args(argv)
    if args.top_k < 5:
        parser.error("--top-k must be at least 5")

    if args.max_tokens is not None and args.max_tokens < 0:
        parser.error("--max-tokens must be nonnegative")

    # Explicit embeddings avoid Chroma's model downloads; also disable telemetry.
    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    config = RAGConfig.from_env()
    chunks = collect_corpus(args.corpus_root, exclude_patterns=config.exclude_patterns)
    dataset = json.loads(args.questions.read_text(encoding="utf-8"))
    validate_question_set(dataset, args.corpus_root, chunks)
    provider_name = ("mock" if args.mock_embedding else
                     "openai" if args.openai_embedding else config.embedding_provider)
    if provider_name not in {"mock", "openai"}:
        parser.error("RAG_EMBEDDING_PROVIDER must be openai or mock")
    unique_chunks = list({chunk.doc_id: chunk for chunk in chunks}.values())
    cache = None if args.no_cache else DiskCache()
    embedding = CachedEmbedding(
        lambda: MockEmbedding(config.embedding_dimension or 1536) if provider_name == "mock"
        else OpenAIEmbedding(api_key=config.openai_api_key, model=config.embedding_model,
                             dimension=config.embedding_dimension, base_url=config.embedding_base_url,
                             batch_size=config.embedding_batch_size),
        provider=provider_name, model=config.embedding_model if provider_name != "mock" else "mock-sha256",
        dimensions=config.embedding_dimension if provider_name != "mock" else config.embedding_dimension or 1536,
        batch_size=config.embedding_batch_size, cache=cache,
        max_tokens=args.max_tokens if provider_name != "mock" else None)
    estimated_tokens = embedding.estimate_missing(
        [c.content for c in unique_chunks] + [q["question"] for q in dataset["questions"]])
    if provider_name == "openai" and args.max_tokens is not None and estimated_tokens > args.max_tokens:
        parser.error(f"estimated {estimated_tokens} tokens exceeds --max-tokens {args.max_tokens}")
    persist_directory = None
    if provider_name == "openai":
        directory = args.index_dir.resolve()
        if directory.is_relative_to(project_root):
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", str(directory / "chroma.sqlite3")],
                cwd=project_root, check=False,
            )
            if ignored.returncode != 0:
                parser.error("--index-dir must be gitignored or outside the repo")
        persist_directory = str(directory)
    index = VectorIndex(embedding, persist_directory=persist_directory,
                        collection_name="retrieval_" + os.urandom(8).hex())
    # Markdown hierarchy can yield identical chunks; Chroma requires unique IDs.
    for offset in range(0, len(unique_chunks), 50):
        index.add_chunks(unique_chunks[offset:offset + 50])
    report = evaluate(dataset, index, top_k=args.top_k)
    report.update({
        "corpus_version": compute_corpus_version(chunks),
        "question_set_sha256": sha256(args.questions.read_bytes()).hexdigest(),
        "embedding_provider": provider_name,
        "embedding_model": getattr(embedding, "model", "mock-sha256"),
        "embedding_dimension": embedding.dimension,
        "chunk_count": index.count(),
        "api_calls": getattr(embedding, "api_calls", 0),
        "estimated_tokens": estimated_tokens,
        "total_tokens": getattr(embedding, "total_tokens", 0),
        "embedding_total_tokens": getattr(embedding, "total_tokens", 0),
        "embedding_usage": embedding.usage(),
        "rerank_usage": usage_stats(),
        "cost_usd": 0 if provider_name == "mock" else None,
    })
    path = write_report(report)
    print(json.dumps({"report": str(path), "splits": report["splits"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
