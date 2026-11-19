# ROADMAP

> **状态：草稿，待确认。**（2026-11-19 已按讨论更新：加入消息类型、插话、`/side`、Worker 结构调整；同日完成消息类型第一步、命令级规则、后台任务。）这是未来规划的单一入口，只放"接下来做什么"。
> 架构现状见 [ARCHITECTURE.md](ARCHITECTURE.md)，历史证据见 [HARNESS_AUDIT.md](HARNESS_AUDIT.md)，
> Project Assistant 的详细计划见 [PHASE3_PLAN.md](PHASE3_PLAN.md)，对标框架对照见 [docs/reference-frameworks.md](docs/reference-frameworks.md)。本文件不替代它们，只做索引和排序。
> 每条后面的链接指向细节出处；更新状态时改这里，不要再往 ARCHITECTURE.md 追加流水账。

## 一、进行中（Project Assistant）

当前应用：面向个人开发者的知识与项目助理。Phase 1/2（RAG 基础设施、`knowledge_search` 工具包）已交付。

| 优先级 | 事项 | 说明 | 出处 |
| --- | --- | --- | --- |
| P0 | 真实 embedding 检索评测 | 目前只有 Mock embedding，没有检索质量证据；测 Recall@k、MRR | [PHASE3_PLAN](PHASE3_PLAN.md) §1 |
| P1 | Project Assistant CLI 入口 | 目前只能用 Python 手工装配；默认 CLI 的 `--tool-package` 尚不含它 | [PHASE3_PLAN](PHASE3_PLAN.md) §2 |
| P1 | 受控 coding 验收场景 | 检索 + 受控修改的端到端闭环，项目展示的核心 | [PHASE3_PLAN](PHASE3_PLAN.md) §3 |
| P2 | git 历史进度恢复 | 需新增语料来源（目前 `.git` 被排除），范围最大 | [PHASE3_PLAN](PHASE3_PLAN.md) §4 |

## 二、下一步（框架补强，建议近期做）

**总原则：先把单 Agent 做扎实，再决定要不要主 Agent / Worker 这一层。** 当前对话默认是"主沟通 Agent + 后台 Worker"，主 Agent 自己不读不写、运行中拒收用户输入，引入偏早（见下方"主 Agent / Worker 结构整理"）。建议顺序：

| 顺序 | 事项 | 理由 |
| --- | --- | --- |
| 1 | 内部消息类型（含 tool_use/tool_result 不变式） | 后面几项都依赖它。**第一步已完成（2026-11-19）**：类型与不变式检查已加，尚未接入内核 |
| 2 | 单 Agent 可打断 + 插话的安全注入 | 现在运行中输入直接被拒绝（`ChatSession.send` 抢不到锁就报错） |
| 3 | `/side` 侧边对话 | fork 最佳场景，同时验证"复制并截断历史" |
| 4 | 权限补强（命令级规则优先） | 并行写和更多自动化之前必须先做。**命令级规则与 deny 审计已完成（2026-11-19）**；剩 OS 沙箱、分类器、`full` 默认更安全 |
| 5 | Anthropic 接入 CLI / 真实验证 | 用到消息类型 |
| 6 | 对话默认改回单 Agent，Worker 变为可选并行能力；再做 summary 模式、失败账本、worktree 并行写 | 基础补齐后再回头 |

### 权限与安全（现状偏薄）

> 对标见 [docs/reference-frameworks.md](docs/reference-frameworks.md)。目标是**三层**：规则（deny 在任何模式都生效）→ OS 沙箱（不可用时 fail closed）→ 可选分类器（规则优先，分类器兜底；连续拦截退回人工；分类器失败即拒绝）。子 agent 继承父级实时权限，不能靠派生绕开。

现有：`--mode`（read-only / edit / full）、`ToolPolicy`（allow/deny/ask + 条件规则）、参数绑定的逐次审批、Docker 后端。缺口：

| 事项 | 说明 |
| --- | --- |
| ~~命令级规则~~ ✅ 2026-11-19 | 已加 `argv_prefix` 与可选 `"precedence": "strictest"`；放行 shell/解释器前缀会告警。见 [docs/command_rules.md](docs/command_rules.md)。剩余局限：前缀规则是弱黑名单（`/bin/rm`、`git clean` 绕得过），规则按工具名配置，新命令工具需单独列出。 |
| 本地后端 OS 沙箱 | LocalBackend 不是沙箱；考虑 macOS seatbelt / Linux bwrap |
| `full` 模式默认更安全 | 现在 CLI/TUI/Web 下写文件默认不审批；考虑默认审批，或默认走 Docker |
| 内置 LLM 分类器（auto 模式） | 现仅有可选外部服务 HarnessKnight（默认关闭）。参照 Claude Code `auto`：规则 → 内置自动放行 → 分类器；默认拦 `curl \| bash`、force push、生产部署；连续 3 次/累计 20 次拦截退回人工；失败/无裁决即拒绝 |
| deny 规则全模式生效 | ✅ 2026-11-19 审计：`--mode full`、审批、Gateway、Intersection（Worker/子任务）、EditToolPolicy、HarnessKnight 均无法把 deny 变成 allow，已补测试（`tests/test_command_rules.py`）。**剩余：** 关键路径 `rm` 的内置始终拦截尚未做 |

### 内部消息类型与子 agent 并行

| 事项 | 说明 |
| --- | --- |
| 内部消息类型 | ✅ **第一步已完成（2026-11-19）**：`kernel/messages.py` 已加（`Message`/`TextPart`/`ToolCall`/`ToolReply`，仓库 3764 条夹具消息逐字节往返一致；`check_tool_pairing` 可检出"孤儿调用后跟 user"），见 [docs/messages.md](docs/messages.md)。**下一步：** 在 `kernel/agent.py` 模型调用前接入配对检查，用 `unanswered_tool_calls` 替换 `_pending_calls`，恢复路径（`session/store.py`、`session/task_recovery.py`）补检查。原计划：新增 `kernel/messages.py`：`Message` / `TextPart` / `ToolCall` / `ToolResult` 等 frozen dataclass，`to_dict`/`from_dict` 与现有 dict 逐字节一致；先只做类型 + 测试，再逐文件迁移。排在 Anthropic 接入 CLI 之前。**待决：** 以消息为主，还是像 dsh 那样以追加式事件日志为主、消息只是投影（压缩只改视图、保留原始事实）。 |
| 并行写 Worker（worktree 方案） | 每个写 Worker 一个 git worktree + 分支，单写者规则改为"每 worktree 单写"；结束产出分支，不自动合并。**依赖上面的权限补强。** |
| Worker 上下文模式 | 现在只有"新建"（只带任务字符串）。参照 Codex `fork_turns: none / N / all`，改成连续旋钮（默认 none，常用"带最近 N 轮"）；默认配自动"失败账本/死胡同"；`all` 后置——要便宜需与父 agent 共享相同系统提示和工具列表（缓存前缀），与"子 agent 工具更少"冲突，Claude Code 的 fork 即继承完全相同的工具池。 |
| 侧边对话 `/side`（fork 历史） | 用户直接触发，复制并截断主会话历史，只读/无工具，答案不回写主线，预算单独计。fork 的最佳场景；先做它来验证"复制并截断历史"。排在消息类型之后。 |
| 插话/引导的安全注入 | **两种语义都支持**：注入当前轮（下一个思考步，参照 Codex Enter）与排队到下一轮（参照 Codex Tab / Claude Code 默认）；steering 与取消作为实时控制事件，与持久事实分开（参照 dsh）。插话先入队，只在安全边界注入（所有 tool_use 已有 tool_result、或回合结束、或新一轮开始前），与 tool_result 合并为同一条 user 消息，文本在后并标注来源；等审批/被中断时先补合成 tool_result。内核加不变式：每个 `tool_use` 在下一次模型调用前必须有对应 `tool_result`（现在 `_pending_calls` 遇 user 会 break，孤儿调用不会被补）。 |
| 长任务与中断语义 | ✅ **①的主体已完成（2026-11-19）**：`--background-jobs` 增加 `start_job`/`job_list`/`job_output`/`job_kill`，任务不随轮次结束或取消而终止，完成后在下次模型调用前通知；仅 LocalBackend，见 [docs/background_jobs.md](docs/background_jobs.md)。**剩余：** `run_command` 超时转后台、重启后恢复任务、Docker 支持、②③④。原计划：① 长命令（如模型训练）做成后台作业：`job_list` / `job_output` / `job_kill`（命名参照 dsh），进程生命周期与对话轮次解耦，用户插话不会杀进程；**命令超时转入后台而不是杀掉**（参照 Claude Code，`sleep` 除外）；完成通知可唤醒空闲 owner 或进入下一步 inbox。现在 `run_command` 是同步、默认 20 秒超时、取消即杀子进程，不适合训练。② 区分三种用户意图：补充信息（注入）、暂停/等待（不动进程）、终止（需明确指令，杀有副作用的进程前先确认）。③ 默认开启 Watchdog 的 stall/repeat 规则；给模型 `ask_user` / `give_up(reason)` 工具，遇阻塞主动停下问人。④ 同一命令连续失败后提示换思路（接失败账本）。 |
| 主 Agent / Worker 结构整理 | 当前"主沟通 Agent + 后台 Worker"引入偏早：主 Agent 只调度、简单问题也要走派发/推送/转述；运行中拒收输入；Worker 只带任务字符串、只能单写者。**方向：对话默认改回单 Agent（能直接读写、可随时打断），Worker 降级为单 Agent 按需派生的可选并行能力。** 一次性 `baseagent "task"` 已是单 Agent，不受影响。待上面 1–4 完成后实施。 |

### 模型适配

| 事项 | 说明 | 出处 |
| --- | --- | --- |
| Anthropic 适配器接入 CLI / config | 目前只能在库里用，CLI 不读 `ANTHROPIC_API_KEY` | [ARCHITECTURE](ARCHITECTURE.md) 路线表第 3 行 |
| Anthropic 真实 API 验证 | 现有测试用脚本模型，真实验证未补齐 | [HARNESS_AUDIT](HARNESS_AUDIT.md) |

### 框架验收收尾

| 事项 | 说明 | 出处 |
| --- | --- | --- |
| 内核独立性真实验证 | 仓库外最小包树 + 干净 venv 只装 jsonschema，验证导入来源 | [ARCHITECTURE](ARCHITECTURE.md) 路线表第 1 行 |
| 打包与新项目起手式 | 公共 API、工具/配置/测试模板，第二个真实 agent 迁移验证 | 路线表第 4 行 |
| 统一库级 `build_agent(config)` | 核对是否需要补齐；无实际需求不新增 YAML 依赖 | 路线表第 5 行 |

## 三、后置 / 候选（按需要再做，不是前置条件）

| 事项 | 说明 | 出处 |
| --- | --- | --- |
| 最薄常驻 runtime | SQLite TaskStore + 状态机 + 单 worker；submit/list/status/cancel | [ARCHITECTURE](ARCHITECTURE.md) 路线表第 6 行 |
| 模型最大在途并发控制 | 现在令牌桶只限速率，不限在途请求数 | [DEVELOPMENT_HANDOFF](DEVELOPMENT_HANDOFF.md) §5 |
| Redis / 消息队列 / 统一容器 / 分布式执行 | 均未实现 | 同上 |
| 检索优化 | 混合检索、重排、chunk 预算（应用 README 列出） | [DEVELOPMENT_HANDOFF](DEVELOPMENT_HANDOFF.md) §4 |
| LLM Router、并行子任务、插件市场 | 按需要引入 | [ARCHITECTURE](ARCHITECTURE.md) 分层约束 |

## 四、Personal Assistant 阶段（远期）

应用范围见 [ARCHITECTURE.md](ARCHITECTURE.md)「个人 assistant 的应用范围」：科研检索、多项目看板、日程提醒。CLI 优先，网页和消息入口后置。

### 记忆系统（决定：留到这一阶段再做，不在 kernel 里预埋）

- **前提：** 依赖常驻 runtime 和跨会话的真实长期数据。
- **现有地基：** 共享记忆（`publish_memory` / `withdraw_memory` / `search_shared_memory`，发布需 `--allow-memory-publish`）、会话库、上下文压缩与溢出。
- **实现位置：** 做成可插拔能力模块，经 `capabilities` 装配，不改 kernel。
- **要设计的内容：**
  1. 分层：工作记忆（当前上下文）、会话记忆、长期事实与偏好、情景记忆
  2. 写入策略：自动提取 + 用户确认；过期、合并、冲突处理
  3. 检索：混合检索 + 重排，控制注入上下文的预算；长程记忆优化
  4. 隐私与权限：哪些记忆可跨任务、跨 Worker 共享，沿用"显式发布"思路

## 维护约定

- 完成一项就从上表移走，并在 `docs/reports/` 或提交信息里留证据，不在本文件写流水账。
- 新增想法先放"后置 / 候选"，决定要做再提升优先级。
- 与其他文档冲突时，以本文件的状态为准，并回头修正那份文档。
