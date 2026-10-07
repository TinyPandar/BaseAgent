"""Metadata event envelope shared with the existing persistent event format.

Lifecycle types include model_started/model_returned (or model_failed),
tool_started/tool_returned, assistant_checkpoint, and run_stopped.
Prompts, arguments, result bodies and exception text belong in state, not here.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import time
from typing import Any


@dataclass(frozen=True)
class AgentEvent:
    type: str
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentEvent":
        return cls(**deepcopy(data))
