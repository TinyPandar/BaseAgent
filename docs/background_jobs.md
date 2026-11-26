# 后台任务（Background jobs）

`run_command` 是同步的：默认 20 秒超时（上限 `max_command_timeout`，默认 60 秒），输出有上限，Ctrl+C / `/cancel` 会杀掉子进程。模型训练、长时间构建、本地服务这类工作跑不完。后台任务让 agent 启动一个**跨越回合**的长命令，之后再查看状态、分页读日志、按需停止。

参考设计：Claude Code 的后台 Bash 与 `/tasks`，DeepSeek Harness 的 `job_list` / `job_output` / `job_kill`，Codex 的后台终端。

## 打开方式

默认关闭，默认工具列表、`run_command` 的 schema 和能力契约都不变（录制的 cassette 与枚举工具的测试不受影响）。

```bash
baseagent --allow-command --background-jobs "在后台训练模型，跑完告诉我结果"
```

配置文件（`--config`）：

```json
{"version": 1, "permissions": {"allow_command": true}, "run": {"background_jobs": true}}
```

库用法：`Workspace(root, allow_command=True, background_jobs=True)`，`coding_tools()` / `assemble_capabilities()` 会多出下面六个工具，能力契约里多一项 `"background_jobs": true`。这些工具不属于任何 `--tool-package`，开关打开就会加上。开关打开时 `run_command` 的 schema 不变，但超时语义变了（见“run_command 超时转后台”）。

打开或关闭开关会改变工具契约；未完成的回合换了开关，恢复时需要 `--accept-config-changes`，与其他工具变化一致。

## 工具

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `start_job` | `argv`（必填，与 `run_command` 同样的字符串数组，不经 shell），`cwd`（可选，工作区内的相对目录，默认根目录），`keep_running`（可选，默认 `false`；`true` 时任务在 baseagent 退出后继续运行，见“生命周期”），`purpose` / `expect` / `next_step`（可选，各 ≤200 字符：目的、期待结果、下一步，随任务保存，只写一次） | 立即返回 `job_id`（形如 `j1a2b3c4d`）、`pid`、`log_path` 等，不等命令结束。需要 `--allow-command` |
| `job_list` | 无 | 最近 30 个任务（运行中 / `unknown` 的总会列出，包括以前进程启动的）的状态、退出码、开始/结束时间、日志大小；更早的记录数在 `omitted`。`retry_safe` |
| `job_output` | `job_id`，`offset`（字节，默认 0），`limit`（1–20000，默认 8000），或 `tail`（读最后 N 字节） | 返回 `content`、`next_offset`、`more`（日志里还有没读的）、`complete`（任务已结束且已读到末尾）。`retry_safe` |
| `job_kill` | `job_id` | 停止任务及其整棵进程树，返回最终状态；对已结束的任务返回 `killed: false`。需要 `--allow-command` |
| `job_wait` | `job_id`，`timeout_seconds`（30–3600，默认 600），`still_needed`，`purpose` / `expect` / `next_step` | 安静等待自己负责的一个任务：任务结束、本 Agent 收到插话、到时或回合停止时返回；从不停止或重启任务。见 [job_wait.md](job_wait.md) |
| `job_settle` | `job_id`，`state`（`handled` / `dropped`），`note` | 记录任务的结果已处理，或原目标已放弃（不碰进程）。见 [job_wait.md](job_wait.md) |

状态：

| 状态 | 含义 |
| --- | --- |
| `running` | 主进程还在运行 |
| `exited` | 主进程自己退出，退出码 0 |
| `failed` | 主进程退出码非 0，或监控线程出错（见 `error`） |
| `killed` | 被 `job_kill` 或会话关闭停止 |
| `lost` | 以前进程的任务：进程已经不在（或 PID 已被别的进程复用），没有留下退出状态，退出码未知 |
| `unknown` | 以前进程的任务：PID 还活着，但无法核对它就是原来的进程（读不到进程创建时间或记录里没有）；`job_kill` 拒绝停止它 |

结果里 `kind` 为 `run_command`（超时转来的）或 `detached`（`keep_running`）时会标出；以前进程的任务带 `from_earlier_process: true`。

启动失败（程序不存在、`cwd` 不存在）直接返回 `not_found`，不会留下任务记录。

## run_command 超时转后台

开关打开时，模型调用的 `run_command` 到了超时（包括被回合截止时间截短的超时）而命令还在运行，**不再杀掉它**，而是把这个正在运行的进程交给任务管理器（这正是“训练跑了一半被超时杀掉”的情形）：

- 还是同一个进程，不会重新启动；PID 不变，进程组 / Job object 不变。
- 已经读到的输出（每个流最多 `run_command` 的 20 KB）写在任务日志开头：先 stdout、再 stderr；之后的输出按到达顺序（两个流合并）追加到同一个日志。超过 20 KB 被丢掉的早期输出找不回来。
- `run_command` 本身以**确定的成功结果**结束：`ok: true`，`data` 里 `moved_to_background: true`、`job_id`、`pid`、`log_path`、`exit_code: null`、到目前为止的 `stdout` / `stderr`，以及说明文字。账本里这次调用是一个普通的已完成调用，恢复语义不变。
- 之后它就是一个普通任务（`kind: "run_command"`）：完成通知、`job_output`、`job_kill` 都适用。它的管道连着 baseagent，所以**随会话结束而停止**（不能 `keep_running`）。要跨退出保留，用 `start_job(keep_running=true)` 启动。
- **取消仍然杀进程**：Ctrl+C、`/cancel`、`--cancel-session` 发生在命令运行期间时，与以前一样杀掉整棵进程树，不转后台。只有超时和用户插话（见下一节）才转。
- **`sleep` 例外**：`argv[0]` 是 `sleep`（或 `sleep.exe`，忽略路径）的命令超时仍然被杀。模型用 `sleep` 是在等待，超时意味着它不需要等更久（Claude Code 同样把 `sleep` 排除在外）。
- `verify_command`、`git_diff` 等其他命令路径、以及库里直接调用 `Workspace.run_command()` 都保持超时即杀；只有注册给模型的 `run_command` 工具（`Workspace.run_command_tool`）会转后台。
- 同时运行的任务已达上限、或任务记录写不进磁盘时不转，照旧杀掉，错误信息里附上 `not moved to background: ...` 原因。任务只有在记录已写入、监控线程已启动后才算建成；中途任何失败都会撤掉登记（内存登记、计数、记录和日志文件、输出转向），进程仍归 `run_command`。
- 开关关闭时行为与以前完全相同。

实现：`run_process(on_timeout=...)` 在超时且进程仍在运行时调用回调，交出 `ProcessHandover`（进程、Job object、读取线程、已捕获输出）；回调返回结果即表示接管，`run_process` 不再清理进程。`LocalBackend.execute` 只是把可选的 `on_timeout` 透传下去；其他后端不受影响（只有本地后端才有任务管理器）。

## 用户插话时 run_command 转后台

插话只能在整批工具调用都有结果后送达（见 [inbox.md](inbox.md)），所以以前一个长 `run_command` 会让用户的插话一直等到命令结束或超时。开关打开时，**用户在本轮插话**（发给这个 Agent 的普通一行，即收件箱里 `source=user, mode=steer` 的消息）而命令还在运行，就把它像超时那样交给任务管理器，不等超时：

- 与超时转后台是同一条路：同一个进程、不重启、不停止；输出、任务记录、完成通知、`job_output` / `job_kill`、随会话结束而停止，全部同上。
- 结果同样是 `ok: true`、`moved_to_background: true`、`exit_code: null`，另有 `handover_reason: "interjection"`，说明文字写明“用户发来消息，命令已交给后台任务，不一定已经结束”，不报告命令成功退出。
- 这次调用返回后，同一批里剩下的工具调用照常执行；整批都有结果后，插话在下一次模型调用前送达（与以前同一个送达点，不增加模型调用）。
- **短命令照常结束**：命令要先跑满 1 秒（`INTERJECTION_GRACE`）才会因插话转后台。插话还没送达时，同一批里后面启动的命令也按这条规则处理：1 秒内结束的照常返回结果，跑得更久的也会转后台。
- **只看这个 Agent 自己的收件箱**（`run_command` 所在 Agent 的 `InboxMiddleware`）：主 Agent 收到的话不会让 Worker 的命令转后台；`/queue`（下一轮）、Worker 结果、任务通知、`/tell` / `message_worker` 的补充要求都不触发。
- **转不成时不杀**：开关关闭、非本地后端、`sleep`、同时运行的任务已满、任务管理器已关闭或任务记录写不进去时，命令继续运行，仍按原来的超时 / 取消规则处理。原因写在结果里：命令自己结束时 `data.not_moved_on_interjection`；之后又超时时，写在超时错误的 `not moved to background: ...` 里。每个命令只因插话尝试转一次。
- **取消优先**：每次检查先看取消。如果 Ctrl+C / `/cancel` 恰好与转后台同时发生（转后台过程中或刚转完、这次调用还没返回），刚建的任务会被立即停止（状态 `killed`，释放名额），这次调用报告 `cancelled`；转后台还没建成时，进程照常被杀，不留下任务。确认不了已停止时，错误信息给出任务 id 并说明无法确认，不会说“已停止”。超时转后台也一样。这次调用已经返回之后再取消回合，按上面的生命周期，任务不受影响，用 `job_kill` 停止。
- 恢复：这次调用在账本里是普通的已完成调用，续跑不会重新启动命令；插话按收件箱原有规则只送达一次。

实现：`run_process(..., interjection=...)` 在等待循环里（每 0.1 秒）查一次这个判断函数，满足条件时以 `ProcessHandover.reason == "interjection"` 提前调用同一个 `on_timeout`（与超时时一样在单独的短线程里执行，见“已知限制”）；回调拒绝时只记下原因、继续等。`Workspace.run_command_tool` 从工具上下文的 middleware 中找到这个 Agent 的 `InboxMiddleware`，做出判断函数（`tools/jobs.py` 的 `user_interjection`），只有在可能接管（开关打开、本地后端、非 `sleep`）时才传下去。

## 输出

stdout 和 stderr 合并写入 `<工作区>/.baseagent/jobs/<job_id>.log`（文件以独占方式创建，不覆盖旧日志）。每个日志最多 10 MB（`JobManager(max_log_bytes=...)`），超出部分继续读走但丢弃，`log_truncated: true`，`output_bytes` 是任务实际输出的总字节数。`.baseagent/` 对文件工具是保护路径，模型通过 `job_output` 读日志。偏移量按字节计；分页边界切开多字节字符时，该字符显示为替换字符。

## 生命周期：什么会停止一个任务

规则：**默认任务随会话结束；`start_job(keep_running=true)` 的任务在 baseagent 退出后继续运行**（训练这类工作应当这样启动）。默认不保留，是为了不在用户不知情时留下孤儿进程。

| 事件 | 普通任务 / 超时转来的任务 | `keep_running` 任务 |
| --- | --- | --- |
| 回合结束、回合取消（Ctrl+C 取消回合、`/cancel`、`--cancel-session`） | **不受影响**，继续运行 | 同左 |
| `job_kill` | 停止整棵进程树 | 停止整棵进程树（重启后也可以，需通过身份核对） |
| 主进程自己退出 | 它留下的子孙进程一并停止（与 `run_command` 结束时的行为一致） | 同左（由 runner 停止） |
| 会话结束：一次性 prompt 跑完、聊天 `/exit`、TUI/Web 关闭、出错退出 | `main()` 的 `finally` 调用 `JobManager.close()` 停止它们 | **继续运行** |
| baseagent 进程退出 | Windows：在 harness 的 Job object 里（`KILL_ON_JOB_CLOSE`），进程退出（包括崩溃）时整棵树被系统终止。POSIX：正常退出时 `atexit` 停止；`SIGKILL` 或崩溃时可能留下（重启后显示为 `running`，可 `job_kill`） | **继续运行** |

`keep_running` 的做法：baseagent 启动一个小的 runner 进程（`python -c`，代码在 `tools/jobs.py` 的 `_RUNNER`），runner 不在 harness 的进程组 / Job object 里（POSIX 新会话；Windows 用 `CREATE_BREAKAWAY_FROM_JOB`，若 harness 所在的 Job 不允许脱离，则退回普通创建，此时 runner 仍会随那个外层 Job 结束）。runner 再启动命令，把命令放进自己的进程组（POSIX）或自己持有的 `KILL_ON_JOB_CLOSE` Job object（Windows），自己负责写有上限的日志，命令结束后把退出码原子写入 `<id>.exit.json`。停止 runner 就停止整棵树。因此 baseagent 不在时结束的 `keep_running` 任务，重启后仍能看到真实退出码和完整日志。

进程隔离：

- Windows：`CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`，没有控制台，控制台的 Ctrl+C 到不了任务；停止时关闭 Job object 句柄，必要时再 `taskkill /T /F`。
- POSIX：`start_new_session=True`，终端的 SIGINT / SIGHUP 到不了任务；停止时对整个进程组发 `SIGKILL`。

## 完成通知

开关打开时，结束的任务在下一次模型调用之前作为一条 user 消息（`[后台任务通知 / background job notice]`：id、状态、退出码、日志大小、命令、日志最后几行；有关联时再加目的、期待、下一步和目标是否仍有效）送到**负责它的 Agent**，与插话合成一条（[inbox.md](inbox.md)）。回合之间结束的任务在下一轮的第一次模型调用时通知。

- **按负责人投递**：启动任务的 Agent 会话（`agent_session`）。主 Agent 模式下 Worker 启动的任务交给那个 Worker；它结束后才转给主 Agent（只转一次，并注明来源）。别的 Agent 或侧边对话先调用模型也拿不到。没有 `--session` 的对话，在第一轮开始时把会话 id 记为任务的 owner，因此用同一个 id 再进入时能收到，另一个无名会话收不到。（以前 owner 为空，结果正好相反。）
- **不重复**：对话里已经有这个任务的结果（以前的通知，或 `job_wait` 的结束结果）时不再通知；收件箱里已有它的通知时也不再放入。
- **找得回**：取走（`notified`）、模型已收到（`delivery.received`）、已处理（`delivery.settled`）分开记录；每次模型调用前都按记录重新找“已结束、负责人还没收到”的任务。所以取走通知后、检查点前崩溃不再丢失（以前的已知窗口）；它在恢复后补发一次。详见 [job_wait.md](job_wait.md#送达的三个阶段以及重新发现)。
- 只通知**本会话**（`owner_session`）的任务，别的会话只能在 `job_list` 里看到。
## 权限与安全

- `start_job` 和 `job_kill` 与 `run_command` 一样需要 `--allow-command`，否则返回 `permission_denied`；没有命令权限时，这两个工具也不会出现在发给模型的工具列表里（`job_list` / `job_output` 只读，保留）。
- argv 校验与 `run_command` 相同（非空字符串数组，JSON 编码后不超过 8 KB），不经 shell；`cwd` 走工作区路径校验，不能逃出工作区，也不能进入 `.git`、`.venv`、`.baseagent` 等保护路径。
- `--tool-policy` 按工具名匹配，`argv_exact` 等条件同样适用于 `start_job`（参数名也是 `argv`）。**注意：限制 `run_command` 的规则不会自动作用于 `start_job`**；用策略约束命令时，要给 `start_job` 写同样的规则，或把它设为 `deny` / `ask`。
- HarnessKnight 把 `start_job` 当作命令执行（`execute` 影响），与 `run_command` 相同。
- **默认的 LocalBackend 不是沙箱。** 不加 `--sandbox` 时，后台任务以运行 baseagent 的用户身份直接在宿主机上运行，能读写该用户能访问的一切，能访问网络。进程组 / Job object 只管理寿命，不限制文件或网络访问。
- **加 `--sandbox read-only|workspace-write` 时**，任务进程经 `spawn_job_process` 用同一个 OS 沙箱启动（见 [sandbox.md](sandbox.md)）；`run_command` 超时转来的进程本来就在沙箱里启动。`keep_running` 在沙箱下被拒绝（`permission_denied`），沙箱已关闭或不可用时任务也被拒绝，绝不在沙箱外运行。原生 Windows 没有沙箱实现，受限模式在启动时就会以退出码 3 拒绝。
- `--backend docker` 不支持后台任务：`--background-jobs` 与 `--backend docker` 同时给出时，启动前直接报错退出（不会启动容器），错误信息说明两种做法：去掉 `--background-jobs`（长命令在容器里用 `run_command`，受超时限制），或去掉 `--backend docker`（任务在宿主机上运行，没有沙箱）。库里给 `Workspace` 注入非本地后端时，四个工具都返回 `permission_denied` 和同样的说明，绝不会悄悄在宿主机上运行；`run_command` 超时照旧杀掉。
- 同时运行的任务最多 8 个（包括以前进程留下、仍在运行或状态 `unknown` 的任务），每个会话最多启动 100 个任务，超出返回 `limit_exceeded`。
- 任务继承 baseagent 进程的环境变量（与 `run_command` 相同），包括其中的凭证。

## 持久化与重启

每个任务在 `<工作区>/.baseagent/jobs/` 下有一组文件：

| 文件 | 内容 |
| --- | --- |
| `<id>.json` | 任务记录（`version: 1`）：`job_id`、`kind`（`job` / `detached` / `run_command`）、`argv`、`cwd`、`pid`（命令进程）、`leader_pid` 与 `leader_identity`（整棵树的领头进程及其创建时间标识，`job_kill` 杀它）、`started_at` / `ended_at`、`status`、`exit_code`、`log_path`、`log_bytes` / `output_bytes`、`owner_session`、`keep_running`、`notified`、`kill_requested`、`error`。每次变化都先写临时文件再原子替换 |
| `<id>.log` | 合并的 stdout/stderr 日志 |
| `<id>.link.json` | 关联（`version: 1`）：负责的 Agent 会话、启动时的回合与用户原话、目的 / 期待 / 下一步（只写一次）、目标是否有效、送达阶段。只由负责会话写；缺失或损坏时按“未说明”处理，见 [job_wait.md](job_wait.md#关联存在哪里怎么接回) |
| `<id>.run.json` / `<id>.exit.json` | 仅 `keep_running`：runner 记录命令 PID / 退出码 |

新的 `JobManager`（新会话、新进程）会读入以前的记录，`job_list` / `job_output` / `job_kill` 都能用。以前记录为 `running` 的任务按下面的规则判断：

1. 有 `<id>.exit.json`：按其中的退出码记为 `exited` / `failed`（或已请求停止时 `killed`）。
2. 领头进程已不存在：`lost`（退出码未知）。
3. 领头进程的 PID 还在，且其创建时间与记录一致：`running`，可以 `job_kill`（杀之前再核对一次）。
4. PID 还在但创建时间不同：说明 PID 被别的进程复用了，原任务已结束：`lost`，绝不会去杀那个进程。
5. PID 还在但读不到创建时间（无权限、平台不支持）或记录里没有：`unknown`，`job_kill` 拒绝（`permission_denied`），需要用户自己处理。

进程创建时间：Windows 用 `GetProcessTimes`，Linux 读 `/proc/<pid>/stat` 的 starttime，其他 POSIX 用 `ps -o lstart=`。

保留与清理：启动时只保留最近 50 个已结束任务的记录、日志和关联（`JobManager(keep_finished=...)`），更早的 `<id>.*` 文件被删除（还没处理的结果也一样，此后只能说明它找不到了）；运行中 / `unknown` 的任务不删。`job_list` 默认只显示最近 30 个。没有新增清理工具。旧版本留下的、没有 `.json` 记录的日志不会被列出，也不会被删除。

## 已知限制

- Windows 上进程先创建、再加入 Job object；在这极短的窗口内派生并立即脱离的子进程可能漏掉（`run_command` 有同样的限制）。POSIX 上自己调用 `setsid` 的子孙进程会离开进程组，`job_kill` 管不到它。
- 超时转来的任务只保留超时前每个流的前 20 KB 输出，且超时前的 stdout 与 stderr 在日志里是先后排列、不是交错的。插话转来的任务相同。
- 终端 / TUI / Web 的 Ctrl+C 在回合线程里以异步异常生效，可能打断任意一行 Python 代码。所以转后台（登记任务、写记录、转向输出、启动监控）放在一个单独的短线程里完成，回合线程只等它；Ctrl+C 打断等待时，会先等这个线程结束，再按结果停止刚建的任务或杀掉进程，不会留下没有进程的 `running` 记录。仍有一个极端情况：两次 Ctrl+C 几乎同时到达，第二次打断了这次等待，此时命令可能作为普通后台任务继续运行（状态真实、有监控，可用 `job_kill` 停止）。
- 两个 baseagent 进程同时使用同一工作区时，各自看到对方的任务（显示为 `from_earlier_process`），状态判断与写盘之间有很小的竞争窗口。
- `keep_running` 任务在 Windows 上依赖能脱离外层 Job object；某些终端 / 服务管理器禁止脱离，此时它会随外层一起结束。
- 日志只保留开头 10 MB；长时间训练如果输出很多，后面的进度会丢，建议让训练脚本自己写日志文件或减少输出。

## 后续工作

- **Docker 后端**：在容器里用 `docker exec -d` 启动 runner 并把日志与记录写在容器内（或挂载目录），`job_kill` 杀容器内进程组；容器随 run 删除，所以 `keep_running` 还需要保留容器的策略。在此之前继续拒绝。
- **沙箱下的 `keep_running`**：现在拒绝。要支持，需要让 runner 留在沙箱外、只把命令放进沙箱，并给它一个不随本次运行删除的临时目录。
- 日志滚动（保留最新部分而不是开头）、按任务的输出上限参数、`/jobs` 之类的聊天命令、`job_input`。
- 可打断的等待与接回结果：已做，见 [job_wait.md](job_wait.md)。
