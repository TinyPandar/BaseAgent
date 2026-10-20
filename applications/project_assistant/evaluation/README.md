# Retrieval evaluation (Phase 3 P0 offline)

Run from a checkout with the existing project dependencies installed:

```bash
python scripts/evaluate_retrieval.py --mock-embedding
python -m unittest tests.test_rag_embedding_offline tests.test_retrieval_evaluation tests.test_evaluate_retrieval_cli
ruff check applications/project_assistant/evaluation applications/project_assistant/rag/embedding.py applications/project_assistant/rag/indexing.py applications/project_assistant/config.py scripts/evaluate_retrieval.py scripts/build_knowledge_base.py scripts/query_knowledge_base.py tests/test_rag_embedding_offline.py tests/test_retrieval_evaluation.py tests/test_evaluate_retrieval_cli.py
```

The script reads `RAG_EMBEDDING_PROVIDER` (`openai` by default, or `mock`);
`--mock-embedding` / `--openai-embedding` override it. Mock mode does not construct an
OpenAI client, uses explicit local vectors, and disables Chroma telemetry. It
builds a fresh `VectorIndex` in batches (in-memory for Mock, persisted for real providers) and retrieves using the existing
`retrieve` function. It does not evaluate generated answers or the agent tool's
ACL/redaction filters. There are no new dependencies. Existing indexes are not
modified. `--openai-embedding` selects the configured OpenAI-compatible provider; no real embedding quality or cost has been measured in this work.

`questions.json` contains 35 manually specified questions: seven each for
exact identifiers, concepts, cross-file evidence, no-answer, and stale claims.
Each category has four tune and three test questions. Tune is for iteration;
test is held out from tuning. Questions and answer summaries are JSON and are
not ingested by the corpus collector. Stale questions distinguish historical
statements in the handoff/README from current source evidence. No-answer labels
mean the requested fact is not established by this checkout. These are manual
labels, not proof that such facts do not exist elsewhere.

The corpus is **exactly `collect_corpus(repository_root)` with its default
Markdown/Python includes and exclusions**. It includes visible tests, scripts,
and evaluation Python/Markdown files, as the existing indexer does. Evaluation
JSON, including the dataset and reports, is not included. The dataset binds to
`compute_corpus_version(chunks)`: sort chunks by path/start line, serialize their
`doc_id` values, SHA256, then take the first 16 hexadecimal characters. This
captures uncommitted source edits and line shifts, unlike git HEAD alone.
The evaluator rejects a different version before constructing an embedding client.

Each gold citation is a repository-relative `file_path:start-end` with inclusive,
one-based line numbers. Validation checks real source lines, the SHA256 of the
exact referenced text, and overlap with an actually collected chunk. For an
intentional corpus change, review all citations and answer summaries, update
`evidence_sha256` from the referenced line slices, and explicitly rebind
`corpus_version` using the two functions above. Do not silently regenerate gold
references during evaluation. A version mismatch is an instruction to review,
not an instruction to discard the old report.

A retrieved chunk hits a gold range when its file path matches exactly and its
inclusive line range overlaps. Recall@1/3/5 is the fraction of distinct gold
ranges covered by the first k chunks, averaged over answerable questions in each
split. Duplicate hits do not increase recall. Cross-file questions can receive
partial credit. MRR is the mean inverse rank of the first hit within `top_k`
(the report records the cutoff). `--top-k` must be at least 5. No-answer questions
contribute to neither denominator; they have separate top distances, where lower
is better. No abstention threshold or no-answer accuracy is claimed.

Reports are created exclusively under `evaluation/reports/` with UTC timestamps
and UUIDs, never overwriting an existing report. They record the corpus hash,
question-set hash, embedding provider/model/dimension, chunk count, top_k, split
metrics, per-question citations/distances, API attempt count (`api_calls`), token usage (`total_tokens`, also retained as
`embedding_total_tokens`), estimated tokens, and
cost (zero for Mock; unknown for paid providers). Inspect the retained Mock JSON
for the measured tune/test Recall@1/3/5 and MRR. Hash embeddings are a plumbing
baseline and do not measure semantic model quality; Chroma approximate-search
ties and dependency versions may affect exact rankings.

Embedding setup in build/query/evaluation uses `RAGConfig.from_env()` without
loading `.env`. `RAG_EMBEDDING_API_KEY` takes precedence over `OPENAI_API_KEY`.
`RAG_EMBEDDING_BASE_URL` selects the compatible endpoint and
`RAG_EMBEDDING_MODEL` selects the model. For Alibaba Bailian, use:

```bash
export RAG_EMBEDDING_PROVIDER=openai
export RAG_EMBEDDING_API_KEY=your-key
export RAG_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export RAG_EMBEDDING_MODEL=text-embedding-v4
export RAG_EMBEDDING_BATCH_SIZE=10
# Optional: export RAG_EMBEDDING_DIMENSIONS=1024
python scripts/evaluate_retrieval.py --index-dir .baseagent/retrieval_evaluation --max-tokens 1000000
```

`RAG_EMBEDDING_BATCH_SIZE` must be positive (default 100); Bailian accepts at
most 10 inputs per request, so set it to 10. Each request contributes to usage
counters. `RAG_EMBEDDING_DIMENSIONS` is optional and positive; the legacy
`RAG_EMBEDDING_DIMENSION` alias is accepted, with the plural name taking priority.
It is sent as `dimensions` only when configured. Otherwise the first response
determines the dimension; text-embedding-3-small/large have pre-response defaults
of 1536/3072. Constructor arguments override environment values. Inconsistent
vectors or later dimension changes fail explicitly. Rebuild when changing the
model or dimension.

Real indexes default to the gitignored `.baseagent/retrieval_evaluation` directory.
`--index-dir` must be gitignored or outside the repository; each run uses a fresh
collection. Reports contain neither the API key nor the base URL (including URL
credentials). `--max-tokens` optionally rejects a real run before creating a client
if the estimated corpus plus query tokens exceed the budget: ceil(characters / 2)
per embedded text, counting overlapping chunks and deduplicating identical chunk
IDs. This is an estimate, not an exact tokenizer or billing cap. Mock runs ignore
the token budget. No network or paid evaluation is needed for the unit tests.

## Results

| Run | Split | Recall@1 | Recall@3 | Recall@5 | MRR | API calls | Tokens |
|-----|-------|---------:|---------:|---------:|----:|----------:|-------:|
| Mock (`mock-sha256`, 2026-10-20) | tune (16) | 0.000 | 0.000 | 0.000 | 0.000 | 0 | 0 |
| Mock (`mock-sha256`, 2026-10-20) | test (12) | 0.000 | 0.000 | 0.000 | 0.000 | 0 | 0 |

The Mock hash embedding has no semantics; it only proves the pipeline runs
offline. Real-embedding numbers are added after an authorized run.
