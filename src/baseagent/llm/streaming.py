"""Streaming deltas and assembly helpers for OpenAI-compatible chat completions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterator

from .response import ModelResponse, TokenUsage


@dataclass(frozen=True)
class StreamDelta:
    """One metadata-safe model chunk. Bodies stay short; never store full prompts."""

    kind: str  # content | tool_call
    index: int
    delta: str
    call_id: str | None = None
    tool_index: int | None = None
    tool_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StreamDelta":
        if not isinstance(value, dict) or value.get("kind") not in {"content", "tool_call"}:
            raise ValueError("invalid stream delta")
        if type(value.get("index")) is not int or value["index"] < 0 or not isinstance(value.get("delta"), str):
            raise ValueError("invalid stream delta fields")
        return cls(kind=value["kind"], index=value["index"], delta=value["delta"],
                   call_id=value.get("call_id"), tool_index=value.get("tool_index"),
                   tool_name=value.get("tool_name"))


class StreamAssembler:
    """Assemble OpenAI chat-completion stream chunks into a final assistant message."""

    def __init__(self) -> None:
        self._content: list[str] = []
        self._tools: dict[int, dict[str, Any]] = {}
        self._index = 0
        self.usage: TokenUsage | None = None
        self.reasoning_tokens = 0

    @property
    def next_index(self) -> int:
        return self._index

    def ingest_openai_chunk(self, chunk) -> list[StreamDelta]:
        """Ingest one OpenAI SDK chunk; return public StreamDelta values emitted."""
        emitted: list[StreamDelta] = []
        choice = None
        try:
            choice = chunk.choices[0]
        except (AttributeError, IndexError, TypeError):
            choice = None
        delta = getattr(choice, "delta", None) if choice is not None else None
        if delta is not None:
            text = getattr(delta, "content", None)
            if isinstance(text, str) and text:
                emitted.append(StreamDelta(kind="content", index=self._index, delta=text))
                self._index += 1
                self._content.append(text)
            for tool in getattr(delta, "tool_calls", None) or ():
                tool_index = getattr(tool, "index", None)
                if type(tool_index) is not int or tool_index < 0:
                    continue
                slot = self._tools.setdefault(tool_index, {"id": None, "name": "", "arguments": []})
                identifier = getattr(tool, "id", None)
                if isinstance(identifier, str) and identifier:
                    slot["id"] = identifier
                function = getattr(tool, "function", None)
                name = getattr(function, "name", None) if function is not None else None
                if isinstance(name, str) and name:
                    slot["name"] = name
                arguments = getattr(function, "arguments", None) if function is not None else None
                if isinstance(arguments, str) and arguments:
                    slot["arguments"].append(arguments)
                    emitted.append(StreamDelta(
                        kind="tool_call", index=self._index, delta=arguments,
                        call_id=slot["id"], tool_index=tool_index, tool_name=slot["name"] or None,
                    ))
                    self._index += 1
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            try:
                self.usage = TokenUsage(usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
                details = getattr(usage, "completion_tokens_details", None)
                reasoning = getattr(details, "reasoning_tokens", None)
                if reasoning is None:
                    reasoning = getattr(usage, "reasoning_tokens", 0) or 0
                self.reasoning_tokens = reasoning if type(reasoning) is int and reasoning >= 0 else 0
            except (AttributeError, TypeError, ValueError):
                pass
        return emitted

    def ingest_delta(self, delta: StreamDelta) -> None:
        """Apply an already-normalized delta (tests / cassette replay)."""
        if delta.kind == "content":
            self._content.append(delta.delta)
        elif delta.kind == "tool_call":
            if type(delta.tool_index) is not int or delta.tool_index < 0:
                raise ValueError("tool_call delta requires tool_index")
            slot = self._tools.setdefault(delta.tool_index, {"id": None, "name": "", "arguments": []})
            if delta.call_id:
                slot["id"] = delta.call_id
            if delta.tool_name:
                slot["name"] = delta.tool_name
            slot["arguments"].append(delta.delta)
        self._index = max(self._index, delta.index + 1)

    def message(self) -> dict[str, Any]:
        content = "".join(self._content) if self._content else None
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if self._tools:
            calls = []
            for tool_index in sorted(self._tools):
                slot = self._tools[tool_index]
                if not isinstance(slot["id"], str) or not slot["id"]:
                    raise ValueError("streamed tool call missing id")
                if not isinstance(slot["name"], str) or not slot["name"]:
                    raise ValueError("streamed tool call missing name")
                calls.append({
                    "id": slot["id"],
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": "".join(slot["arguments"])},
                })
            message["tool_calls"] = calls
        if content is None and "tool_calls" not in message:
            message["content"] = ""
        return message

    def response(self) -> ModelResponse:
        return ModelResponse(self.message(), self.usage, self.reasoning_tokens)


def drain_stream(stream: Iterator[StreamDelta], *, on_delta=None) -> ModelResponse:
    """Consume a complete_stream generator that returns ModelResponse via StopIteration."""
    try:
        while True:
            delta = next(stream)
            if on_delta is not None:
                on_delta(delta)
    except StopIteration as finished:
        value = finished.value
        if not isinstance(value, ModelResponse):
            raise TypeError("complete_stream must return ModelResponse") from None
        return value
