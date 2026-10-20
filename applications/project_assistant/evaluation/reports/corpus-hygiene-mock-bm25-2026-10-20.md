# Corpus hygiene: free Mock/BM25 evaluation (2026-10-20)

Corpus version: `dbceda73af46de89`; **622 unique chunks**. Earlier bindings:
methods-only @200 `b87ed71229b971ef` (929 chunks), 80-line regression
`58a09bdfe2390326`, original question binding `5f6a966874b3fc13`.
All rows score 12 answerable test questions at top-k 5; no-answer questions
are excluded from Recall/MRR.

| Test run | R@1 | R@5 | MRR |
|---|---:|---:|---:|
| Previous BM25, Bailian/OpenAI-compatible real embedding index | 0.326 | 0.604 | 0.562 |
| Exclude-only ablation, old whole-definition chunker (provided result) | 0.326 | 0.632 | 0.625 |
| BM25, max_lines=80 regression | 0.125 | 0.479 | 0.403 |
| Mock vector, methods-only max_lines=200 | 0.000 | 0.000 | 0.000 |
| BM25, methods-only max_lines=200 | 0.208 | 0.479 | 0.444 |
| New Mock vector, class-size policy max_lines=200 | 0.000 | 0.000 | 0.000 |
| New BM25, class-size policy max_lines=200 | **0.410** | **0.604** | **0.667** |

Keeping whole classes when they fit within **200 lines**, including decorators,
recovers the methods-only regression. Compared with the supplied exclude-only
ablation, R@1 improves by 0.083 and MRR by 0.042; R@5 is 0.028 lower.
The result is near the target, with slightly lower R@5. The supplied exclude-only
ablation was not rerun. No parameters were tuned on test.

The previous values come from the [evaluation README](../README.md#candidate-experiments-2026-10-20-frozen-corpus-c2b4c7b4b440ebe3-reused-bailian-index),
whose experiment used frozen corpus `c2b4c7b4b440ebe3` and a real Bailian index.
Mock vector is not comparable to Bailian vector: SHA256-based vectors have
no semantic quality. BM25 is lexical, but comparisons with the original baseline
also reflect corpus changes and the earlier removal of one excluded gold range.
The 80-line, methods-only 200-line, and class-size-policy runs use the same
questions and gold ranges.

Changes:

- Both default exclude lists exclude `tests/` and
  `applications/project_assistant/evaluation/`; application RAG source stays included.
- Classes spanning at most 200 lines (decorator start through `end_lineno`) emit
  one whole `python_class` chunk, including methods, without separate method chunks.
  Only oversized classes split into a `python_class` preamble before the first
  method and `python_function` chunks for direct methods, retaining `class_name`.
  Preambles and functions/methods exceeding 200 lines use windows with 5-line
  overlap and retained names. Iteration stays top-level; nested definitions are
  not emitted separately. Module statements retain constants.
  The 101-line sanity function stays whole; 250-line bodies split into chunks
  of at most 200 lines. `src/baseagent/main.py`'s `main()` splits at lines
  71–270 and 266–439.
- Recomputed `collect_corpus` and `compute_corpus_version`, rebound questions,
  refreshed all 36 evidence digests (unchanged from the preceding hygiene run),
  and passed `validate_question_set`. No questions or gold ranges were added.
  The earlier hygiene change removed only stale-04's excluded evaluator reference,
  retained its handoff/script evidence, and adjusted its summary.

Both new runs made **0 API calls, consumed 0 API tokens, and cost $0**.
BM25 used `k1=1.5`, `b=0.75`, fixed threshold 0, and `--final --configs bm25`
with supplied hybrid parameters `rrf_k=30`, `vector_weight=1`, `bm25_weight=2`,
`candidate_depth=20`. Its fixed threshold accepts every no-answer query
(balanced accuracy 0.5).
The Mock evaluator built an in-memory index. A separate Mock-only collection,
`corpus_hygiene_class_policy`, was built at gitignored
`.baseagent/corpus_hygiene_class_policy` for the experiment runner; its query cache
is `.baseagent/corpus_hygiene_class_policy_queries.json`. Older indexes were retained.

Raw evidence: [new Mock vector report](retrieval-20261020T102605234714Z-2fc6698303384352b4b08dce0e87352b.json) and
[new BM25 final report](retrieval-20261020T102616980777Z-f89896f7e6b14983810fd14769a42c81.json).
The BM25 report's embedding model string is inherited configuration; its provider
is `mock`, and no real embedding was used. Older JSON reports are preserved,
including the [methods-only 200-line Mock report](retrieval-20261020T102320410145Z-62981e7eae9e4a5f8ad4ecbc9c9a8465.json),
[methods-only 200-line BM25 report](retrieval-20261020T102342783702Z-480d25ff292f49549763536e18b22dbd.json),
[80-line Mock report](retrieval-20261020T102008174567Z-da2a33deefe94d9fa2f2ea3d6219dfb9.json)
and [80-line BM25 report](retrieval-20261020T102042164062Z-3cb5c5588b29442b8e4564518ab7391e.json).
Both new JSON reports were emitted by `write_report`.

Validation: gold-set validation passed; all **26 tests** in
`tests.test_rag_chunking`, `tests.test_rag_corpus`, and `tests.test_retrieval_evaluation`
passed, covering small whole classes, decorator-inclusive size boundaries,
oversized preambles, oversized classes with small methods, oversized functions
and methods, and nested-definition suppression. An initial test run before
rebinding failed only on the expected corpus-version mismatch; the full suite
passed after rebinding. A temporary local `tests/__init__.py` avoided shadowing
by an installed `tests` package for the exact requested module invocation;
the marker was removed. Ruff remains unavailable in this environment.
`git diff --check` passed. No commit or push was performed; `.git` is read-only.
