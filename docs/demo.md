# 三分钟演示

不调用付费模型。三条命令都能在本机走完。数字来自已经入库的报告，这次演示不重新计算它们。

## 0:00 带引用的检索

```bash
python -B scripts/demo_knowledge_search.py
```

临时夹具里跑真实的 `knowledge_search`。回答里会出现 `path:start-end` 引用。没有模型请求。

## 0:45 流式、账本、审批、恢复

```bash
python -B scripts/demo_offline.py
```

脚本用假模型走真实 harness，并调用同一次 `--inspect-session`：

1. `--stream` 路径写出 `model_delta`，答案里带 `README.md:1-2`。
2. `verify_command` 在审批前不执行；批准后 `ran.txt` 只写一次，再 resume 不重复。
3. 工具已经产生副作用后被打断，状态先是 `interrupted`，未对账时是 `needs_recovery`，对账后续跑不追加第二行。

对应的 CLI 形状（要等模型配置，现在不要跑）：

```text
python -m baseagent --inspect-session --session demo-stream --db .baseagent/sessions.sqlite3
python -m baseagent --approve-tool CALL_ID --approval-digest DIGEST --session demo-verify
python -m baseagent --session demo-resume --resume
```

## 2:00 指标

```bash
python scripts/evaluate_suite.py offline
```

报告写到 `applications/project_assistant/evaluation/reports/suite/eval-<timestamp>.md`。已提交摘要在 `docs/reports/phase1-eval.md`。费用是 0。

已经测过、这次不重跑的数字：

| 指标 | 数字 | 来源 |
| --- | ---: | --- |
| coding 成功率 | 5/5（基线 2/3） | `docs/reports/m1_coding_comparison-20261102T111829Z-b21f9bdd6a3745a4b572a5e3dcf309e1.md` |
| 平均 token | 80780 → 36185.4（−55.2%） | 同上，五次 after 平均约 0.030 元 |
| test hybrid MRR | 0.845 | `applications/project_assistant/evaluation/reports/bailian-80q-summary-2026-10-21.md` |

rerank 默认关闭：R@5 从 0.782 降到 0.770，每次查询大约 1.4 万 token。

生成质量这条离线报告用的是合成答案，只证明打分管道。它不是 qwen 的正确率。
