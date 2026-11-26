# 可打断等待与接回后台任务（`job_wait` / `job_settle`）

后台任务（[background_jobs.md](background_jobs.md)）能跨回合运行，但以前 Agent 只能用 `job_list` / `job_output` 一次次去看，或者等完成通知随下一次模型调用送达。本页说明 `--background-jobs` 下新增的两件事：

- **`job_wait`**：没有结果时安静等待；本 Agent 收到插话就让出；回答后，原任务仍然需要时可以再等同一个任务。等待期间不调用模型，不启动或停止任何进程。
- **任务和上下文的关联**：每个任务记着谁负责、为什么启动、期待什么结果、下一步做什么、这个目标现在是否还成立，以及结果走到了哪一步。上下文压缩或进程重启后仍然能接上。

开关关闭时，工具列表、能力契约和所有输出都与以前相同。

## 谁等待、谁接结果、谁答复用户

| 组合 | 谁启动、谁等待 | 结果交给谁 | 谁答复用户 |
| --- | --- | --- | --- |
| 单 Agent 对话（`--no-main-agent`）、一次性 prompt | 这个 Agent | 这个 Agent | 这个 Agent：先把中途答复展示给用户，再继续等 |
| 主 Agent + Worker（对话默认） | 可写 Worker（`allow_writes`）。主 Agent 只有 `delegate_task` / `message_worker`，**没有为等待增加任何执行工具**；只读 Worker 只有 `job_list` / `job_output` | 启动它的 Worker；Worker 已结束时转给主 Agent，只转一次，并注明来自哪个 Worker | 主 Agent：它随时可以回答，因为 `delegate_task` 立即返回。Worker 的结果通过原有的 Worker 推送到达 |

**投递按负责人走，不按“谁先调用模型”。** 任务记录里有 `agent_session`（启动它的会话）。主 Agent、其他 Worker 或一个侧边对话即使先调用模型，也拿不到这条通知；`job_wait` 和 `job_settle` 也只对负责人开放（其他人得到 `not_responsible` / `permission_denied`，`job_list` / `job_output` 照旧可读）。

对哪些消息让出等待：

| 输入 | 让出等待？ |
| --- | --- |
| 单 Agent：用户普通一行（本轮 steer） | 是 |
| Worker：`/tell` 或主 Agent 的 `message_worker`（发给这个 Worker 的 steer） | 是 |
| 主 Agent 模式下用户的普通一行 | 否：它交给主 Agent，主 Agent 立即答复；Worker 的等待不受影响（不改输入路由） |
| `/queue`、Worker 结果、其他任务的通知、发给别的 Agent 的消息 | 否 |
| 回合取消（Ctrl+C、`--cancel-session`、关闭会话） | 等待结束，任务照旧运行（会话结束时按原规则停止非 `keep_running` 任务） |
| 别的 Agent 或侧边对话被停止、Worker 的 `/cancel` | 只停那个对象；不会被当成取消本 Agent 的等待 |

## 三个动作分开

| 动作 | 工具 | 碰进程吗 |
| --- | --- | --- |
| 结束一次等待 | `job_wait` 返回（任务结束、插话、到时、回合停止） | 不碰 |
| 放弃原目标 | `job_settle(state="dropped", note=…)` | 不碰；之后的完成通知只作为信息，不执行原来的下一步 |
| 停止进程 | 既有的 `job_kill`（原有权限） | 停止整棵进程树 |

## `job_wait`

参数：`job_id`（必填），`timeout_seconds`（30–3600，默认 600，并受本轮剩余时长限制），`still_needed`（有新输入后再等时必填：为什么还需要这个任务），`purpose` / `expect` / `next_step`（补登从未说明过的项）。

每 0.1 秒看一次本地状态（是否已结束、自己的收件箱、取消请求；以前进程的任务每秒探测一次）。不调用模型，不循环调 `job_list` / `job_output`。Ctrl+C 在 0.1 秒内生效。每次看一眼只得到一个结论，按下面的顺序取第一个成立的：

| 顺序 | 看到的事实 | `outcome` |
| --- | --- | --- |
| 1 | 会话正在关闭 | `cancelled`，同时停止本轮，不会再发模型请求 |
| 2 | 任务已结束（`exited` / `failed` / `killed`） | `finished`；`lost` 为 `ended_unknown` |
| 3 | 回合被取消 | `cancelled` |
| 4 | 状态 `unknown`（无法核对 PID 就是原进程） | `unknown_state`，不等待 |
| 5 | 本 Agent 有插话 | `interrupted`，任务继续运行 |
| 6 | 等待时限到 | `wait_expired` |

所以“结束和插话同时发生”时提交的是结束：插话仍在收件箱里，下一次模型调用照常送达，两个事实都不丢。

不进入等待、立即返回的情况：`not_responsible`、`held`（见“中断与恢复”）、`goal_dropped`、`confirm_first`（见下节）。这几种结果不含变化的数字，模型如果原样重复，会被已有的“三轮相同工具结果”保护拦下（`tool_loop_detected`）。

结果始终包含：`outcome` 和说明；`job_succeeded`（`exited` 为 true，`failed` / `killed` 为 false，其余为 null）；真实的 `job`（状态、退出码、PID、命令）；`brief`（目的、期待、下一步、目标是否有效，没说过的就是 null）；任务结束时还有日志末尾，以及日志被截断或缺失的说明。大小控制在 3500 字节以内，低于默认的工具结果裁剪线（4000 字节），模型能看到完整结果。

**等待到时不是执行失败。** `wait_expired` 时任务状态仍是 `running`，没有 TIMEOUT 错误码，也不会被停止。任务仍需要时可以再等，受原有的步数、工具调用次数、时长和费用上限约束；目标不成立或任务已结束时没有理由再等。

**未知不是失败。** `ended_unknown` / `unknown_state` 的 `job_succeeded` 为 null，说明文字要求先核对影响，不要自动重跑。

## 先答复，用户看到后才再等

单 Agent 在任务目标最近一次确认之后**又收到用户输入**（插话，或新的一轮）时，`job_wait` 先检查两件事：

1. `still_needed`：为什么这个任务还需要。
2. 一段答复，并且**前端报告它已经显示给用户**。只在模型输出里有文字不算数。

缺哪一项就返回 `confirm_first` 并列出来，模型补上即可，**不需要用户再说一句**。答复的来源是本轮最新输入之后、带工具调用的 assistant 文字（通常和 `job_wait` 写在同一条消息里）。一轮只在结束时显示最终回复，所以这种中途答复由 `InterimAnswerMiddleware` 在模型返回后立即发布：

| 前端 | 什么时候算“已显示” |
| --- | --- |
| CLI（终端，线程模式和管道模式） | 打印并 flush 之后 |
| TUI | 包含这段答复的那一帧画面刷新之后（`call_after_refresh`） |
| Web | 页面把它插入对话、绘制两帧之后，并且页面当时可见（`document.visibilityState`），通过 `POST /shown` 报告。只收到 SSE 不算 |
| 一次性 prompt（文本输出） | 打印到 stderr 之后（stdout 仍只有最终回复） |
| `--output json/jsonl`、库调用里没有前端 | 无法显示：`confirm_first`，要求改为结束本轮、给出最终回复 |

报告之前处于“答复待显示”阶段：仍然安静地看任务和插话（任务结束或新插话照样返回），但**等待没有开始**，目标也没有被确认。报告之后才确认目标、记下 `waiting_since`，结果里有 `answer_shown_at <= waiting_since`。始终没有人报告（例如页面关着）时，到时返回 `wait_expired`，`answer_shown: false`，并说明“等待从未开始”。

回执按条记录，并区分会话：别的输出（另一个事件、另一个进程的事件编号、侧边对话自己的输出）不会确认这一条。同一条报告重复发送没有影响；Web 重新连接后只补发确认，不会重新提交消息或重新生成。Worker 的文字不是给用户看的，它只需要 `still_needed`。

## 关联：存在哪里、怎么接回

任务记录 `<id>.json` 保存进程事实，任何管理器都可能重写它。关联保存在旁边的 `<id>.link.json`，只由负责会话写（它持有会话锁，单写者）：

| 字段 | 含义 |
| --- | --- |
| `agent_session` | 负责的 Agent 会话 |
| `origin` | 启动时的回合、调用 id，以及用户当时的原话摘录（标为原话，不当作目的） |
| `purpose` / `expect` / `next_step` | 模型声明的目的、期待结果、下一步。只写一次，之后不能改，避免新目标把旧任务“改名”；没说就是 null，不补造 |
| `goal` | `active` / `dropped`，以及确认时 Agent 已看到的用户输入条数 |
| `delivery.received` | 哪些会话的模型已收到最终结果 |
| `delivery.settled` | `handled`（`job_settle`）、`reported`（收到结果的一轮正常完成且没有未完成的下一步）或 `legacy` |

“最新要求是否仍使原目标有效”不另存：从原始对话（压缩从不改写原始对话）里数“确认之后还有几条用户输入”，并显示其中最近的原话。

**每次请求附一段“未了任务”参考。** 内容是负责的、未结束或未结算的任务，每个一行 JSON：编号、状态、负责人、送达阶段、目的、期待、下一步、目标、确认后又有几条输入、是否正在等待。它和 Worker 推送、任务通知一样，是一条带标签的 user 消息，放在 system 提示之后，只加在本次请求里，**不写进对话**，不含运行时长，事实不变时文字也不变。压缩把启动任务的那几轮换成摘要后，任务编号、目的和下一步仍在请求里（离线摘要本身不保留它们，见测试）。

侧边对话可以调用 `InboxMiddleware.reference(state)` 在创建时取一份文字并冻结：这份文字包含当时已公开的任务事实（含“正在等待”）；取它不会拿走通知，也不改送达状态；主线之后的请求照常更新，侧聊那份不变。侧聊本身不在本期实现，这里只保证接口和不变量。

## 送达的三个阶段，以及重新发现

| 阶段 | 记在哪里 | 由谁推进 |
| --- | --- | --- |
| 取走（taken） | 任务记录 `notified` | 通知放进收件箱时 |
| 已收到（received） | `delivery.received` | 对话里出现这个任务的最终结果（通知或 `job_wait` 的结束结果），并且模型在它之后已经回复 |
| 已处理（settled） | `delivery.settled` | `job_settle handled`；或收到结果的一轮正常完成，且没有未完成的下一步 |

每次模型调用前，`InboxMiddleware` 都按记录重新找“已结束、但负责人还没收到”的任务，**不依赖 `notified`**：

- 对话里已经有它的结果：不再通知（`job_wait` 已经说过的完成不会再发一次“新完成”）；模型回复后记为已收到。
- 收件箱里已有它的通知：不重复放入。
- 都没有，比如通知取走后进程在检查点前崩溃：重新放入。以前这种情况会丢失通知（旧文档列为已知窗口），现在会补发一次。

单 Agent 声明了 `next_step` 的任务，收到结果后仍保持“已收到、下一步未确认处理”，直到 `job_settle handled` 或 `dropped`。期间每次请求的参考里都会提示：下一步可能已经做过，先核对再做。主 Agent 和 Worker 没有 `job_settle`，结果在答复或汇报的那一轮完成后记为 `reported`。旧版本留下、没有关联的记录：已经通知过的视为旧记录，不再通知；没通知过的照常通知，目的标为未知。

## 中断与恢复

- **Ctrl+C、关闭会话、进程被杀时正好在等待中。** 账本里这次调用停在“运行中”。以前这会让会话进入 `needs_recovery`，需要人工核对。现在 `job_wait` 在注册时声明了收尾方式（`ToolRegistry.register(on_interrupted=…)`，只允许非 destructive、非 open_world 的工具）。恢复时用当前事实补上结果：任务已结束就给出结束结果；否则给出 `wait_stopped`。如果原调用已经返回结果、只是没来得及提交，就用那个结果。工具不会重跑，其他工具的中断仍按原规则需要人工核对。
- **`held` 只用于这种异常恢复。** 本轮里有等待被中断或取消（`wait_stopped` / `cancelled`），并且之后还没有新的用户输入时，再调用 `job_wait` 立即返回 `held`。这样续跑的旧轮不会自己又开始等，重启后也不会自动续等。普通插话之后的再等不受影响，这一点有专门的测试。
- **Web 关闭页面或断线只结束事件连接。** 回合和任务都继续，不会变成取消，也不会进入 `needs_recovery`。重新打开页面时，快照里带着待确认的答复，确认后等待才开始。停止 Web 服务（Ctrl+C）属于会话结束：等待以 `cancelled` 结束，不会再发模型请求；`main()` 结束时照旧停止非 `keep_running` 任务，下次进入时补发它的结束通知。
- **重启后不恢复执行。** 不会重新启动命令，也不会执行原来的下一步；`unknown` / `lost` 都不会被当作失败而重跑。

## 状态显示

`ChatSession.status()` 多一个 `jobs` 字段（只在开关打开时出现），列出本会话未结算的任务和最近的任务：状态、退出码、负责人、目的、目标、送达阶段，以及 `waiting`（`showing_answer` 答复待显示 / `waiting` 正在等 / null）。CLI 的 `/status` 多一组 “Background jobs” 行；TUI 状态栏和 Web 页眉显示正在运行或正在等待的任务。等待期间仍可输入、`/status`、`/queue`、`/tell`、`/cancel`。

## 不做

`job_input`、Docker 后台、`/jobs` / `/kill` 聊天命令、侧边对话、通用调度器、自动重试。`job_wait` 不是 `retry_safe`：它会写任务关联，因此不会被自动重试、不会并行执行、不会交给只读 Worker。

## 验收对照（README AC01–AC14）

测试：`tests/test_job_wait.py`（单 Agent、竞争、送达、恢复、联合场景）和 `tests/test_job_wait_chat.py`（真实 `baseagent.main` 组装的 CLI / TUI / Web、主 Agent + Worker、一次性 prompt）。全部使用假模型和受控本地进程：进程记录每次启动和 PID，等测试写入闸门文件后才退出，闸门内容就是退出码。竞争用 `job_wait._on_tick` 精确控制“第 n 次看本地状态时发生什么”，不靠 sleep 赌时序。

| AC | 主要测试 |
| --- | --- |
| 01 | `test_ac01_ac04_quiet_until_the_job_ends_same_process_started_once`、`test_ac02_a_moved_command_keeps_the_users_words_and_the_responsible_agent`、`test_ac01_unknown_and_invalid_jobs`；联合场景（插话转后台的同一进程） |
| 02 | `test_ac02_purpose_is_declared_once_and_never_made_up`、`test_ac02_a_moved_command_keeps_…` |
| 03 | `SurfaceTests`（开关、只读 Worker、主 Agent 工具面、Docker、策略）、`test_ac03_ac07_the_turn_deadline_caps_…`、`test_ac03_a_wait_counts_like_any_tool_call` |
| 04 | `test_ac01_ac04_…`（等待期间模型调用增量为 0）、`test_ac13_threaded_terminal_…`（等待中 `/status`） |
| 05 | `test_ac05_interrupted_then_answered_then_the_same_job_is_waited_for_again`、`test_ac05_rest_of_the_batch_…`、`test_held_is_never_the_effect_of_an_ordinary_interjection` |
| 06 | `test_ac06_only_messages_for_this_agent_end_the_wait`、`test_ac06_a_worker_yields_to_tell`、`test_ac06_queue_keeps_its_order_…`、`WorkerTests` |
| 07 | `test_ac07_expiry_is_not_a_failure_and_waiting_again_is_possible`、`test_ac03_ac07_…` |
| 08 | `RaceTests`（等待前结束、同一拍结束与插话、先插话后结束、结束与取消同拍、回答期间结束）、`DecideTests`（判断表全组合） |
| 09 | `ReportingTests`（非 0、kill、lost、unknown、日志截断或缺失、结果大小） |
| 10 | `GoalTests`（新输入须确认、放弃目标不碰进程、放弃后须有新输入才能恢复） |
| 11 | `test_ac11_compaction_keeps_the_association_and_restart_too`、`test_ac11_a_damaged_association_is_reported_as_missing`、`DeliveryTests` |
| 12 | `RecoveryTests`（Ctrl+C、持久取消、崩溃、会话关闭、其他工具仍需核对）、`test_ac12_ctrl_c_in_the_terminal_…`、`test_stopping_the_server_during_a_wait_…` |
| 13 | `CliTests`、`WebTests`（`/shown`、重连、关闭页面、校验）、`TuiTests`、`WorkerTests` |
| 14 | 以上全部；联合场景 `JointScenarioTests`（插话转后台 → 模拟侧聊打开但不抢通知 → 中途答复真正显示后才等 → 侧聊停止不影响主线等待 → 结果交回原负责人；其间穿插压缩、确认重发和停止，检查消息不串、结果不丢、模型调用次数一致） |
