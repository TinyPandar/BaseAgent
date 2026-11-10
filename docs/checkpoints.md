# 检查点与撤销（按文件快照）

每个用户轮次一个检查点。agent 用 `write_file` / `edit_file` 第一次写某个文件之前，先把它的原貌存进会话库；之后可以按轮次把这些文件写回去。只覆盖文件工具，不扫仓库，不用 git。

```text
/checkpoints                 列出轮次号、时间、改动文件（新建/修改、来自哪个 worker）、是否已撤销
/undo [--force]              撤回最近一个未撤销的轮次
/rewind N [--force]          倒序撤回第 N 轮及之后的所有轮次
```

三条命令在 `ChatSession.command` 里，CLI、TUI、Web 共用同一段代码。不进对话也能用，都不调模型、不要密钥：

```bash
baseagent --session S --checkpoints                 # JSON
baseagent --session S --rewind last [--force]       # 等于 /undo；输出 JSON
baseagent --session S --rewind 3 [--force]          # 第 3 轮及之后
```

库调用：`SessionStore.checkpoints(session_id)`、`SessionStore.rewind(workspace, session_id, "last" | N, force=False)`，或 `baseagent.session.list_checkpoints` / `rewind_session`。代码在 `src/baseagent/session/checkpoints.py`。

## 设计

**钩子只有一个。** 所有写文件都经过 `Workspace._write_file`（`write_file`、`edit_file` 都走它）。在它调用 `tools/edit_policy.py::write_file` 真正落盘之前，`checkpoints.capture()`：

1. 定出归属：`(会话, 轮次号, worker_id)`。
2. 读当前文件。不存在记 `missing`；存在就读字节（上限 200 KB，和读写工具一致），算 sha256。
3. 在一个 SQLite 事务里：这一轮没有检查点就建一个；文件这一轮第一次被写，就把原貌按 sha256 存一份（相同内容只存一份），记下「写前哈希」和「预期写后哈希」（新内容的 sha256）；同一轮再写同一文件，只更新写后哈希，不再存原貌。
4. 任何一步失败（原貌超限、读失败、数据库写失败、已到 500 个文件、这一轮已经被撤销）都抛 `ToolFailure`，文件不写（fail closed）。

写完：后端返回的哈希和预期不同时以后端为准。写失败（哈希冲突、编辑策略拒绝、`post_edit_check` 回滚、中断）时 `abort()` 撤掉这次的记录：第一次写就删掉这条文件记录（检查点空了也删，原貌块没人引用就回收），重复写就把写后哈希改回上一次。`abort()` 先看一眼文件：如果新内容其实已经落盘（例如替换之后才被中断），记录保留。

**轮次号。** 每个新轮次只追加一条用户消息，记录是只追加的（压缩把摘要另存，不删原消息），所以轮次号就是用户消息条数，从 1 开始；Worker 收件箱里的补充要求不算。没有往会话状态里加字段，旧会话天然有号，内核兼容基线不变。

**Worker 的写入挂在派出它的主轮次下。** `delegate_task` 派出 Worker 时记下主 Agent 当前轮次号（`WorkerRecord.main_turn`，并写进 Worker 会话元数据 `main_turn`）。Worker 会话是 `<主会话>-wN`、元数据带 `main_session` / `worker_id`；它的写入记在主会话的那个检查点里，每个文件带 `worker_id`。哪怕 Worker 在第 3 轮才真正写文件，只要是第 1 轮派出的，就算第 1 轮。撤销粒度是主轮次。单 Agent 对话和一次性模式用本会话自己的轮次号。具名子任务（`--subtasks-config`）的写入挂在根会话的当前轮次。

**撤销。** `rewind()` 先拿会话锁（拿不到说明有一轮在跑，拒绝），再检查：

- 主会话这一轮必须已完成；未完成、等待审批都拒绝。
- 注册表里可写的 Worker 不能处于运行中（它的会话锁被占用）或等待审批。对话里 `ChatSession` 还会先查内存中的 Worker 状态和「主 Agent 正在回复」。

然后选出要撤的轮次（`last` = 最近一个未撤销的；`N` = 第 N 轮及之后所有未撤销的），对每个文件算：目标 = 最早那一轮的写前哈希，预期当前 = 最晚那一轮的写后哈希，并检查相邻两轮首尾相接（中间没被别人改过）。逐个比对当前哈希：

- 等于目标：已经是原样，不动；
- 等于预期且链条完整：写回；
- 其他：冲突。

有任何冲突，整批都不动，列出冲突文件，要 `--force`。写回逐个文件加 `file_lock`，锁内再比一次哈希，用临时文件 + fsync + `os.replace`（保留权限位）；目标是 `missing` 的直接删除。路径经过 `editing.write_target`：工作区边界、保护路径（`.git`、`.env*`、`.baseagent`、会话库）、不跟随符号链接。写回是用户自己的恢复操作，不需要 `--allow-write`。

最后在一个事务里把这些检查点标记为已撤销，每个轮次往 `events` 表记一条 `checkpoint_restored`（`--events` 可查），并在主会话元数据里留一个待说明的轮次列表。下一轮开始时，agent 循环在用户消息前插一条 system 消息「第 N 轮的文件改动已撤回……」，只说一次。

## 数据模型（会话库 v7）

v7 只新增三张表，v0–v6 的表和数据原样保留（`CREATE TABLE IF NOT EXISTS` + `PRAGMA user_version=7`）。

| 表 | 字段 | 说明 |
| --- | --- | --- |
| `checkpoints` | `session_id`、`turn`、`created_at`、`restored_at` | 主键 `(session_id, turn)`；`restored_at` 为空表示未撤销。随会话删除级联 |
| `checkpoint_files` | `session_id`、`turn`、`path`、`before_sha256`、`after_sha256`、`worker_id`、`writer_session_id`、`created_at`、`updated_at` | 主键 `(session_id, turn, path)`；`path` 是工作区内相对 POSIX 路径；`before_sha256='missing'` 表示新建 |
| `checkpoint_blobs` | `sha256`、`size`、`content` | 按内容寻址，同一份原貌只存一次；没有任何 `checkpoint_files.before_sha256` 引用时回收 |

`--delete-session` 和 `--cleanup-days --apply-cleanup` 会删掉该会话的检查点和文件记录，再回收所有无人引用的内容块（别的会话还在引用的块保留）。

## 时序

```text
agent             Workspace._write_file      checkpoints.capture      SessionStore(SQLite)       文件系统
  │ write_file(a.txt) │                          │                        │                       │
  │──────────────────▶│ check_path / 权限        │                        │                       │
  │                   │─────────────────────────▶│ 读原貌（≤200 KB）      │                       │
  │                   │                          │──────────────────────────────────────────────▶│
  │                   │                          │ 事务：检查点 + 原貌块 + 文件记录（写前/写后哈希）│
  │                   │                          │───────────────────────▶│                       │
  │                   │◀──────── 存不下：ToolFailure，不写 ────────────────│                       │
  │                   │ edit_policy.write_file → atomic_write                                     │
  │                   │──────────────────────────────────────────────────────────────────────────▶│
  │                   │ 失败/回滚：capture.abort() 删除这次记录                                      │
  │◀──── 结果 ────────│                          │                        │                       │
  ⋮
user /undo        ChatSession              checkpoints.rewind
  │──────────────────▶│ 主轮次在跑？可写 worker 活跃？ → 拒绝
  │                   │─────────────────────────▶│ 会话锁；轮次已完成？
  │                   │                          │ 选轮次，按文件算 目标/预期，比对当前哈希
  │                   │                          │──────────────────────────────────────────────▶│ 读哈希
  │                   │                          │ 有冲突且无 --force → CheckpointError，整批不动
  │                   │                          │ 逐文件：file_lock → 再比哈希 → 临时文件+fsync+replace / 删除
  │                   │                          │──────────────────────────────────────────────▶│
  │                   │                          │ 事务：restored_at、checkpoint_restored 事件、下一轮说明
  │                   │                          │───────────────────────▶│                       │
  │◀── 已撤回第 N 轮 ──│                          │                        │                       │
```

## 限制

- **`run_command` 的副作用撤不回。** 命令改的文件、装的依赖、删掉的东西都不在检查点里。需要可撤销的执行环境时用 DockerBackend（[docker_backend.md](docker_backend.md)），或者用 `--mode read-only` / `--mode edit` 不开命令。`--mode full` 在 LocalBackend 下会打印一行警告说明这一点。
- **只覆盖文件工具。** 只有经过 `Workspace._write_file` 的写入（`write_file`、`edit_file`）有检查点；人手改、编辑器、其他进程的改动不记录，撤销时它们表现为冲突。
- **不能 redo。** 撤销后的轮次关闭，不能再写入，也不能「撤销撤销」。轮次号继续往后编。
- **单个原貌上限 200 KB，单轮最多 500 个文件。** 与读写工具、observed 文件上限一致；超出时那次写入直接失败，文件不变。
- **新建的目录不删。** 撤销删除 agent 新建的文件，但不删它顺带建出的空目录。
- **撤销只支持 LocalBackend。** 写回直接操作本机文件；其他后端会拒绝撤销（记录照常保存，`--checkpoints` 照常列出）。
- **没有会话库就没有检查点。** 直接调用 `Workspace.write_file(context=None)` 或不带 `store` 的 `run_agent` 不记录。
- **撤销中途被别人改动。** 每个文件在锁内会再比一次哈希；万一在两个文件之间被改，已写回的文件会列在错误里，剩下的不动。

## 备选方案：影子 git（考虑过，没选）

做法是在工作区旁边维护一个独立的 git 仓库（`GIT_DIR` 指向会话目录），每轮开始前 `git add -A && git commit`，撤销时 `git checkout` 回去。

好处是能覆盖 `run_command` 造成的改动，redo 也容易。没选的原因：

- **要扫整个工作区。** 每轮一次 `add -A` 要遍历、哈希所有文件，大仓库每轮都要付出这个成本；而现在只碰 agent 写过的那几个文件。
- **依赖 git。** 没装 git、或者工作区本身就是 git 仓库（嵌套、子模块、worktree、`.git` 是文件）时都要特殊处理。现在的实现只用 SQLite 和标准库。
- **ignore 和大文件。** 要决定是否遵守 `.gitignore`（不遵守会把 `node_modules`、构建产物、虚拟环境都存一遍；遵守又会漏掉被忽略但被改的文件），还要处理二进制大文件、换行转换、文件权限和符号链接。
- **与白盒目标不符。** 「这一轮改了什么、原貌是什么」要从另一个仓库的提交里反推，不能在会话库里直接查、导出、和工具账本对上；撤销时也很难做到「逐文件比对写后哈希，冲突整批不动」这种可解释的规则。

所以选了按文件快照：只记录工具写过的文件，规则简单、可测、可审计；代价是命令副作用不在范围内，这一点在文档、`--mode full` 警告和撤销结果里都写明了。
