# 内部消息类型（`baseagent.kernel.messages`）

## 目的

`KernelState.messages` 目前存放 OpenAI chat 风格的原始 dict。各处代码直接读写
`message["role"]`、`tool_calls`、`tool_call_id`，合法性校验分散在
`kernel/agent.py`（`_tool_calls`、`_pending_calls`）、`kernel/context.py`
（`_exchanges`）和各模型适配器里。本模块提供一个**不可变、带校验**的类型视图，
作为后续统一校验与适配的基础。本次只新增类型与测试，**不迁移任何调用点**，
存储格式不变。

## 字段

- `Message(role, content, tool_calls, tool_call_id, meta, key_order)`
  - `role`：`system` / `developer` / `user` / `assistant` / `tool`。
  - `content`：字符串、`None`、`ABSENT`（键不存在）或内容片段元组。
  - `tool_calls`：`ToolCall` 元组、`None`、`ABSENT`。
  - `tool_call_id`：仅 `tool` 消息有。
  - `meta`：未知或供应商特有键（`reasoning_content`、`name`、`refusal` 等），只读 `FrozenDict`。
  - `key_order`：原始键顺序，仅用于无损还原。
- `ToolCall(id, name, arguments, type, extra, function_extra, ...)`：`arguments`
  保持模型给出的**原始 JSON 字符串**。审批和请求摘要绑定的是这段原文，绝不解析后重新序列化。
  `extra` 保留诸如流式 `index` 之类的附加键。
- `TextPart(text)` / `RawPart(data)`：内容片段；只有键顺序恰为 `type, text` 的文本片段映射为
  `TextPart`，其余（图片、顺序不同的文本片段等）原样保存在 `RawPart`。
- `ToolReply(call_id, content, is_error)`：`Message.tool_reply()` 返回的语义视图；`is_error`
  与 Anthropic 适配器相同，按 `ToolResult` 信封 `ok is False` 且 `error` 为对象判断。
  命名避开已有的 `kernel.result.ToolResult`。

`__post_init__` 校验：角色合法；只有 `assistant` 可带 `tool_calls`；只有 `tool` 可带
`tool_call_id`，且必须为非空字符串，内容必须是字符串或片段；调用 id 非空且在消息内唯一；
`name` / `arguments` 为字符串；所有数据必须是 JSON 形状。所有容器在构造时深拷贝并冻结。

## 往返保证

对仓库中实际产生或存储的每一种消息形态：

```python
Message.from_dict(d).to_dict() == d
json.dumps(Message.from_dict(d).to_dict(), ensure_ascii=False) == json.dumps(d, ensure_ascii=False)
```

即键顺序（包括 cassette 中按字母排序的键、tool call 内部键顺序）、`None` 与缺省、
`content` 为 `None` 与 `""` 都被保留。因此已存会话和 cassette 无需迁移。
`tests/test_messages.py` 对手写样例和全部已提交 fixture（约 3700 条消息）逐条验证。

已知形态：`system`/`user`/`developer` 的 `{role, content}`；内核写入的
`{role, tool_call_id, content}`；Anthropic 适配器与流式组装的
`{role, content: None, tool_calls}`；OpenAI `model_dump(exclude_none=True)` 产生的无
`content` 形态；DeepSeek cassette 中带 `reasoning_content`、tool call 带 `index`、键排序的形态。

## 不变量

`check_tool_pairing(messages, allow_pending=True)` 正向扫描：

1. assistant 的每个 tool call 必须在下一条非 tool 消息之前得到 `tool` 回复；
2. `tool` 回复只能回答紧邻的上一批调用，且只能回答一次；未知 id 报错；
3. 同一用户轮内调用 id 不得复用（新的 `user` 消息后可复用，与 `context._exchanges` 一致）；
4. `allow_pending=False` 时，结尾仍未回复的调用也报错（用于模型调用前）。

违规抛出 `ToolPairingError(ValueError)`，带 `index` 与 `call_ids`。
`unanswered_tool_calls(messages)` 返回结尾那一批尚未回复的调用。
注意：现有 `_pending_calls` 反向扫描遇到 `user` 就停止，"孤儿调用后紧跟 user 消息"
会被静默忽略；新辅助函数会检测到这种情况。

## 迁移计划

1. （本次）新增类型、往返测试与不变量函数，不改调用点。
2. 在内核接入不变量：`kernel/agent.py` 主循环中调用模型前
   （`driver.model_result(ModelRequest(...))` 之前）对 `state.messages` 执行
   `check_tool_pairing(..., allow_pending=False)`；用 `unanswered_tool_calls` 替换
   `_pending_calls`；会话恢复（`session/store.py`、`session/task_recovery.py` 补写
   tool 回复之后）也做一次检查。`context._exchanges` 的配对部分可改为复用该函数。
3. 模型边界类型化：`_as_dict` / `_tool_calls` 改为 `Message.from_dict`，适配器
   （`AnthropicModel._payload/_response`、流式组装）以 `Message` 为输入输出。
4. 中间件和上层模块逐步改读 `Message` 语义访问器（`calls`、`text`、`tool_reply()`），
   存储仍为 `to_dict()` 的结果。
5. 最后再评估 `KernelState.messages` 是否直接存 `Message`（需同步调整序列化与 hooks 的深拷贝）。

## 开放问题

以消息为中心（当前方案：状态即消息列表，派生信息靠扫描）还是采用类似 DeepSeek
Harness 的**只追加事件日志**（模型请求、调用、回复、审批、压缩都是事件，消息列表是投影）？
事件日志更利于审计、断点恢复和压缩可追溯，但要求重写会话存储与上下文构建；
本类型在两种方案下都可作为"投影后的消息"复用，因此先落地，不预先决定。
