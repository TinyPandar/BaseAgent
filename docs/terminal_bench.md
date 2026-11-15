# Terminal-Bench

离线管道用一个本地伪造题证明计分和报告。真实 `terminal-bench@2.0` 子集不在这里下载，也不调用模型。

Harbor 是官方 harness。题目包保持它的形状：`task.toml`、`instruction.md`、`tests/test.sh`。固定子集是 Harbor registry `7367e809` 里 `terminal-bench@2.0` 按字母序的前 18 个 id，写在 `applications/project_assistant/evaluation/terminal_bench_subset.json`。

失败分成沙箱拦下、步数耗尽、答案错、基础设施失败。基础设施失败不算模型能力。

```bash
python scripts/evaluate_terminal_bench.py --fixture
python scripts/evaluate_terminal_bench.py --live --budget-cny 15
```

第二条只打印计划并退出。计划里的 `agent_command` 是本地链路的写法（`--backend docker`），**不是已核实的 Harbor 接入方式**（计划里 `harbor_integration: "unverified"`）。

## 范围：`--backend docker` 是本地编码沙箱，不是 Terminal-Bench 的承载环境

`--backend docker`（[docker_backend.md](docker_backend.md)）只为本地编码任务提供隔离：挂载宿主工作区到 `/workspace`、默认断网、每次运行结束删容器、能力收紧。它**不会**为了 Terminal-Bench 改成题目的 `/app` 目录、为判分保留容器或默认开网。真实评测怎么接 Harbor（agent 跑在哪个容器里、谁起题目环境、在哪判分）要另行按 Harbor 官方的接入方案核实，不以本仓库的 docker 后端为前提。

## fixture 走 `--backend docker`：只证明链路

```bash
python scripts/evaluate_terminal_bench.py --fixture --backend docker                 # 假 Docker，离线
python scripts/evaluate_terminal_bench.py --fixture --backend docker --real-docker   # 真 Docker，需要本地有 python:3.12-slim
```

在临时目录复制那道本地题，用真实 CLI `baseagent --backend docker --mode full --output-format json` 跑它（脚本化假模型，不调供应商：模型让容器跑 `sh tests/test.sh`，再把输出当回答），然后在一个新容器里跑 `tests/test.sh` 检查。记录多了 `backend: "docker"`、`docker: "fake"|"real"`、`agent_exit_code`、`container_removed`；报告带 `fixture_only: true` 和一句说明。Docker 起不来（CLI 退出码 3）记为 `infrastructure`。`score_records` 和失败分类没改。

**这不是跑分成绩。** 题目是一道本地伪造题（`echo ok`），模型是脚本，`success_rate: 1.0` 只说明「CLI → docker 后端 → 计分」这条链路能走通，不能当作 Terminal-Bench 成绩引用。

## 真实评测另行处理（不在本仓库 docker 后端的范围里）

- 核实 Harbor 官方的 agent 接入方案，再决定 BaseAgent 怎么作为 agent 接进去；`--live` 目前只打印计划、不执行。
- 预算：一次性任务没有 `--budget-cny`（它只用于主 Agent 对话），付费跑要另有总预算控制。15 到 20 题大约 10 到 15 元，全量大约 40 到 60 元，另一次授权。不要为了分数改题目。
