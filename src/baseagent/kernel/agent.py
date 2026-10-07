"""One-task model/tool loop; persistence and authorization stay with callers."""

from collections.abc import Iterable, Iterator
from copy import deepcopy
import json
import time
from typing import Any

from .cancellation import CancellationToken, Cancelled
from .context import ContextLimitExceeded, ContextPolicy
from .events import AgentEvent
from .hooks import AgentMiddleware, LoopHooks, MiddlewarePipeline, ModelRequest, TaskStopped, ToolCallRequest
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


class _SingleTaskHooks(LoopHooks):
    """Default boundaries for an in-memory task and ordinary middleware."""

    def __init__(self, model, state, tools, hooks, cancellation):
        self.model, self.state, self.tools = model, state, tools
        self.cancellation = cancellation
        self.pipeline = MiddlewarePipeline(hooks)
        self.model_handler = self.pipeline.wrap_model(self.execute_model)

    def checkpoint(self, kind, **data):
        value = AgentEvent(kind, timestamp=time.time(), data=data)
        self.state.events.append(value.to_dict())
        del self.state.events[:-100]
        return value

    def check(self):
        self.cancellation.check()

    def check_start(self):
        self.check()

    def started(self):
        return self.checkpoint("run_started", model_calls=self.state.model_calls, tool_calls=self.state.tool_calls)

    def before_agent(self, state):
        self.pipeline.before_agent(state)

    def before_model(self, state):
        self.pipeline.before_model(state)
        self.check()

    def model_started(self):
        return self.checkpoint("model_started", attempt=self.state.model_calls + 1)

    def model_returned(self, duration_seconds):
        return self.checkpoint("model_returned", attempt=self.state.model_calls, duration_seconds=duration_seconds)

    def messages(self):
        return deepcopy(self.state.messages)

    def execute_model(self, request):
        self.check()
        if self.state.model_calls >= self.state.max_model_calls:
            raise _LimitExceeded(RunStatus.MAX_MODEL_CALLS_EXCEEDED)
        view = ContextPolicy(self.state.max_context_bytes, self.state.max_tool_context_bytes).build(request.messages, request.tools)
        self.check()
        self.state.model_calls += 1
        if hasattr(self.model, "complete_with_control"):
            response = self.model.complete_with_control(view.messages, deepcopy(request.tools), cancellation=self.cancellation, timeout=None)
        else:
            response = self.model.complete(view.messages, deepcopy(request.tools))
        return response.message if hasattr(response, "message") else response

    def model_result(self, request):
        self.check()
        return self.model_handler(request)

    def model_failed(self, exc, duration_seconds):
        if isinstance(exc, (Exception, Cancelled, KeyboardInterrupt)):
            return self.checkpoint("model_failed", attempt=self.state.model_calls, error_type=type(exc).__name__,
                                   duration_seconds=duration_seconds)

    def prepare_tool(self, request):
        if self.state.tool_calls >= self.state.max_tool_calls:
            raise _LimitExceeded(RunStatus.MAX_TOOL_CALLS_EXCEEDED)
        self.check()
        self.pipeline.before_tool(request)
        self.check()
        return None

    def tool_started(self, request):
        return self.checkpoint("tool_started", call_id=request.call_id)

    def tool_result(self, request):
        self.check()
        original_id = request.call_id

        def execute_tool(actual):
            self.check()
            if actual.call_id != original_id:
                raise ValueError("middleware cannot change the tool call ID")
            if self.state.tool_calls >= self.state.max_tool_calls:
                raise _LimitExceeded(RunStatus.MAX_TOOL_CALLS_EXCEEDED)
            self.state.tool_calls += 1
            if not self.tools.retry_safe(actual.name):
                self.state.unsafe_tool_calls += 1

            def reject_constant(value):
                raise ValueError("non-finite numbers are not valid JSON")

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate object key")
                    result[key] = value
                return result

            try:
                arguments = json.loads(actual.arguments or "{}", parse_constant=reject_constant, object_pairs_hook=unique_object)
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be a JSON object")
                json.dumps(arguments, allow_nan=False)
            except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
                return ToolResult.failure(ErrorCode.INVALID_ARGUMENTS, "tool arguments must be a valid JSON object")
            return self.tools.execute(actual.name, arguments, context=ToolContext(self.state, self.checkpoint, self.cancellation))

        return self.pipeline.wrap_tool(execute_tool)(request)

    def commit_tool(self, request, result):
        pass

    def tool_returned(self, request, result, duration_seconds):
        return self.checkpoint("tool_returned", call_id=request.call_id, ok=result.ok,
                               error_code=result.error.code if result.error else None, duration_seconds=duration_seconds)

    def tool_completed(self, request, result):
        return self.checkpoint("tool_completed", call_id=request.call_id, ok=result.ok,
                               error_code=result.error.code if result.error else None)

    def accept_completion(self):
        return True

    def commit_assistant(self, calls):
        pass

    def after_model(self, state, message):
        self.pipeline.after_model(state, message)

    def step_completed(self, calls):
        return self.checkpoint("assistant_checkpoint", step=self.state.step, tool_count=len(calls))

    def handle_exception(self, exc):
        if isinstance(exc, Cancelled):
            self.state.status = RunStatus.CANCELLED
            self.state.error = "execution cancelled; inspect completed and uncertain calls before resuming"
        elif isinstance(exc, _LimitExceeded):
            self.state.status = exc.status
        elif isinstance(exc, ContextLimitExceeded):
            self.state.status = RunStatus.CONTEXT_LIMIT_EXCEEDED
            self.state.error = str(exc)
        elif isinstance(exc, KeyboardInterrupt):
            self.state.status = RunStatus.INTERRUPTED
            self.state.error = "execution interrupted; resume the session to inspect its checkpoint"
        elif isinstance(exc, Exception):
            self.state.status = RunStatus.FAILED
            self.state.error = f"{type(exc).__name__}: {exc}"
        else:
            return False
        return True

    def cleanup(self):
        try:
            self.pipeline.after_agent(self.state)
        except Cancelled:
            self.state.status = RunStatus.CANCELLED
            self.state.error = "execution cancelled during cleanup; inspect the saved session"
        except KeyboardInterrupt:
            self.state.status = RunStatus.INTERRUPTED
            self.state.error = "execution interrupted during cleanup; resume the saved session"
        except Exception as exc:
            self.state.status = RunStatus.FAILED
            self.state.error = f"{type(exc).__name__}: {exc}"

    def stopped(self):
        return self.checkpoint("run_stopped", status=self.state.status,
                               model_calls=self.state.model_calls, tool_calls=self.state.tool_calls)


def iter_agent(
    model: Model,
    state: KernelState,
    *,
    tools: ToolRegistry,
    hooks: Iterable[AgentMiddleware] = (),
    cancellation: CancellationToken | None = None,
) -> Iterator[AgentEvent]:
    """Advance one prepared task; its result is the supplied state object.

    Ordinary hooks retain synchronous middleware semantics. A LoopHooks adapter
    can supply existing harness boundaries without importing them into kernel.
    """
    cancellation = cancellation or CancellationToken()
    layers = tuple(hooks)
    boundaries = [layer for layer in layers if isinstance(layer, LoopHooks)]
    if boundaries:
        if len(layers) != 1:
            raise ValueError("execution boundaries must be supplied as the only kernel hook")
        driver = boundaries[0]
    else:
        driver = _SingleTaskHooks(model, state, tools, layers, cancellation)

    try:
        yield driver.started()
        driver.check_start()
        driver.before_agent(state)
        while True:
            driver.check()
            pending = _pending_calls(state)
            if pending:
                for call in pending:
                    function = call["function"]
                    request = ToolCallRequest(state, call["id"], function["name"], function["arguments"], cancellation)
                    result = driver.prepare_tool(request)
                    started = time.monotonic()
                    if result is None:
                        yield driver.tool_started(request)
                        result = driver.tool_result(request)
                        if not isinstance(result, ToolResult):
                            raise TypeError("wrap_tool_call must return ToolResult")
                        result = tools.normalize_result(result)
                    state.messages.append({"role": "tool", "tool_call_id": request.call_id,
                                           "content": json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False)})
                    try:
                        driver.commit_tool(request, result)
                    except BaseException:
                        state.messages.pop()
                        raise
                    yield driver.tool_returned(request, result, time.monotonic() - started)
                    yield driver.tool_completed(request, result)
                continue
            last_message = state.messages[-1]
            if last_message["role"] == "assistant" and not _tool_calls(last_message):
                if not driver.accept_completion():
                    continue
                state.status = RunStatus.COMPLETED
                state.final_answer = last_message.get("content") or ""
                break
            if state.step >= state.max_steps:
                state.status = RunStatus.MAX_STEPS_EXCEEDED
                break
            driver.before_model(state)
            yield driver.model_started()
            started = time.monotonic()
            try:
                response = driver.model_result(ModelRequest(state, driver.messages(), tools.specs(), cancellation))
            except BaseException as exc:
                failed = driver.model_failed(exc, time.monotonic() - started)
                if failed is not None:
                    yield failed
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
            try:
                driver.commit_assistant(calls)
            except BaseException:
                state.messages.pop()
                state.step -= 1
                raise
            yield driver.model_returned(time.monotonic() - started)
            driver.after_model(state, message)
            yield driver.step_completed(calls)
    except TaskStopped:
        pass
    except BaseException as exc:
        if not driver.handle_exception(exc):
            raise
    finally:
        driver.cleanup()
        stopped = driver.stopped()
    yield stopped
