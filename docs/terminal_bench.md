# Terminal-Bench

离线管道用一个本地伪造题证明计分和报告。真实 `terminal-bench@2.0` 子集不在这里下载，也不调用模型。

Harbor 是官方 harness。题目包保持它的形状：`task.toml`、`instruction.md`、`tests/test.sh`。固定子集是 Harbor registry `7367e809` 里 `terminal-bench@2.0` 按字母序的前 18 个 id，写在 `applications/project_assistant/evaluation/terminal_bench_subset.json`。

失败分成沙箱拦下、步数耗尽、答案错、基础设施失败。基础设施失败不算模型能力。

```bash
python scripts/evaluate_terminal_bench.py --fixture
python scripts/evaluate_terminal_bench.py --live --budget-cny 15
```

第二条只打印计划并退出，计划里的 `agent_command` 写明用 `--backend docker`。

## 用 `--backend docker` 调起 agent

```bash
python scripts/evaluate_terminal_bench.py --fixture --backend docker                 # 假 Docker，离线
python scripts/evaluate_terminal_bench.py --fixture --backend docker --real-docker   # 真 Docker，需要本地有 python:3.12-slim
```

在临时目录复制本地题，用真实 CLI `baseagent --backend docker --mode full --output-format json` 跑它（脚本化假模型，不调供应商：模型让容器跑 `sh tests/test.sh`，再把输出当回答），然后像 Harbor 那样在一个新容器里跑 `tests/test.sh` 判分。记录多了 `backend: "docker"`、`docker: "fake"|"real"`、`agent_exit_code`、`container_removed`。Docker 起不来（CLI 退出码 3）记为 `infrastructure`，不算模型失败。`score_records` 和失败分类没改。

付费跑之前还缺：

- `--live` 仍然只打印计划，不执行；没有下载 Harbor 题目包、逐题起环境、收集结果的执行器。
- 题目包对接：Harbor 题目自带 Dockerfile / 镜像和工作目录（常见 `/app`），agent 应该在那个镜像里、对那个目录工作。现在的 `--backend docker` 只把宿主上的一个目录挂到 `/workspace`，镜像必须已在本地；要么先把题目镜像构建好、把题目工作目录拷到宿主再挂进去，要么给 DockerBackend 加「用题目容器、不 bind mount」的模式（本分支没做）。
- 判分容器：Harbor 在 agent 结束后在同一个题目容器里跑 `tests/test.sh`；我们每次运行结束就删容器，容器里工作区以外的改动（装的包、起的服务）判分时已经不在。要对齐，需要让判分跑在同一个容器里（延后删除），或者只考题目工作目录里的结果。
- 联网：有些题要装依赖，默认 `--docker-network none` 会失败；按题决定是否 `--docker-network bridge`，并在报告里记录。
- 资源：内存上限固定 512m，部分题（编译、训练）可能 OOM；目前没有开参数。
- 镜像里的工具：文件操作需要 `/bin/sh` 和 coreutils/busybox，`git_status` / `git_diff` 需要镜像里有 `git`。
- 真实 Docker 冒烟测试（`tests/test_docker_smoke.py`）本轮在没有 Docker 的机器上开发，没有实际跑过；付费前先在跑分机器上跑一遍。
- 预算：一次性任务没有 `--budget-cny`（它只用于主 Agent 对话），付费跑要靠 `--max-steps` / `--max-total-tokens` 和外层脚本的总预算控制。15 到 20 题大约 10 到 15 元，全量大约 40 到 60 元，另一次授权。不要为了分数改题目。
