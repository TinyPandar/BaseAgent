"""Publish tool call/result previews as chat events. Observes only; never changes a result."""

from __future__ import annotations

from baseagent.middleware import AgentMiddleware

from .events import preview


class ToolEventMiddleware(AgentMiddleware):
    """``tool_call`` before dispatch and ``tool_result`` after it; ``worker`` is None for the main agent."""

    def __init__(self, events, *, worker=None, chars=300):
        self.events, self.worker, self.chars = events, worker, chars

    def wrap_tool_call(self, request, handler):
        self.events.publish("tool_call", worker=self.worker, call_id=request.call_id, name=request.name,
                            arguments=preview(request.arguments, self.chars))
        result = handler(request)
        if result.ok:
            shown = preview(result.data, self.chars)
        else:
            shown = preview(f"{result.error.code}: {result.error.message}", self.chars)
        self.events.publish("tool_result", worker=self.worker, call_id=request.call_id, name=request.name,
                            ok=bool(result.ok), preview=shown)
        return result
