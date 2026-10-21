# Local retrieval cache

Both evaluation CLIs use `.baseagent/cache/rag.sqlite3` by default. Set
`RAG_CACHE_DIR` to share another directory (for example
`/workspace/bailian_cache/embeddings`). `--no-cache` disables reads and writes;
`run_retrieval_experiments.py --query-cache DIRECTORY` overrides the directory.
The former standalone query JSON file is no longer used.

Embedding entries use `(provider, model, dimensions, sha256(text))`; chunks and
queries share entries. Explicit dimensions are recommended when recovering an
index. Auto dimensions are inferred from the first response and remembered for
later runs. Rerank entries use the model, query and ordered candidate text hashes.
Rerank requests retrieve all candidate scores so cached results support different
`top_n` values. SQLite commits each complete entry, preserving prior entries if
a later request fails or the process stops.

Recover existing vectors locally, without an embedding request:

```bash
python scripts/seed_embedding_cache_from_chroma.py \
  --index-dir /workspace/bailian_index --collection-name project_docs \
  --provider openai --model text-embedding-v4 --dimensions 1024
```

Use the same provider/model/dimensions in subsequent evaluations, including
`RAG_EMBEDDING_DIMENSIONS=1024` for this example. The recovery tool disables Chroma
telemetry and does not attach an embedding function. Its `--batch-size` controls
local database reads; the embedding API batch size defaults to 10.

`--max-tokens` guards only missing embedding texts; `--max-rerank-tokens` guards
only missing rerank requests. Token estimates use characters/2 (rounded up),
including repeated query text per rerank candidate. Estimates are not a billing
guarantee. JSON reports and their Markdown sidecars record `cache_hits`,
`cache_misses`, `cache_hit_rate`, actual `api_calls`, `estimated_tokens` for sent
inputs and `total_tokens` from provider usage (rerank falls back to the estimate
when usage is unavailable). Embedding counts are text occurrences; rerank counts
are requests. Duplicate missing embedding texts within a batch are sent once.
