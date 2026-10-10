"""Provider-independent message and validated reported usage."""

from dataclasses import asdict, dataclass
import json
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    def __post_init__(self):
        if any(type(value) is not int or value < 0 for value in asdict(self).values()):
            raise ValueError("token counts must be nonnegative integers")
        if self.total_tokens < self.prompt_tokens + self.completion_tokens:
            raise ValueError("total tokens cannot be smaller than input plus output")

    @classmethod
    def from_dict(cls, value: dict) -> "TokenUsage":
        if not isinstance(value, dict) or set(value) != {"prompt_tokens", "completion_tokens", "total_tokens"}:
            raise ValueError("usage requires prompt_tokens, completion_tokens, and total_tokens")
        return cls(**value)


@dataclass(frozen=True)
class ModelResponse:
    message: Any
    usage: TokenUsage | None = None

    def __post_init__(self):
        if self.usage is not None and not isinstance(self.usage, TokenUsage):
            raise ValueError("model usage must be TokenUsage or None")

    def to_dict(self):
        message = self.message
        if hasattr(message, "model_dump"):
            message = message.model_dump(exclude_none=True)
        if not isinstance(message, dict):
            raise ValueError("model message must be an object")
        # Detach SDK/container objects and reject non-JSON values.
        return json.loads(json.dumps({"message": message, "usage": asdict(self.usage) if self.usage else None}, allow_nan=False))

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"message", "usage"} or not isinstance(value["message"], dict):
            raise ValueError("invalid model response envelope")
        snapshot = json.loads(json.dumps(value, allow_nan=False))
        return cls(snapshot["message"], TokenUsage.from_dict(snapshot["usage"]) if snapshot["usage"] is not None else None)
