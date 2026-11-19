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

库用法：`Workspace(root, allow_command=True, background_jobs=True)`，`coding_tools()` / `assemble_capabilities()` 会多出下面四个工具，能力契约里多一项 `"background_jobs": true`。这四个工具不属于任何 `--tool-package`，开关打开就会加上。

打开或关闭开关会改变工具契约；未完成的回合换了开关，恢复时需要 `--accept-config-changes`，与其他工具变化一致。

## 工具

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `start_job` | `argv`（必填，与 `run_command` 同样的字符串数组，不经 shell），`cwd`（可选，工作区内的相对目录，默认根目录） | 立即返回 `job_id`（形如 `j1a2b3c4d`）、`pid`、`log_path` 等，不等命令结束。需要 `--allow-command` |
| `job_list` | 无 | 所有任务的状态、退出码、开始/结束时间、日志大小。`retry_safe` |
| `job_output` | `job_id`，`offset`（字节，默认 0），`limit`（1–20000，默认 8000），或 `tail`（读最后 N 字节） | 返回 `content`、`next_offset`、`more`（日志里还有没读的）、`complete`（任务已结束且已读到末尾）。`retry_safe` |
| `job_kill` | `job_id` | 停止任务及其整棵进程树，返回最终状态；对已结束的任务返回 `killed: false`。需要 `--allow-command` |

状态：

| 状态 | 含义 |
| --- | --- |
| `running` | 主进程还在运行 |
| `exited` | 主进程自己退出，退出码 0 |
| `failed` | 主进程退出码非 0，或监控线程出错（见 `error`） |
| `killed` | 被 `job_kill` 或会话关闭停止 |

启动失败（程序不存在、`cwd` 不存在）直接返回 `not_found`，不会留下任务记录。

## 输出

stdout 和 stderr 合并写入 `<工作区>/.baseagent/jobs/<job_id>.log`（文件以独占方式创建，不覆盖旧日志）。每个日志最多 10 MB（`JobManager(max_log_bytes=...)`），超出部分继续读走但丢弃，`log_truncated: true`，`output_bytes` 是任务实际输出的总字节数。`.baseagent/` 对文件工具是保护路径，模型通过 `job_output` 读日志。偏移量按字节计；分页边界切开多字节字符时，该字符显示为替换字符。

## 生命周期：什么会停止一个任务

| 事件 | 任务 |
| --- | --- |
| 回合结束、回合取消（Ctrl+C 取消回合、`/cancel`、`--cancel-session`）、`run_command` 超时 | **不受影响**，继续运行 |
| `job_kill` | 停止整棵进程树 |
| 主进程自己退出 | 它留下的子孙进程一并停止（与 `run_command` 结束时的行为一致），避免孤儿进程 |
| 会话结束：一次性 prompt 跑完、聊天 `/exit`、TUI/Web 关闭、出错退出 | `main()` 的 `finally` 调用 `JobManager.close()`，停止所有仍在运行的任务 |
| baseagent 进程退出 | Windows：任务在 Job object 里（`KILL_ON_JOB_CLOSE`），进程退出（包括崩溃）时句柄关闭，整棵树被系统终止。POSIX：正常退出时 `atexit` 停止所有任务；`SIGKILL` 或崩溃时任务可能留下 |

也就是说，任务的寿命以**会话进程**为界：在交互式聊天里可以跨多个回合；一次性 prompt 在回答结束时就会停掉它启动的任务。要跨重启保留任务，见“后续工作”。

进程隔离：

- Windows：`CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`，没有控制台，控制台的 Ctrl+C 到不了任务；停止时关闭 Job object 句柄，必要时再 `taskkill /T /F`。
- POSIX：`start_new_session=True`，终端的 SIGINT / SIGHUP 到不了任务；停止时对整个进程组发 `SIGKILL`。

## 完成通知

开关打开时，`main()` 会加一个 `JobNoticeMiddleware`：在下一次模型调用之前，把自上次通知以来结束的任务（id、状态、退出码、日志大小、命令）作为一条 user 消息追加到对话，每个任务只通知一次，做法与 worker 的 `InboxMiddleware` 相同。回合之间结束的任务在下一个回合的第一次模型调用时通知。主 Agent 聊天里，通知由下一个发出模型调用的 agent（主 Agent 或某个 worker）收到。不打开开关时没有这一层。

## 权限与安全

- `start_job` 和 `job_kill` 与 `run_command` 一样需要 `--allow-command`，否则返回 `permission_denied`；没有命令权限时，这两个工具也不会出现在发给模型的工具列表里（`job_list` / `job_output` 只读，保留）。
- argv 校验与 `run_command` 相同（非空字符串数组，JSON 编码后不超过 8 KB），不经 shell；`cwd` 走工作区路径校验，不能逃出工作区，也不能进入 `.git`、`.venv`、`.baseagent` 等保护路径。
- `--tool-policy` 按工具名匹配，`argv_exact` 等条件同样适用于 `start_job`（参数名也是 `argv`）。**注意：限制 `run_command` 的规则不会自动作用于 `start_job`**；用策略约束命令时，要给 `start_job` 写同样的规则，或把它设为 `deny` / `ask`。
- HarnessKnight 把 `start_job` 当作命令执行（`execute` 影响），与 `run_command` 相同。
- **LocalBackend 不是沙箱。** 后台任务以运行 baseagent 的用户身份直接在宿主机上运行，能读写该用户能访问的一切，能访问网络。进程组 / Job object 只管理寿命，不限制文件或网络访问。
- `--backend docker` 不支持后台任务：`--background-jobs` 与 `--backend docker` 同时给出时，启动前直接报错退出（不会启动容器）；库里给 `Workspace` 注入非本地后端时，四个工具都返回 `permission_denied` 和说明，绝不会悄悄在宿主机上运行。
- 同时运行的任务最多 8 个，每个会话最多 100 个任务记录，超出返回 `limit_exceeded`。
- 任务继承 baseagent 进程的环境变量（与 `run_command` 相同），包括其中的凭证。

## 已知限制

- Windows 上进程先创建、再加入 Job object；在这极短的窗口内派生并立即脱离的子进程可能漏掉（`run_command` 有同样的限制）。POSIX 上自己调用 `setsid` 的子孙进程会离开进程组，`job_kill` 管不到它。
- 任务记录只在内存里，`job_list` 看不到以前进程启动的任务；旧日志文件留在 `.baseagent/jobs/`，不会自动清理。
- 日志只保留开头 10 MB；长时间训练如果输出很多，后面的进度会丢，建议让训练脚本自己写日志文件或减少输出。

## 后续工作

- **跨重启持久化**：把任务记录写进会话库，重启后能看到并接管仍在运行的任务（需要真正脱离父进程，并处理 PID 复用）。
- **Docker 后端**：在容器里用 `docker exec -d` 启动并把日志写在容器内，`job_kill` 杀容器内进程组。
- **超时转后台**：开关打开时，`run_command` 超时不杀进程，而是把它转成一个后台任务（Claude Code 的做法）。这需要把 `run_process` 已读到内存的输出和读取线程交接给任务管理器，并改变 `run_command` 超时结果的语义，本次没有做。
- 日志滚动（保留最新部分而不是开头）、按任务的输出上限参数、`/jobs` 之类的聊天命令。
