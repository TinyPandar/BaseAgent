# BaseAgent

白盒 agent 框架：单任务内核、可回放的工具账本、审批与预算，附带一个带引用的代码 RAG 应用（Project Assistant）。
目标是让一个编码 agent 的每一步都能被检查、回放和计量。

不需要密钥、费用 0 就能看全貌：

- 三分钟演示：[docs/demo.md](docs/demo.md)
- 离线总评测：`python scripts/evaluate_suite.py offline`
- 离线 harness 验收：`uv run baseagent --eval-harness`

<!-- BAS-2 side-dialogue: BEGIN -->
## 施工中 / 本分支需求：`/side` 侧边问答（BAS-2）

> **状态：仅需求占位，当前不能直接并行实现。** 必须先等 [插话收尾](https://github.com/TinyPandar/BaseAgent/tree/plan/inbox-finish) 验收合入，再以更新后的 master 为开发基线；另有下方唯一产品语义待 Frank 拍板。在依赖和该决定齐备前，不启动具体实现或自行补规格。

**共同起点：** `origin/master` `f78be3f8535c9b2788619b828b5c56ef5af4dc28`（2026-11-24 核对）。这是需求计划，不是功能完成声明；下方原有“已交付”仍描述共同 master。

**分工与费用：** 本分支当前只写 README 需求，不授权业务实现、合入或付费。后续由 Claude 用 pstack architect 先给至少两套结构不同的技术方案，Frank 拍板后再实现；这里不规定模块、接口或实现步骤。离线验收用假模型/脚本模型和临时本地命令，模型 API 费用为 0；任何真实模型/API 验证须 Frank 另行明确授权和预算。CNY 仍为软上限，不重置或抬高既有累计用量；不声称在途请求不会超支。需求文档变更不重绑语料版本，实现/合入时按既有流程处理。

### What / Why

仅落实 [ROADMAP 的 `/side` 原文](ROADMAP.md#内部消息类型与子-agent-并行)：用户直接触发，复制并截断主会话历史，只读/无工具，答案不回写主线，预算单独计。目的是执行主任务时能就已有上下文另问问题，而不是把问题当 steer 改变正在执行的任务；先用这个受限场景验证历史复制/截断，不做 Worker 上下文重构。

### 使用路径与已确认边界

- 用户通过现有 CLI/TUI/Web 连续对话里的 `/side` 意图另问问题；这是命令语义，不是 Web 侧栏、新 GUI 或额外 Worker 类型。具体呈现不在此规定。
- 侧边回答只使用被允许复制的主线历史与侧边问题，不读取发起之后主线新增内容，不把问题/答案作为普通输入、steer、queue、`/tell` 或隐式新任务送回主线。
- 侧边模型不执行工具，不借主线/Worker 的文件、命令、MCP 或委派权限执行操作；主线正在做什么保持不变。
- 预算/用量单独可识别；不能通过开侧边问答重置或抬高主任务/共享预算，不以“单独计量”为名绕过已有总预算护栏。本轮离线验收零模型费，真实问答另行授权。
- ROADMAP 没有定义回写/合并命令，因此**不提供隐式合并或自动采纳**；不能仅因为答案有建议就更改主任务。

### 唯一待 Frank 拍板的产品点

**`/side` 应看到主会话的哪一段历史：默认历史范围是什么，用户是否可以指定范围？** ROADMAP 只写“复制并截断”，没有可据以验收的具体视野规则。本分支不擅自选择最近 N 轮、全部历史或某个回合起点，也不把截断算法当成已拍板需求。以下 AC 只锁定已有明确边界；该点确认后才补齐范围对应的输入与预期用例、进入设计/开发。

### Done / 已明确部分的验收标准

- **AC01｜只能显式触发。** 只有用户明确发起 `/side` 才进入侧边问答；普通输入、`/queue`、`/tell` 仍按原语义处理，不因系统猜测自动分叉，也不默认多调用一次模型。
- **AC02｜历史快照边界。** 侧边只看到发起时点之前、按 Frank 最终确认范围选入的主线历史；复制不修改原历史，截断不制造无结果的工具调用或失去来源的工具结果，不包含发起后主线新增消息。**具体范围用例在上述决定前不标已完成。**
- **AC03｜无工具且只读。** 假模型尝试读写文件、执行命令、调用 MCP 或派生 Worker 时均不能实际执行；侧边问答不继承可执行权限，也不改主线工作区或任务状态。
- **AC04｜答案不回写。** 比较侧边问答前后的主任务历史、收件箱、Worker 指令与原任务目标：除主任务自身正常推进外，无新增侧边问题/答案/自动采纳指令；不暂停、取消或改派主任务。侧边输出在现有前端可明确与主线区分。
- **AC05｜计量隔离不等于预算逃逸。** 用假模型返回可核对的 usage，侧边用量独立可识别，主线已用额度不清零，既有共享/总预算限制不被绕过；侧边失败或超限不伪报回答成功，不因此取消主任务。
- **AC06｜不发明回主线功能。** 无自动合并、隐式 steer 或“回答完接着替用户改代码”；测试用含行动指令的侧边问题/答案证明它们不会自行成为主任务的新指令。
- **AC07｜三前端语义一致。** CLI/TUI/Web 现有对话入口都区分 `/side` 和普通/queue 消息；相同输入的上下文隔离、无工具、计量和不回写行为相同。无需开发 Web 侧栏或新 GUI；旧 Chainlit 不扩展。界面模拟覆盖与真实终端/浏览器证据分开标注。
- **AC08｜离线证据与依赖门槛。** 待依赖合入且历史范围决定补齐后，以脚本模型验证 AC01–AC07、主任务继续推进和原 Inbox/消息配对回归，运行专项与全量离线测试并记录结果；不以只有计划或未覆盖历史范围的测试宣称整条需求完成，真实 API 验证须另行授权。

### 依赖 / 不做

先完成 `plan/inbox-finish` 并合入、更新 base，再开发本分支；`job_wait` 不是本分支语义前置，二者依赖齐备后可分别设计/开发，最终仍需集成验收，不能据此保证没有代码冲突。不做 Worker `fork_turns: none/N/all`、单 Agent 默认、summary/失败账本、worktree 并行写、持久化多分叉管理、新工具、job_wait/job_input、Docker 后台、`/jobs`/`/kill` 或自动合并答案。其他未在 ROADMAP 中明确的侧边产品能力不自行补齐。
<!-- BAS-2 side-dialogue: END -->

## 状态

### 已交付（master，离线测试覆盖）

| 能力 | 要点 | 文档 |
| --- | --- | --- |
| 单任务内核与持久会话 | SQLite 检查点、工具账本（`pending → running → completed`）、中断后保守恢复与人工对账、步数 / 模型调用 / 工具调用 / token / 时长预算 | [参考手册](docs/reference.md) |
| 一次性任务与连续对话 | `baseagent "task"` 一次性；不带任务进入连续对话；`/status`、`/continue [N]`、`Ctrl+C` 只取消当前轮 | 下文「快速上手」 |
| 插话与统一收件箱 | 主 Agent 回复中可以继续输入：普通一行在下一次模型调用前送达本轮（只在整批工具调用都有结果之后），`/queue <text>` 排到下一轮；本轮结束时没送达的自动作为下一轮，输入不会丢。Worker 结果、`/tell`、后台任务通知走同一个收件箱；未送达的内容随会话保存。终端 CLI 在后台线程跑回合、继续读输入，审批时下一行就是回答（`y` 允许；`n`/回车拒绝；其它文字拒绝并作为插话交给 Agent）。管道输入保持逐行顺序执行 | [docs/inbox.md](docs/inbox.md) |
| 主 Agent + 后台 Worker | 连续对话默认开启；主 Agent 只对话和分派，Worker 在后台跑，结果自动推送；`--max-workers` 默认 2，同时只允许一个写入 Worker；`--no-main-agent` 回到单 Agent。主 Agent 和当轮 Worker 共用步数、模型调用和可选的 `--budget-cny`；续跑不清零，恢复不出已用额度则拒绝。CNY 是软上限，在途请求可能超支。 | [docs/router_worker.md](docs/router_worker.md#预算共享) |
| 三个对话前端 | `--ui cli\|tui\|web`，共用同一层会话和事件；Web 一个进程一个会话 | [docs/ui.md](docs/ui.md) |
| 权限与审批 | `--mode read-only\|edit\|full`；`full` 下命令类工具默认先询问，`--auto-approve` 关闭；`--tool-policy` 只能再收紧；审批绑定单次调用 | 下文「权限与审批」 |
| 命令级规则 | `--tool-policy` 条件规则支持 `argv_prefix`（如允许 `git status`、询问 `git push`、拒绝 `rm`）；可选 `"precedence": "strictest"`（deny > ask > allow）；放行 shell / 解释器前缀会告警。前缀规则只是弱黑名单，建议默认 `ask` / `deny` | [docs/command_rules.md](docs/command_rules.md) |
| 工具属性与关键路径保护 | 每个工具标注 `read_only` / `destructive` / `idempotent` / `open_world`，`--tool-policy` 可按属性设 `ask` / `deny`（如所有破坏性工具都要询问）；MCP 服务器自带的提示不放宽权限。内置的关键路径保护始终生效，拒绝删除磁盘根、工作区根、家目录、`.git`、`.baseagent` 的命令（argv 层面的尽力检查，不是沙箱） | [docs/tool_annotations.md](docs/tool_annotations.md) |
| OS 沙箱 | `--sandbox read-only\|workspace-write`（默认 `off`）：命令只能写工作区和临时目录，保护路径只读，默认断网（`--sandbox-network` 打开）；macOS `sandbox-exec`、Linux `bwrap`，**只做了单元测试，未在真机验证**；原生 Windows 不支持。不可用时退出码 3，不会退回不受限执行 | [docs/sandbox.md](docs/sandbox.md) |
| 后台任务 | `--background-jobs`（默认关闭，需 `--allow-command`）增加 `start_job` / `job_list` / `job_output` / `job_kill`；任务不随轮次结束或取消而终止；`run_command` 超时转为后台任务而不是被杀；重启后可查到以前的任务（防 PID 复用）；`keep_running=true` 的任务在退出后继续运行；可在 OS 沙箱里运行；仅 LocalBackend | [docs/background_jobs.md](docs/background_jobs.md) |
| 检查点与撤销 | 每轮一个文件快照；`/checkpoints`、`/undo`、`/rewind N`；只撤文件工具的写入，撤不回命令 | [docs/checkpoints.md](docs/checkpoints.md) |
| 多文件原子补丁 | `apply_patch` 一次最多 50 个文件，全有或全无，失败按快照回滚 | [docs/patch.md](docs/patch.md) |
| 机器可读输出 | 一次性任务和 `--resume` 支持 `--output-format json\|jsonl`，`schema_version` 1 | [docs/output.md](docs/output.md) |
| 项目规则文件 | 根目录 `AGENTS.md` 作为参考数据交给模型；单文件 20 KB、合计 40 KB，超出截断；不读 `CLAUDE.md`；`--no-project-rules` 关闭 | [参考手册](docs/reference.md) |
| MCP 客户端 | 只支持 stdio 和工具调用（`initialize` / `tools/list` / `tools/call`），默认关闭；有副作用的 MCP 工具每次审批；项目 `.mcp.json` 首次使用要确认信任 | [docs/mcp.md](docs/mcp.md) |
| 上下文管理 | 超大工具结果溢出到会话文件（默认开）；接近窗口上限时确定性摘要旧轮次，不调用模型 | [参考手册](docs/reference.md) |
| Watchdog | 任务级停滞 / 重复 / 预算 / 超时巡检，默认关闭 | [docs/watchdog.md](docs/watchdog.md) |
| HarnessKnight 适配层 | 可选的外部安全审查服务，默认关闭 | 下文「安全边界」 |
| Docker 沙箱 | CLI `--backend docker`：一次运行一个容器（对话的各轮和 Worker 共用），命令和文件读写都在容器里；默认断网，工作区挂到 `/workspace`。Docker 起不来时调用模型前退出码 3，不会退回 local。默认仍是 LocalBackend，它不是沙箱。离线测试用假 Docker，真实冒烟尚未跑。 | [docs/docker_backend.md](docs/docker_backend.md) |
| Harbor / Terminal-Bench 官方接入 | Harbor 0.26.0 外部 agent + ExecBackend；容器生命周期与评分由 Harbor 管理；固定 18 题 id。假环境与离线故障回归已验，真 Docker fixture、完整 dry-run 和真实成绩仍未验证。 | [docs/terminal_bench.md](docs/terminal_bench.md) |
| Project Assistant（代码 RAG） | 文档 / 代码入库、带 `path:start-end` 引用的检索；默认 vector + BM25 混合检索，rerank 默认关闭 | [应用说明](applications/project_assistant/README.md) |

### 只有离线管道，没有真实结果

| 项目 | 已有 | 没有 |
| --- | --- | --- |
| 流式输出、并行只读工具 | `--stream`、`--parallel-readonly-tools`，离线测试 | 付费冒烟 |
| Trigger（定时 / 文件变化唤醒 Worker） | 库 + `scripts/demo_triggers.py` | CLI 入口 |
| 生成答案质量 | 分层打分管道，用合成答案验证 | 真实模型 80 题结果 |
| Terminal-Bench | `--synthetic` 为合成报告；`--fixture` 走 Harbor；`--fixture --backend docker` 保留独立的本地 CLI 容器链路冒烟（默认假 Docker）。两条 fixture 的真 Docker 路径均未实测 | 真实子集成绩、真 Docker fixture 与完整 dry-run |

## 评测数据

| 项目 | 结果 | 样本与条件 | 来源 |
| --- | --- | --- | --- |
| 检索 hybrid MRR | 0.845（vector 0.678，BM25 0.759） | 80 题集的 test 划分，28 道可答题；参数在 tune 划分上选定；2026-10-21，当时的语料 | [报告](applications/project_assistant/evaluation/reports/bailian-80q-summary-2026-10-21.md) |
| 受控 coding 任务成功率 | 改动前 2/3 → 改动后 5/5 | 同一个夹具任务；qwen-plus 录像，改动前 3 次、改动后 5 次；2026-11-02 | [报告](docs/reports/m1_coding_comparison-20261102T111829Z-b21f9bdd6a3745a4b572a5e3dcf309e1.md) |
| 该任务平均总 token | 80780 → 36185.4（−55.2%） | 同上；改动后平均约 0.030 元/次 | 同上 |
| 离线 harness 场景 | 6/6 | 脚本化模型，测的是框架机制，不是模型能力 | `baseagent --eval-harness` |

检索各模式（test 划分，只计可答题）：

| 模式 | R@1 | R@5 | MRR |
| --- | ---: | ---: | ---: |
| vector | 0.378 | 0.710 | 0.678 |
| BM25 | 0.407 | 0.734 | 0.759 |
| hybrid（默认） | 0.567 | 0.782 | 0.845 |
| hybrid + qwen3-rerank | 0.583 | 0.770 | 0.857 |

真实 80 题生成质量、Terminal-Bench 真实子集，以及流式 / 并行 / 主 Agent 对话的付费冒烟都还没跑。付费命令要单独授权并带 `--budget-cny` 上限，见 [docs/eval.md](docs/eval.md)。

## 失败与取舍

- rerank 默认关闭：MRR 只高约 0.012，R@5 反而从 0.782 降到 0.770，每次查询多约 1.4 万 token。
- MRR 只说明找到了证据，不说明答对了；生成质量只验证过打分管道。
- coding 数字来自一个受控夹具任务的少量录像，不代表通用编码能力。
- LocalBackend（默认）不是沙箱。需要隔离时用 `--backend docker`：默认断网，工作区挂到 `/workspace`；它是容器不是虚拟机（共享内核），工作区是 bind mount，命令对工作区的改动 `/undo` 撤不回，MCP 服务器仍在宿主上跑。Docker 起不来不会退回 local，退出码 3。真实 Docker 冒烟尚未跑，见 [docs/docker_backend.md](docs/docker_backend.md)。
- `/undo` 只撤文件工具的写入，撤不回命令的副作用。
- `--budget-cny` 是软上限：一次已放行的请求返回后可能把总额推过上限，这部分照样计费；`/continue` 和 `--resume` 不会清零或自动提高金额上限。
- Watchdog 的停滞规则只把写入和验证命令算作进展，只读任务可能被提前停止（见「安全边界」）。

## 快速上手

### 1. 安装

需要 Python `>=3.12`。推荐 [uv](https://docs.astral.sh/uv/)（仓库含 `uv.lock`）：

```bash
git clone https://github.com/TinyPandar/BaseAgent.git && cd BaseAgent
uv sync                     # 核心依赖
uv sync --extra frontends   # 可选：TUI + Web 两个对话前端（也可分别 --extra tui / --extra web）
uv sync --extra ui          # 可选：Chainlit 浏览器界面
```

也可用 venv：`python3 -m venv .venv && .venv/bin/python -m pip install -e .`。Windows 下把 `.venv/bin/python` 换成 `.\.venv\Scripts\python.exe`。

### 2. 配置模型

在仓库根目录建 `.env`（已被 `.gitignore` 忽略）：

```dotenv
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
# DEEPSEEK_MODEL=deepseek-chat
```

也可以用 `BASEAGENT_LLM_BASE_URL`、`BASEAGENT_LLM_API_KEY`、`BASEAGENT_LLM_MODEL` 指定任意 OpenAI 兼容模型；设置任一 `BASEAGENT_LLM_*` 后不再混用 DEEPSEEK 凭据，`BASEAGENT_LLM_EXTRA_BODY` 可传 JSON 对象。

| 变量 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` | 使用 DeepSeek 配置时必需 |
| `DEEPSEEK_MODEL` | 默认 `deepseek-chat`；`--model` 优先 |
| `OPENAI_API_KEY` | RAG embedding 凭据回退项（`RAG_EMBEDDING_API_KEY` 优先）；`--mock-embedding` 时不需要 |
| `RAG_EMBEDDING_PROVIDER` / `RAG_EMBEDDING_MODEL` | 默认 `openai` / `text-embedding-3-small` |
| `RAG_INDEX_DIR` / `RAG_COLLECTION_NAME` | 默认 `.baseagent/rag_index` / `baseagent_docs` |
| `BASEAGENT_CASSETTE_MODE` | 测试用，默认 `replay` |

CLI 和 `python -m applications.project_assistant` 会加载 `.env`；`scripts/build_knowledge_base.py`、`scripts/query_knowledge_base.py` 只读进程环境变量。

Anthropic：`--provider anthropic`（或配置 `run.provider`），只读 `ANTHROPIC_API_KEY`（必需）、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`（默认 `claude-opus-5-5`），不会混用其它供应商的凭据；`--thinking` / `--effort` 只对它生效，思考块随会话保存并原样回传；`--stream` 暂不支持。**目前只有离线测试，真实 API 尚未验证**（`scripts/anthropic_smoke.py --yes-spend-money`，需要密钥，最多 3 次请求）。见 [docs/providers.md](docs/providers.md)。

### 3. 不需要密钥的命令

```bash
uv run baseagent --list-tool-packages
uv run baseagent --eval-harness          # 六个脚本化场景，临时工作区里跑
python -B scripts/demo_offline.py        # 假模型走真实 harness：流式、审批、恢复
python -B scripts/demo_duplex.py         # 假模型演示主 Agent + 两个 Worker
```

### 4. 一次性任务

```bash
uv run baseagent "Explain the project structure" --root .                    # 默认只读
uv run baseagent "Fix the failing test and verify it" --root . --mode full   # 允许写文件和跑命令；命令先询问，一次性任务会停在第一条命令
uv run baseagent "Fix the failing test and verify it" --root . --mode full --auto-approve   # 命令不询问（以前的行为）
uv run baseagent "Explain the project structure" --root . --output-format json
uv run baseagent "Fix the failing test" --root . --mode full --auto-approve --backend docker
# 命令和文件读写在容器里，默认断网；先 docker pull python:3.12-slim
# Docker 不在或缺镜像时退出码 3，不会退回 local
```

默认上限：8 步、8 次模型调用、32 次工具调用。撞上限后用 `baseagent --session ID --resume --max-steps 32 --max-model-calls 32` 接着做，已用计数不清零。会话默认存在 `.baseagent/sessions.sqlite3`（`--db` 可改）。

### 5. 连续对话

```bash
uv run baseagent --root . --mode edit                          # 终端逐行对话
uv run --extra tui baseagent --ui tui --root . --session demo  # 终端界面
uv run --extra web baseagent --ui web --root . --session demo  # http://127.0.0.1:8765/，非本机地址必须带 --token
```

- 默认上限放宽到 16 步、16 次模型调用，剩 2 步时提示模型收尾；显式参数或 `--config` 优先。
- 撞上限时输入 `/continue [N]` 在对话里提高本轮步数和模型调用上限，已用计数不清零、已完成的工具调用不重做；`--budget-cny` 不会因此清零或提高。
- 常用命令：`/status`、`/wait`、`/cancel <id|all>`、`/tell <id> <text>`、`/checkpoints`、`/undo`、`/rewind N`、`/exit`。
- 默认是主 Agent 模式：主 Agent 不读写文件、不跑命令，需要动手时派出 Worker；Worker 默认只拿只读工具，用户明确要求修改时才允许写入。主 Agent 这一轮和它派出的 Worker 共用步数、模型调用和可选的 `--budget-cny`；续跑沿用已花金额。完整规则见 [参考手册](docs/reference.md#共享预算) 和 [预算共享](docs/router_worker.md#预算共享)。
- `--session` 可以在三个前端之间接着用；连续对话也可以加 `--backend docker`。

完整行为（Worker 推送、检查点、MCP 首次信任、`AGENTS.md`、模型重试、时间戳约定）见 [参考手册](docs/reference.md)。

### 6. Project Assistant 离线演示

```bash
.venv/bin/python -B scripts/demo_knowledge_search.py
.venv/bin/python -B scripts/build_knowledge_base.py --mock-embedding
.venv/bin/python -B scripts/query_knowledge_base.py "kernel architecture" --mock-embedding
```

mock embedding 只验证流程，不代表检索质量。

## 权限与审批

`--mode` 是现有权限标志的简写，用于一次性任务、`--resume` 和三个对话前端；不传时等同 `read-only`。

| `--mode` | 等价标志 | 模型能看到的写 / 命令工具 |
| --- | --- | --- |
| `read-only` | （默认） | 无；读、`grep` / `glob`、`git_diff`、`git_status`、计划和记忆工具照常可用 |
| `edit` | `--allow-write` | `write_file`、`edit_file`、`apply_patch` |
| `full` | `--allow-write --allow-command`，外加"命令先询问" | 再加 `run_command`、`verify_command`；**这些命令类工具（同时是 destructive 和 open_world 的工具，含 `start_job` 和非只读 MCP 工具）默认先询问**，文件工具、`job_kill` 仍自动执行；`--auto-approve` 恢复不询问。LocalBackend 下 stderr 警告命令不在沙箱里，`/undo` 撤不回命令造成的改动；`--backend docker` 下改为说明命令在容器里、网络是否开启 |

- 未开启的工具不出现在发给模型的工具列表里，硬调用也会返回 `permission_denied`。`publish_memory` / `withdraw_memory` 另由 `--allow-memory-publish` 控制。
- 显式的 `--allow-write` / `--no-allow-write` 等标志与 `--mode` 矛盾时报错（退出码 2）。
- **`--mode full` 的命令默认询问**（见上表），一次性任务停在审批时会打印可直接复制的批准命令，JSON 输出带 `approval_hint`；`--tool-policy` 里对某个命令工具显式 `allow`（或命中的 `argv_prefix` 允许规则）会免去询问，`deny` 始终生效。**注意：** 显式写 `--allow-write --allow-command` 而不用 `--mode full` 时**不会**默认询问。改动前开始、尚未完成的 full 模式回合续跑时会提示配置已变化：加 `--auto-approve` 照旧，或 `--accept-config-changes` 接受新规则。见 [docs/tool_annotations.md](docs/tool_annotations.md)。
- **CLI / TUI / Web 下写文件默认不审批。** 要逐次批准，用 `--tool-policy` 把对应工具设为 `ask`；策略按工具名精确匹配，要拦住全部写入必须同时列出 `write_file`、`edit_file`、`apply_patch`。`--tool-policy` 只能收紧，写 `allow` 打不开 `--mode` 没给的能力。`--backend docker` 下 `--mode` 和 `--tool-policy` 的含义不变，docker 不放宽任何权限，只是命令和文件操作在容器里执行，见 [docs/docker_backend.md](docs/docker_backend.md)。
- 审批绑定调用 ID、轮次、工具名和参数原文、策略指纹；批准不会授予工作区没开的能力。
- **例外：Chainlit 界面**（`uv run --extra ui baseagent-ui`，默认端口 8000）启动时自动生成策略，写入、编辑、`apply_patch` 和命令每次都要点「允许」。它每条消息跑一次性 CLI，没有主 Agent / Worker 和 `/continue`，也不能传 `--mode`、`--tool-policy`、`--watchdog`、`--harness-url`。

## 机器可读输出

一次性任务和 `--resume` 可用 `--output-format json`（结束时 stdout 只有一个对象：`status`、`final_answer`、`error`、`session_id`、`usage`、`exit_code`、`pending_approval`、`schema_version`）或 `jsonl`（每行一个元数据事件，最后一行 `type: "result"`）。日志、trace 和 Worker 输出都走 stderr。退出码：`0` 本轮完成，`1` 未完成或会话错误，`2` 参数错误，`130` 被中断，`3` 是 `--backend docker` 起不来（调用模型之前，不会退回 local）。`--backend docker` 时结果多一个 `backend` 对象（jsonl 另有一行 `backend_ready`），没有这个键就是 local。对话模式和其他运维操作带这个参数会报错。字段和版本规则见 [docs/output.md](docs/output.md)。

```bash
baseagent --root . --mode edit --output-format json "Rename the helper"
baseagent --session S --resume --output-format jsonl | jq -c 'select(.type == "result") | .data.status'
```

## 安全边界

- **LocalBackend（默认）不是沙箱**：开了 `--allow-command`，命令就有当前系统账户的全部文件和网络权限。只在可信的任务和工作区里开启。
- **`--backend docker`**：一次运行一个容器，默认断网，工作区挂到 `/workspace`。Docker 起不来时退出码 3，不会退回 local。容器与宿主共享内核；工作区是 bind mount，命令改动 `/undo` 撤不回；MCP 服务器仍在宿主上跑。真实冒烟尚未跑。见 [docs/docker_backend.md](docs/docker_backend.md)。

| 组件 | 管什么 | 时机 | 默认 |
| --- | --- | --- | --- |
| Watchdog（内置） | 跨步骤的停滞、重复、预算、超时 | 先给提示，超限后以 `aborted_by_watchdog` 结束 | 关闭；`--watchdog` 打开，阈值见 [docs/watchdog.md](docs/watchdog.md) |
| HarnessKnight（外部服务，可选） | 单次提示 / 工具调用的安全判定 ALLOW / REVIEW / BLOCK | 工具进入账本之前；REVIEW 转为逐次审批；提示被 BLOCK 时不发模型请求 | 关闭；`--harness-url URL` 打开，`--harness-fail-mode` 默认 `closed`（服务不可用即拦截），`--harness-timeout` 默认 3 秒；需要鉴权时设 `HARNESS_API_TOKEN` |

两者职责不同、互不依赖：Watchdog 只读取 HarnessKnight 留下的决策计数，没有 HarnessKnight 也能用。

作用范围：两者都作用于一次性任务、连续对话的主 Agent 和它派出的每个 Worker（Worker 继承入口的中间件、审批策略和 Watchdog 设置）；这条路径目前没有专门测试。

- Watchdog 按会话计数：每个 Worker 的 `--watchdog-max-*` 单独计算，不是整轮总额。
- 停滞规则只把写入（`write_file` / `edit_file` / `apply_patch`）和 `verify_command` 算作进展；只读 Worker 和主 Agent 连续 5 步没有这两类调用会被停止。只读任务请设 `--watchdog-stall-abort-steps 0`。
- HarnessKnight 对 Worker 的调用判 REVIEW 时，Worker 进入等待审批，在对话里逐条确认。

## 库接口

内核 `iter_agent(model, state, *, tools, ...)` 只依赖标准库和 `jsonschema`；`run_agent(...)` 在其上提供持久化、审批、预算和完成门槛。模型适配器有 `OpenAICompatModel` 和 `AnthropicModel`，`CassetteModel` 提供严格的录制 / 回放。

```python
from baseagent.agent import KernelState, iter_agent
from baseagent.kernel import ToolRegistry

class Model:
    def complete(self, messages, tools):
        return {"content": "done"}

state = KernelState(messages=[{"role": "user", "content": "Say done."}])
for event in iter_agent(Model(), state, tools=ToolRegistry()):
    print(event.type)
print(state.status, state.final_answer)
```

适配器、重试与限流、录制回放、子任务、事件日志、取消、完成门槛、会话记忆、工具策略规则和 `WorkspaceBackend` 协议见 [参考手册](docs/reference.md)；模块地图和路线见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 文档索引

| 主题 | 文档 |
| --- | --- |
| 完整行为契约、默认值与限制 | [docs/reference.md](docs/reference.md) |
| 主 Agent 与 Worker | [docs/router_worker.md](docs/router_worker.md) |
| TUI / Web 前端 | [docs/ui.md](docs/ui.md) |
| 检查点与撤销 | [docs/checkpoints.md](docs/checkpoints.md) |
| `apply_patch` | [docs/patch.md](docs/patch.md) |
| JSON / JSONL 输出 | [docs/output.md](docs/output.md) |
| MCP | [docs/mcp.md](docs/mcp.md) |
| 流式与并行 | [docs/async_react_streaming.md](docs/async_react_streaming.md) |
| Watchdog | [docs/watchdog.md](docs/watchdog.md) |
| Trigger | [docs/triggers.md](docs/triggers.md) |
| Docker 沙箱 / `--backend docker` | [docs/docker_backend.md](docs/docker_backend.md) |
| 评测入口 | [docs/eval.md](docs/eval.md) |
| Terminal-Bench | [docs/terminal_bench.md](docs/terminal_bench.md) |
| Project Assistant | [applications/project_assistant/README.md](applications/project_assistant/README.md) |
| 架构与路线 | [ARCHITECTURE.md](ARCHITECTURE.md) |

## 运行测试

测试不需要密钥，不联网。全量用 pytest（`uv sync` 默认装上 dev 依赖组里的 pytest）：有 9 个模块用了 pytest 的 fixture，`unittest` 加载不了它们；其余用 `unittest` 写的模块 pytest 也照常运行。

```bash
.venv/bin/python -B -m pytest tests -q                                     # 全量
.venv/bin/python -B -m unittest discover -s tests -p "test_kernel*.py" -v  # 内核
.venv/bin/python -B -m unittest discover -s tests -p "test_rag*.py" -v     # RAG
```

`unittest discover` 仍能跑用 `unittest` 写的模块，但会把那 9 个 pytest 模块报成导入失败，不能当全量。两种跑法都不会写真实的 MCP 信任库（`tests/config_isolation.py`）。
