# 收件箱：回合进行中插话、排队，以及外部输入的统一送达

外部输入以前走四条互不相干的路：用户的下一行（主 Agent 回复时被拒绝）、Worker 结果（粘在用户下一条消息前面）、`/tell` / `message_worker`（Worker 的 `InboxMiddleware`）、后台任务结束（`JobNoticeMiddleware`）。现在它们都先进同一个**收件箱**（`baseagent/agent/inbox.py`，harness 层，不在 kernel 里），再按同一条规则送到 Agent。

## 数据模型

一条收件箱消息是 `InboxItem`：

| 字段 | 含义 |
| --- | --- |
| `id` | `m` + 12 位十六进制，只用来去重和在界面上对应「已排队 / 已送达」 |
| `source` | `user`（用户本人）、`worker`（Worker 结果推给主 Agent）、`job`（后台任务结束）、`lead`（`/tell` 或 `message_worker` 给 Worker 的补充要求） |
| `mode` | `steer`：送进**正在进行的这一轮**；`queue`：这一轮结束后**作为新的一轮**发送 |
| `text` | 正文（Worker 结果是那条推送 JSON，任务通知是那几行说明） |
| `created_at` | 进收件箱的时间（秒） |

每个 Agent 一个收件箱：主 Agent（或单 Agent 对话）一个，由 `main.py` 建好后交给 `ChatSession`、`WorkerManager` 和主 Agent 的 `InboxMiddleware`；每个 Worker 一个（`WorkerRecord.inbox`）。`Inbox` 是线程安全的，任何线程都可以 `put`。

## 送达规则

只有一个送达点：`InboxMiddleware.before_model`，也就是**下一次模型调用之前**，并且上一批工具调用**每一个都已有结果**（`_tool_batch_open` 为假）。这时：

1. 若配了后台任务（`--background-jobs`），先把本会话刚结束的任务通知放进收件箱（source `job`）；
2. 取出**所有** `steer` 消息（不分来源），合成**一条** user 消息追加到对话末尾；
3. 把还没送达的消息（包括 `queue`）同步到 `state.metadata["inbox"]`。

合成的消息按来源分段，每段沿用原来的标题，模型能分清哪段是用户本人说的、哪段是系统附加的：

```text
[主 Agent 转达的补充要求]          ← lead（只出现在 Worker 的消息里）
...
[worker 推送：系统自动附加的结果，不是用户输入]
{"task_id": "w1", "status": "completed", ...}
[后台任务通知 / background job notice]
- j0000002a exited (exit code 0, ...): make test
Use job_output to read the log.
[用户在本轮进行中补充的消息（用户本人输入）]
先别改测试，只修实现。
```

为什么这样定：

- **绝不插在工具调用和它的结果之间。** kernel 在每次请求前检查调用/结果配对（[messages.md](messages.md)），中间插一条消息会让这一轮以 `needs_recovery` 停下。`before_model` 只在整批工具都返回后才运行，另外仍检查一次 `_tool_batch_open`，和看门狗提示的延后规则一致。审批停下的调用也一样：插话要等这次调用有了结果（批准执行或拒绝）后才送达。正在运行的长 `run_command` 不再让插话一直等：开了 `--background-jobs` 时，用户的 `steer` 行会让它转成后台任务、这次调用立即返回（同一个进程，不停止），见 [background_jobs.md](background_jobs.md#用户插话时-run_command-转后台)。没开时仍等命令结束或超时。
- **送达不增加模型调用。** 收件箱只往下一次本来就要发出的请求里加一条消息。如果这一轮的最后一次模型调用已经发出（模型马上要给最终回答），插话就留到下一轮。
- **一条消息，不是几条。** 用户、Worker、任务通知同时到达时合成一条，减少无意义的轮换，也让「哪些是用户原话」一目了然。

这些合成消息不算新的一轮：`turn_number`（检查点、`/undo`、共享预算按它分轮）跳过它们（`is_inbox_message`）。注意：以前后台任务通知会被算成一轮，现在不算了；旧会话里如果有任务通知，`/checkpoints` 的轮次号会相应变小。

## 用户的两种插话

主 Agent 正在回复时：

| 输入 | 模式 | 效果 |
| --- | --- | --- |
| 普通一行 | `steer` | 在这一轮的下一次模型调用前送达（类似 Codex 回车）。这一轮已经在给最终回答时，就作为下一轮自动发出 |
| `/queue <text>` | `queue` | 这一轮结束后作为新的一轮发出，多条按输入顺序一轮一条（类似 Claude Code 的排队） |

主 Agent 空闲时，普通一行照旧直接开始一轮；`/queue <text>` 也直接开始一轮（前面没有要等的）。

**一轮正常结束（给出最终回答）后**：先看是否还有没送达的 `steer` 行——有就把它们合成下一轮自动发出（不会丢掉用户打过的字）；没有就取最早的一条 `queue` 开一轮；直到收件箱里没有用户的消息。Worker 结果不会自己开一轮（那会多花一次模型调用），它们等下一次模型调用或下一轮。

**一轮没正常结束**（Ctrl+C / 取消、停在审批、撞上限）：收件箱里的消息保留，不自动开新的一轮。下一条用户消息先续跑那一轮（续跑时的模型调用会带上 `steer` 消息），续跑完成后再发新消息；然后排队的消息按顺序接着发。没送达的消息都在会话里，进程退出再进来也还在（见下）。

Ctrl+C 和 `/cancel` 的含义不变：Ctrl+C 取消主 Agent 当前这一轮，`/cancel <id|all>` 取消 Worker。普通 `steer` 行不取消任何东西：它最多让本 Agent 正在运行的 `run_command` 转后台（见上），不停止命令、不取消已经发出的模型请求。主 Agent 模式下普通一行只交给主 Agent，Worker 里正在跑的命令不受影响（要让 Worker 转向仍用 `/tell`）。

## 各前端

- **TUI / Web**：`ChatSession.send()` 在回复中不再拒绝，而是放进收件箱（默认 `steer`，`/queue <text>` 为 `queue`），返回 `"queued"`（Web 仍是 200，输入框清空）。事件：
  - `user_message`：`text, mode, queued, id`。`mode` 为 `turn`（立即开始一轮）、`steer` 或 `queue`；`queued: true` 表示还在收件箱里。
  - `inbox_delivered`：`ids, sources, how`。`how` 为 `model_call`（在这一轮里送达）或 `new_turn`（作为/随下一轮送达）。
  界面在用户那行后面标「已加入本轮，等待送达」或「已排队：本轮结束后发送」，送达时再显示一行「已送达主 Agent」。`resync` 快照多一个 `inbox` 字段，列出还没送达的用户消息；对话历史里，回合中送达的用户原话以 `mode: "steer"` 显示，系统附加的部分不显示。
- **CLI（终端）**：stdin 和 stdout 都是终端时，主 Agent 的一轮在后台线程里跑，主线程继续用 `input()`（readline）读行：
  - 普通行 → `steer`，`/queue <text>` → `queue`，空闲时照常开始一轮；
  - **主 Agent 的审批**：后台线程打印「Approval required …」和 `Allow <tool>? [y/N]`，**下一行输入就是回答**：`y`/`yes`/`allow` 允许；`n`/`no`/`deny` 或直接回车拒绝；输入其它任何内容也拒绝这次调用，但这句话**不会丢**，会作为 `steer` 插话交给主 Agent（审批弹出时你正好在打字纠正它，是最常见的情况）。有多个问题时按显示顺序依次回答；
  - **Worker 的审批**：和以前一样在两次提示之间逐条问（`Allow write_file for worker w1? [y/N]`），下一行就是回答；主 Agent 有待答审批时先问它；
  - Ctrl+C：有待答审批时拒绝那一条（和以前一样）；否则有回合在跑就取消这一轮（在后台线程里抛 `KeyboardInterrupt`，效果与以前前台 Ctrl+C 相同：`interrupted`，可续跑）；空闲时退出（有 Worker 在跑会逐个取消并记录）；
  - `/exit` 时有回合在跑：先等它结束（Ctrl+C 取消），这期间出现的审批保持待定，下次进入再问；EOF 时等这一轮结束，审批按以前的 EOF 规则拒绝；
  - 后台输出：回复、说明行经同一个 `PromptLine` 打印；真实终端上会在提示符上方打印并重画提示符和已输入的文字。主 Agent 的 trace 行（`--trace`）仍直接写 stderr，可能和提示符挤在一行。
  管道或脚本输入（不是终端）保持原来的顺序语义：每一行等上一轮结束后再处理，`/queue <text>` 就是下一轮。一次性 `baseagent "task"` 不变。
- **忙时的斜杠命令**：`/status`、`/checkpoints`、`/cancel`、`/tell`、`/wait` 随时可用；`/continue` 在回复中会被拒绝（「上一条消息还在处理中」）；`/undo`、`/rewind` 在回复中被拒绝（见 [checkpoints.md](checkpoints.md)）。

## 持久化：不丢、只送一次

- 回合进行中，`InboxMiddleware` 在 `before_agent`、每次 `before_model` / `after_model` 和 `after_agent` 把还没送达的消息写进 `state.metadata["inbox"]`，由循环原有的检查点保存；空收件箱不写这个键（旧会话和录制数据不变）。
- 空闲时，`ChatSession.persist()` 把收件箱写进会话（和 Worker 登记表一起）。
- 送达和「从 `metadata["inbox"]` 去掉」发生在同一次状态修改里，下一个检查点一起保存：崩溃在保存前，就两者都没发生，续跑时重新送达；保存后，就只留下已送达的那条消息。
- 再次进入同一 `--session`（或 `--resume`）时，`ChatSession.start()` / `InboxMiddleware.before_agent` 从会话恢复没送达的消息；收件箱记着本进程已送达的 id，过期的快照不会把它们再带回来。CLI 会提示「有 N 条未送达的消息，会随下一轮发送」。

## 迁移到收件箱的三条旧路

| 以前 | 现在 |
| --- | --- |
| `WorkerManager._pushes`，只在用户下一条消息前附上 | `source=worker, mode=steer` 进主 Agent 收件箱：主 Agent 正在回复时在下一次模型调用前送达；空闲时仍附在下一条用户消息前面（`compose`，格式不变：推送标题、JSON 行、`[用户消息]`、原文） |
| Worker 的 `InboxMiddleware(record)` 读 `record._inbox` | `/tell` / `message_worker` → `record.inbox`（`source=lead`），由通用 `InboxMiddleware(record.inbox, jobs=…)` 送达，标题仍是 `[主 Agent 转达的补充要求]` |
| `JobNoticeMiddleware` 直接追加一条消息 | 任务通知进收件箱（`source=job`），和其它消息合成一条；`JobNoticeMiddleware` 保留为只处理任务通知的兼容外壳 |

显示层不变：`chat/session.py` 的 `strip_attachments` 仍去掉附在用户消息前的推送。
