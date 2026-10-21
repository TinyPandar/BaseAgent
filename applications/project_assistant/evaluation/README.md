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

`questions.json` contains 80 manually specified questions: sixteen each for
exact identifiers, concepts, cross-file evidence, no-answer, and stale claims.
There are 44 tune and 36 test questions. Each answerable category has nine tune
and seven test questions; no-answer has eight in each split. Tune is for iteration;
test is held out from tuning. Questions and answer summaries are JSON and are
not ingested by the corpus collector. Stale questions distinguish historical
statements in the handoff/README from current source evidence. No-answer labels
mean the requested fact is not established by this checkout. These are manual
labels, not proof that such facts do not exist elsewhere.

The corpus is **exactly `collect_corpus(repository_root)` with its default
Markdown/Python includes and exclusions**. It excludes `tests/` and
`applications/project_assistant/evaluation/` by default, including evaluation
questions and reports. Application source and scripts remain included. The dataset binds to
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
| Bailian `text-embedding-v4` (1024-d, 2026-10-20) | tune (16) | 0.156 | 0.281 | 0.281 | 0.260 | 236 (both splits) | 461,308 (both splits) |
| Bailian `text-embedding-v4` (1024-d, 2026-10-20) | test (12) | 0.222 | 0.521 | 0.563 | 0.493 | — | — |

The Mock hash embedding has no semantics; it only proves the pipeline runs
offline. Both runs used corpus_version `c2b4c7b4b440ebe3`, top_k 5, 2004 chunks.
The real run used batch size 10 (236 requests incl. 35 queries, 461,308
reported tokens vs. a 973k chars/2 estimate). No-answer questions are excluded
from Recall/MRR; their top-1 distances (tune 0.76-1.01, test 0.89-1.11) overlap
answerable ones, so a distance threshold alone cannot reject them yet.
The tune split was not used for any tuning before this first run.

## Candidate retrieval experiments (Phase 3 P0 step 5)

`run_retrieval_experiments.py` reads an **existing** Chroma collection; it never
builds an index or embeds corpus chunks. Use `--corpus-root` pointing to the frozen
checkout bound by `questions.json`, plus its existing `--index-dir` and optional
`--collection-name`. Changes to this working checkout intentionally invalidate
the old corpus binding: retain the old checkout for comparisons rather than
rebinding the gold set. The script validates evidence and corpus version, chunk
count, IDs, text and metadata before creating an embedding client. Old indexes
without version metadata are accepted only when every chunk equals the snapshot.

```bash
# Offline plumbing baseline; the existing index must also use mock embeddings.
python scripts/run_retrieval_experiments.py --split tune --mock-embedding \
  --corpus-root /tmp/frozen-corpus --index-dir /tmp/frozen-index \
  --query-cache .baseagent/mock-query-embeddings.json
```

Omit `--mock-embedding` to use `RAGConfig`'s configured embedding provider. Query
vectors for all questions are cached once as JSON under `.baseagent/` by
default, or at `--query-cache` (gitignored or outside both checkouts). Matching
cache hits do not construct an embedding client and cost no query embedding
calls. Caches bind to provider, model, dimensions and an endpoint fingerprint;
use separate caches for different embedding configurations. Query vectors must
use the same model/dimensions as the existing index; mock rankings do not measure
semantic quality. No live experiment was run as part of this implementation.

Tune runs vector, BM25, 18 hybrid combinations (`rrf_k=10/30/60`, vector:BM25
weights `1:1/1:2/2:1`, depth `20/50`), and heuristic reranking of the best hybrid.
The winner maximizes MRR, then Recall@5 (remaining ties use grid order). Only tune
questions are scored. Each config records answerable/no-answer top-1 score
samples, the threshold maximizing tune balanced accuracy, balanced accuracy and
ordinary accuracy. Missing candidates always abstain. All experiment scores are
higher-is-better: vector distances are negated; other scores retain relevance.
Threshold ties use the smallest threshold. Tune accuracy is an in-sample result.

The tune report and stdout provide `best_hybrid_params` and `final_thresholds`.
Pass those JSON objects explicitly for the held-out run; do not adjust them on
test. `--final` evaluates each named config once and writes one new report using
`write_report`; no grid or threshold selection runs on test.

```bash
python scripts/run_retrieval_experiments.py --final --mock-embedding \
  --corpus-root /tmp/frozen-corpus --index-dir /tmp/frozen-index \
  --query-cache .baseagent/mock-query-embeddings.json \
  --hybrid-params '{"rrf_k":30,"vector_weight":1,"bm25_weight":2,"candidate_depth":50}' \
  --thresholds '{"vector":-0.8,"bm25":2.5,"hybrid":0.05,"hybrid+heuristic":0.4}'
```

The values above illustrate the format; substitute actual tune output. Optionally
select final configs with `--configs vector bm25 hybrid hybrid+heuristic`.
Reports under `evaluation/reports/` are append-only and record parameters,
per-question results, corpus/question hashes, API attempts, actual tokens and
estimates. Keep BM25 (`RAG_BM25_K1/B`) and embedding settings fixed between runs.

Qwen reranking is excluded unless `--allow-rerank-api --max-rerank-tokens N` is
supplied. It uses `qwen3-rerank` at `POST {base}/reranks`, truncates documents to
6000 characters, and checks a cumulative conservative chars/2 estimate before
every request (including repeated queries per document). Response usage is
counted when supplied; otherwise tokens are marked estimated. Actual usage above
the estimate reduces the remaining budget; estimates cannot guarantee exact
provider billing. `RAG_RERANK_BASE_URL` overrides the embedding base, otherwise a
trailing `/compatible-mode/v1` becomes `/compatible-api/v1`.
`RAG_RERANK_API_KEY` falls back to `RAG_EMBEDDING_API_KEY`. Neither endpoint nor
key is included in reports or transport errors. Rerank API calls themselves
are not cached; omit the flag for repeat runs without rerank charges.

Application retrieval defaults to hybrid (RRF k=30, vector:BM25=1:2, candidate depth 20).
`RAG_RETRIEVAL_MODE=vector|bm25|hybrid` or `retrieve(..., config=RAGConfig(...))`
selects a mode explicitly. Fusion parameters use `RAG_RRF_K`, `RAG_VECTOR_WEIGHT`,
`RAG_BM25_WEIGHT`, `RAG_CANDIDATE_DEPTH`; BM25 uses `RAG_BM25_K1/B`.
Reranking requires explicitly passing a `Reranker` to `retrieve`; it is off by
default. BM25/hybrid/reranked scores use a relevance floor for `min_score`, while
vector uses the original distance ceiling. A reusable `lexical_index` can be
passed to avoid rebuilding BM25 for each application request.

```bash
python -m unittest tests.test_retrieval_experiments
ruff check applications/project_assistant/rag/lexical.py applications/project_assistant/rag/hybrid.py applications/project_assistant/rag/rerank.py applications/project_assistant/evaluation/experiments.py scripts/run_retrieval_experiments.py tests/test_retrieval_experiments.py
```

### Candidate experiments (2026-10-20, frozen corpus `c2b4c7b4b440ebe3`, reused Bailian index)

Params were chosen on tune only (best hybrid: `rrf_k=30`, vector:BM25 = 1:2,
depth 20); test was evaluated once with those fixed params and tune thresholds.

| Config | tune R@1/3/5 | tune MRR | test R@1/3/5 | test MRR |
|---|---|---:|---|---:|
| vector | 0.156/0.281/0.281 | 0.260 | 0.222/0.500/0.562 | 0.486 |
| BM25 | 0.302/0.365/0.427 | 0.434 | 0.326/0.354/0.604 | 0.562 |
| hybrid (RRF) | 0.250/0.396/0.594 | 0.452 | 0.326/0.438/0.632 | 0.565 |
| hybrid + heuristic rerank | 0.208/0.271/0.323 | 0.294 | 0.104/0.299/0.382 | 0.312 |
| hybrid + qwen3-rerank | 0.344/0.458/0.615 | 0.558 | 0.465/0.562/0.674 | 0.646 |

No-answer rejection (top-1 score threshold picked on tune, 4 no-answer vs 16
answerable; test has only 3 no-answer, so treat as indicative): vector distance
balanced accuracy tune 0.97 / test 0.88; hybrid RRF tune 0.75 / test 1.00;
qwen3-rerank score tune 0.88 / test 0.96. No threshold is reliable at this
sample size. The heuristic reranker hurts and should not be used.

These historical results used vector as the default on 16/12 answerable questions.
The current default follows the expanded 80-question report below; reranking
remains opt-in. Historical spend: 4 query-embedding calls (580 tokens, cached), 35
rerank calls (410,621 tokens).

## Expanded offline set (2026-10-21)

The 80-question set covers configuration, error handling, source location,
architecture, cross-file flows and stale documentation using the existing five
category names. There are 16 no-answer questions (nine newly added), requesting
operational facts or details of unimplemented services absent from the default
corpus. Original questions retain their IDs, wording, categories and splits.
Reviewed corrections to answer/evidence are enumerated in
[the expansion report](reports/offline-expanded-80-2026-10-21.md), with complete
before/after records in its companion JSON. Earlier results above are historical
and use different corpus/gold snapshots.

Run the BM25-only evaluator without an embedding client or index:

```bash
PYTHONPATH=.:src .venv/bin/python -m applications.project_assistant.evaluation.offline \
  --baseline-ref 87aea286ff106d05b3db37a06a1aeebb9cb69561 \
  --output applications/project_assistant/evaluation/reports/offline-expanded-80-rerun.json
```

The runner blocks network connections, uses fixed BM25 defaults (k1=1.5,
b=0.75), validates the corpus binding and evidence, and writes a new report
exclusively. Both splits include answerable/no-answer top1 score samples and
min/p25/median/p75/max summaries (linear interpolation). The threshold is chosen
on tune only and applied unchanged to test. Scores are relevance, higher is
better; retrieval metrics exclude no-answer questions and MRR is cut off at 5.

Token estimates use ceil(Unicode characters/3) per text. All corpus chunks,
query strings, and every query's BM25 top20 candidate documents are accounted
for. Full-document totals and the existing reranker's 6000-character document
cap are reported separately, along with repeated-query pairwise estimates.
No embedding or reranking API is called. These are planning estimates, excluding
request framing and provider tokenizer differences.

## Current default (80-question test, 2026-10-21)

The [retained Bailian report](reports/bailian-80q-summary-2026-10-21.md) measured
vector R@1/MRR **0.378/0.678**, bm25 **0.407/0.759**, hybrid **0.567/0.845**,
and hybrid+qwen **0.583/0.857**. Hybrid is now the application default, using
existing tune-selected parameters: RRF k=30, vector:BM25=1:2, candidate depth 20.
Qwen adds little and costs about 14K tokens per query, so it stays optional and
disabled by default. These are existing measurements; this change uses only
local Mock tests and does not repeat API evaluation.

Hybrid falls back to vector if BM25 cannot be built/searched (including empty
corpus), and to bm25 if vector count/search is unavailable or yields no
candidates while BM25 is available. Each direction logs one warning per process.
Explicit vector/bm25 modes do not switch. Empty indexes produce empty evidence;
if BM25 is unavailable and vector operations also fail, retrieval raises an
error. Vector fallback retains distance scores; BM25 fallback retains relevance
scores. Runtime tools and evaluation rows report the effective mode/score kind;
a report with both score kinds is marked `mixed`. See the
[application configuration](../README.md#配置) for settings and threshold semantics.

## 受控 coding 离线验收（Phase 3 第 3 项）

运行 `.venv/bin/python -m applications.project_assistant.evaluation.coding`。
固定夹具位于 `fixtures/coding/`：复制约定文档、减法错误的加法实现、
故意失败的独立测试到系统临时目录，使用 Mock embedding 和脚本模型。
复用 `app.py` 装配、`run_agent`、工作流工具及 completion policy；不调用真实模型。
先成功检索 `CONVENTIONS.md` 才允许修改；必须完成计划、以精确 argv
运行 `check_addition.py` 且通过，并生成 `REPORT.md`。

权限只在该场景打开。默认拒绝工具策略仅允许固定补丁、报告写入和固定测试命令，
拒绝任意命令与夹具测试修改；测试脚本断言 cwd 位于系统 tmp。
工作区必须是原始夹具副本且不包含软链接，结束时校验真实仓库普通文件哈希未变。
LocalBackend 本身不是 OS 沙箱，所以此处刻意不开放任意代码替换。

JSON 结果分别记录状态、外部验收、completion report、模型/工具尝试数、
输入/输出/总 tokens、未知 usage、未授权/重复副作用、精确补丁和产物存在性。
外部验收会独立运行只读固定测试，即使 agent 未获命令权限；它不能代替
agent 自己的 `verify_command` 证据。离线测试覆盖失败路径及新临时目录断网回放。

真实录制脚本（会调用配置的服务，本次仅用 fake client 离线验证）：
`python -m scripts.record_coding_scenario --runs 3 --budget-cny 2`。
必须提供 `BASEAGENT_LLM_BASE_URL`、`BASEAGENT_LLM_API_KEY`、`BASEAGENT_LLM_MODEL`，
缺失或配置无效时打印 pending 并退出；录制不回退到 DEEPSEEK。
`BASEAGENT_LLM_EXTRA_BODY='{"enable_thinking":false}'` 可用于百炼 compatible-mode；默认不传。
CLI 与 Project Assistant 共享初始化，未使用通用环境变量时继续沿用 DEEPSEEK 配置。

`--runs` 接受 3–5，**每个 run 最多 20 次模型请求**（包括失败的传输尝试），SDK 不重试。
所有 run 共享 `--budget-cny`（默认 2），逐响应按 usage 累计；预算达到后不再调用模型。
最后一次响应可越过预算，usage 缺失或传输结果不明时停止后续请求。
输入默认 0.8 元/百万、普通输出 2 元/百万，可用 `--input-cny-per-million` /
`--output-cny-per-million` 覆盖；completion 中 reasoning tokens 按 8 元/百万计，不重复计费。
默认价按用户提供的 2026-10-21 百炼北京 qwen-plus 非思考价格，
来源 https://help.aliyun.com/zh/model-studio/qwen-plus ，本次未联网核价。

`--smoke` 只发送一次带 ping 工具定义的小请求，打印 tool_calls 是否合规及 usage，不录像。
录像各自写入 `tests/cassettes/coding_scenario/run_N.json`（CassetteModel 的 JSONL 格式），
`.result.json` 旁车保存预期最终状态、工具序列、验收结果及费用；拒绝覆盖已有文件。
保存前扫描 key、私有 llm 子域和 Authorization，endpoint 仅记录为 `<redacted-endpoint>`。
失败的 run 也保存录像和原因，未知副作用字段用 null 表示未能完成验收。
汇总 JSON 写入 evaluation/reports；不含 key 或 endpoint。
`--replay --output-dir <录像目录>`（兼容 `--replay-dir`）无需凭据，阻断 socket 连接，
逐个使用新临时夹具比较最终状态、工具请求顺序与参数及验收结果；差异返回非零。
回放规范化临时根路径和验证 UUID，保留 argv、输出和文件哈希。
