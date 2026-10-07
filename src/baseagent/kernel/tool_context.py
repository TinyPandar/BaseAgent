"""Per-dispatch context for stateful tools; never supplied by model arguments."""

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import KernelState


@dataclass(frozen=True)
class ToolContext:
    state: "KernelState"
    checkpoint: Callable
    cancellation: object = None
