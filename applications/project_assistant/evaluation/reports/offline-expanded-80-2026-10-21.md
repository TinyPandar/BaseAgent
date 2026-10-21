# 80-question offline BM25 evaluation — 2026-10-21

共 **80 题**，tune/test = **44/36（55%/45%）**，无答案 **16 题**（新增 9 题）。五类各 16 题；保留 schema_version=1 和原类别名。配置、错误处理覆盖在定位、概念和跨文件题中。

| Category | tune | test | Total |
|---|---:|---:|---:|
| exact_identifier | 9 | 7 | 16 |
| concept | 9 | 7 | 16 |
| cross_file | 9 | 7 | 16 |
| stale | 9 | 7 | 16 |
| no_answer | 8 | 8 | 16 |

## Corpus and gold review

使用默认 `collect_corpus(repository_root)`，排除 tests/、evaluation/、隐藏和凭据路径。版本按既有 `compute_corpus_version(chunks)` 重新计算为 `dbceda73af46de89`；本次修改均在默认排除范围内，因此与原绑定相同，未人为更换算法或版本值。全部 622 个 chunk（唯一 ID 也是 622 个）均来自当前工作树。

题集 SHA256：`7e24b60674354b917a98f751380af3013ff737fa0409ca244099be359b37933e`。逐条打开所有不同证据范围，核对源码、答案及包含端点的行号，再计算文本摘要并调用既有 validate。共 100 个不同引用，最长 33 行；没有 tests/ 或 evaluation/ 证据。JSON 的 evidence_audit 记录每段范围、行数及 SHA256。

原 35 题的 ID、问句、类别和 split 全部保留。16 题有明确列出的答案/引用纠正，其余逐字段不变。只有 concept-05 的 answer_summary 改动：原文把所有 score 都视为 Chroma distance；当前源码 BM25/hybrid/rerank 使用越高越好的 relevance。其余更改只修正过宽或漏掉关键语句的证据定位。范围收紧会改变 gold 数量/覆盖计算，不能把本报告与旧 35 题指标直接当成检索质量提升。

| Original ID | Locator correction |
|---|---|
| exact_identifier-01 | `applications/project_assistant/corpus.py:15-72` → `applications/project_assistant/corpus.py:31-45`; `applications/project_assistant/corpus.py:49-71` |
| exact_identifier-02 | `applications/project_assistant/corpus.py:80-96` → `applications/project_assistant/corpus.py:91-97` |
| exact_identifier-05 | `applications/project_assistant/rag/chunking.py:40-112` → `applications/project_assistant/rag/chunking.py:89-101` |
| exact_identifier-07 | `applications/project_assistant/rag/retrieval.py:47-121` → `applications/project_assistant/rag/retrieval.py:99-121` |
| concept-01 | `applications/project_assistant/corpus.py:80-96` → `applications/project_assistant/corpus.py:91-97` |
| concept-05 | `applications/project_assistant/rag/retrieval.py:47-121` → `applications/project_assistant/rag/retrieval.py:75-92`; `applications/project_assistant/rag/retrieval.py:99-105` |
| cross_file-01 | `scripts/build_knowledge_base.py:23-117`; `applications/project_assistant/rag/embedding.py:47-91` → `scripts/build_knowledge_base.py:80-86`; `applications/project_assistant/rag/embedding.py:59-77`; `applications/project_assistant/rag/embedding.py:88-91`; `applications/project_assistant/rag/embedding.py:98-105` |
| cross_file-02 | `applications/project_assistant/corpus.py:15-72` → `applications/project_assistant/corpus.py:31-45`; `applications/project_assistant/corpus.py:49-71` |
| cross_file-04 | `applications/project_assistant/rag/indexing.py:97-136`; `applications/project_assistant/rag/retrieval.py:47-121` → `applications/project_assistant/rag/indexing.py:126-136`; `applications/project_assistant/rag/retrieval.py:99-121` |
| cross_file-06 | `applications/project_assistant/corpus.py:80-96` → `applications/project_assistant/corpus.py:91-97` |
| cross_file-07 | `scripts/query_knowledge_base.py:22-126` → `scripts/query_knowledge_base.py:50-65` |
| stale-01 | `applications/project_assistant/corpus.py:15-72` → `applications/project_assistant/corpus.py:31-45`; `applications/project_assistant/corpus.py:49-71` |
| stale-02 | `applications/project_assistant/rag/embedding.py:47-91` → `applications/project_assistant/rag/embedding.py:59-77`; `applications/project_assistant/rag/embedding.py:88-91` |
| stale-04 | `scripts/evaluate_retrieval.py:27-100` → `scripts/evaluate_retrieval.py:84-100` |
| stale-06 | `applications/project_assistant/corpus.py:15-72` → `applications/project_assistant/corpus.py:31-45`; `applications/project_assistant/corpus.py:49-71` |
| stale-07 | `src/baseagent/main.py:71-439` → `src/baseagent/main.py:144-145` |

特别纠正：原 corpus.py:80-96 未包含第 97 行实际 SHA256/截断 return，现为 91-97；cross_file-01 补上 embedding.py:98-105 的请求 dimensions 赋值；stale-07 原 369 行 main.py 引用缩为真实 CLI choices 的 144-145。JSON 中 original_question_corrections 保留完整 before/after 和原因。

无答案复核：在默认语料中搜索并阅读相关源码/规划。Redis/队列/常驻运行仅为后置规划；PDF OCR、增量 watcher/checkpoint、Git webhook 索引、Kafka、cron、Kubernetes 分片、副本、S3 夜间备份、Grafana 运维配置未实现或没有提供题目要求的具体事实。旧 7 题的生产账单、人员、延迟、签名等事实亦未由当前语料建立。所有无答案题的 expected_evidence 为空。标签仅对本仓库当前语料成立。

## BM25 metrics

固定 stdlib BM25，k1=1.5、b=0.75，无参数搜索或 reranker 调用。候选深度 20，仅前 5 个用于评测。Recall 按不同 gold 范围的覆盖比例计算，跨文件题可获部分分；MRR 截止 5。无答案题不进入 Recall/MRR 分母。

| Split | Answerable | No answer | R@1 | R@3 | R@5 | MRR@5 |
|---|---:|---:|---:|---:|---:|---:|
| tune | 36 | 8 | 0.442593 | 0.611111 | 0.694444 | 0.671296 |
| test | 28 | 8 | 0.370833 | 0.668452 | 0.716071 | 0.741071 |

## Top1 BM25 score distribution

分数越高越相关；分位数使用 sorted values 上的 (n−1)×p 线性插值。没有缺失结果；分数不等同于答案存在概率。

| Split | Label | n | min | p25 | median | p75 | max |
|---|---|---:|---:|---:|---:|---:|---:|
| tune | answerable | 36 | 20.141031 | 26.234319 | 31.636638 | 38.663928 | 61.457497 |
| tune | no_answer | 8 | 12.622600 | 15.249589 | 16.974631 | 18.509930 | 19.015459 |
| test | answerable | 28 | 12.797854 | 23.608071 | 29.155007 | 33.852364 | 54.544823 |
| test | no_answer | 8 | 12.618587 | 15.119559 | 16.465745 | 18.454119 | 27.679897 |
| all | answerable | 64 | 12.797854 | 24.456499 | 31.538845 | 38.435563 | 61.457497 |
| all | no_answer | 16 | 12.618587 | 15.119559 | 16.942616 | 18.509930 | 27.679897 |

拒答阈值仅用 tune 选择：top1 ≥ **20.141031** 才预测有答案，无候选一律拒答。tune balanced accuracy=1.000000；原样应用到 test 后 balanced accuracy=0.821429、accuracy=0.861111（3 个有答案误拒、2 个无答案误收）。test 的两组分数重叠，单一分数阈值只能作为候选规则；没有据 test 调整阈值。它衡量可回答性分类，不能证明回答正确或来源验证已通过。

## Token estimates (no calls)

统一按每个文本 ceil(Unicode 字符数 / 3) 估算，中英混合近似；包含 chunk 重叠，逐文本向上取整。没有调用 tokenizer、embedding 或 rerank API。

| Item | Characters | Estimated tokens |
|---|---:|---:|
| All corpus chunks (622) | 811,904 | 270,838 |
| All 80 queries | 6,693 | 2,259 |

每题使用其 BM25 top20 候选；query 计一次，再加 20 个文档逐文本估算值。原始完整候选与现有 reranker 每文档最多 6000 字符两种口径均保留：

| Candidate text policy | Total tokens (80 questions) | Mean/query | min | p25 | median | p75 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| Full documents | 1,828,548 | 22,856.850 | 8,148.00 | 17,362.50 | 23,845.00 | 27,805.75 | 37,366.00 |
| 6000 chars/document | 1,635,782 | 20,447.275 | 8,148.00 | 16,905.75 | 21,610.00 | 24,220.75 | 31,168.00 |

若按每个 query/document pair 重复计 query，6000 字符截断口径总量约 **1,678,308 tokens**。JSON 的 tokens.rerank_per_question 对每题记录 query 字符/token、20 条候选引用、完整与截断文档估算、重复 query 估算；by_split 给出分 split 总量。估算不含 metadata、JSON/协议封装、提示词和实际 provider tokenizer 差异，不代表账单。

## Validation and reproduction

- `validate_question_set`: passed；原有证据 SHA256、语料绑定、实际收录重叠检查均通过。
- 相关 pytest：93 passed，0 failed（检索、评测 CLI、语料、chunk、离线 embedding、索引和知识工具）。项目 venv 缺少 pytest，使用本机已有 pytest 追加到 sys.path；项目依赖优先，禁用第三方 pytest 插件自动加载。未安装或下载依赖。完整命令见 JSON validation.pytest。
- `.venv/bin/ruff check applications/project_assistant/evaluation tests/test_retrieval_evaluation.py tests/test_retrieval_experiments.py`: passed。
- 全仓 ruff：349 条既有问题。已用原 HEAD 的 git archive 在临时目录复查，baseline/current 均 349，按文件、规则、行列逐条一致，新增 0。本次未修改语料源码来清理历史 lint。全仓 ruff 不宣称通过。
- `git diff --check`: passed。
- 实测仅 BM25；pytest 使用本地 Mock/假 SDK。评测入口封禁 socket 连接；未 source env 文件，无付费/网络 API 调用，api_calls=0、cost_usd=0。

```bash
PYTHONPATH=.:src .venv/bin/python -m applications.project_assistant.evaluation.offline \
  --baseline-ref 87aea286ff106d05b3db37a06a1aeebb9cb69561 \
  --output applications/project_assistant/evaluation/reports/offline-expanded-80-rerun.json
```

新输出路径须不存在。JSON 保留逐题指标、top1 样本、候选引用、阈值、估算和全部题集纠正信息；可用其 question_set_sha256 与 corpus_version 核对快照。

## Local commit environment

原仓库 `.git` 为只读文件系统，提交创建 index.lock 失败。改在可写本地副本 `/tmp/baseagent-evaluation-commit-m10wn7fq` 创建 git commit，不 push；原工作树文件保留、原分支指针未改。对应提交保存在同目录 `offline-expanded-80-2026-10-21.bundle`，该 bundle 以原 HEAD 为前提，可在可写仓库中导入。commit hash 由最终回复提供，避免报告自引用。
