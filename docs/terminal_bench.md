# Terminal-Bench

BaseAgent 通过 Harbor 官方的外部 agent 接口参加 Terminal-Bench 2.0。Harbor 版本锁定 `0.26.0`。容器、`/app`、同容器评分、超时和 `result.json` 都由 Harbor 负责；本仓库不自建评测运行器。

**还没有真实跑分。** 下面所有数字都来自离线检查。旧版 `--fixture` 的「1/1」是一条写死 `passed=True` 的记录，不是评分结果，那条路径已删除。

## 怎么接上

```text
harbor run ──► 任务容器（/app，题目自己的镜像、超时、资源）
   │                ▲
   │                │ environment.exec（命令、读写文件；不带任何环境变量）
   ▼                │
BaseAgentHarborAgent（宿主上的 Harbor 进程里）
   └─ run_agent 循环 + 模型调用（密钥只在宿主）
```

- 适配器：`baseagent.tbench.harbor_agent:BaseAgentHarborAgent`。`setup()` 什么都不装；循环在宿主，命令和文件操作全部经 `environment.exec` 进入任务容器。
- 后端：`baseagent.backends.ExecBackend`。工作目录取容器自己报告的 `pwd -P`（TB 2.0 是 `/app`），在宿主上是 `PurePosixPath`，不可能对它做本地 I/O。文件传输只依赖容器里的 POSIX `sh`、`base64`、`wc`、`mktemp`、`mv`、`ln`、`cp`、`rm`；停止进程需要 `/proc`、`grep`、`kill` 和可写的 `/tmp`（有 `setsid`、`timeout` 时会用上）。
- `/app` 下不写框架文件。写文件时的 base64 分块暂存在容器的 `$TMPDIR`（默认 `/tmp`），只在最后一步用同目录临时文件做原子改名，失败时在同一条命令里清掉。
- 密钥：从宿主环境变量（`BASEAGENT_LLM_*` 或 `DEEPSEEK_*`）读，从不传给 `environment.exec`。测试会把假密钥放进宿主环境，再确认容器命令、`env` 输出、元数据和日志里都没有它。
- 错误文本：provider 或环境的异常文字可能带着密钥。写进 `metadata.error`、`agent/baseagent-summary.json` 和抛给 Harbor 的异常（会进 `result.json` 的 `exception_message` / `exception_traceback`）之前一律脱敏：模型适配器自己持有的 key、宿主环境里名字含 KEY / TOKEN / SECRET / PASSWORD / CREDENTIAL 的变量值，以及 `sk-…`、`Bearer …` 形状。适配器抛出的异常都不带原始异常链（`from None`），所以 Harbor 记录的 traceback 里也没有原始异常的文字。
- `grep` / `glob` / `search_files` 仍然只支持本地后端；模型在容器里用 `run_command` 跑 `grep`。

## 超时

- 单条命令的超时上限默认仍是 60 秒（`Workspace(max_command_timeout=...)`，工具 schema 的 `maximum` 随之变化）。TB 运行用 agent 参数 `max_command_timeout_sec` 调高，`--plan` 里是 600。
- 无论上限多少，命令都不会超过题目剩余的墙钟时间：适配器把 `max_duration_seconds` 设为「agent 超时 − 15 秒余量 − 已用时间」，`Workspace` 本来就按回合剩余时间截断每条命令。
- agent 超时：Harbor 0.26.0 不把超时传给外部 agent。适配器按顺序取 agent 参数 `agent_timeout_sec`，再从本次 trial 目录的 `config.json` 和题目 `task.toml` 按 Harbor 0.26.0 的公式（`min(base, max) × multiplier`）算；都拿不到时元数据里 `deadline_source` 记为 `unknown`，只靠 Harbor 外层超时。
- 取消一次 `exec` 只是放弃了客户端：和 `docker exec` 一样，容器里的进程会继续跑。所以每个操作都经一个小启动器运行：
  - **打标记。** 启动器以 `BASEAGENT_LAUNCH=<本轮 id + 序号>` 的环境变量启动，它和它启动的一切进程从一开始就带着这个标记（`setsid` 改不掉它）。被启动的子进程有 `setsid` 时自成一个会话，并把自己的进程组 / 会话号记到容器的 `/tmp/baseagent-<本轮 id>.groups`（清空环境变量改不掉它）；**命令结束后记录也保留**，所以命令退出时留在后台的子进程以后还找得到。子进程先记录、再看停止标记，所以停止方「先写标记、再读记录」不会漏掉一个照样启动了的进程。
  - **停。** 停止是另一条 `exec`：先写停止标记（还没启动的操作看到就不再启动），再扫描 `/proc`：环境变量带标记、或进程组 / 会话是记录过的，就按 pid 杀掉（不依赖 `pgrep`、`ps` 或进程组版的 `kill` 语法），反复扫描直到一个都不剩（僵尸进程除外）才算停住。记录的会话号可能是被复用的旧 pid，所以停止命令自己所在的会话不按会话号匹配（带标记的照样匹配）。没有 `/proc` 可查、写不了标记、杀不掉，都报失败，**从不凭 `kill` 的返回值认定已经停干净**。进程消失也不等于是我们停掉的（它可能自己跑完了、途中还写了文件），所以：第一次杀之前，先用同样的方式杀停止脚本自己起的一个探针子进程（它本来要睡 20 秒），必须先在 `/proc` 里看到它活着、随后结束，且 `wait` 报告它是被信号杀死的，才算 `kill` 在这个容器里真的有效；每轮杀完后实际等待 `sleep 0.3`，再逐个读被杀进程的状态，还活着就失败；`kill` 报了「No such process」以外的错也立即失败。本轮里只要有一次停止失败，之后即使已经没有活进程，`agent_processes_stopped` 也记为 `false`。文件操作被停时先让它在约 3 秒内自然结束（避免留下半写的临时文件）。
  - **什么时候停。** 单条命令超时：停这一条命令启动的全部进程（含后台子进程）。整轮因墙钟到点（自己的截止或 Harbor 外层超时）、预算或基础设施失败而结束：停本轮启动过的**全部**进程，包括早已结束的命令留在后台的子进程，然后再交给 Harbor 评分。元数据 `agent_processes_stopped` 记为 `true`。
  - **确认不了就不评分。** 任何一次需要的停止确认不了，这一轮就按基础设施失败结束（`BaseAgentInfrastructureError`，不评分、奖励无效），不在可能还在写文件的进程旁边照常评分。
  - **正常结束不杀后台进程。** 模型自己说完成（或步数 / 权限停下）时，它特意留着的后台进程保持运行，元数据 `agent_processes_stopped` 为 `null`。TB 2.0 有题目要求留一个服务给测试连（例如测试去访问 agent 启动的服务器），一律杀掉会把这类题判错。
  - 容器里有 `timeout` 时它只作兜底，比宿主截止时间晚 5 秒。
- Harbor 外层超时取消 `run()` 时：先冻结已统计的用量，再置取消标记，然后在 Harbor 进入评分之前，从事件循环直接发出整轮停止命令。之后不会再发新的模型请求，后续文件和命令操作直接拒绝。写进 `AgentContext` 的 token 是取消那一刻的累计值；如果当时有一个模型请求在途，`in_flight_model_calls_at_stop` 记为 1（那次请求可能已计费但没统计）。

## 结果分类

适配器把停止原因写进 `agent_result.metadata.stop_reason`，并用异常类名告诉 Harbor 怎么处理：

| 情况 | 适配器 | Harbor 处理（`override_error_policy`） | 报告 |
| --- | --- | --- | --- |
| 正常结束 | 返回 | 评分 | reward 1 为通过，否则答错 |
| 步数 / 模型调用 / 工具调用用尽、循环保护 | 返回，`steps_exhausted` | 评分 | reward 1 为通过，否则步数用尽 |
| 自己的墙钟到了 / Harbor `AgentTimeoutError` | 返回 `timeout` / 被取消 | 评分（Harbor 默认） | reward 1 为通过，否则超时 |
| 等待审批而停 | 返回，`permission_blocked` | 评分 | reward 1 为通过，否则权限拦截 |
| CNY 预算用完 | 抛 `BudgetExceededError` | 评分、奖励无效、不重试 | 预算停止 |
| 环境、provider、harness 出错 | 抛 `BaseAgentInfrastructureError` | 不评分、奖励无效、不重试 | 基础设施失败 |
| Harbor / 环境自己的 `RuntimeError`（缺镜像、docker 失败） | — | 不评分、奖励无效、不重试 | 基础设施失败 |

导入时（`--import`），除 `AgentTimeoutError`、`BudgetExceededError` 和上下文 / 输出长度类异常外，任何异常类型都算基础设施失败，所以未知异常不会被算成答错；没有有效 reward 的 trial 也算基础设施失败。基础设施失败单列（`infrastructure_tasks`），不进成功率分母。预算停止进分母（算没通过），单独计数。

`BaseAgentInfrastructureError` 也包括 provider 错误（鉴权、网络）和模型输出格式错到内核直接失败的情况（例如同一轮重复的工具调用 ID）。后者其实是模型的问题，但它会被排除出分母；报告的 `exception_types` 和 `metadata.error` 里能看到原因。

## 命令

```bash
# 需要 harbor==0.26.0 的单独 Python 环境（不装进项目 .venv）；BaseAgent 用 PYTHONPATH=src 引入，
# 适配器只用到 Harbor 环境里已有的包（openai、jsonschema、python-dotenv 等）：
uv venv -p 3.12 .harbor-venv
uv pip install -p .harbor-venv harbor==0.26.0

# 只打印付费子集的 harbor run 命令和 job 配置，不执行：
python scripts/evaluate_terminal_bench.py --plan --budget-cny 15 --write-config terminal-bench-job.json

# 用 Harbor 自己的代码检查这份配置（dry-run 的全部检查，除了需要 Docker 的环境预检）：
PYTHONPATH=src .harbor-venv/bin/python scripts/check_harbor_plan.py --config terminal-bench-job.json
# 有 Docker 时再跑完整的：
PYTHONPATH=src .harbor-venv/bin/harbor run -c terminal-bench-job.json --dry-run

# 用 Harbor 给本地 fixture 评分（需要 Docker，不调模型）：
PATH=.harbor-venv/bin:$PATH PYTHONPATH=src python scripts/evaluate_terminal_bench.py --fixture

# 把 Harbor 的结果导入报告：
python scripts/evaluate_terminal_bench.py --import jobs/terminal-bench/<job>

# 报告管道冒烟（合成记录，不是 Terminal-Bench 分数）：
python scripts/evaluate_terminal_bench.py --synthetic
```

`--plan` 生成的配置：`timeout_multiplier` 1.0，不设任何超时倍数、超时上限、覆盖或资源字段；`n_concurrent_trials` 1、`n_attempts` 1、`max_retries` 0；数据集用子集文件里的 Harbor registry 提交 `7367e809` 和 18 个 `task_names`；每题预算是总预算的 1/18（向下取整到 0.0001 元），每题使用独立的 CNY 软上限，不提供跨 trial 共享账本。预算在派发前检查，在途请求可能超过每题份额及总预算，超出部分仍计费；当前保持单并发。

费用按人民币算（`metadata.cost_cny`，默认用 `baseagent.agent.workers` 里现有的计费单价，可用 `input_cny_per_million` / `output_cny_per_million` 改）。没有显式给 `usd_per_cny` 时 `cost_usd` 保持为空，不编汇率。

## 本地 fixture

`applications/project_assistant/evaluation/fixtures/terminal_bench/local-echo/` 是一个合法的 Harbor 任务：`task.toml`（`verifier.environment_mode = "shared"`，与 TB 2.0 一样在 agent 的容器里评分）、`instruction.md`、`environment/Dockerfile`（`ubuntu:24.04`，`WORKDIR /app`）、`solution/solve.sh`（官方 oracle 用）、`tests/test.sh`（只有 `/app/answer.txt` 恰好是 `ok` 时写 reward 1，否则写 0）。它不是 Terminal-Bench 的题。

`--fixture` 跑三个 Harbor job：官方 oracle（期望 1）、`--ak scripted=correct`（期望 1）、`--ak scripted=wrong`（期望 0），再从 Harbor 写的 `result.json` 导入。每次调用用新的 job 名（`fixture-<agent>-<时间>-<随机>`；同名 job 会被 Harbor 续跑，旧目录也不能冒充新结果），目录事先存在就不算；一个 job 只有同时满足下面这些才算通过：`harbor run` 退出码为 0；正好一个 trial；trial 记录的 agent 就是期望的那个；reward 是期望值且有效，导入后的分类正好是「通过」（期望 1）或「答错」（期望 0）——无效评分、基础设施失败都不算；trial 没有记录任何异常；token、`cost_cny`、`cost_usd`、`provider_calls` 全为 0 或空；脚本化 agent 自己的元数据还要写明是这个脚本化模型、`stop_reason` 为 `completed`。脚本化模型不连 provider、用量为 0。没有 Docker 时输出 `verified: false` 和原因，退出码 2，不声称通过。`harbor lint` 对这个任务会提示 `shared-verifier`（建议分开评分容器）；这是有意的，与 TB 2.0 一致。

## 升级 Harbor

适配器启动和预检时检查 `harbor` 版本，必须正好是 `0.26.0`（`baseagent/tbench/settings.py` 的 `HARBOR_VERSION`）。升级时改这个常量，然后重跑：

```bash
PYTHONPATH=src:tests:. .harbor-venv/bin/python -B -m unittest tests/test_tbench_harbor.py tests/test_tbench_runner.py tests/test_tbench_exec_backend.py
PYTHONPATH=src .harbor-venv/bin/python scripts/check_harbor_plan.py
PATH=.harbor-venv/bin:$PATH PYTHONPATH=src python scripts/evaluate_terminal_bench.py --fixture   # 有 Docker 时
```

`tests/test_tbench_harbor.py` 用 Harbor 自己的模型校验 job 配置和错误策略，并真实运行两次 `harbor run`（环境故意失败：缺镜像、运行中 exec 断开），确认导入后都是基础设施失败。没装 Harbor 的环境里这些测试跳过。

## 子集

固定子集是 Harbor registry `7367e809` 里 `terminal-bench@2.0` 按字母序的前 18 个 id，写在 `applications/project_assistant/evaluation/terminal_bench_subset.json`。`check_harbor_plan.py` 用 Harbor 解析这份 registry，确认解析出的任务正好是这 18 个、来自数据集提交 `69671fba`。15 到 20 题大约 10 到 15 元，全量大约 40 到 60 元，另一次授权。不要为了分数改题目。


## 保留的本地 Docker CLI 冒烟（与 Harbor 独立）

```bash
python scripts/evaluate_terminal_bench.py --fixture --backend docker
# 真 Docker 版本需要本机已有 python:3.12-slim；当前尚未实测：
python scripts/evaluate_terminal_bench.py --fixture --backend docker --real-docker
```

该路径保留 master 已有的真实 CLI → DockerBackend → 新容器验证链路，默认用 FakeDocker 与脚本模型，provider 调用 0；本地 echo 夹具单独保存在 `fixtures/terminal_bench/local-docker-echo/`。
它不使用 Harbor 的任务、执行环境或评分，不是 TB2 成绩。普通 `--fixture` 则是 Harbor 官方 oracle / scripted-correct / scripted-wrong 三个 job，真 Docker 下仍未验证。`--synthetic` 只检查合成报告，不冒充任何 fixture 成绩。

旧 `--live` 继续只打印本地 Docker 命令（`harbor_integration: unverified` 指该旧命令不是官方 Harbor 调用），不执行；Harbor 配置使用 `--plan`，评分结果使用 `--import`。两条入口均不授权付费执行。
