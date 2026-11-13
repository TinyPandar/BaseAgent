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
        text = patch_text(request, result)
        if text is not None:
            # The batch diff (bounded by MAX_PATCH_DIFF_CHARS) as an ordinary notice line: TUI and Web
            # already render notices, so no frontend needs an apply_patch branch.
            self.events.publish("notice", stream="out", text=(f"[{self.worker}] " if self.worker else "") + text)
        return result


def patch_text(request, result):
    """Header plus unified diff of a successful apply_patch call, else None."""
    if request.name != "apply_patch" or not result.ok or not isinstance(result.data, dict):
        return None
    from baseagent.tools.patch import describe
    return describe(result.data)


class PatchDisplayMiddleware(AgentMiddleware):
    """Text mode: print the batch diff under each successful apply_patch. Observes only."""

    def __init__(self, emit):
        self.emit = emit

    def wrap_tool_call(self, request, handler):
        result = handler(request)
        text = patch_text(request, result)
        if text is not None:
            self.emit(text)
        return result
