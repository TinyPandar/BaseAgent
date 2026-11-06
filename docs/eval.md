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
- 用合成答案跑生成质量打分，tune / test 分开。正确率来自规则，不是 LLM judge。
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

可选本地 judge 文件，不联网：`{"judgments": {"exact_identifier-01": {"correct": true, "reason": "manual"}}}`. 用 `--judge FILE`。没有这个文件时不打 judge。

规则项：可答题的引用必须盖住全部标注行号；无答案题必须拒答且不附引用。约 20% 题号写在 `human_review`，`reviewed` 保持 null。

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
