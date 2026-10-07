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
class State:
    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    max_steps: int = 8
    model_calls: int = 0
    tool_calls: int = 0
    status: RunStatus = RunStatus.RUNNING
    final_answer: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    turn_id: str = ""
    turn_start: int = 1
    workspace_root: str | None = None
    max_model_calls: int = 8
    max_tool_calls: int = 32
    blocked_tool_calls: list[str] = field(default_factory=list)
    max_context_bytes: int = 96_000
    max_tool_context_bytes: int = 4_000
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    unknown_usage_calls: int = 0
    max_total_tokens: int | None = None
    max_duration_seconds: float | None = None
    turn_started_at: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    unsafe_tool_calls: int = 0
    plan: list[dict[str, Any]] = field(default_factory=list)
    plan_revision: int = 0
    verifications: list[dict[str, Any]] = field(default_factory=list)
    memory_notes: dict[str, dict[str, Any]] = field(default_factory=dict)
    memory_revision: int = 0
    history_summary: dict[str, Any] | None = None
    estimate_model: bool = False
    preauthorize_model: bool = False
    model_reservations: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "State":
        data = dict(data)
        if "unknown_usage_calls" not in data:
            data["unknown_usage_calls"] = data.get("model_calls", 0)
        data["status"] = RunStatus(data["status"])
        return cls(**data)
