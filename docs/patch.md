# 多文件原子补丁：`apply_patch`

模型一次调用就能新增、修改、删除多个文件。这一批要么全部生效，要么一个文件都不改。成功后 CLI / TUI / Web 和 `--output-format json` 都能看到整批的 unified diff，`/undo` 整批撤回。代码在 `src/baseagent/tools/patch.py`，测试在 `tests/test_apply_patch*.py`，离线演示 `python -B scripts/demo_patch.py`（费用 0）。

## 入参

```json
{"operations": [
  {"op": "modify", "path": "app.py", "expected_sha256": "<当前哈希>", "expected_instructions": "<指令摘要>",
   "edits": [{"old_text": "\"hello\"", "new_text": "\"你好\""}]},
  {"op": "add", "path": "notes.md", "expected_sha256": "missing", "expected_instructions": "<指令摘要>",
   "content": "- 问候改成了中文\n"},
  {"op": "delete", "path": "old.txt", "expected_sha256": "<当前哈希>", "expected_instructions": "<指令摘要>"}
]}
```

- `operations`：1 到 `MAX_PATCH_FILES`（50）项，顶层和每项都是 `additionalProperties: false`。
- 每项都有 `op`、`path`、`expected_sha256`、`expected_instructions`（与 `write_file` 相同的指令摘要，来自 `read_file` / `get_instructions`）。`expected_sha256` 始终是字符串，不用 `null`。
- `modify`：必填 `edits`，不许有 `content`。`edits` 与 `edit_file` 完全相同：1–20 条 `{old_text, new_text}`，直接调用现有的 `apply_edits`：每条 `old_text` 非空、在当前内容里恰好出现一次、按顺序替换，替换后不超过 200 KB。`expected_sha256` 是当前文件的 64 位小写十六进制哈希，文件必须存在。
- `add`：必填完整 `content`，不许有 `edits`；`expected_sha256` 必须是 `"missing"`（与 `write_file` 新建相同），文件必须不存在。
- `delete`：只带 `expected_sha256`（当前哈希）和 `expected_instructions`，不许有 `content` / `edits`。
- 同一批里同一路径只能出现一次。比较的是工作区规范化后的相对路径，**在所有系统上都不分大小写**（`a.txt` 与 `A.TXT` 算重复）。这比「按系统规则」更严，但只会多拒绝，不会放过，规则也不随机器变化。
- schema 是一个扁平的 item 对象（`op` 用 `enum`，`content` / `edits` 可选），按 `op` 区分的必填/禁止字段在代码里检查。没有用 `oneOf`，是因为部分模型服务对嵌套的组合 schema 支持不好；代码检查给出点名文件的 `invalid_arguments`。

### 为什么不用 unified diff 当入参，也不对 `modify` 传全文

- 模型只学一套编辑格式：`modify` 就是 `edit_file` 的 `edits`，语义一字不改，也没有另写的打补丁或模糊匹配。
- 改文件时不重传全文，比整文件内容省 token；`add` 本来就只能给全文。
- 展示用的 diff 由我们根据底本和替换后的新内容自己算（`difflib`），格式稳定，不依赖模型写对 diff 语法（行号、上下文行、`\ No newline`）。

## 返回

```json
{"files": [{"path": "app.py", "op": "modify", "before_sha256": "…", "after_sha256": "…", "added": 1, "removed": 1},
           {"path": "notes.md", "op": "add", "before_sha256": "missing", "after_sha256": "…", "added": 1, "removed": 0},
           {"path": "old.txt", "op": "delete", "before_sha256": "…", "after_sha256": "missing", "added": 0, "removed": 1}],
 "added": 2, "removed": 2, "diff": "--- a/app.py\n+++ b/app.py\n@@ …", "diff_truncated": false}
```

- `added` / `removed`：按行计（`SequenceMatcher` 的增删行，与 `EditPolicy.changed_lines` 同一算法，换行符变化也算）。
- `diff`：整批 unified diff，`a/` `b/` 前缀；新建以 `/dev/null` 为底，删除以 `/dev/null` 为目标；文件末尾没有换行时加 `\ No newline at end of file`。由快照和落盘后的新内容计算，不采用模型提交的任何 diff。
- **截断规则**：diff 正文最多 `MAX_PATCH_DIFF_CHARS`（20000）个字符，按整行截（不切半行）；超出时 `diff_truncated: true`，并在末尾追加一行标记：`[diff truncated at 20000 characters: N more lines in M file(s) not shown; the full request is in the tool ledger (--inspect-session)]`。M 是没有完整显示的文件数。同一上限用于工具结果、`json` 输出、CLI / TUI / Web 显示和待批预览。

## 流程：校验 → 快照 → 写入 → 回滚

1. **校验整批，不写任何字节。** 按顺序检查每一项：字段；工作区边界与受保护路径（`.git`、`.venv`、`.baseagent`、`.env*`、`.baseagent-edit-*`、会话库，即 `Workspace._path` 的规则，与 `write_file` 相同）；链接 / junction 别名（词法路径必须等于解析后的路径）；目标不能是目录；`EditPolicy` 的 allowlist / denylist；重复路径；当前哈希等于 `expected_sha256`（`add` 时文件必须不存在）；指令摘要仍是当前值；对 `modify` 在内存里跑 `apply_edits`；新内容不超过 200 KB（`MAX_BLOB_BYTES`）。然后整批检查：文件数 ≤ 50、本轮检查点剩余文件数（`MAX_FILES_PER_TURN` 500）和观察记录（500）够放整批、`EditPolicy.max_changed_lines` 按整批合计。任一不过：返回 `ToolFailure`，信息以第一个出问题的文件开头（`b.txt: file version changed …; no file was written`）。错误码沿用现有的：哈希不符、替换不唯一、文件已存在 / 不存在、指令变化用 `conflict`；越界、受保护、链接、编辑策略用 `permission_denied`；超限用 `limit_exceeded`；字段不合法、重复路径用 `invalid_arguments`。这里说的「忽略规则」是指上面这套受保护路径规则；`.gitignore` 只影响搜索，`write_file` 不看它，`apply_patch` 也不看。
2. **快照。** 内存里保存每个文件的原字节（或「不存在」），LocalBackend 下还记下文件权限位。
3. **逐个写入。** 修改和新建经过 `Workspace._write_file`，删除经过新加的 `Workspace._delete_file`，所以每个文件都走同一套边界、编辑策略、检查点捕获、CAS（写前最后一次比对哈希）和观察记录。每个文件之间检查一次取消。
4. **写到一半失败**（后端写失败、写前最后一次 CAS 冲突、`post_edit_check` 失败、取消 / 中断）：已经写下去的文件按快照**倒序**恢复：修改的写回原字节，新建的删掉，删除的写回原字节并恢复权限位。工作区回到调用前的字节状态。恢复直接调用后端的 `write_file` / `delete_file`（仍是带 CAS 的原子发布），不再经过编辑策略和检查点。恢复成功后：这批写入在本轮检查点里的记录全部撤掉（本轮之前其它写入的记录原样保留），本轮的 `observed_files`、`edited_this_turn`、验证记录也恢复到调用前，所以 `/checkpoints` 看不到这批文件被改过。返回 `ToolFailure`，以失败的文件开头，带 `patch rolled back, no file changed`。取消返回 `cancelled`；`KeyboardInterrupt` 这类非普通异常在回滚后原样抛出。
5. **回滚本身失败（二次故障）**：某个文件写不回去，或者在我们写完后又被别人改了（不覆盖不认识的内容），就记为「状态未知」。这时这批的检查点记录**全部保留**（原貌不丢），返回 `execution_failed`：`d.txt: …; rollback failed: state unknown for a.txt. Their originals stay in this turn's checkpoint; inspect them and use /undo (or --rewind last) to restore`。已经恢复好的文件在 `/undo` 时表现为「已是原样」。失败的那个文件如果我们的写入根本没落盘（例如 CAS 冲突），它不是我们改的，不动它，也不算状态未知。

原子性只针对本工具自己的写入，**不是文件系统事务**。外部编辑器在两次单文件发布之间改文件，仍按现有乐观检测处理：写前 CAS 失败就整批回滚。删除也是乐观的：在协作写锁下比两次哈希再 `unlink`，POSIX 没有「比较后删除」，不配合的进程在这两步之间改文件检测不到。

## 删除入口与后端协议（拍板）

- `WorkspaceBackend` 协议新增 `delete_file(path, expected_sha256, expected_instructions) -> dict`，返回 `sha256: "missing"`。哈希不符或文件不存在是 `conflict`。实现：`LocalBackend`（`tools/editing.py::atomic_delete`：同一把文件锁、两次比对、`unlink`，可选删除后检查）、`DockerBackend`（宿主上同一把文件锁，容器里比对哈希后 `rm`，可选删除后检查）、测试里的内存后端。
- `Workspace._delete_file` 与 `_write_file` 对称：观察容量 → `allow_write` → `_path` → `EditPolicy.check_path` → `checkpoints.capture(content=None)`（写后哈希记 `missing`）→ `edit_policy.delete_file`（`max_changed_lines`、`post_edit_check`，失败时在锁内把原字节写回）→ `commit` / `abort` → 观察记录与验证失效。不绕开 Workspace 直接 `unlink`。
- `_write_file` / `_delete_file` 多了一个只给 `apply_patch` 用的可选参数 `captures`，用来收集这批的检查点记录，以便回滚时撤掉；不传时行为不变。
- 契约变化：`WorkspaceBackend` 是 `runtime_checkable` 协议，第三方后端要补 `delete_file` 才能再传给 `Workspace`。各后端的 `contract()` 没变，会话库结构和 `PRAGMA user_version` 没变；旧会话照常打开。代码改动本身会改变 `harness_source` 摘要，未完成的旧会话续跑照旧需要 `--accept-config-changes`（与任何代码升级相同）。

## 和 `write_file` / `edit_file` 怎么选

| 情况 | 用 |
| --- | --- |
| 改一个文件的几处 | `edit_file` |
| 新建或整份替换一个文件 | `write_file` |
| 几个文件必须一起改（改名函数及其调用方、加文件并登记、删文件并去掉引用），或者要删除文件 | `apply_patch` |

`write_file` / `edit_file` 的名字、参数、描述和行为都没变。不会把一轮里的多次 `write_file` 自动攒成事务；`run_command` 也不在事务里。

## 审批跟 `--mode` / `--tool-policy` 走

- `--mode read-only`：`apply_patch` 不在发给模型的工具列表里（`ExecutionGuidanceMiddleware` 隐藏，并列在 `Disabled tools`）；硬调用返回 `permission_denied`。没注册 `apply_patch` 的工具集，提示词逐字不变。
- `--mode edit|full`，没写 `--tool-policy` 或策略对它是 `allow`：直接执行，不停，跑完展示整批 diff。没有新增默认审批。
- 策略写了 `{"tools": {"apply_patch": "ask"}}`：走现有的 `awaiting_approval` 和 call id + digest。待批信息里有文件个数和整批 diff（按当前工作区现算，同样按 20000 字符截断，截断时提示用 `--inspect-session` 看完整请求）。digest 是参数原文的 `request_digest`，覆盖整批：任一文件的路径、操作、底本哈希、`edits` 或 `content` 变了，旧批准作废。批准只针对这一次调用；恢复执行时仍要通过上面第 1 步的全部校验（包括再跑一次 `apply_edits`），批准后文件被别人改了或替换不再唯一，照样整批拒绝。
- `EditPolicy.max_changed_lines`：按**整批**增删行数合计判断（每个文件都不超、合计超也算超），超出时整批改为待审批（`EditToolPolicy` 对 `apply_patch` 预演整批）；绕过 agent 的库调用没批准就在写入前 `permission_denied`。
- `EditPolicy.post_edit_check`：每写完一个文件跑一次，任一失败整批回滚。注意它是在批次写到一半时运行的，检查命令要能容忍中间状态（例如只做语法检查）。
- `deny`、钩子（`restrict(request, "deny"|"ask")`）、中间件都只能再收紧：放宽 mode、工作区边界或受保护路径不行。Chainlit 启动器（`baseagent-ui`）与 `write_file` 一样，对 `apply_patch` 默认 `ask`，没开写权限时 `deny`。
- 计作写工具：不是 `retry_safe`，不自动重试；只读 Worker 看不到它；已加入 `agent/router.py` 的 `WRITE_TOOLS`，「同时只有一个可写 Worker」照旧按 `allow_writes` 判定。

## 检查点与 `/undo`

一次成功的 `apply_patch` 里的所有文件都记在当前轮次的同一个检查点里（Worker 的写入挂在派出它的主轮次下）。`/checkpoints` 列出「新建 / 修改 / 删除」（删除是写后哈希 `missing` 的记录）。`/undo`、`/rewind N`、`--rewind last` 把这一批一起撤回：修改的写回原貌、新建的删除、删除的恢复。`--force`、漂移检测、等待审批时拒绝撤销等规则不变。同一轮先 `write_file` 再 `apply_patch` 同一文件，撤回到这一轮之前的原貌。`/undo` 重建被删文件时权限位用默认值（与 `/undo` 重建其它文件相同）；只有 `apply_patch` 自己的回滚会恢复权限位。

## 展示与输出（拍板）

- **CLI 文本**：一次性任务和 `--ui cli` 对话里，成功后在 stderr 的工具结果下面打印 `apply_patch 已应用：N 个文件（新建 a，修改 b，删除 c），+x -y 行` 和整批 diff（`PatchDisplayMiddleware`，与 `--trace` 无关）。`ask` 时对话的审批提示里先打印 `apply_patch 待审批：…` 和 diff 再问 `Allow apply_patch? [y/N]`；一次性任务停在审批时在 stderr 打印同样内容和 `--approve-tool` 命令。
- **TUI / Web**：共用会话 / 事件层。成功后 `ToolEventMiddleware` 发一条 `notice`（文本就是上面那段）；待批时 `ChatSession` 在 `approval_requested` 之前发一条同样的 `notice`，审批视图里多一个 `patch` 字段。两个前端本来就渲染 `notice`，没有新增前端分支。
- **`--output-format json`**：结果对象新增可选字段 `patches`：这一轮每次成功调用一项 `{call_id, files, diff, diff_truncated}`，从工具账本读（不受上下文裁剪影响）。没用 `apply_patch` 时没有这个字段，输出与以前逐字一致；`schema_version` 仍是 1。
- **`jsonl`**：新增元数据事件 `patch_applied`：`{call_id, file_count, files: [{path, op, added, removed}]}`，没有 diff 正文、文件内容或参数原文。为了守住这一点，jsonl 最后一行 `result` **不带** `patches`（其余字段与 `json` 相同）。

## 上限常量

| 常量 | 值 | 位置 |
| --- | --- | --- |
| `MAX_PATCH_FILES` | 50 个文件 / 次 | `tools/patch.py` |
| `MAX_PATCH_DIFF_CHARS` | 20000 字符 | `tools/patch.py` |
| 单文件新内容 / 原貌 | 200 KB（`MAX_BLOB_BYTES`） | `session/checkpoints.py` |
| `edits` | 1–20 条 / 文件 | `apply_edits` |
| 每轮检查点文件 | 500（`MAX_FILES_PER_TURN`） | `session/checkpoints.py` |
| 每轮观察记录 | 500 | `Workspace._observe` |

## 限制

- 不包 `run_command`：命令的副作用不在批次里，也撤不回。
- 不是文件系统事务：见上面的乐观检测说明。
- 只处理 UTF-8 文本：`modify` / `delete` 的目标必须能按 UTF-8 解码（diff 和回滚都需要文本）。
- Watchdog 和 HarnessKnight 的写工具名单本支没改，它们暂时不把 `apply_patch` 当文件写入识别（Watchdog 按 `WRITE_TOOLS` 记录「这一步有写入」用于停滞判断，HarnessKnight 的 `infer_impacts` 只把 `write_file` / `edit_file` 归为文件写入；`apply_patch` 在两者里都按普通工具处理）。
- `--backend docker` 下全有或全无同样成立：校验在容器里读完、算完才写；回滚用 `DockerBackend.restore_bytes` 在同一把文件锁内把原字节和权限位写回（删除也有锁）；`/undo` 也支持（[docker_backend.md](docker_backend.md)）。其它没有 `restore_bytes` 的第三方后端，回滚仍靠它的 `write_file` / `delete_file`。
