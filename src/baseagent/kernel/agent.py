"""One-task model/tool loop; persistence and authorization stay with callers."""

from collections.abc import Iterable, Iterator
from copy import deepcopy
import json
import time
from typing import Any

from .cancellation import CancellationToken, Cancelled
from .context import ContextLimitExceeded, ContextPolicy
from .events import AgentEvent
from .hooks import AgentMiddleware, MiddlewarePipeline, ModelRequest, ToolCallRequest
from .model import Model
from .result import ErrorCode, ToolResult
from .state import KernelState, RunStatus
from .tool_context import ToolContext
from .tools import ToolRegistry


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    raise TypeError("model message must be a dict or support model_dump()")


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        raise ValueError("tool_calls must be a list")
    normalized = []
    ids = set()
    for value in calls:
        call = deepcopy(_as_dict(value))
        function = _as_dict(call.get("function", {}))
        if not isinstance(call.get("id"), str) or not call["id"] or call["id"] in ids:
            raise ValueError("tool call IDs must be nonempty and unique")
        if not isinstance(function.get("name"), str) or not isinstance(function.get("arguments"), str):
            raise ValueError("tool function name and arguments must be strings")
        call["function"] = function
        ids.add(call["id"])
        normalized.append(call)
    return normalized


def _pending_calls(state: KernelState) -> list[dict[str, Any]]:
    replies = set()
    for message in reversed(state.messages[state.turn_start:]):
        if message["role"] == "tool":
            replies.add(message["tool_call_id"])
        elif message["role"] == "assistant":
            return [call for call in _tool_calls(message) if call["id"] not in replies]
        elif message["role"] == "user":
            break
    return []


class _LimitExceeded(Exception):
    def __init__(self, status: RunStatus):
        self.status = status


def iter_agent(
    model: Model,
    state: KernelState,
    *,
    tools: ToolRegistry,
    hooks: Iterable[AgentMiddleware] = (),
    cancellation: CancellationToken | None = None,
) -> Iterator[AgentEvent]:
    """Consume a prepared task state; results are written to that same object.

    Hooks retain the existing synchronous middleware semantics. Model/tool
    events bracket a wrapped operation; dispatch counters count actual calls,
    including retries, rather than cached results supplied by wrappers.
    """
    cancellation = cancellation or CancellationToken()
    pipeline = MiddlewarePipeline(hooks)

    def checkpoint(kind: str, **data) -> AgentEvent:
        value = AgentEvent(kind, data=data)
        state.events.append(value.to_dict())
        del state.events[:-100]
        return value

    def execute_model(request: ModelRequest):
        cancellation.check()
        if state.model_calls >= state.max_model_calls:
            raise _LimitExceeded(RunStatus.MAX_MODEL_CALLS_EXCEEDED)
        view = ContextPolicy(state.max_context_bytes, state.max_tool_context_bytes).build(request.messages, request.tools)
        cancellation.check()
        state.model_calls += 1
        if hasattr(model, "complete_with_control"):
            response = model.complete_with_control(view.messages, deepcopy(request.tools), cancellation=cancellation, timeout=None)
        else:
            response = model.complete(view.messages, deepcopy(request.tools))
        # The provider response envelope is structural, never a client import.
        return response.message if hasattr(response, "message") else response

    model_handler = pipeline.wrap_model(execute_model)

    def execute_tool(request: ToolCallRequest) -> ToolResult:
        cancellation.check()
        if request.call_id != active_call_id:
            raise ValueError("middleware cannot change the tool call ID")
        if state.tool_calls >= state.max_tool_calls:
            raise _LimitExceeded(RunStatus.MAX_TOOL_CALLS_EXCEEDED)
        state.tool_calls += 1
        if not tools.retry_safe(request.name):
            state.unsafe_tool_calls += 1

        def reject_constant(value: str):
            raise ValueError("non-finite numbers are not valid JSON")

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate object key")
                result[key] = value
            return result

        try:
            arguments = json.loads(request.arguments or "{}", parse_constant=reject_constant, object_pairs_hook=unique_object)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            json.dumps(arguments, allow_nan=False)
        except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
            return ToolResult.failure(ErrorCode.INVALID_ARGUMENTS, "tool arguments must be a valid JSON object")
        return tools.execute(request.name, arguments, context=ToolContext(state, checkpoint, cancellation))

    tool_handler = pipeline.wrap_tool(execute_tool)
    active_call_id = None

    try:
        yield checkpoint("run_started", model_calls=state.model_calls, tool_calls=state.tool_calls)
        cancellation.check()
        pipeline.before_agent(state)
        while True:
            cancellation.check()
            pending = _pending_calls(state)
            if pending:
                for call in pending:
                    if state.tool_calls >= state.max_tool_calls:
                        raise _LimitExceeded(RunStatus.MAX_TOOL_CALLS_EXCEEDED)
                    cancellation.check()
                    active_call_id = call["id"]
                    function = call["function"]
                    request = ToolCallRequest(state, active_call_id, function["name"], function["arguments"], cancellation)
                    pipeline.before_tool(request)
                    cancellation.check()
                    yield checkpoint("tool_started", call_id=active_call_id)
                    cancellation.check()
                    started = time.monotonic()
                    result = tool_handler(request)
                    if not isinstance(result, ToolResult):
                        raise TypeError("wrap_tool_call must return ToolResult")
                    result = tools.normalize_result(result)
                    state.messages.append({"role": "tool", "tool_call_id": active_call_id,
                                           "content": json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False)})
                    yield checkpoint("tool_returned", call_id=active_call_id, ok=result.ok,
                                     error_code=result.error.code if result.error else None,
                                     duration_seconds=time.monotonic() - started)
                    yield checkpoint("tool_completed", call_id=active_call_id, ok=result.ok,
                                     error_code=result.error.code if result.error else None)
                continue
            last_message = state.messages[-1]
            if last_message["role"] == "assistant" and not _tool_calls(last_message):
                state.status = RunStatus.COMPLETED
                state.final_answer = last_message.get("content") or ""
                break
            if state.step >= state.max_steps:
                raise _LimitExceeded(RunStatus.MAX_STEPS_EXCEEDED)
            pipeline.before_model(state)
            cancellation.check()
            yield checkpoint("model_started", attempt=state.model_calls + 1)
            cancellation.check()
            started = time.monotonic()
            try:
                response = model_handler(ModelRequest(state, deepcopy(state.messages), tools.specs(), cancellation))
            except BaseException as exc:
                if isinstance(exc, (Exception, Cancelled, KeyboardInterrupt)):
                    yield checkpoint("model_failed", attempt=state.model_calls, error_type=type(exc).__name__,
                                     duration_seconds=time.monotonic() - started)
                raise
            message = deepcopy(_as_dict(response))
            if message.get("role", "assistant") != "assistant":
                raise ValueError("model returned a non-assistant message")
            message["role"] = "assistant"
            calls = _tool_calls(message)
            known_ids = {call["id"] for previous in state.messages[state.turn_start:]
                         if previous["role"] == "assistant" for call in _tool_calls(previous)}
            if any(call["id"] in known_ids for call in calls):
                raise ValueError("model reused a tool call ID within the same turn")
            if calls:
                message["tool_calls"] = calls
            state.messages.append(message)
            state.step += 1
            yield checkpoint("model_returned", attempt=state.model_calls, duration_seconds=time.monotonic() - started)
            pipeline.after_model(state, message)
            yield checkpoint("assistant_checkpoint", step=state.step, tool_count=len(calls))
    except Cancelled:
        state.status = RunStatus.CANCELLED
        state.error = "execution cancelled; inspect completed and uncertain calls before resuming"
    except _LimitExceeded as exc:
        state.status = exc.status
    except ContextLimitExceeded as exc:
        state.status = RunStatus.CONTEXT_LIMIT_EXCEEDED
        state.error = str(exc)
    except KeyboardInterrupt:
        state.status = RunStatus.INTERRUPTED
        state.error = "execution interrupted; resume the session to inspect its checkpoint"
    except Exception as exc:
        state.status = RunStatus.FAILED
        state.error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            pipeline.after_agent(state)
        except Cancelled:
            state.status = RunStatus.CANCELLED
            state.error = "execution cancelled during cleanup; inspect the saved session"
        except KeyboardInterrupt:
            state.status = RunStatus.INTERRUPTED
            state.error = "execution interrupted during cleanup; resume the saved session"
        except Exception as exc:
            state.status = RunStatus.FAILED
            state.error = f"{type(exc).__name__}: {exc}"
    yield checkpoint("run_stopped", status=state.status, model_calls=state.model_calls, tool_calls=state.tool_calls)
