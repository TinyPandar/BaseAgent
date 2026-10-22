"""Provider-independent agent loop with optional durable sessions."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from baseagent.kernel import AgentEvent, iter_agent
from baseagent.kernel.agent import _as_dict as _as_dict  # noqa: PLC0414 -- Compatibility export.
from baseagent.kernel.agent import _pending_calls as _pending_calls  # noqa: PLC0414 -- Compatibility export.
from baseagent.kernel.agent import _tool_calls as _tool_calls  # noqa: PLC0414 -- Compatibility export.
from baseagent.kernel.errors import ModelTimeoutError
from baseagent.kernel.hooks import LoopHooks, TaskStopped
from baseagent.kernel.model import Model
from baseagent.llm.estimation import TokenEstimate
from baseagent.llm.reservation import ReservationUnavailable, TokenReservation
from baseagent.llm.response import ModelResponse
from baseagent.middleware import (
    AgentMiddleware,
    MiddlewarePipeline,
    ModelRequest,
    ToolCallRequest,
)
from baseagent.session.compatibility import check_contract, runtime_contract
from baseagent.tools.context import ToolContext
from baseagent.tools.memory import project_history
from baseagent.tools.policy import ToolPolicy, request_digest
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.result import ErrorCode, ToolResult

from .cancellation import CancellationToken, Cancelled
from .compaction import Summarizer, TruncatingSummarizer, compact_history
from .context import ContextLimitExceeded, ContextPolicy
from .errors import BudgetExceeded, TaskPaused, WorkspaceChanged
from .events import event
from .scope import ExecutionScope
from .state import RunStatus, State

if TYPE_CHECKING:
    from baseagent.session import SessionStore


def _set_limits(state: State, max_steps: int | None, max_model_calls: int | None, max_tool_calls: int | None) -> None:
    if max_steps is not None:
        state.max_steps = max_steps
    if max_model_calls is not None:
        state.max_model_calls = max_model_calls
    if max_tool_calls is not None:
        state.max_tool_calls = max_tool_calls
    if any(type(value) is not int or value < minimum for value, minimum in
           ((state.max_steps, 1), (state.max_model_calls, 1), (state.max_tool_calls, 0))):
        raise ValueError("step/model limits must be positive integers and tool limit a nonnegative integer")


def _new_turn(state: State, user_input: str, max_steps: int | None, max_model_calls: int | None, max_tool_calls: int | None) -> None:
    if not user_input.strip():
        raise ValueError("user input must not be blank")
    state.turn_start = len(state.messages)
    state.messages.append({"role": "user", "content": user_input})
    state.turn_id = uuid4().hex
    state.step = state.model_calls = state.tool_calls = 0
    state.status = RunStatus.RUNNING
    state.final_answer = state.error = None
    state.blocked_tool_calls = []
    state.prompt_tokens = state.completion_tokens = state.total_tokens = state.unknown_usage_calls = 0
    state.turn_started_at = time.time()
    state.events = []
    state.unsafe_tool_calls = 0
    state.model_reservations = {}
    state.metadata.pop("estimated_reservations", None)
    state.metadata.pop("last_token_estimate", None)
    state.metadata.pop("request_bound_violation", None)
    state.metadata.pop("direct_usage", None)
    for key in ("observed_files", "root_instruction_digest", "workspace_drift", "duration_origin"):
        state.metadata.pop(key, None)
    state.metadata.pop("tool_approvals", None)
    state.metadata.pop("approval_request", None)
    state.metadata.pop("completion_report", None)
    state.max_steps = 8 if max_steps is None else max_steps
    state.max_model_calls = state.max_steps if max_model_calls is None else max_model_calls
    state.max_tool_calls = 32 if max_tool_calls is None else max_tool_calls
    _set_limits(state, None, None, None)


def run_agent(
    model: Model,
    user_input: str | None = None,
    *,
    tools: ToolRegistry,
    middleware: Iterable[AgentMiddleware] = (),
    system_prompt: str = "You are a helpful agent. Use tools to verify facts before answering.",
    max_steps: int | None = None,
    max_tool_calls: int | None = None,
    max_model_calls: int | None = None,
    store: SessionStore | None = None,
    session_id: str | None = None,
    workspace_root: str | Path | None = None,
    compaction_max_chars: int | None = None,
    compaction_keep_turns: int | None = None,
    summarizer: Summarizer | None = None,
    max_context_bytes: int | None = None,
    max_tool_context_bytes: int | None = None,
    runtime_config: dict[str, Any] | None = None,
    accept_config_changes: bool = False,
    max_total_tokens: int | None = None,
    max_duration_seconds: float | None = None,
    tool_policy: ToolPolicy | None = None,
    cancellation: CancellationToken | None = None,
    completion_policy=None,
    estimate_model: bool | None = None,
    preauthorize_model: bool | None = None,
    accept_request_bound_violation: bool = False,
    subtasks=None,
) -> State:
    """A prompt starts a turn; no prompt resumes a persisted turn without resetting budgets."""
    if store is None and subtasks is not None:
        if session_id is not None:
            raise ValueError("session_id requires a SessionStore")
        # Enter the identical orchestration path with a process-local ledger.
        # Capture arguments before any tool binding or contract transformation.
        arguments = dict(locals())
        from baseagent.session import MemorySessionStore
        with MemorySessionStore() as transient:
            arguments["store"] = transient
            state = run_agent(**arguments)
            state.metadata["execution_storage"] = {"durable": False, "resumable": False}
            state.metadata["subtask_tree"] = transient.task_tree(state.session_id, state.turn_id)["nodes"]
            return state
    root = str(Path(workspace_root).resolve()) if workspace_root is not None else None
    if subtasks is not None:
        tools = subtasks.bind_tools(tools)
    from baseagent.tools.edit_policy import bind_edit_policy
    policy = bind_edit_policy(tool_policy or ToolPolicy(), tools)
    # Preserve legacy contracts for the default permissive policy.
    config = runtime_config
    if subtasks is not None:
        config = {"runtime": config, "subtasks": subtasks.contract()}
    if policy.contract() != ToolPolicy().contract():
        config = {"runtime": config, "tool_policy": policy.contract()}
    if completion_policy is not None:
        config = {"runtime": config, "completion_policy": completion_policy.contract()}

    def prepare(existing: State | None) -> State:
        def configure(state: State) -> State:
            if state.model_reservations and ((estimate_model is not None and estimate_model != state.estimate_model) or (preauthorize_model is not None and preauthorize_model != state.preauthorize_model)):
                raise ValueError("resolve outstanding model usage before changing budget mode")
            if estimate_model is not None:
                if type(estimate_model) is not bool:
                    raise ValueError("estimate_model must be boolean")
                state.estimate_model = estimate_model
            if state.estimate_model and (preauthorize_model or (preauthorize_model is None and state.preauthorize_model)):
                raise ValueError("estimated and strict budgets cannot be combined")
            if preauthorize_model is not None:
                if type(preauthorize_model) is not bool:
                    raise ValueError("preauthorize_model must be boolean")
                state.preauthorize_model = preauthorize_model
            if max_total_tokens is not None:
                state.max_total_tokens = max_total_tokens
            if max_duration_seconds is not None:
                state.max_duration_seconds = max_duration_seconds
            if state.max_total_tokens is not None and (type(state.max_total_tokens) is not int or state.max_total_tokens < 1):
                raise ValueError("token budget must be a positive integer")
            if (state.preauthorize_model or state.estimate_model) and state.max_total_tokens is None:
                raise ValueError("model preauthorization requires a total token budget")
            if accept_request_bound_violation and state.metadata.get("request_bound_violation"):
                state.metadata["acknowledged_request_bound_violation"] = state.metadata.pop("request_bound_violation")
            if state.max_duration_seconds is not None and (type(state.max_duration_seconds) not in (int, float) or not math.isfinite(state.max_duration_seconds) or state.max_duration_seconds <= 0):
                raise ValueError("duration budget must be a positive finite number")
            if state.turn_started_at is None:
                state.turn_started_at = time.time()
                state.metadata["duration_origin"] = "legacy_upgrade"
            if max_context_bytes is not None:
                state.max_context_bytes = max_context_bytes
            if max_tool_context_bytes is not None:
                state.max_tool_context_bytes = max_tool_context_bytes
            if compaction_max_chars is not None:
                state.compaction_max_chars = compaction_max_chars or None
            if compaction_keep_turns is not None:
                state.compaction_keep_turns = compaction_keep_turns
            if compaction_max_chars is not None and (type(compaction_max_chars) is not int or compaction_max_chars < 0):
                raise ValueError("compaction character budget must be a nonnegative integer (0 disables)")
            if type(state.compaction_keep_turns) is not int or state.compaction_keep_turns < 1:
                raise ValueError("compaction keep turns must be a positive integer")
            ContextPolicy(state.max_context_bytes, state.max_tool_context_bytes)
            state.metadata["runtime_contract"] = runtime_contract(tools, state.messages[0]["content"], config)
            return state

        if existing is None:
            if user_input is None:
                raise ValueError("new sessions require a prompt")
            state = State(messages=[{"role": "system", "content": system_prompt}], session_id=session_id, workspace_root=root)
            _new_turn(state, user_input, max_steps, max_model_calls, max_tool_calls)
            return configure(state)
        if root is not None and existing.workspace_root != root:
            raise ValueError("session belongs to a different workspace")
        if user_input is not None:
            if existing.status != RunStatus.COMPLETED:
                raise ValueError("finish or reconcile the current turn before appending a new prompt")
            _new_turn(existing, user_input, max_steps, max_model_calls, max_tool_calls)
        elif existing.status != RunStatus.COMPLETED:
            check_contract(existing.metadata.get("runtime_contract"), runtime_contract(tools, existing.messages[0]["content"], config), accept_changes=accept_config_changes)
            _set_limits(existing, max_steps, max_model_calls, max_tool_calls)
            existing.status = RunStatus.RUNNING
            existing.error = None
        return existing if user_input is None and existing.status == RunStatus.COMPLETED else configure(existing)

    if store is None:
        if session_id is not None:
            raise ValueError("session_id requires a SessionStore")
        return _run_loop(model, prepare(None), tools, middleware, None, policy, cancellation or CancellationToken(), completion_policy, subtasks=subtasks, accept_config_changes=accept_config_changes, summarizer=summarizer)
    session_id = session_id or uuid4().hex
    with store.exclusive(session_id):
        existing = store.load(session_id)
        if existing is not None:
            unfinished = [node for node in store.task_tree(session_id, existing.turn_id)["nodes"] if node["state"]["status"] != RunStatus.COMPLETED]
            if unfinished and subtasks is not None and existing.status == RunStatus.COMPLETED:
                raise ValueError("completed root has unfinished child tasks; inspect and reconcile the inconsistent execution tree")
            if unfinished and subtasks is None:
                # Until the delegation runtime is attached, ordinary root
                # recovery must not ignore an active descendant or reset it.
                existing.status = RunStatus.NEEDS_RECOVERY
                existing.error = "unfinished child tasks require task orchestration recovery before root execution"
                store.save(existing, event=event("task_recovery_blocked", task_count=len(unfinished)))
                return existing
        state = prepare(existing)
        if user_input is None and state.status == RunStatus.COMPLETED:
            return state
        store.save(state)
        supplied = cancellation
        token = CancellationToken(lambda: bool((supplied and supplied.cancelled()) or store.cancellation(state.session_id, state.turn_id)))
        return _run_loop(model, state, tools, middleware, store, policy, token, completion_policy, subtasks=subtasks, accept_config_changes=accept_config_changes, summarizer=summarizer)


def _run_loop(model: Model, state: State, tools: ToolRegistry, middleware: Iterable[AgentMiddleware], store: SessionStore | None, policy: ToolPolicy, cancellation: CancellationToken, completion_policy, *, execution_scope=None, subtasks=None, accept_config_changes=False, summarizer=None) -> State:
    scope = execution_scope or ExecutionScope(state)
    if scope.state is not state:
        raise ValueError("execution scope must own the active state")
    if store is not None:
        expected = []
        parent = getattr(store, "parent", None)
        while parent is not None:
            expected.append(parent.state)
            parent = parent.parent
        if hasattr(store, "root_state"):
            expected.append(store.root_state)
        if len(expected) != len(scope.ancestors) or any(left is not right for left, right in zip(expected, scope.ancestors)):
            raise ValueError("persistent execution scope must match the node store's ancestor snapshots")
    supplied_cancellation = cancellation
    cancellation = CancellationToken(lambda: supplied_cancellation.cancelled() or scope.cancelled() or bool(scope.ancestors and store and store.cancellation(state.session_id, state.turn_id)))
    pipeline = MiddlewarePipeline(middleware)
    active_call_id: str | None = None

    def checkpoint(kind: str | None = None, **data) -> None:
        value = event(kind, **data) if kind else None
        if store:
            store.save(state, event=value)
        elif value:
            state.events.append(value)
            del state.events[:-100]

    def remaining_seconds() -> float | None:
        return scope.remaining_seconds()

    def check_budgets() -> None:
        cancellation.check()
        scope.check()

    def tool_context(call_id):
        return ToolContext(state, checkpoint, cancellation, store, remaining_seconds, call_id, scope, tools, policy, model, pipeline.layers, accept_config_changes)

    if store:
        state.blocked_tool_calls = [record["call_id"] for record in store.calls(state.session_id, state.turn_id) if record["status"] == "running" and not (subtasks and subtasks.managed(tool_context(record["call_id"]), record))]
        if state.blocked_tool_calls:
            state.status = RunStatus.NEEDS_RECOVERY
            state.error = "tool outcome requires reconciliation: " + ", ".join(state.blocked_tool_calls)
            checkpoint("recovery_blocked", blocked_count=len(state.blocked_tool_calls))
            return state

    def execute_model(request: ModelRequest) -> Any:
        check_budgets()
        scope.admit_model()
        messages = request.messages
        if completion_policy is not None:
            messages = deepcopy(messages)
            messages.insert(1, {"role": "system", "content": json.dumps(completion_policy.instructions(), ensure_ascii=False)})
        view = ContextPolicy(state.max_context_bytes, state.max_tool_context_bytes).build(messages, request.tools, preserve_turns=state.compaction_max_chars is not None)
        state.metadata["context"] = {"input_bytes": view.input_bytes, "removed_messages": view.removed_messages,
                                     "clipped_tool_results": view.clipped_tool_results}
        check_budgets()
        reservation = None
        if scope.preauthorize_model or scope.estimate_model:
            try:
                if not hasattr(model, "complete_reserved") or (scope.preauthorize_model and not hasattr(model, "reserve_request")):
                    raise ReservationUnavailable("model adapter does not support trusted request bounds")
                if scope.estimate_model:
                    if not hasattr(model, "estimate_request"):
                        raise ReservationUnavailable("model adapter has no request estimator")
                    reservation = model.estimate_request(deepcopy(view.messages), deepcopy(request.tools))
                    if not isinstance(reservation, TokenEstimate):
                        raise ReservationUnavailable("estimation requires an explicitly estimated reservation")
                else:
                    reservation = model.reserve_request(deepcopy(view.messages), deepcopy(request.tools))
                    if isinstance(reservation, TokenEstimate):
                        raise ReservationUnavailable("an estimate cannot satisfy strict preauthorization")
                if not isinstance(reservation, TokenReservation):
                    raise ReservationUnavailable("adapter returned an invalid reservation")
                reservation.validate(view.messages, request.tools)
            except ReservationUnavailable as exc:
                state.error = str(exc)
                raise BudgetExceeded(RunStatus.RESERVATION_UNAVAILABLE) from exc
            scope.admit_model(reservation)
        # A provider counter is caller code and may take time or observe a
        # cancellation. Recheck before recording a dispatched/unknown attempt.
        check_budgets()
        reservation_keys = scope.model_started(reservation)
        checkpoint("model_started", attempt=state.model_calls, input_bytes=view.input_bytes,
                   reserved_tokens=reservation.total_tokens if reservation else None,
                   reservation_mode="estimated" if isinstance(reservation, TokenEstimate) else "strict" if reservation else None)
        started = time.monotonic()
        try:
            remaining = remaining_seconds()
            if remaining is not None and remaining <= 0:
                raise BudgetExceeded(RunStatus.DEADLINE_EXCEEDED)
            if reservation:
                response = model.complete_reserved(view.messages, deepcopy(request.tools), reservation=reservation, cancellation=cancellation, timeout=remaining)
            elif hasattr(model, "complete_with_control"):
                response = model.complete_with_control(view.messages, deepcopy(request.tools), cancellation=cancellation, timeout=remaining)
            elif remaining is not None and hasattr(model, "complete_with_timeout"):
                response = model.complete_with_timeout(view.messages, deepcopy(request.tools), timeout=max(0.001, remaining))
            else:
                response = model.complete(view.messages, deepcopy(request.tools))
        except BaseException as exc:
            checkpoint("model_failed", attempt=state.model_calls, error_type=type(exc).__name__, duration_seconds=time.monotonic() - started)
            raise
        usage = response.usage if isinstance(response, ModelResponse) else None
        scope.model_returned(usage, reservation, reservation_keys)
        checkpoint("model_returned", attempt=state.model_calls, usage_reported=usage is not None,
                   total_tokens=usage.total_tokens if usage else None, duration_seconds=time.monotonic() - started)
        return response.message if isinstance(response, ModelResponse) else response

    model_handler = pipeline.wrap_model(execute_model)

    def execute_tool(request: ToolCallRequest) -> ToolResult:
        check_budgets()
        if request.call_id != active_call_id:
            raise ValueError("middleware cannot change the tool call ID")
        # Recheck actual dispatch after wrappers; redirected arguments never
        # inherit authorization for another request.
        if policy.action(state, request.call_id, request.name, request.arguments) != "allow":
            return ToolResult.failure(ErrorCode.PERMISSION_DENIED, "actual tool request is not authorized by tool policy")
        scope.admit_tool()
        scope.tool_started(retry_safe=tools.retry_safe(request.name))
        if store:
            store.record_attempt(state, active_call_id, {"name": request.name, "arguments": request.arguments})
        else:
            checkpoint("tool_attempt", call_id=active_call_id, name=request.name, tool_calls=state.tool_calls)
        started = time.monotonic()
        try:
            def reject_constant(value: str):
                raise ValueError("non-finite numbers are not valid JSON")

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate object key")
                    result[key] = value
                return result

            arguments = json.loads(request.arguments or "{}", parse_constant=reject_constant, object_pairs_hook=unique_object)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")  # noqa: TRY004 -- ToolResult validation contract
            json.dumps(arguments, allow_nan=False)
        except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
            result = ToolResult.failure(ErrorCode.INVALID_ARGUMENTS, "tool arguments must be a valid JSON object")
        else:
            result = tools.execute(request.name, arguments, context=tool_context(active_call_id))
        if store:
            store.record_terminal_result(state, active_call_id, result, duration_seconds=time.monotonic() - started)
        else:
            checkpoint("tool_returned", call_id=active_call_id, ok=result.ok, error_code=result.error.code if result.error else None, duration_seconds=time.monotonic() - started)
        return result

    tool_handler = pipeline.wrap_tool(execute_tool)

    class _HarnessHooks(LoopHooks):
        """Keep existing harness boundaries around the kernel's shared loop."""

        def check_start(self):
            cancellation.check()

        def check(self):
            check_budgets()

        def started(self):
            checkpoint("run_started", model_calls=state.model_calls, tool_calls=state.tool_calls)
            return AgentEvent("run_started", data={"model_calls": state.model_calls, "tool_calls": state.tool_calls})

        def before_agent(self, current):
            pipeline.before_agent(current)

        def before_model(self, current):
            pipeline.before_model(current)
            compact_history(state, summarizer if summarizer is not None else TruncatingSummarizer(), checkpoint)

        def model_started(self):
            # Actual dispatch events are still recorded inside execute_model.
            return AgentEvent("model_started", data={"attempt": state.model_calls + 1})

        def model_returned(self, duration_seconds):
            return AgentEvent("model_returned", data={"attempt": state.model_calls})

        def model_failed(self, exc, duration_seconds):
            if isinstance(exc, (Exception, Cancelled, KeyboardInterrupt)):
                return AgentEvent("model_failed", data={"attempt": state.model_calls, "error_type": type(exc).__name__})

        def messages(self):
            return project_history(state)

        def model_result(self, request):
            try:
                return model_handler(replace(request, remaining_seconds=remaining_seconds))
            except ModelTimeoutError:
                # Preserve task deadline/budget statuses without another dispatch.
                check_budgets()
                raise

        def prepare_tool(self, request):
            nonlocal active_call_id
            active_call_id = request.call_id
            self.record = store.call(state, active_call_id) if store else None
            record = self.record
            if record and record["status"] == "completed":
                return ToolResult.from_dict(json.loads(record["result_json"]))
            if record and subtasks and subtasks.managed(tool_context(active_call_id), record):
                return tools.normalize_result(subtasks.resume(tool_context(active_call_id), record))
            scope.admit_tool()
            cancellation.check()
            pipeline.before_tool(request)
            cancellation.check()
            self.action = policy.action(state, active_call_id, request.name, request.arguments)
            if self.action == "ask":
                state.metadata["approval_request"] = {"call_id": active_call_id, "turn_id": state.turn_id,
                    "name": request.name, "request_digest": request_digest(request.name, request.arguments),
                    "policy": policy.fingerprint}
                state.status = RunStatus.AWAITING_APPROVAL
                state.error = "tool requires an explicit approval or denial: " + active_call_id
                checkpoint("approval_requested", call_id=active_call_id, name=request.name)
                raise TaskStopped()
            state.metadata.pop("approval_request", None)
            if store:
                store.start_call(state, active_call_id)
            else:
                checkpoint("tool_started", call_id=active_call_id)
            return None

        def tool_started(self, request):
            return AgentEvent("tool_started", data={"call_id": request.call_id})

        def tool_result(self, request):
            try:
                return (ToolResult.failure(ErrorCode.PERMISSION_DENIED, "tool request denied by policy")
                        if self.action == "deny" else tool_handler(replace(request, remaining_seconds=remaining_seconds)))
            except ModelTimeoutError:
                check_budgets()
                raise

        def commit_tool(self, request, result):
            if store:
                if self.record["status"] == "completed":
                    checkpoint()
                else:
                    store.complete_call(state, active_call_id, result)
            else:
                checkpoint("tool_completed", call_id=active_call_id, ok=result.ok, error_code=result.error.code if result.error else None)

        def tool_returned(self, request, result, duration_seconds):
            return AgentEvent("tool_returned", data={"call_id": request.call_id, "ok": result.ok,
                                                   "error_code": result.error.code if result.error else None})

        def tool_completed(self, request, result):
            return AgentEvent("tool_completed", data={"call_id": request.call_id, "ok": result.ok,
                                                    "error_code": result.error.code if result.error else None})

        def accept_completion(self):
            if completion_policy is not None:
                report = completion_policy.evaluate(state)
                state.metadata["completion_report"] = report
                checkpoint("completion_checked", passed=report["passed"], issue_count=len(report["issues"]))
                if not report["passed"]:
                    state.messages.append({"role": "system", "content": "Harness completion requirements are unmet. Continue the current task within remaining budgets. " + json.dumps(report["issues"], ensure_ascii=False)})
                    checkpoint("completion_rejected", issue_count=len(report["issues"]))
                    return False
            return True

        def commit_assistant(self, calls):
            if store:
                store.save(state, calls, event=event("assistant_checkpoint", step=state.step, tool_count=len(calls)))

        def after_model(self, current, message):
            pipeline.after_model(current, message)

        def step_completed(self, calls):
            return AgentEvent("assistant_checkpoint", data={"step": state.step, "tool_count": len(calls)})

        def handle_exception(self, exc):
            if isinstance(exc, TaskPaused):
                state.status = exc.status
                state.error = f"subtask {exc.task_id} paused; inspect its node and reconcile or approve before resuming"
            elif isinstance(exc, Cancelled):
                state.status = RunStatus.CANCELLED
                state.error = "execution cancelled; inspect completed and uncertain calls before resuming"
            elif isinstance(exc, BudgetExceeded):
                state.status = exc.status
                state.error = {RunStatus.USAGE_UNAVAILABLE: "model usage is unknown; verify provider records and reconcile usage before continuing with a token budget",
                               RunStatus.TOOL_LOOP_DETECTED: "three consecutive tool rounds returned identical requests and outcomes; change the approach before resuming",
                               RunStatus.REQUEST_BOUND_VIOLATED: "adapter request bound violated; verify usage and repair the counter before explicitly acknowledging this violation",
                               RunStatus.RESERVATION_UNAVAILABLE: state.error or "trusted model reservation unavailable",
                               RunStatus.MAX_TOKENS_EXCEEDED: "reported token budget exhausted; increase the absolute turn limit to continue",
                               RunStatus.DEADLINE_EXCEEDED: "turn deadline exceeded; increase the duration from the original turn start to continue"}.get(exc.status)
            elif isinstance(exc, ContextLimitExceeded):
                state.status = RunStatus.CONTEXT_LIMIT_EXCEEDED
                state.error = str(exc)
            elif isinstance(exc, WorkspaceChanged):
                state.status = RunStatus.WORKSPACE_CHANGED
                state.error = str(exc)
            elif isinstance(exc, KeyboardInterrupt):
                state.status = RunStatus.INTERRUPTED
                state.error = "execution interrupted; resume the session to inspect its checkpoint"
            elif isinstance(exc, Exception):
                state.status = RunStatus.FAILED
                state.error = f"{type(exc).__name__}: {exc}"
            else:
                return False
            return True

        def cleanup(self):
            try:
                pipeline.after_agent(state)
            except Cancelled:
                state.status = RunStatus.CANCELLED
                state.error = "execution cancelled during cleanup; inspect the saved session"
            except KeyboardInterrupt:
                state.status = RunStatus.INTERRUPTED
                state.error = "execution interrupted during cleanup; resume the saved session"
            except Exception as exc:  # noqa: BLE001 -- Persist cleanup failures as run status.
                state.status = RunStatus.FAILED
                state.error = f"{type(exc).__name__}: {exc}"

        def stopped(self):
            checkpoint("run_stopped", status=state.status, model_calls=state.model_calls, tool_calls=state.tool_calls,
                       total_tokens=state.total_tokens, unknown_usage_calls=state.unknown_usage_calls)
            return AgentEvent("run_stopped", data={"status": state.status, "model_calls": state.model_calls,
                                                  "tool_calls": state.tool_calls, "total_tokens": state.total_tokens,
                                                  "unknown_usage_calls": state.unknown_usage_calls})

    for _ in iter_agent(model, state, tools=tools, hooks=(_HarnessHooks(),), cancellation=cancellation):
        pass
    return state
