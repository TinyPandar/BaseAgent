"""Script to query the BaseAgent knowledge base."""

import argparse
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from applications.project_assistant.config import RAGConfig
from applications.project_assistant.rag.embedding import OpenAIEmbedding, MockEmbedding
from applications.project_assistant.rag.indexing import VectorIndex
from applications.project_assistant.access import SourceAccess
from applications.project_assistant.tools import knowledge_package
from baseagent.kernel import ToolRegistry
from baseagent.tools.workspace import Workspace


def main():
    parser = argparse.ArgumentParser(description="Query the BaseAgent knowledge base")
    parser.add_argument(
        "query",
        help="Search query",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of results to return (default: 5)",
    )
    parser.add_argument(
        "--mock-embedding",
        action="store_true",
        help="Use mock embedding provider (must match index build)",
    )
    parser.add_argument(
        "--index-dir",
        default=".baseagent/rag_index",
        help="Directory containing the index",
    )
    parser.add_argument("--corpus-root", default=".", help="Root of the currently visible source files")
    parser.add_argument("--include", action="append", help="Visible source glob; repeat to add scopes")
    parser.add_argument("--exclude", action="append", default=[], help="Additional denied source glob")

    args = parser.parse_args()

    # Load configuration
    config = RAGConfig.from_env()
    config.index_persist_dir = args.index_dir

    # Initialize embedding provider
    if args.mock_embedding:
        embedding = MockEmbedding()
    else:
        try:
            embedding = OpenAIEmbedding(
                api_key=config.openai_api_key,
                model=config.embedding_model,
            )
        except ValueError as e:
            print(f"ERROR: {e}")
            print("Use --mock-embedding if the index was built with mock embeddings")
            return 1

    # Load index
    print(f"Loading index from: {args.index_dir}")
    try:
        index = VectorIndex(
            embedding_provider=embedding,
            persist_directory=args.index_dir,
            collection_name=config.collection_name,
        )
    except Exception as e:
        print(f"ERROR: Failed to load index ({type(e).__name__})")
        print("Run scripts/build_knowledge_base.py first to build the index")
        return 1

    chunk_count = index.count()
    if chunk_count == 0:
        print("ERROR: Index is empty. Run scripts/build_knowledge_base.py first")
        return 1

    print(f"Index loaded: {chunk_count} chunks")
    print(f"Top {args.top_k} results:\n")

    # Use the same source visibility and redaction boundary as the agent tool.
    registry = ToolRegistry()
    knowledge_package(index, SourceAccess(Workspace(args.corpus_root),
                      tuple(args.include or ("*.md", "*.py")), tuple(args.exclude)),
                      corpus_version="untracked-interactive-query").install(registry)
    result = registry.execute("knowledge_search", {"query": args.query, "top_k": args.top_k})
    if not result.ok:
        print(f"ERROR: {result.error.code}: {result.error.message}")
        return 1
    evidence_list = result.data["evidence"]

    if not evidence_list:
        print("No results found.")
        return 0

    # Display results
    for i, evidence in enumerate(evidence_list, 1):
        print(f"--- Result {i} (distance: {evidence['score']:.4f}) ---")
        print(f"Citation: {evidence['citation']}")
        print(f"Type: {evidence['chunk_type']}")
        if evidence["parent_section"]:
            print(f"Section: {evidence['parent_section']}")
        if evidence["function_name"]:
            print(f"Function: {evidence['function_name']}")
        if evidence["class_name"]:
            print(f"Class: {evidence['class_name']}")
        if evidence["redacted"]:
            print("Sensitive text redacted")
        print(f"\nContent preview (first 200 chars):")
        print(evidence["content"][:200])
        if len(evidence["content"]) > 200:
            print("...")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
