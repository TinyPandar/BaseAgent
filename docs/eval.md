# 评测入口

离线命令不读取密钥，不发起模型请求。真实生成、M2 冒烟、Terminal-Bench 子集都停在命令，等 Frank 授权。

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

## 不要跑的付费命令

80 题生成，预计不到 2 元。这条命令只打印计划并退出：

```bash
python scripts/evaluate_answers.py --live --budget-cny 2
```

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
