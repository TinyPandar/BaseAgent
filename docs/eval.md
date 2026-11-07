# 评测入口

离线命令不读取密钥，不发起模型请求。真实生成只由显式 `--live` 启动，并受 `--budget-cny` 硬上限约束；`--plan` 只打印命令。M2 冒烟和 Terminal-Bench 子集仍然停在命令，等 Frank 授权。本文档中的测试全部离线，没有调用任何 provider。

## 离线总入口

```bash
python scripts/evaluate_suite.py offline
python scripts/evaluate_answers.py --synthetic
python -m baseagent --eval-harness
```

默认报告写到 `applications/project_assistant/evaluation/reports/suite/`。这个目录不进检索语料，所以重复跑不会把金标 `corpus_version` 弄脏。已提交的摘要在 `docs/reports/phase1-eval.md`。

`offline` 会：

- 跑六个脚本化 harness 场景，记下成功率。token 是合成用量，不是账单。
- 引用已入库的 hybrid MRR 0.845。语料版本对不上时跳过 BM25 复现，不重新绑定金标。
- 用合成答案跑生成质量打分，tune / test 分开。合成答案会原样复述 `answer_summary`，并引用每一条金标行，所以 span hit、line recall、line precision、refusal accuracy、summary_token_f1、summary_rouge_l 都是 1.000。这只证明打分管道，不是模型分数。指标分层，不合成一个分数。
- 写 `docs/reports/eval-<timestamp>.md`。费用 0。

退出码：harness 失败则非零。没跑真实 80 题生成评测默认不算失败。

```bash
python scripts/evaluate_suite.py offline --min-coding-success 1 --min-citation-support 1
python scripts/evaluate_suite.py offline --require-live-answers
```

`--require-live-answers` 在还没跑真实生成时返回 1。默认不要加。

生成质量单独跑，只打 test，避免和 tune 写混：

```bash
python scripts/evaluate_answers.py --synthetic --split test --report-dir docs/reports/answers
```

可选本地 judge 文件，不联网，也不调用模型：`{"judgments": {"exact_identifier-01": {"faithful": true, "equivalent": false, "reason": "manual"}}}`. 用 `--judge FILE`。没有这个文件时不打 judge。旧的 `correct` 只进调试对照，不当作答案对错。若以后授权真 judge，只问两句：是否忠实于所引原文，是否与 `answer_summary` 等价。

分层指标，分开报告。`citation_and_refusal_pass` 是调试字段，不是答案对错，报告表头也不再把它写成主指标。

- 引用层按文件行号集合计算。`citation_line_recall` = |引用行 ∩ 金标行| / |金标行|，`citation_line_precision` = |引用行 ∩ 金标行| / |引用行|。`citation_span_hit` 要求每个 `expected_evidence` span 至少有一行被引用。可答题要 span 全中，且行召回 ≥ 0.3（`--line-recall-min`，默认 0.3）才算 `citation_supported`。不要求金标行被引用盖满。`citation_coverage` 若还在，只是行召回的旧名，一行重叠不会写成 1.0。行精确率的分母不含没有引用的题：那些题的 precision 是 null，不进平均，所以平均 1.000 不是“每题都引用了金标”。
- 无答案题必须拒答且不附引用。这条不用 summary F1 当通过条件。
- 词面重叠层只对可答题，把答案和 `answer_summary` 算 `summary_token_f1`、`summary_rouge_l`。英文按词切，代码标识符整段保留。`summary_match` 是 token F1 ≥ 0.25 或 ROUGE-L ≥ 0.3（`--summary-f1-min`、`--summary-rouge-min`）。这是词面重叠，不是语义正确。
- 忠实度默认关。`--nli` 才读取引用行。`--nli-model base` 对应 `cross-encoder/nli-deberta-v3-base`，`--nli-model large` 对应 `cross-encoder/nli-deberta-v3-large`，也可以直接写模型 id。base 和 large 分字段存放，不混成一个分数。没跑 NLI 时，逐题 JSON 不写 `faithfulness`、`faithfulness_base`、`faithfulness_large`、`nli_variant`。加载针对 transformers 4.57.1（写在 `pyproject.toml`）：`pipeline(..., local_files_only=True)` 到不了 `cached_file`，仍可能联网。加载器会先打开 `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE`，再对 config、tokenizer、权重分别传 `local_files_only=True`。这个 transformers 版本要求 `huggingface-hub<1`，和本仓库的 hub 1.x 冲突，所以不放进默认依赖，要跑 `--nli` 时另建环境。默认测试不下载权重；加载边界用假的 transformers 4.57.1 和断网断言。

约 20% 题号写在 `human_review`，`reviewed` 保持 null。已入库的旧报告若还有 Accuracy 列，那是当时的 `rule_correct`，不是答案对错。

## 生成评测：计划与执行

计划模式不读取 `BASEAGENT_LLM_*`，不建索引，也不发起请求：

```bash
python scripts/evaluate_answers.py --plan --budget-cny 2
```

`--plan` 输出 `executed: false` / `authorization_required`。`evaluate_suite.py live-answers` 仍然只做计划，离线总入口不会花钱。

获得授权后才执行真实生成。它会先校验 `BASEAGENT_LLM_API_KEY`、`BASEAGENT_LLM_BASE_URL`、`BASEAGENT_LLM_MODEL`；缺一个就打印 pending 并退出，零 provider 调用。

```bash
python scripts/evaluate_answers.py --live --budget-cny 2
```

`--live` 的流程：

- 用当前 checkout 建 BM25 + vector 索引（`collect_corpus` + `CachedEmbedding` + `VectorIndex`），默认 hybrid，`retrieve(..., reranker=None)`，不启用 rerank。
- 索引写到 `.baseagent/live_answers_index/`（已 gitignore）或仓库外目录；拒绝写入未忽略的仓库路径。
- 嵌入使用 `CachedEmbedding` + `DiskCache`，缓存目录由 `RAG_CACHE_DIR` 决定。使用 `/workspace/bailian_cache` 时需要 `RAG_EMBEDDING_PROVIDER=openai`、`RAG_EMBEDDING_MODEL=text-embedding-v4`、`RAG_EMBEDDING_DIMENSIONS=1024`，并设置 `RAG_CACHE_DIR=/workspace/bailian_cache`。
- 在任何嵌入请求之前，对唯一 chunk 文本和 80 个问题调用 `estimate_missing()`；按默认 0.5 元/百万嵌入 token 估算，超过剩余预算就直接中止，零 provider 调用。
- 每题 `retrieve` 一次，然后只做一次 completion。prompt 要求用 `path:start-end` 引用，把检索内容当作不可信数据，证据不足时用 `answers.py` 能识别的拒答措辞（“not established”）。
- 生成和嵌入都计入同一个 `--budget-cny`。生成用 `MeteredModel`（qwen-plus 非思考北京价：输入 0.8 元/百万、输出 2 元/百万、reasoning 8 元/百万）；最后一笔可越过预算，之后不再调用。预算由 `consume_budget` 驱动，不能用 agent loop 或自动压缩绕过。
- 生成后用现有离线分层打分：引用层、summary token F1 / ROUGE-L、拒答准确率；tune 和 test 分开。不跑 `--nli`、`--judge` 或 rerank。
- 报告写 `cost_cny`（生成 + 嵌入）、`provider_calls`、`live_executed`、`stopped_for_budget` 和 `embedding_usage`（cache hits/misses、api_calls、tokens）。`write_answer_report` 仍会拒绝 secret 标记。
- 标准输出只打印一个小 JSON：报告路径、split、费用、调用数、是否执行、是否因预算停止。

读取百炼 cache 时不要 `source ~/.config/baseagent/bailian.env`；由环境直接提供 `BASEAGENT_LLM_*`、`RAG_EMBEDDING_*`、`RAG_CACHE_DIR`、`RAG_EMBEDDING_DIMENSIONS`，需要非思考模式时再加 `BASEAGENT_LLM_EXTRA_BODY='{"enable_thinking":false}'`。

## ModelSummarizer 冒烟

默认生产压缩 summarizer 仍是 `StructuredSummarizer`。下面的脚本只验证可选的 `ModelSummarizer` 适配器，最多一次付费调用，不做三次失败熔断冒烟：

```bash
python scripts/smoke_model_summarizer.py --budget-cny 0.5
```

它用很小的 `compaction_max_chars` / `max_summary_input_chars` 构造超过触发线的 projected history，调用 `compact_history(state, ModelSummarizer(metered_model), checkpoint)`。成功要求出现 `context_compacted` 且 summary 非空；`prefix-too-short` 不算成功。若前缀超过 `max_summary_input_chars`，现有的一次 turn-group trim 可先触发 `context_compaction_input_trimmed`，然后仍只允许一次 summarizer 调用。输出 JSON 包含 success、events、cost_cny、budget_cny、stopped_for_budget、trimmed 和 short summary excerpt，不含 prompt 或 endpoint。

## 其他付费命令

M2 冒烟，建议上限 0.20 元。当前 coding CLI 没有 `--budget-cny`，所以这里只记录命令，不启动：

```bash
python scripts/evaluate_suite.py m2-smoke
```

打印出的三条是：

```text
python -m baseagent --stream --root "$PWD" --session demo-stream --follow-events 30 -- "Summarize README in one sentence"
python -m baseagent --parallel-readonly-tools --root "$PWD" --session demo-par -- "Read README.md and pyproject.toml then compare their titles"
python -m baseagent --root "$PWD" --session demo-resume --resume
```

验收还没做：流式增量、取消后 `interrupted`、resume 不重复副作用。离线演示脚本覆盖了同一条 harness 路径，不能代替这次付费冒烟。

录像回放不新建模型请求，但录像绑了录制时的绝对路径。需要时再跑：

```bash
python scripts/evaluate_suite.py cassette-replay
```
