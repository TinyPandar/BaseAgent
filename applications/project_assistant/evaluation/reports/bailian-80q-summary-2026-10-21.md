# Bailian retrieval comparison on the 80-question set (2026-10-21)

Corpus: current master (622 chunks, tests/ and evaluation/ excluded); text-embedding-v4 @1024 dims; qwen3-rerank depth 20.
Hybrid params tuned on tune split: rrf_k=30, vector:BM25=1:2, candidate_depth=20. Thresholds chosen on tune, applied unchanged to test.

## Test split (28 answerable / 8 no-answer)

| Config | R@1 | R@3 | R@5 | MRR | No-answer rejected | Answerable falsely rejected |
|---|---|---|---|---|---|---|
| vector | 0.378 | 0.615 | 0.710 | 0.678 | 8/8 | 5/28 (17.9%) |
| bm25 | 0.407 | 0.668 | 0.734 | 0.759 | 6/8 | 3/28 (10.7%) |
| hybrid | 0.567 | 0.752 | 0.782 | 0.845 | 5/8 | 2/28 (7.1%) |
| hybrid+qwen3-rerank | 0.583 | 0.758 | 0.770 | 0.857 | 8/8 | 5/28 (17.9%) |

Thresholds (tune): vector -0.7627, bm25 20.18, hybrid 0.0909, hybrid+qwen 0.618.

## Usage (actual, from API responses)

- Embedding: 224,433 tokens (corpus 223,120 + queries 1,313); later runs hit the cache 100%.
- Rerank: 1,150,787 tokens (tune 634,527 + test 516,260).
- Cost at Beijing list price 0.5 RMB / 1M tokens for both: about 0.69 RMB.

## Recommendation

Default to hybrid (RRF k=30, 1:2, depth 20). qwen3-rerank only adds about 1.6 points of R@1 and 1.2 points of MRR, R@5 is slightly lower, and it costs about 14K tokens per query. Its scores do separate no-answer questions much better, though, so offer it as an opt-in rerank / refusal signal.
Raw reports: retrieval-20261021T020726*, T020923* (tune), T021030* (test).
