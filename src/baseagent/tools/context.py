"""Per-dispatch context for stateful tools; never supplied by model arguments."""

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from baseagent.agent.state import State


@dataclass(frozen=True)
class ToolContext:
    state: "State"
    checkpoint: Callable
    cancellation: object = None
    store: object = None
    remaining_seconds: Callable | None = None
    call_id: str | None = None
    execution_scope: object = None
    tools: object = None
    policy: object = None
    model: object = None
    middleware: tuple = ()
    accept_config_changes: bool = False
