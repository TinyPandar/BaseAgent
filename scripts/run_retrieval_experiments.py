"""Tune candidate retrieval on a frozen corpus; evaluate fixed configs once on test."""

import argparse
import itertools
import json
import math
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
from applications.project_assistant.evaluation.experiments import evaluate_ranking
from applications.project_assistant.evaluation.retrieval import (
    QUESTION_SET,
    validate_question_set,
    write_report,
)
from applications.project_assistant.rag.embedding import MockEmbedding, OpenAIEmbedding
from applications.project_assistant.rag.hybrid import reciprocal_rank_fusion
from applications.project_assistant.rag.lexical import BM25Index, chunk_result
from applications.project_assistant.rag.rerank import HeuristicReranker, QwenRerank


def open_frozen_index(directory, collection_name, chunks, version):
    """Read an existing collection, checking all IDs, content and citation metadata.

    Old indexes have no version metadata. Exact equality of their chunks to the
    versioned snapshot establishes the binding without rewriting the collection.
    """
    if not (directory / "chroma.sqlite3").is_file():
        raise ValueError("--index-dir must contain an existing Chroma index")
    import chromadb

    collection = chromadb.PersistentClient(path=str(directory)).get_collection(
        collection_name, embedding_function=None)
    expected = {c.doc_id: chunk_result(c) for c in chunks}
    if not expected or collection.count() != len(expected):
        raise ValueError("index chunk count differs from frozen snapshot")
    if (collection.metadata or {}).get("corpus_version", version) != version:
        raise ValueError("index corpus_version differs from frozen snapshot")
    data = collection.get(include=["documents", "metadatas"])
    if set(data["ids"]) != set(expected):
        raise ValueError("index chunk identities/corpus_version differ from frozen snapshot")
    for key, text, metadata in zip(data["ids"], data["documents"], data["metadatas"]):
        row = expected[key]
        if text != row["content"] or any(
            (metadata.get(field) or None) != (value or None)
            for field, value in row["metadata"].items()
        ):
            raise ValueError("index content or metadata differs from frozen snapshot")
    return collection


def validate_cache_path(path, roots):
    """Cache must be ignored by git or outside both the code and corpus checkouts."""
    path = path.resolve()
    for root in roots:
        root = root.resolve()
        if path.is_relative_to(root):
            ignored = subprocess.run(["git", "check-ignore", "-q", str(path)],
                                     cwd=root, check=False, capture_output=True)
            if ignored.returncode:
                raise ValueError("--query-cache must be gitignored or outside git")


def query_vectors(questions, path, config, *, mock=False):
    """Embed only cache misses; bind cache to provider/model/dimension/endpoint."""
    identity = {"provider": "mock" if mock else "openai", "model": config.embedding_model,
                "dimension": config.embedding_dimension,
                "endpoint_fingerprint": sha256((config.embedding_base_url or "").encode()).hexdigest()}
    cache = json.loads(path.read_text()) if path.exists() else {"identity": identity, "vectors": {}}
    if cache["identity"] != identity:
        raise ValueError("query cache embedding configuration mismatch; use another cache file")
    vectors = cache["vectors"]
    missing = list(dict.fromkeys(q["question"] for q in questions if q["question"] not in vectors))
    provider = None
    if missing:
        provider = MockEmbedding(config.embedding_dimension or 1536) if mock else OpenAIEmbedding(
            api_key=config.openai_api_key, model=config.embedding_model,
            dimension=config.embedding_dimension, base_url=config.embedding_base_url,
            batch_size=config.embedding_batch_size)
        for offset in range(0, len(missing), config.embedding_batch_size):
            batch = missing[offset:offset + config.embedding_batch_size]
            try:
                embedded = provider.embed(batch)
            except Exception:  # noqa: BLE001 -- SDK diagnostics may contain credentials/endpoint.
                raise RuntimeError("query embedding request failed") from None
            vectors.update(zip(batch, embedded))
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(cache), encoding="utf-8")
            temporary.replace(path)
    selected = {q["question"]: vectors[q["question"]] for q in questions}
    dimensions = {len(vector) for vector in selected.values()}
    if (len(dimensions) != 1 or not next(iter(dimensions), 0)
            or any(not isinstance(x, (int, float)) or not math.isfinite(x)
                   for vector in selected.values() for x in vector)):
        raise ValueError("invalid cached query vectors")
    return selected, {"api_calls": getattr(provider, "api_calls", 0),
                      "total_tokens": getattr(provider, "total_tokens", 0),
                      "estimated_tokens": sum(math.ceil(len(q) / 2) for q in missing),
                      "cache_hits": len(selected) - len(missing)}


def hybrid_params(value):
    """Validate explicitly supplied fixed hybrid parameters."""
    params = json.loads(value)
    if set(params) != {"rrf_k", "vector_weight", "bm25_weight", "candidate_depth"}:
        raise ValueError("hybrid params require rrf_k, vector_weight, bm25_weight, candidate_depth")
    RAGConfig(**params)
    if type(params["candidate_depth"]) is not int:
        raise ValueError("candidate_depth must be an integer")
    return params


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-root", type=Path, default=project_root)
    parser.add_argument("--questions", type=Path, default=QUESTION_SET)
    parser.add_argument("--index-dir", type=Path, default=project_root / ".baseagent/rag_index")
    parser.add_argument("--collection-name")
    parser.add_argument("--query-cache", type=Path, default=project_root / ".baseagent/query_embeddings.json")
    parser.add_argument("--mock-embedding", action="store_true")
    parser.add_argument("--split", choices=["tune"], default=None)
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--hybrid-params", help="Explicit JSON fixed on tune")
    parser.add_argument("--thresholds", help="JSON mapping config name to tune-selected threshold")
    parser.add_argument("--configs", nargs="+", default=None,
                        choices=["vector", "bm25", "hybrid", "hybrid+heuristic", "hybrid+qwen"])
    parser.add_argument("--allow-rerank-api", action="store_true")
    parser.add_argument("--max-rerank-tokens", type=int)
    args = parser.parse_args(argv)
    if args.final and args.split:
        parser.error("choose --split tune or --final")
    if args.allow_rerank_api and (args.max_rerank_tokens is None or args.max_rerank_tokens < 0):
        parser.error("--allow-rerank-api requires a nonnegative --max-rerank-tokens")
    if args.configs and "hybrid+qwen" in args.configs and not args.allow_rerank_api:
        parser.error("hybrid+qwen requires --allow-rerank-api")
    try:
        return run(args)
    except (ValueError, RuntimeError) as error:
        # Only our own credential-free errors are emitted; SDK exceptions may contain URLs.
        parser.error(str(error))


def run(args):
    config = RAGConfig.from_env()
    if config.embedding_provider not in {"mock", "openai"} and not args.mock_embedding:
        raise ValueError("RAG_EMBEDDING_PROVIDER must be openai or mock")
    names = args.configs or ["vector", "bm25", "hybrid", "hybrid+heuristic"] + (
        ["hybrid+qwen"] if args.allow_rerank_api else [])
    if len(set(names)) != len(names):
        raise ValueError("duplicate config names are not allowed")
    params = hybrid_params(args.hybrid_params) if args.hybrid_params else None
    thresholds = json.loads(args.thresholds) if args.thresholds else {}
    if args.final:
        if params is None or any(name not in thresholds for name in names):
            raise ValueError("--final requires --hybrid-params and --thresholds for every named config")
        if any(not isinstance(thresholds[name], (int, float)) or not math.isfinite(thresholds[name])
               for name in names):
            raise ValueError("thresholds must be finite numbers")
    elif args.configs or params or thresholds:
        raise ValueError("tune runs the predefined grid; config/params/thresholds flags are for --final")
    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    chunks = collect_corpus(args.corpus_root, exclude_patterns=config.exclude_patterns)
    dataset = json.loads(args.questions.read_text(encoding="utf-8"))
    validate_question_set(dataset, args.corpus_root, chunks)
    version = compute_corpus_version(chunks)
    collection = open_frozen_index(args.index_dir, args.collection_name or config.collection_name,
                                   chunks, version)
    validate_cache_path(args.query_cache, [project_root, args.corpus_root])
    vectors, embedding_usage = query_vectors(dataset["questions"], args.query_cache, config,
                                              mock=args.mock_embedding or config.embedding_provider == "mock")
    lexical = BM25Index(chunks, config.bm25_k1, config.bm25_b)
    max_depth = max(50, params["candidate_depth"] if params else 50)
    vector_cache, lexical_cache = {}, {}

    def vector_search(query):
        if query not in vector_cache:
            result = collection.query(query_embeddings=[vectors[query]],
                                      n_results=min(max_depth, collection.count()))
            vector_cache[query] = [{"doc_id": key, "content": text, "metadata": metadata,
                                    "score": -float(distance)}
                                   for key, text, metadata, distance in zip(
                                       result["ids"][0], result["documents"][0],
                                       result["metadatas"][0], result["distances"][0])]
        return vector_cache[query]

    reranker = QwenRerank(max_tokens=args.max_rerank_tokens, allow_api=True) if (
        "hybrid+qwen" in names) else None
    split = "test" if args.final else "tune"
    reports = {}

    def evaluate_config(name, parameters=None, report_name=None):
        def search(query):
            if name == "vector":
                return vector_search(query)
            if query not in lexical_cache:
                lexical_cache[query] = lexical.search(query, top_k=max_depth)
            if name == "bm25":
                return lexical_cache[query]
            fused = reciprocal_rank_fusion(
                [vector_search(query), lexical_cache[query]], rrf_k=parameters["rrf_k"],
                weights=[parameters["vector_weight"], parameters["bm25_weight"]],
                candidate_depth=parameters["candidate_depth"])
            if name == "hybrid+heuristic":
                return HeuristicReranker().rerank(query, fused[:parameters["candidate_depth"]])
            if name == "hybrid+qwen":
                return reranker.rerank(query, fused[:parameters["candidate_depth"]])
            return fused
        report = evaluate_ranking(dataset["questions"], search, split=split,
                                  threshold=thresholds.get(name))
        reports[report_name or name] = {"params": parameters or {}, **report}
        return report

    if args.final:
        for name in names:
            evaluate_config(name, params if name.startswith("hybrid") else None)
    else:
        evaluate_config("vector")
        evaluate_config("bm25")
        grid = []
        for k, weights, depth in itertools.product((10, 30, 60), ((1, 1), (1, 2), (2, 1)), (20, 50)):
            candidate = {"rrf_k": k, "vector_weight": weights[0], "bm25_weight": weights[1],
                         "candidate_depth": depth}
            name = f"hybrid-k{k}-w{weights[0]}:{weights[1]}-d{depth}"
            report = evaluate_config("hybrid", candidate, name)
            grid.append((candidate, report))
        params, winner = max(grid, key=lambda pair: (pair[1]["metrics"]["mrr"],
                                                    pair[1]["metrics"]["recall"]["5"]))
        reports["hybrid"] = {"params": params, **winner}
        evaluate_config("hybrid+heuristic", params)
        if reranker:
            evaluate_config("hybrid+qwen", params)
    report = {"experiment": "candidate_retrieval", "split": split, "configs": reports,
              "best_hybrid_params": params, "corpus_version": version,
              "question_set_sha256": sha256(args.questions.read_bytes()).hexdigest(),
              "chunk_count": collection.count(), "embedding_usage": embedding_usage,
              "embedding_model": config.embedding_model,
              "embedding_provider": "mock" if args.mock_embedding else config.embedding_provider,
              "rerank_usage": {key: getattr(reranker, key, 0) for key in
                               ("api_calls", "total_tokens", "estimated_tokens", "usage_estimated")},
              "bm25_params": {"k1": config.bm25_k1, "b": config.bm25_b}}
    report["api_calls"] = embedding_usage["api_calls"] + report["rerank_usage"]["api_calls"]
    report["total_tokens"] = embedding_usage["total_tokens"] + report["rerank_usage"]["total_tokens"]
    if not args.final:
        report["final_thresholds"] = {name: reports[name]["no_answer"]["threshold"] for name in names}
    path = write_report(report)
    print(json.dumps({"report": str(path), "best_hybrid_params": params,
                      "final_thresholds": report.get("final_thresholds")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
