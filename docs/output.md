# 机器可读输出：`--output-format text|json|jsonl`

一次性任务（`baseagent "prompt"`）和 `--resume` 可以选输出格式。默认 `text`，输出与以前逐字一致（`tests/fixtures/one_shot_text_master.json` 是在 master 上用同一个假模型录下来的，`tests/test_output_format.py` 逐字比对）。

```bash
baseagent --root . --mode edit --output-format json "把 README 的标题改成中文"
baseagent --session S --resume --output-format jsonl
```

退出码三种格式都一样：`0` 本轮完成，`1` 未完成或会话错误，`130` 被中断（Ctrl+C），`3` 是 `--backend docker` 起不来（没装 Docker、daemon 没起、镜像不在本地；在调用模型之前，费用 0，见下面「Docker 后端」）。参数错误（`argparse`）仍是 `2`，只在 stderr 打印用法，不输出 JSON；`--backend docker` 时会话库放在工作区里、又不在 `.baseagent/` 下也是 `2`。

**stdout 只有 JSON。** `json` / `jsonl` 下，给人看的内容全部走 stderr：`Session: …`、`Reported tokens: …`、`--trace` 行、`--stream` 的增量文本、警告（例如 `--mode full`）、`Agent error:` / `Agent stopped:`、主 Agent 会话 `--resume` 时 Worker 的推送。

**适用范围。** 只用于一次性任务和 `--resume`。连续对话（含 `--ui tui/web`）和其他运维操作（`--events`、`--inspect-session`、`--checkpoints`、`--list-sessions` ……）带这个参数会以退出码 2 报错，并写明原因：对话写给终端或界面，运维操作本来就输出 JSON 或固定文本。

**会话 id。** `json` / `jsonl` 下没给 `--session` 时，CLI 先生成一个 id，这样即使在第一次落盘前就失败，结果里也有 `session_id`。

## `json`：一个对象

运行结束时在 stdout 打印一行 JSON：

```json
{"schema_version": 1, "status": "completed", "final_answer": "done", "error": null, "session_id": "golden",
 "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13, "unknown_usage_calls": 0,
           "model_calls": 2, "tool_calls": 1, "steps": 2},
 "exit_code": 0, "pending_approval": null}
```

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 当前为 `1`。字段只增不改；改变含义时升版本 |
| `status` | 本轮状态（`RunStatus` 的值：`completed`、`awaiting_approval`、`max_steps_exceeded`、`failed`、`interrupted` ……）；会话错误为 `error`，中断在开始前为 `interrupted` |
| `final_answer` | 最终回答；没有时为 `null` |
| `error` | 本轮错误；会话错误时是 `Session error: …` 原文 |
| `session_id` | 会话 id |
| `usage` | `prompt_tokens`、`completion_tokens`、`total_tokens`（模型报告的本轮用量）、`unknown_usage_calls`（没报用量的调用数）、`model_calls`、`tool_calls`、`steps`；算得出时加 `cny` |
| `usage.cny` | 只有模型名在已知价目里（目前只有 `qwen-plus`：输入 0.8、输出 2 元/百万 token，与 `--budget-cny` 相同）且 `unknown_usage_calls == 0` 时才出现，四舍五入到 6 位小数 |
| `exit_code` | 与进程退出码相同 |
| `patches` | 可选，只在这一轮有成功的 `apply_patch` 时出现：每次调用一项 `{"call_id", "files", "diff", "diff_truncated"}`（`files` 每项 `path`、`op`、`before_sha256`、`after_sha256`、`added`、`removed`；`diff` 按 20000 字符截断）。没用 `apply_patch` 时没有这个键，输出与以前逐字一致。见 [patch.md](patch.md) |
| `backend` | 可选，只在 `--backend docker` 时出现：`{"kind": "docker", "image", "network", "container", "cpus", "memory", "pids"}`（`network` 是 `none` 或 `bridge`；不含凭据）。**没有这个键就是 local 后端**，所以不传 `--backend`（或 `--backend local`）时输出与以前逐字一致。Docker 起不来时见下面「Docker 后端」 |
| `pending_approval` | `status == "awaiting_approval"` 时为 `{"call_id", "digest", "name"}`（具名子任务节点的审批再加 `task_id`）；否则为 `null`。用 `--approve-tool CALL_ID --approval-digest DIGEST`（节点加 `--task-id`）审批后 `--resume` |

## `jsonl`：事件流 + 结果

每行一个 JSON，统一用 `agent/events.py` 的信封 `{"type", "timestamp", "data"}`，没有别的顶层键：

```text
{"type": "output_started", "timestamp": 1794335063.58, "data": {"schema_version": 1, "format": "jsonl", "session_id": "S"}}
{"type": "run_started", "timestamp": …, "data": {"model_calls": 0, "tool_calls": 0}}
{"type": "model_started", "timestamp": …, "data": {"attempt": 1, "input_bytes": 8757, "reserved_tokens": null, "reservation_mode": null}}
…
{"type": "result", "timestamp": …, "data": { …与 json 格式的对象完全相同… }}
```

- 第一行 `output_started`，带 `schema_version`、`format`、`session_id`。
- 中间每行是这一轮写进 `events` 表的一条元数据事件，顺序与 `baseagent --session S --events` 相同（运行中每 0.1 秒拉一次，结束前补齐）。类型和字段见 README「Structured events」；它们只有元数据：没有提示词、工具参数、工具结果正文或异常文本。
- 两处整理：`model_delta`（`--stream`）里的 `delta` 文本换成 `chars`（字符数），流式文本本身仍实时打到 stderr；具名子任务节点的事件把 `task_id` / `parent_task_id` 放进 `data`。
- `apply_patch` 成功时有一条 `patch_applied`：`{"call_id", "file_count", "files": [{"path", "op", "added", "removed"}]}`，没有 diff 正文、文件内容或参数原文。
- 最后一行一定是 `type: "result"`，`data` 与 `json` 格式打印的对象相同，唯一例外：不带 `patches`（jsonl 不写 diff 正文）。启动阶段就失败时只有 `output_started` 和 `result` 两行。

版本规则同上：`schema_version` 变化才表示行的含义变了；新增事件类型或字段不升版本，读取方应忽略不认识的类型和字段。

## Docker 后端（`--backend docker`）

运行成功或失败的结果对象都多一个 `backend`（上表）。`jsonl` 在 `output_started` 之后多一行：

```text
{"type": "backend_ready", "timestamp": …, "data": {"kind": "docker", "image": "python:3.12-slim", "network": "none", "container": "baseagent-…", "cpus": 1, "memory": "512m", "pids": 128}}
```

**Docker 起不来（退出码 3）。** 没装 Docker、daemon 连不上、镜像不在本地时，CLI 在调用模型之前停下，stderr 一行 `Docker error (<kind>): <原因和解决办法>`，绝不退回 local。`kind` 是 `cli_missing`、`daemon_unreachable` 或 `image_missing`。`json` 输出：

```json
{"schema_version": 1, "status": "error", "final_answer": null,
 "error": "Docker error (image_missing): Docker image is missing locally: python:3.12-slim. Pull it first: docker pull python:3.12-slim",
 "session_id": "…", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "unknown_usage_calls": 0,
 "model_calls": 0, "tool_calls": 0, "steps": 0}, "exit_code": 3, "pending_approval": null,
 "backend": {"kind": "docker", "image": "python:3.12-slim", "network": "none", "container": null, "unavailable": "image_missing"}}
```

`jsonl` 这时只有 `output_started` 和 `result` 两行（没有 `backend_ready`），`result` 的 `data` 同上。会话不会建出来。运行中容器挂掉不是退出码 3：那一轮以 `failed` 落盘（退出码 1），`error` 写明容器已不在运行、修好后 `--resume`。详见 [docker_backend.md](docker_backend.md)。
