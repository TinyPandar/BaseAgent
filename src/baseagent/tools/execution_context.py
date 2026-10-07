"""Harness extensions kept outside the single-task kernel."""

from dataclasses import dataclass
from typing import Callable

from baseagent.kernel.tool_context import ToolContext as KernelToolContext


@dataclass(frozen=True)
class ToolContext(KernelToolContext):
    store: object = None
    remaining_seconds: Callable | None = None
    call_id: str | None = None
    execution_scope: object = None
    tools: object = None
    policy: object = None
    model: object = None
    middleware: tuple = ()
    accept_config_changes: bool = False
