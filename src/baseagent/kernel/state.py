"""Serializable state for one task, without session or runtime dependencies."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    TOOL_LOOP_DETECTED = "tool_loop_detected"
    MAX_TOOL_CALLS_EXCEEDED = "max_tool_calls_exceeded"
    MAX_MODEL_CALLS_EXCEEDED = "max_model_calls_exceeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    NEEDS_RECOVERY = "needs_recovery"
    CONTEXT_LIMIT_EXCEEDED = "context_limit_exceeded"
    MAX_TOKENS_EXCEEDED = "max_tokens_exceeded"
    USAGE_UNAVAILABLE = "usage_unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    WORKSPACE_CHANGED = "workspace_changed"
    AWAITING_APPROVAL = "awaiting_approval"
    CANCELLED = "cancelled"
    RESERVATION_UNAVAILABLE = "reservation_unavailable"
    REQUEST_BOUND_VIOLATED = "request_bound_violated"


@dataclass
class KernelState:
    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    max_steps: int = 8
    model_calls: int = 0
    tool_calls: int = 0
    status: RunStatus = RunStatus.RUNNING
    final_answer: str | None = None
    error: str | None = None
    max_model_calls: int = 8
    max_tool_calls: int = 32
    turn_start: int = 1
    max_context_bytes: int = 96_000
    max_tool_context_bytes: int = 4_000
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KernelState":
        data = deepcopy(data)
        data["status"] = RunStatus(data["status"])
        return cls(**data)
