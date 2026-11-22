# 模型供应商（`--provider`）

CLI 默认行为不变：设置了任一 `BASEAGENT_LLM_*` 时走 OpenAI 兼容配置，否则走 `DEEPSEEK_*`。
`--provider`（或 `--config` 的 `run.provider`）可以显式指定供应商：

| `--provider` | 读取的环境变量 | 模型 | 适配器 |
|---|---|---|---|
| 不写（默认） | 任一 `BASEAGENT_LLM_*` 已设置 → `BASEAGENT_LLM_*`；否则 `DEEPSEEK_*` | `--model`，否则 `BASEAGENT_LLM_MODEL` / `DEEPSEEK_MODEL`（默认 `deepseek-chat`） | `Model`（OpenAI SDK） |
| `openai-compatible` | 只读 `BASEAGENT_LLM_API_KEY` / `BASE_URL` / `MODEL` / `EXTRA_BODY` / `PROVIDER` | `--model` 或 `BASEAGENT_LLM_MODEL` | `Model` |
| `deepseek` | 只读 `DEEPSEEK_API_KEY` / `BASE_URL` / `MODEL`（即使设置了 `BASEAGENT_LLM_*`） | `--model` 或 `DEEPSEEK_MODEL`（默认 `deepseek-chat`） | `Model` |
| `anthropic` | 只读 `ANTHROPIC_API_KEY`（必填）、`ANTHROPIC_BASE_URL`（默认 `https://api.anthropic.com`）、`ANTHROPIC_MODEL` | `--model`，否则 `ANTHROPIC_MODEL`，否则 `claude-opus-5-5` | `AnthropicModel`（标准库 urllib，无新依赖） |

```bash
export ANTHROPIC_API_KEY=...            # 或写进 .env；CLI 会加载 .env
baseagent --provider anthropic "总结 README"
baseagent --provider anthropic --model claude-sonnet-5-5 --effort medium "..."
baseagent --provider anthropic --model claude-haiku-5-5 --thinking adaptive --root . --session demo
```

`--config` 写法（`run` 段）：`{"version": 1, "run": {"provider": "anthropic", "model": "claude-sonnet-5-5", "effort": "medium", "thinking": "adaptive"}}`，命令行参数优先。

## 凭据隔离

- 与已有规则「设置任一 `BASEAGENT_LLM_*` 后不再混用 `DEEPSEEK` 凭据」一致：每个供应商只读自己命名空间的变量。
- `anthropic` 不读 `BASEAGENT_LLM_*` / `DEEPSEEK_*` 的任何变量，**包括 `BASEAGENT_LLM_MODEL`**：`.env` 里为 Qwen 等配置的模型名不会被发给 Anthropic；Anthropic 模型用 `--model` 或 `ANTHROPIC_MODEL` 指定。`BASEAGENT_LLM_EXTRA_BODY` 也不适用。
- OpenAI 兼容 / DeepSeek 路径从不读 `ANTHROPIC_*`。
- 密钥只由适配器在发请求时从 `ANTHROPIC_API_KEY` 读取，不写进会话、运行时契约、事件或错误信息。
- 缺少 `ANTHROPIC_API_KEY` 时，在任何模型调用之前退出（退出码 1，`Session error: --provider anthropic requires ANTHROPIC_API_KEY ...`），不会回退到别的供应商的密钥，也不创建会话。

## 会话与运行时契约

`--provider anthropic` 把 `provider: "anthropic"`、模型、端点和 `anthropic: {max_tokens, thinking, output_config}` 记进运行时配置（只存摘要，不存密钥）。所以未完成的回合用另一个供应商（或改了 thinking/effort）`--resume` 时，和换模型一样报 `session runtime configuration changed: runtime`，需要 `--accept-config-changes`。不写 `--provider` 时契约与以前完全相同，已有会话照常续跑；显式 `--provider deepseek` / `openai-compatible` 只记录各自命名空间的模型与端点。

## Anthropic 专用参数

- `--max-completion-tokens`：每次请求的 `max_tokens`。`anthropic` 下默认 16000（其它供应商仍是 4096）；思考也计入这个上限。
- `--effort low|medium|high|xhigh|max` → `output_config.effort`。不写则用模型默认（例如 Claude Opus 5.5 默认 `medium`）。
- `--thinking adaptive|disabled|N`：
  - `adaptive` → `thinking: {type: "adaptive"}`；
  - `disabled` → `{type: "disabled"}`；
  - 整数 `N`（≥ 1024 且小于 `--max-completion-tokens`）→ `{type: "enabled", budget_tokens: N}`，只适用于仍支持 `budget_tokens` 的旧模型。
  - 不写则不发送 `thinking`，由模型决定。注意：Claude Opus 5.5 / Sonnet 5.5 / Haiku 5.5 等当前模型思考默认开启（自适应），对 `budget_tokens` 和 `disabled` 会返回 400（Sonnet 5.5 关闭思考另有 `between_tools`，这里未提供）；控制深度请用 `--effort`。
- 请求超时 600 秒（思考模型回复慢）；`--max-duration-seconds` 等回合截止时间照样生效。
- 以下参数与 `anthropic` 组合会在解析阶段报错：`--stream`、`--preauthorize-model`、`--request-bound-profile`、`--estimate-model`、`--tokenizer-file`（Anthropic 适配器没有请求预留/估算）。`--thinking` / `--effort` 只能配 `--provider anthropic`。

## 思考块的保存与回传

Anthropic 要求在「思考 + 工具调用」的多步回合里，把上一条 assistant 回复的 `thinking` / `redacted_thinking` 块原样（含 `signature` / `data`）传回。实现：

- 响应里有思考块时，assistant 消息除了照旧的 `content`（文本块用 `\n` 拼接）和 `tool_calls` 外，多一个键 `anthropic_content`：API 返回的完整 `content` 数组，**按原顺序、逐字节不改**（思考、文本、工具调用块都在内）。没有思考块的响应不加这个键，消息形态与以前完全相同。
- `AnthropicModel._payload` 回放该 assistant 消息时：若 `content` / `tool_calls` 与 `anthropic_content` 中的文本和 `tool_use` 一致（消息没被改过），直接原样发回整个数组；若消息被改过（例如被编辑），则先按原顺序放所有思考块，再放由当前 `content` / `tool_calls` 生成的文本和 `tool_use` 块。
- `Message.from_dict(...).to_dict()` 无损往返（未知键进 `meta` 并保序）；配对检查只看 `tool_calls` / `tool_call_id`，不受影响。会话和 cassette 无需迁移。
- OpenAI 兼容路径（`Model`、`OpenAICompatModel`，含流式）在发请求前去掉 `anthropic_content`；没有消息带这个键时直接传原列表，请求逐字节不变。
- 用量：`output_tokens` 本身已含思考 token；若 `usage.output_tokens_details` 报告了 `thinking_tokens`（或 `reasoning_tokens`）且不超过 `output_tokens`，记入 `ModelResponse.reasoning_tokens`，否则为 0。该字段名尚未用真实 API 核实。
- 代价：带思考的 assistant 消息会多存一份文本/工具参数，计入 `--max-context-bytes`。

## stop_reason 处理

OpenAI 兼容路径不检查 `finish_reason`（`length` 截断的文本照样作为回复）。Anthropic 适配器与之保持一致，并对无法使用的情况给出明确错误：

| `stop_reason` | 处理 |
|---|---|
| `end_turn` / `tool_use` / `stop_sequence` | 正常；`end_turn` 且没有文本（只有思考或空）→ 空回答 `content: ""`，回放时跳过空 assistant 轮 |
| `max_tokens` 有文本、无工具调用 | 与 OpenAI `length` 一致：返回截断文本 |
| `max_tokens` 含工具调用 | `ResponseParseError`（工具参数可能不完整），提示调高 `--max-completion-tokens` |
| `max_tokens` 无文本（思考用完上限） | `ResponseParseError`，提示调高 `--max-completion-tokens` |
| `model_context_window_exceeded` | 同 `max_tokens`；无文本时为 `ContextLengthError` |
| `refusal` | `BadRequestError: Anthropic model declined the request: stop_reason refusal (category …)`，不重试；类别只在是简单标识符时显示 |
| `pause_turn` | `ResponseParseError`：只在服务端工具时出现，本适配器不发送服务端工具 |
| 其它/缺失且没有文本和工具调用 | `ResponseParseError` |

这些都是不可重试的 `ModelError`，回合以 `failed` 结束并显示错误。

## 流式

不支持。`--stream` 与 `--provider anthropic` 一起使用时直接报错退出（退出码 2）。原因：现有流式实现（`llm/streaming.py`）只解析 OpenAI chat completion 分块；而且 CLI 默认的模型组合（`main.py` 的 `LazyModel`）本来就没有流式接口（见 [ui.md](ui.md)），加 Anthropic SSE 也看不到效果。库里直接用 `AnthropicModel` 时它没有 `complete_stream*`，内核会自动走非流式。

## 尚未完成：真实 API 验证

以上全部用离线的假 HTTP 层测试（`tests/test_anthropic_provider.py`、`tests/test_model_adapters.py`），**还没有对真实 Anthropic API 跑过**。需要一个 `ANTHROPIC_API_KEY` 和少量费用（默认 `claude-haiku-5-5`，最多 3 次请求）：

```bash
ANTHROPIC_API_KEY=... python scripts/anthropic_smoke.py --yes-spend-money
```

脚本不读 `.env`，不带 `--yes-spend-money` 或没有密钥时不发任何请求；输出只有状态、块类型和 token 数。需要核实的点：思考块回传在真实工具循环里被接受；`usage` 中思考 token 的字段名；`refusal` 的 `stop_details` 形态；`claude-opus-5-5` 等模型对 `--thinking` / `--effort` 的实际约束。
