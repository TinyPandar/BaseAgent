# Terminal-Bench

离线管道用一个本地伪造题证明计分和报告。真实 `terminal-bench@2.0` 子集不在这里下载，也不调用模型。

Harbor 是官方 harness。题目包保持它的形状：`task.toml`、`instruction.md`、`tests/test.sh`。固定子集是 Harbor registry `7367e809` 里 `terminal-bench@2.0` 按字母序的前 18 个 id，写在 `applications/project_assistant/evaluation/terminal_bench_subset.json`。

失败分成沙箱拦下、步数耗尽、答案错、基础设施失败。基础设施失败不算模型能力。

```bash
python scripts/evaluate_terminal_bench.py --fixture
python scripts/evaluate_terminal_bench.py --live --budget-cny 15
```

第二条只打印计划并退出。15 到 20 题大约 10 到 15 元，全量大约 40 到 60 元，另一次授权。不要为了分数改题目。
