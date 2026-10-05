"""Bound the model's input without rewriting the durable conversation.

Budgets are serialized UTF-8 bytes, not a claim about a provider's tokenization.
Completed old turns and then old exchanges can be removed as whole units.
"""

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any


class ContextLimitExceeded(ValueError):
    pass


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def input_bytes(messages: list[dict], tools: list[dict]) -> int:
    return len(encoded({"messages": messages, "tools": tools}))


def _exchanges(messages: list[dict]) -> list[list[dict]]:
    """Validate each assistant/tool exchange and keep it indivisible."""
    groups = []
    pending = set()
    used = set()
    for message in messages:
        role = message.get("role")
        if role == "tool":
            identifier = message.get("tool_call_id")
            if identifier not in pending:
                raise ValueError("tool reply has no matching pending call")
            pending.remove(identifier)
            groups[-1].append(message)
            continue
        if pending:
            raise ValueError("assistant tool calls are missing replies")
        if role == "assistant":
            calls = message.get("tool_calls") or []
            ids = [call["id"] for call in calls]
            if len(set(ids)) != len(ids) or any(identifier in used for identifier in ids):
                raise ValueError("duplicate tool call ID within a turn")
            used.update(ids)
            pending = set(ids)
        elif role == "user":
            used.clear()
        elif role not in {"system", "developer"}:
            raise ValueError(f"unsupported message role: {role}")
        groups.append([message])
    if pending:
        raise ValueError("assistant tool calls are missing replies")
    return groups


@dataclass(frozen=True)
class ContextView:
    messages: list[dict]
    input_bytes: int
    removed_messages: int
    clipped_tool_results: int


@dataclass(frozen=True)
class ContextPolicy:
    max_input_bytes: int = 96_000
    max_tool_result_bytes: int = 4_000

    def __post_init__(self):
        if type(self.max_input_bytes) is not int or self.max_input_bytes < 256:
            raise ValueError("context byte limit must be an integer >= 256")
        if type(self.max_tool_result_bytes) is not int or self.max_tool_result_bytes < 256:
            raise ValueError("tool context byte limit must be an integer >= 256")

    def build(self, messages: list[dict], tools: list[dict]) -> ContextView:
        projected = deepcopy(messages)
        groups = _exchanges(projected)
        original_count = len(projected)
        clipped = 0
        clipped_messages = set()

        # Keep the stable error envelope. Only data is replaced by an explicitly
        # marked excerpt; the full original result stays in the checkpoint.
        for message in projected:
            if message["role"] != "tool":
                continue
            content = message["content"]
            if len(content.encode("utf-8")) <= self.max_tool_result_bytes:
                continue
            value = json.loads(content)
            from baseagent.tools.result import ToolResult
            ToolResult.from_dict(value)
            data = encoded(value["data"])
            value["data"] = {"context_truncated": True, "original_data_bytes": len(data), "preview": ""}
            overhead = len(encoded(value))
            # JSON escaping of preview may expand bytes. Reduce until the actual
            # envelope fits, rather than assuming character count equals bytes.
            available = max(0, self.max_tool_result_bytes - overhead - 16)
            value["data"]["preview"] = data[:available].decode("utf-8", errors="ignore")
            while len(encoded(value)) > self.max_tool_result_bytes and value["data"]["preview"]:
                preview = value["data"]["preview"]
                value["data"]["preview"] = preview[:len(preview) // 2]
            if len(encoded(value)) > self.max_tool_result_bytes:
                # Keep error metadata even if it exceeds the excerpt target.
                # The global budget is enforced after removable history is gone.
                value["data"]["preview"] = ""
            message["content"] = encoded(value).decode("utf-8")
            clipped += 1
            clipped_messages.add(id(message))

        user_indices = [index for index, group in enumerate(groups) if group[0]["role"] == "user"]
        if not user_indices:
            raise ValueError("model context requires a user message")
        latest_user = user_indices[-1]
        prefix = groups[:user_indices[0]]
        previous = groups[user_indices[0]:latest_user]
        active = groups[latest_user:]
        # A notification is trusted instruction text, never a generated summary
        # made by promoting untrusted tool output into the system role.
        notice = {"role": "system", "content": "Some earlier conversation or tool data was omitted to fit the input limit. Tool data marked context_truncated is incomplete. Use tools to verify any missing details."}

        def flatten():
            values = [message for group in prefix + previous + active for message in group]
            if len(values) < original_count or clipped:
                position = sum(len(group) for group in prefix)
                values.insert(position, notice)
            return values

        view = flatten()
        while input_bytes(view, tools) > self.max_input_bytes and previous:
            # Remove an entire completed user turn, including all tool replies.
            end = next((index for index in range(1, len(previous)) if previous[index][0]["role"] == "user"), len(previous))
            del previous[:end]
            view = flatten()
        while input_bytes(view, tools) > self.max_input_bytes and len(active) > 2:
            # Preserve the current prompt and the most recent full exchange.
            del active[1]
            view = flatten()
        size = input_bytes(view, tools)
        # A large latest batch must stay paired, but its individual data previews
        # can be reduced further to fit the total budget.
        while size > self.max_input_bytes:
            changed = False
            for message in view:
                if message["role"] != "tool":
                    continue
                value = json.loads(message["content"])
                from baseagent.tools.result import ToolResult
                ToolResult.from_dict(value)
                if id(message) in clipped_messages:
                    preview = value["data"]["preview"]
                    if not preview:
                        continue
                    value["data"]["preview"] = preview[:len(preview) // 2]
                else:
                    data = encoded(value["data"])
                    if len(data) < 256:
                        continue
                    value["data"] = {"context_truncated": True, "original_data_bytes": len(data),
                                     "preview": data[:len(data) // 4].decode("utf-8", errors="ignore")}
                content = encoded(value).decode("utf-8")
                if len(content.encode("utf-8")) >= len(message["content"].encode("utf-8")):
                    continue
                message["content"] = content
                if id(message) not in clipped_messages:
                    clipped_messages.add(id(message))
                    clipped += 1
                changed = True
            if not changed:
                break
            view = flatten()
            size = input_bytes(view, tools)
        if size > self.max_input_bytes:
            raise ContextLimitExceeded(f"required prompt/tools/latest exchange need {size} bytes; limit is {self.max_input_bytes}")
        retained = sum(len(group) for group in prefix + previous + active)
        return ContextView(view, size, original_count - retained, sum(id(message) in clipped_messages for message in view))
