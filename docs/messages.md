# 内部消息类型（`baseagent.kernel.messages`）

## 目的

`KernelState.messages` 目前存放 OpenAI chat 风格的原始 dict。各处代码直接读写
`message["role"]`、`tool_calls`、`tool_call_id`，合法性校验分散在
`kernel/agent.py`（`_tool_calls`、`_pending_calls`）、`kernel/context.py`
（`_exchanges`）和各模型适配器里。本模块提供一个**不可变、带校验**的类型视图，
作为后续统一校验与适配的基础。配对不变量已接入内核循环与会话持久化（见下文
"已强制的位置"），存储格式不变。

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
`{role, content: None, tool_calls}`；Anthropic 带思考块时多一个 `anthropic_content`
（响应原始块数组，见 [providers.md](providers.md)）；OpenAI `model_dump(exclude_none=True)` 产生的无
`content` 形态；DeepSeek cassette 中带 `reasoning_content`、tool call 带 `index`、键排序的形态。

## 不变量

`check_tool_pairing(messages, allow_pending=True)` 正向扫描：

1. assistant 的每个 tool call 必须在下一条非 tool 消息之前得到 `tool` 回复；
2. `tool` 回复只能回答紧邻的上一批调用，且只能回答一次；未知 id 报错；
3. 同一用户轮内调用 id 不得复用（新的 `user` 消息后可复用，与 `context._exchanges` 一致）；
4. `allow_pending=False` 时，结尾仍未回复的调用也报错（用于模型调用前）。

违规抛出 `ToolPairingError(ValueError)`，带 `index` 与 `call_ids`。
`unanswered_tool_calls(messages)` 返回结尾那一批尚未回复的调用。

`open_tool_batches(messages)` 列出每个仍有未回复调用的 assistant 批次
（`OpenToolBatch(assistant_index, insert_at, call_ids, superseded)`）：回复只在下一条
`user`/`assistant` 消息之前匹配；其后若还有 `user`/`assistant` 消息，该批次即
**被取代**（`superseded=True`），`insert_at` 是该批次已有连续回复之后、下一条非 tool
消息之前的位置。夹在中间的 `system`/`developer` 消息既不算回复也不算取代，
其位置是否合法留给模型调用前的检查判断。

## 已强制的位置

1. **模型调用前（`kernel/agent.py` 主循环）**：`driver.before_model(state)` 之后、
   `model_started` 事件与 `driver.model_result(...)` 之前，对本次真正要发送的对话
   `driver.messages()`（单任务驱动是 `state.messages` 的副本；harness 驱动是
   `project_history(state)` 的投影，含摘要与 spill 替换）执行
   `check_tool_pairing(..., allow_pending=False)`。检查的是整段投影而非
   `turn_start` 之后，因为发给供应商的就是整段。同一个对象随后作为
   `ModelRequest.messages` 发出，中间件 `before_model` 追加的消息也在检查范围内。
2. **违规处理**：循环中任何 `ToolPairingError` 都由内核直接处理（不经驱动的
   `handle_exception`），状态置为 `RunStatus.NEEDS_RECOVERY`，`state.error` 以固定前缀
   `invalid tool call pairing; the conversation was not sent to the model: ` 开头并附具体
   违规（消息序号、调用 id）。模型计数不增加，请求不会到达供应商。选用已有的
   `NEEDS_RECOVERY` 而不新增状态：对话需要人工核对/修复后才能继续，未修复就恢复会
   在发送前再次被拒绝，结果稳定。
3. **待执行调用**：`_pending_calls` 改为基于 `open_tool_batches`，只返回**未被取代**的
   最后一批中尚未回复的调用（与原语义一致：中断/审批暂停后恢复时继续执行该批）。
   被取代的批次永远不会被执行。
4. **`context._exchanges`**：改为先调用 `check_tool_pairing(..., allow_pending=False)`，
   再按"tool 回复归入前一组"分组。规则与原实现一致（回复必须对应紧邻批次、
   新 `user` 后可复用 id、未知角色报错），异常类型变为 `ToolPairingError`（仍是
   `ValueError`），且额外做 `Message` 形状校验。`compact_history` 与 `ContextPolicy.build`
   因而也抛 `ToolPairingError`，在循环内同样归为 `NEEDS_RECOVERY`。
5. **会话加载**：`SessionStore.load`、任务节点 `TaskNodeStore`（构造与 `load`）对每条
   存储消息执行 `Message.from_dict`，失败时抛
   `ValueError("session 'id': stored message N is malformed: ...")`（节点为 `task 'id'`）。
   加载时**不**要求配对，以便内核能对旧会话中的孤儿调用做修复或拒绝。
6. **恢复补写**：`SessionStore.resolve_call` 与 `task_recovery.resolve_call` 追加经核实的
   tool 回复后，立即 `check_tool_pairing(..., allow_pending=True)`；若追加后对话不再配对
   （例如该调用之后已有 user 消息），抛 `ValueError("... recovered transcript breaks tool
   call pairing: ...")`，账本保持 `running`，状态不保存。

## 孤儿调用的修复规则

在每次 `iter_agent` 开始（`before_agent` 之后、进入循环之前，即新轮开始或恢复时）：

1. 找出所有被取代的批次中未回复的调用。
2. 若某调用在 `state.events` 中有开始证据（`tool_started` / `tool_call_started` /
   `tool_attempt`）但没有完成证据（`tool_completed` / `tool_call_finished`），其结果
   **未知**：不做任何修改，抛 `ToolPairingError` → `NEEDS_RECOVERY`，交由人工核对。
   harness 带存储时，账本中 `running` 的调用在进入内核前已被 `recovery_blocked` 拦下，
   与此规则一致。
3. 否则为每个调用合成一条错误回复
   `ToolResult.failure(ErrorCode.CANCELLED, "tool call was not run: the turn stopped before
   this call executed and a later message superseded it; do not assume its effects")`
   （编码方式与内核写入的回复相同，`ok=false`），插入到 `insert_at`，即该 assistant
   批次已有回复之后、取代它的消息（以及其前的 system 通知）之前。若插入位置
   `<= turn_start`，`turn_start` 相应后移，使合成回复归属上一轮。
4. 修复后的整段对话必须通过 `check_tool_pairing(..., allow_pending=True)`，否则（如未知
   id 的回复、重复回复）原样拒绝，状态 `NEEDS_RECOVERY`。
5. 修复成功时发出 `tool_calls_superseded` 事件（`data.call_ids`）；harness 驱动同时
   checkpoint，合成回复随之持久化。账本中这些调用保持 `pending`（从未开始），
   不会被执行，也不声称已执行。

被取代的调用不会在恢复时静默执行：只有未被取代的最后一批才继续执行，这与原有
账本语义一致（`completed` 直接复用结果，`running` 阻塞待核对，`pending` 才执行）。

已知局限：开始证据依赖 `state.events`（单任务驱动只保留最近 100 条；harness 带存储时
事件在数据库中，依赖账本拦截）；修复会在历史中间插入消息，已有摘要
（`history_summary` 的 `source_digest`）若覆盖插入点将失效并退回原始历史，按索引记录
的 spill 替换因 `call_id` 校验不再匹配而不生效，两者都只降级、不出错。
watchdog 在 `before_tool` 阶段追加的提示 `system` 消息会落在批次中间，这本身违反
不变量（此前由 `ContextPolicy` 以 `FAILED` 拒绝），现在在发送前以 `NEEDS_RECOVERY` 拒绝；
该位置问题本身未在此修改。

## 迁移计划

1. （已完成）新增类型、往返测试与不变量函数。
2. （已完成）在内核与持久化接入不变量，见"已强制的位置"与"孤儿调用的修复规则"。
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
