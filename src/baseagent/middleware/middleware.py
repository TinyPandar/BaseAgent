"""Lightweight middleware API inspired by LangChain's node and wrap hooks."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any, Callable, TYPE_CHECKING
from baseagent.tools.result import ToolResult

if TYPE_CHECKING:
    from baseagent.agent.state import State


@dataclass(frozen=True)
class ModelRequest:
    state: State
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    cancellation: Any = None


@dataclass(frozen=True)
class ToolCallRequest:
    state: State
    call_id: str
    name: str
    arguments: str
    cancellation: Any = None


class AgentMiddleware:
    """Override only the hooks needed by a middleware component.

    Node hooks observe lifecycle events. Wrap hooks may call ``handler(request)``
    zero, one, or multiple times, and may return an alternate result.
    """

    def before_agent(self, state: State) -> None:
        pass

    def before_model(self, state: State) -> None:
        pass

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        return handler(request)

    def after_model(self, state: State, message: dict[str, Any]) -> None:
        pass

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolResult]) -> ToolResult:
        return handler(request)

    def before_tool(self, request: ToolCallRequest) -> None:
        """Preflight validation before a pending call enters its running ledger state."""
        pass

    def after_agent(self, state: State) -> None:
        pass


class MiddlewarePipeline:
    """Compose middleware in registration order; the first layer is outermost."""

    def __init__(self, middleware: Iterable[AgentMiddleware] = ()) -> None:
        self.layers = tuple(middleware)

    @staticmethod
    def _link(layer: AgentMiddleware, hook: str, next_handler: Callable[[Any], Any]) -> Callable[[Any], Any]:
        def invoke(request: Any) -> Any:
            return getattr(layer, hook)(request, next_handler)

        return invoke

    def _wrap(self, hook: str, handler: Callable[[Any], Any]) -> Callable[[Any], Any]:
        for layer in reversed(self.layers):
            handler = self._link(layer, hook, handler)
        return handler

    def wrap_model(self, handler: Callable[[ModelRequest], Any]) -> Callable[[ModelRequest], Any]:
        return self._wrap("wrap_model_call", handler)

    def wrap_tool(self, handler: Callable[[ToolCallRequest], ToolResult]) -> Callable[[ToolCallRequest], ToolResult]:
        return self._wrap("wrap_tool_call", handler)

    def before_agent(self, state: State) -> None:
        for layer in self.layers:
            layer.before_agent(state)

    def before_model(self, state: State) -> None:
        for layer in self.layers:
            layer.before_model(state)

    def before_tool(self, request: ToolCallRequest) -> None:
        for layer in self.layers:
            layer.before_tool(request)

    def after_model(self, state: State, message: dict[str, Any]) -> None:
        for layer in reversed(self.layers):
            layer.after_model(state, message)

    def after_agent(self, state: State) -> None:
        first_error: Exception | None = None
        for layer in reversed(self.layers):
            try:
                layer.after_agent(state)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


# Keep the original import usable for external callers.
Middleware = AgentMiddleware
