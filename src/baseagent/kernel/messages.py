"""Typed, immutable view of the OpenAI-style message dicts stored in KernelState.

The kernel still stores plain dicts. ``Message.from_dict(d).to_dict()`` returns a
dict equal to ``d`` whose ``json.dumps`` output is byte-identical (key order,
``None`` versus absent keys and unknown provider keys are all preserved), so
stored sessions and cassettes never need migration.

Tool call arguments stay the raw JSON string produced by the model: approvals
and request digests bind that exact text, so it is never parsed and re-encoded.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import json
from typing import Any


class _Absent:
    """Marks a key that is not present in the source dict (distinct from None)."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "ABSENT"

    def __bool__(self):
        return False

    def __reduce__(self):
        return (_Absent, ())


ABSENT: Any = _Absent()

ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})
_MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id")
_CALL_KEYS = ("id", "type", "function")
_FUNCTION_KEYS = ("name", "arguments")


class FrozenDict(Mapping):
    """Read-only, hashable mapping that keeps insertion order."""

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any] = ()):
        object.__setattr__(self, "_data", dict(data))

    def __setattr__(self, name, value):
        raise AttributeError("FrozenDict is immutable")

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __eq__(self, other):
        return isinstance(other, Mapping) and dict(self.items()) == dict(other.items())

    def __hash__(self):
        return hash(frozenset(self._data.items()))

    def __repr__(self):
        return f"FrozenDict({self._data!r})"

    def __reduce__(self):
        return (FrozenDict, (self._data,))


def _freeze(value: Any) -> Any:
    """Recursively copy JSON-shaped data into immutable containers."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("message data keys must be strings")
        return FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    raise TypeError(f"message data must be JSON-shaped, got {type(value).__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _pairs(extra: Any, reserved: tuple[str, ...]) -> tuple[tuple[str, Any], ...]:
    items = extra.items() if isinstance(extra, Mapping) else tuple(extra)
    result = []
    seen = set()
    for key, value in items:
        if not isinstance(key, str) or key in reserved or key in seen:
            raise ValueError(f"invalid or duplicate extra key: {key!r}")
        seen.add(key)
        result.append((key, _freeze(value)))
    return tuple(result)


def _order(order: tuple[str, ...], present: list[str]) -> tuple[str, ...]:
    """Validate a stored key order, or build the canonical order for new values."""
    if not order:
        return tuple(present)
    order = tuple(order)
    if len(order) != len(set(order)) or set(order) != set(present):
        raise ValueError("key order must list exactly the present keys")
    return order


@dataclass(frozen=True)
class TextPart:
    """A ``{"type": "text", "text": ...}`` content part."""

    text: str

    def __post_init__(self):
        if not isinstance(self.text, str):
            raise ValueError("text part text must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass(frozen=True)
class RawPart:
    """Any other content part (image, audio, provider-specific), kept verbatim."""

    data: Mapping[str, Any]

    def __post_init__(self):
        if not isinstance(self.data, Mapping):
            raise ValueError("content part must be an object")
        object.__setattr__(self, "data", _freeze(self.data))

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self.data)


def _part_from_dict(value: Any) -> TextPart | RawPart:
    if (isinstance(value, Mapping) and list(value) == ["type", "text"]
            and value["type"] == "text" and isinstance(value["text"], str)):
        return TextPart(value["text"])
    return RawPart(value)


@dataclass(frozen=True)
class ToolCall:
    """One assistant tool call; ``arguments`` is the model's raw JSON text."""

    id: str
    name: str
    arguments: str
    type: Any = "function"
    extra: tuple[tuple[str, Any], ...] = ()
    function_extra: tuple[tuple[str, Any], ...] = ()
    key_order: tuple[str, ...] = field(default=(), repr=False)
    function_key_order: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("tool call id must be a non-empty string")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("tool call name must be a non-empty string")
        if not isinstance(self.arguments, str):
            raise ValueError("tool call arguments must be the raw JSON string")
        if self.type is not ABSENT and not isinstance(self.type, str):
            raise ValueError("tool call type must be a string")
        extra = _pairs(self.extra, _CALL_KEYS)
        function_extra = _pairs(self.function_extra, _FUNCTION_KEYS)
        present = (["id"] + (["type"] if self.type is not ABSENT else []) + ["function"]
                   + [key for key, _ in extra])
        object.__setattr__(self, "extra", extra)
        object.__setattr__(self, "function_extra", function_extra)
        object.__setattr__(self, "key_order", _order(self.key_order, present))
        object.__setattr__(self, "function_key_order",
                           _order(self.function_key_order, ["name", "arguments"] + [key for key, _ in function_extra]))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ToolCall":
        if not isinstance(value, Mapping):
            raise ValueError("tool call must be an object")
        function = value.get("function")
        if not isinstance(function, Mapping):
            raise ValueError("tool call function must be an object")
        if "id" not in value or "name" not in function or "arguments" not in function:
            raise ValueError("tool call requires id, function.name and function.arguments")
        return cls(value["id"], function["name"], function["arguments"], value.get("type", ABSENT),
                   tuple((key, item) for key, item in value.items() if key not in _CALL_KEYS),
                   tuple((key, item) for key, item in function.items() if key not in _FUNCTION_KEYS),
                   tuple(value), tuple(function))

    def to_dict(self) -> dict[str, Any]:
        function_values = {"name": self.name, "arguments": self.arguments, **dict(self.function_extra)}
        function = {key: _thaw(function_values[key]) for key in self.function_key_order}
        values = {"id": self.id, "type": self.type, "function": function, **dict(self.extra)}
        return {key: _thaw(values[key]) for key in self.key_order}


@dataclass(frozen=True)
class ToolReply:
    """Semantic view of a ``tool`` message.

    Named ToolReply to avoid confusion with ``baseagent.kernel.result.ToolResult``
    (the structured result envelope that is JSON-encoded into ``content``).
    """

    call_id: str
    content: Any
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    """One conversation message. ``ABSENT`` fields are not present in the dict.

    ``content`` is a string, ``None``, ``ABSENT`` or a tuple of content parts.
    ``meta`` holds unknown or provider-specific keys (``reasoning_content``,
    ``name`` ...) in their original order and is read-only.
    """

    role: str
    content: Any = ABSENT
    tool_calls: Any = ABSENT
    tool_call_id: Any = ABSENT
    meta: Mapping[str, Any] = field(default_factory=dict)
    key_order: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if self.role not in ROLES:
            raise ValueError(f"unsupported message role: {self.role!r}")
        content = self.content
        if isinstance(content, (list, tuple)):
            content = tuple(part if isinstance(part, (TextPart, RawPart)) else _part_from_dict(part)
                            for part in content)
        elif content is not ABSENT and content is not None and not isinstance(content, str):
            raise ValueError("message content must be a string, None or a list of parts")
        calls = self.tool_calls
        if calls is not ABSENT and calls is not None:
            if not isinstance(calls, (list, tuple)):
                raise ValueError("tool_calls must be a list")
            calls = tuple(call if isinstance(call, ToolCall) else ToolCall.from_dict(call) for call in calls)
            ids = [call.id for call in calls]
            if len(set(ids)) != len(ids):
                raise ValueError("tool call ids must be unique within a message")
        if calls is not ABSENT and self.role != "assistant":
            raise ValueError("only assistant messages may carry tool_calls")
        if self.role == "tool":
            if not isinstance(self.tool_call_id, str) or not self.tool_call_id:
                raise ValueError("tool message requires a non-empty tool_call_id")
            if not isinstance(content, (str, tuple)):
                raise ValueError("tool message content must be a string or a list of parts")
        elif self.tool_call_id is not ABSENT:
            raise ValueError("only tool messages may carry tool_call_id")
        meta = self.meta
        if not isinstance(meta, Mapping):
            raise ValueError("message meta must be a mapping")
        if any(not isinstance(key, str) or key in _MESSAGE_KEYS for key in meta):
            raise ValueError("message meta keys must be strings other than known message keys")
        meta = _freeze(meta)
        present = (["role"] + [key for key, value in (("content", content), ("tool_calls", calls),
                                                     ("tool_call_id", self.tool_call_id)) if value is not ABSENT]
                   + list(meta))
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "tool_calls", calls)
        object.__setattr__(self, "meta", meta)
        object.__setattr__(self, "key_order", _order(self.key_order, present))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Message":
        if not isinstance(value, Mapping):
            raise ValueError("message must be an object")
        if "role" not in value:
            raise ValueError("message requires a role")
        return cls(value["role"], value.get("content", ABSENT), value.get("tool_calls", ABSENT),
                   value.get("tool_call_id", ABSENT),
                   {key: item for key, item in value.items() if key not in _MESSAGE_KEYS}, tuple(value))

    def to_dict(self) -> dict[str, Any]:
        content = self.content
        if isinstance(content, tuple):
            content = [part.to_dict() for part in content]
        calls = self.tool_calls
        if isinstance(calls, tuple):
            calls = [call.to_dict() for call in calls]
        values = {"role": self.role, "content": content, "tool_calls": calls,
                  "tool_call_id": self.tool_call_id, **self.meta}
        return {key: _thaw(values[key]) for key in self.key_order}

    @property
    def calls(self) -> tuple[ToolCall, ...]:
        """Tool calls as a tuple; empty when absent or None."""
        return self.tool_calls if isinstance(self.tool_calls, tuple) else ()

    @property
    def text(self) -> str | None:
        """Concatenated text content, or None when there is no text."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, tuple):
            texts = [part.text for part in self.content if isinstance(part, TextPart)]
            return "".join(texts) if texts else None
        return None

    def tool_reply(self) -> ToolReply:
        """Semantic view of a tool message; is_error follows the ToolResult envelope."""
        if self.role != "tool":
            raise ValueError("only tool messages have a tool reply")
        is_error = False
        if isinstance(self.content, str):
            try:
                value = json.loads(self.content)
            except ValueError:
                value = None
            is_error = isinstance(value, dict) and value.get("ok") is False and isinstance(value.get("error"), dict)
        return ToolReply(self.tool_call_id, self.content, is_error)


def _message(value: Message | Mapping[str, Any]) -> Message:
    return value if isinstance(value, Message) else Message.from_dict(value)


class ToolPairingError(ValueError):
    """The conversation breaks the assistant tool_call / tool reply pairing."""

    def __init__(self, message: str, *, index: int, call_ids: Iterable[str] = ()):
        super().__init__(message)
        self.index = index
        self.call_ids = tuple(call_ids)


def check_tool_pairing(messages: Iterable[Message | Mapping[str, Any]], *,
                       allow_pending: bool = True) -> tuple[ToolCall, ...]:
    """Validate tool call pairing and return the calls still awaiting replies.

    Every tool call of an assistant message must be answered by a ``tool``
    message before the next non-tool message; a tool message must answer a call
    of the immediately preceding assistant batch, exactly once. Call ids may not
    be reused within one user turn. Calls left open at the end are legal while
    the kernel is executing them (``allow_pending=True``) but must be rejected
    right before a model call (``allow_pending=False``).
    """
    pending: dict[str, ToolCall] = {}
    answered: set[str] = set()
    used: set[str] = set()
    index = -1
    for index, raw in enumerate(messages):
        message = _message(raw)
        if message.role == "tool":
            call_id = message.tool_call_id
            if call_id in pending:
                del pending[call_id]
                answered.add(call_id)
                continue
            if call_id in answered:
                raise ToolPairingError(f"message {index}: duplicate tool reply for call id {call_id!r}",
                                       index=index, call_ids=[call_id])
            raise ToolPairingError(f"message {index}: tool reply references unknown call id {call_id!r}",
                                   index=index, call_ids=[call_id])
        if pending:
            raise ToolPairingError(
                f"message {index} ({message.role}) follows assistant tool call(s) without tool replies: "
                + ", ".join(sorted(pending)), index=index, call_ids=sorted(pending))
        answered.clear()
        if message.role == "user":
            used.clear()
        elif message.role == "assistant":
            reused = [call.id for call in message.calls if call.id in used]
            if reused:
                raise ToolPairingError(f"message {index}: tool call id reused within a turn: " + ", ".join(reused),
                                       index=index, call_ids=reused)
            used.update(call.id for call in message.calls)
            pending = {call.id: call for call in message.calls}
    if pending and not allow_pending:
        raise ToolPairingError("conversation ends with unanswered tool call(s): " + ", ".join(sorted(pending)),
                               index=index, call_ids=sorted(pending))
    return tuple(pending.values())


def unanswered_tool_calls(messages: Iterable[Message | Mapping[str, Any]]) -> tuple[ToolCall, ...]:
    """Calls of the final assistant batch that still lack replies, in call order.

    Raises ToolPairingError if the conversation is otherwise malformed, e.g. a
    call left unanswered before a later user message (which the kernel's
    ``_pending_calls`` scan silently ignores).
    """
    return check_tool_pairing(messages, allow_pending=True)
