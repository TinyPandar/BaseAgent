# 主沟通 Agent + 后台并行 Worker（全双工）

无参数连续对话（`baseagent`，以及不带问题的 `python -m applications.project_assistant ask`）默认是主沟通 Agent。它只和人对话、分派任务，自己不读文件、不改文件、不跑命令。需要动手时，它派出后台 Worker，**不等 Worker 结束就继续对话**；Worker 的结果之后自动推送回对话。一次性 `baseagent "task"` 仍是原来的单 Agent 路径，不走双层。

## 职责

| 角色 | 做什么 | 不做什么 |
| --- | --- | --- |
| 主 Agent | 闲聊和澄清直接回答；需要仓库时调用 `delegate_task(task, allow_writes=false)`，立刻告诉用户派了谁；用 `message_worker(task_id, text)` 给运行中的 Worker 补要求；转述推送回来的结果 | 没有 `read_file` / `grep` / `glob` / `edit_file` / `write_file` / `run_command`；不等待、不编造 Worker 结果；Worker 成功后不重做同一件事 |
| Worker | 用入口已授权的工具完成任务，最后写一段汇报 | 不直接对用户说话；不能再派生 Worker（深度 1）；不会多出入口没有的工具 |

Worker 没有类型，区别只在本次是否允许写入：

- `allow_writes=false`（默认）：只拿入口工具里的只读子集（`retry_safe` 的工具）。即使开了 `--allow-command`，只读 Worker 也拿不到 `run_command`。
- `allow_writes=true`：只在用户明确要求改文件或跑命令时使用。Worker 拿到入口工具全集；写入仍要过 `--allow-write` / `--allow-command` 和 `--tool-policy`，要审批的调用照常停下等审批。
- 文件检查点：Worker 派出时记下主 Agent 当前的轮次号（`main_turn`），它用 `write_file` / `edit_file` 写的文件记在主会话这一轮的检查点里，并标出 `worker_id`；哪怕它在后面几轮才真正写。`/undo` 按主轮次撤销；有可写 Worker 在运行或等待审批时拒绝撤销。见 [checkpoints.md](checkpoints.md)。

主 Agent 的提示词约束模型这样做，这不是硬保证；硬保证是工具面：主 Agent 只有 `delegate_task` 和 `message_worker`，只读 Worker 没有写入工具。

## 一次派生的时序

```text
用户            对话线程(主 Agent)                 WorkerManager              Worker 线程 (会话 <sid>-w1)
 │ "查一下 X" ─▶ │ run_agent(主 Agent 一轮)          │                          │
 │               │──delegate_task(task)────────────▶│ 检查上限/单写者/预算       │
 │               │◀─{task_id:"w1",status:"running"}─│──启动线程────────────────▶│ run_agent(worker 会话)
 │◀─"已派出 w1"──│ 本轮结束，回到提示符               │                          │ 读文件、搜索……
 │ "你好" ──────▶│ 主 Agent 正常回答（不被阻塞）      │                          │ ……
 │◀─"你好！"─────│                                   │◀─结束：摘要/引用/路径──────│
 │               │ 两次提示之间打印推送：             │ 推送进队列                 
 │◀─"[w1 已完成] …"                                 │                            
 │ "结果呢" ────▶│ 下一轮用户消息前附上推送 JSON ──▶ 主 Agent 转述或据此再派任务
```

1. 主 Agent 调用 `delegate_task`。`WorkerManager` 检查并发规则和预算，分配 id（`w1`、`w2`……），为 Worker 建一个普通会话 `<主会话 id>-w<n>`（同一个 SQLite 库），然后在后台线程里对它跑 `run_agent`。工具调用立刻返回 `{task_id, status: "running", allow_writes, limits}`。
2. Worker 会话有自己的 turn、账本、spill 和上下文窗口，沿用入口的系统提示（加 Worker 汇报要求）、中间件、spill/compaction/guard 配置和 `--tool-policy`。Worker 不流式输出（流式片段会从后台线程打进输入行）。
3. Worker 结束、失败、被取消或停在审批时，`WorkerManager` 生成一条推送：现有摘要字段加 worker id —— `task_id`、`status`、`summary`（最多 4000 字符）、`summary_truncated`、`citations`、`paths`、`call_ids`、`spill_refs`；失败/取消时多一个 `detail`（例如 `max_steps_exceeded`、`shared_max_model_calls`）。不带工具原文；完整输出留在 Worker 会话的账本和 spill 文件里。
4. 推送有两个去处：
   - **终端**：两次提示之间打印 `[w1 已完成] 摘要…`、`引用:`、`路径:`；失败和取消用中文说明原因和能否重试（stderr）。
   - **主 Agent 上下文**：下一条用户消息发出时，前面附上
     ```text
     [worker 推送：系统自动附加的结果，不是用户输入]
     {"task_id": "w1", "status": "completed", "summary": "...", "citations": [...], ...}
     [用户消息]
     <用户原文>
     ```
     它是这一轮用户消息的一部分，按现有上下文规则（spill、Layer 2 压缩、窗口裁剪）进入主 Agent 窗口，没有新的上下文通道，也不改压缩默认值。推送本身不触发额外的付费模型调用。

## 并发规则

- `--max-workers N`（连续对话默认 2）：同时「活跃」（运行中或等待审批）的 Worker 上限。
- 只读 Worker 可以并行；同一时间最多一个 `allow_writes=true` 的 Worker。
- 超出时 **拒绝，不排队**：`delegate_task` 返回失败，消息以 `rejected: max_workers (2/2 active: w1, w2)` 或 `rejected: single_writer (w1 already holds write tools)` 开头，`data` 里有 `{"status": "rejected", "reason": ...}`。主 Agent 把原因告诉用户（等推送，或 `/cancel` 一个再问）。

**为什么拒绝而不排队**：排队意味着一个「已接受但没开始」的状态，要决定它何时用哪一轮的预算、退出时怎么处理、重启后要不要自动开跑、审批顺序如何——每一项都是新的隐式行为。拒绝只有一个结果，原因原样回到对话，用户和主 Agent 都看得见、能决定下一步。

## 预算共享

- **每一轮主 Agent 一份共享预算**（`TurnBudget`）：这一轮主 Agent 自己的调用，加上这一轮派出的所有 Worker，共用 `--max-model-calls`、`--max-steps` 和（可选）`--budget-cny` 的总额。连续对话默认 16 / 16，`--budget-cny` 默认不开。
- 计数在真正派发模型请求的边界（最内层中间件 `SharedBudgetMiddleware`）上原子加锁检查；并发下合计绝不超过上限。被节点自身上限拒绝、没有真正派发的调用会退还。
- 派出 Worker 时为主 Agent 的回复**预留 1 次**模型调用和步数：Worker 拿不到这一次，主 Agent 的下一次调用用掉预留。单个 Worker 的上限仍是 `min(12, 剩余 − 1)`，工具调用 `min(24, 主 Agent 本轮剩余工具额度)`；剩余不够时 `delegate_task` 直接失败，主 Agent 用已知信息回答。
- 下一条用户消息开新的一轮、新的一份预算；已经在跑的 Worker 继续用它出发那一轮的预算。续跑一个没完成的主 Agent 轮次时，用新的一份预算，计入主 Agent 这一轮已用的次数。
- `--budget-cny` 按模型报告的用量和 qwen-plus 价格（输入 0.8、输出 2 元/百万 token，与评测 metering 相同）累计，**派发前**检查是否已花完。花费只能事后得知，所以在额度被越过时已经在途的调用（最多每个并行者一次）仍可能计费；某次调用没报告用量时，后续调用一律拒绝。模型调用次数和步数是精确上限，CNY 不是。
- 共享额度用完的 Worker 记为 `failed`，`detail` 是 `shared_max_model_calls` / `shared_max_steps` / `shared_budget_cny`，中文说明会写清楚是「这一轮的共享额度」而不是 Worker 自己的上限。

## 对话命令

| 命令 | 作用 |
| --- | --- |
| `/status` | 会话用量，最近一轮的共享预算，以及**所有** Worker：id、状态（`running` / `awaiting_approval` / `completed` / `failed` / `cancelled`，后接中文）、已用时间、模型调用/步数/工具调用的已用/上限、是否可写、任务开头 |
| `/wait` | 阻塞到没有运行中的 Worker（Ctrl+C 只停止等待，不停 Worker），然后打印推送 |
| `/cancel <id>` / `/cancel all` | 取消；状态记为 `cancelled`。`run_command` 起的子进程通过取消令牌被整组杀掉并回收，不留孤儿进程 |
| `/tell <id> <text>` | 给运行中（或等待审批）的 Worker 补要求，在它下一次模型调用前注入为 `[主 Agent 转达的补充要求]`。已结束的 Worker 返回明确错误 |

主 Agent 的 `message_worker(task_id, text)` 与 `/tell` 走同一条路径。

取消的边界：Worker 在工具里（例如 `run_command`）会立即停；正在等一次模型 HTTP 响应时，要等这次调用返回后才停（适配器能感知取消的除外）。`/cancel` 最多等 10 秒，之后仍把状态记为 `cancelled`（`detail: cancel_requested`），线程收尾后不会再改回别的状态。

## 审批

- 任一 Worker 的待审批调用都显示在对话里：`Approval required: write_file`、`requested by: worker w2 (session demo-w2)`、任务开头、参数预览、call id，提示符是 `Allow write_file for worker w2? [y/N]`。
- 多条待审批按到达顺序**逐条**问；`y` / `N` 只作用于当前显示的那条（它记住的是这条的 Worker 会话、call id 和请求摘要），后面还有几条会提示。
- 停在审批的 Worker 不占线程，其它 Worker 和对话都不被它阻塞；审批只在两次提示之间提问，回答后那个 Worker 在后台继续。
- 审批提示处输入结束（EOF）时，这条保持待定；用同一 `--session` 再进对话会重新逐条询问。

## 终端不乱序（选择和理由）

推送和 Worker 的工具跟踪行**只在两次提示之间打印**：Worker 在后台产生的输出先进队列，下一次显示提示符前统一打印。真实终端上还多一步：如果提示符正显示且输入行是空的（readline 的行缓冲为空），推送直接打印在提示符上方并重画提示符；只要用户已经敲了字，就照样排队。

选这个而不是 prompt_toolkit：不加新依赖；行为可以离线测试（测试断言用户正在输入的那一行不会被插入）；排队规则一句话能讲清楚。代价是用户一直不按回车时，排队的推送要等下一次提示才出现（空回车或 `/wait` 即可刷新）。

## 线程，而不是 asyncio

整个执行栈是同步的：`run_agent`、模型适配器的阻塞调用、`run_process` 的子进程轮询、SQLite 检查点。用 asyncio 需要把这些全部改成协程或塞进 `to_thread`，结果仍是线程。所以直接每个 Worker 一个守护线程：

- 对话线程是主 Agent 会话的唯一写入者（主 Agent 的轮次只在它上面跑）；Worker 线程只写自己的会话，跨线程的共享状态只有 `WorkerManager` 的登记表（一把锁）和 `TurnBudget`（一把锁）。
- 取消用现有的 `CancellationToken`；`run_command` 已经在轮询它并整组杀进程。
- Ctrl+C 只到主线程：对话中的 Ctrl+C 取消当前主 Agent 轮次，不碰 Worker。

## 持久化、退出和重启

- Worker 登记表（id、会话、任务、是否可写、状态、detail、起止时间、用量、摘要字段）写在主会话的 `metadata["workers"]`：每轮结束、每次打印推送后和退出时由对话线程保存。Worker 会话本身也在同一个库里，元数据记着 `session_mode: worker`、`main_session`、`worker_id`。
- `/exit` 时还有 Worker 在运行：先问「输入 c 取消它们并退出，输入 w 继续等待（留在对话里）」。选退出就逐个取消并记为 `cancelled`。在提示符处按 Ctrl+C 退出同样逐个取消并记录。
- 输入结束（EOF，例如管道输入）时：等运行中的 Worker 跑完、打印推送再退出；等待中按 Ctrl+C 则取消它们。
- 用同一 `--session` 再进入：`/status` 显示每个 Worker 的最终状态；已经推送过的结果不会再推一遍；停在审批的 Worker 重新逐条询问，批准后在后台继续。上次进程异常退出时仍是 `running` 的 Worker 记为 `cancelled`（`detail: process_exited`），不会自动重跑。
- `--resume` 一个主 Agent 会话的未完成轮次：没有对话可推送，所以续跑后等所有 Worker 结束、打印推送再退出。

## 模式开关

- 新的连续对话默认开启主 Agent。`--no-main-agent` 回到自己执行工具的单 Agent 对话。
- 会话模式写在会话元数据 `metadata["session_mode"]`（`main-agent` / `single-agent`），`is_main_agent_session` 先看它；没有这个字段的旧会话回退到比对系统提示（当前提示和全双工之前的顺序版提示都算主 Agent）。已有会话保持创建时的模式，`--main-agent` / `--no-main-agent` 对它不生效并给出提示。一次性会话不写这个字段，行为不变。
- **一次性 `baseagent "task" --session <主 Agent 会话>` 被拒绝**，退出码 1，提示用 `baseagent --session <id>` 在对话里继续。理由：一次性路径是单 Agent，没有对话可以接收后台推送；如果按单 Agent 跑，会把主 Agent 会话混成另一种模式，下次对话的工具面和提示就对不上。拒绝最简单，也不改变一次性模式本身的行为。
- `--main-agent`、`--max-workers`、`--budget-cny` 只用于连续对话；和一次性提示一起用会报参数错误。`--main-agent` 和 `--subtasks-config` 互斥。
- `--subtasks-config` / `run_subtask(name, prompt)` 和同步的 `WorkerRuntime` 仍是库接口和显式调试入口，不是对话的产品路径。

## 成本

一次需要动手的请求至少是「主 Agent 分派 1 次 + 主 Agent 回一句 1 次 + Worker 若干次」，比单 Agent 多 2 次左右付费调用；推送本身不调用模型，主 Agent 只在用户下一条消息时转述。并行 Worker 各自付费，但这一轮的合计受共享预算约束。付费冒烟（qwen-plus/百炼）未跑，所有测试离线。

## 调试分类器

`route` 是白盒规则，只用于调试和演示，不参与对话决策。输出 `action`（`answer` / `delegate` / `clarify`）和 `writes`，不输出 Worker 类型：

```bash
python -m applications.project_assistant route "hi"                              # answer
python -m applications.project_assistant route "knowledge_search 怎么防越权"       # delegate, writes=false
python -m applications.project_assistant route "Fix sample.py and run the tests"   # delegate, writes=true
python -m applications.project_assistant route "能不能帮我处理一下这个文件"          # clarify
```

## 演示

```bash
python -B scripts/demo_duplex.py   # 全双工：并行两个只读问题、等待时聊天、/status、/cancel、推送
python -B scripts/demo_router.py   # 同步库接口 WorkerRuntime：子节点、只回摘要、写入停在审批
```

都离线，费用 0。面试演示脚本见 [demo.md](demo.md)。

## 实现位置

- `src/baseagent/agent/workers.py`：`WorkerManager`（`delegate_task` / `message_worker`、并发规则、线程、推送、取消、审批队列、登记表持久化）、`TurnBudget` 和 `SharedBudgetMiddleware`（共享预算）、`InboxMiddleware`（补要求注入）。
- `src/baseagent/agent/router.py`：`MAIN_AGENT_PROMPT`、`WORKER_PROMPT`、`worker_report`、`is_main_agent_session`、同步库入口 `WorkerRuntime`、调试用 `route`。
- `src/baseagent/cli_chat.py`：提示符之间的推送打印（`PromptLine`）、`/status` `/wait` `/cancel` `/tell`、逐条审批、退出确认。
- `src/baseagent/main.py`：`--max-workers`、`--budget-cny`、Worker 的 `run_agent` 组装、每轮共享预算、会话模式元数据、一次性提示拒绝主 Agent 会话。
