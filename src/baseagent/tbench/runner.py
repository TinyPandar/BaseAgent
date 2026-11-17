"""Run one task: the agent loop on the host, every action through ``environment.exec``.

Harbor-independent. ``environment`` only needs Harbor's ``async exec(command, cwd=,
timeout_sec=)`` returning an object with ``stdout``, ``stderr`` and ``return_code``;
``context`` only needs Harbor ``AgentContext``'s token, cost and metadata attributes.

Outcomes (``metadata["stop_reason"]``):

* ``completed`` - the agent finished; Harbor grades it.
* ``steps_exhausted`` - step / model-call / tool-call limit or a loop guard; graded.
* ``timeout`` - the remaining wall clock ran out (our deadline or Harbor's outer timeout); graded.
* ``permission_blocked`` - the run stopped waiting for a tool approval; graded.
* ``context_limit`` - the context window budget stopped the run; graded.
* ``budget_stopped`` - the CNY budget stopped the run: raises ``BudgetExceededError``.
* ``infrastructure`` - environment, provider or harness failure: raises
  ``BaseAgentInfrastructureError``.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from baseagent.agent import run_agent
from baseagent.agent.workers import (
    INPUT_CNY_PER_MILLION,
    OUTPUT_CNY_PER_MILLION,
    SharedBudgetMiddleware,
    TurnBudget,
)
from baseagent.backends.exec_backend import STOP_TIMEOUT, EnvironmentUnavailable, ExecBackend
from baseagent.kernel.cancellation import CancellationToken, Cancelled
from baseagent.kernel.state import RunStatus
from baseagent.middleware import AgentMiddleware
from baseagent.tools.result import ErrorCode
from baseagent.tools.workspace import DEFAULT_COMMAND_TIMEOUT, Workspace, coding_tools

from .channel import AsyncExecChannel
from .errors import BaseAgentInfrastructureError, BudgetExceededError

SYSTEM_PROMPT = """You are an autonomous coding agent solving a terminal task inside a Linux container.
The task's working directory is {root}. File tools take paths relative to it; absolute paths under it
also work. Use run_command for everything else, including files outside {root}, installing packages
and services. run_command takes an argv list and does not use a shell: for pipes, redirects or
several commands use ["bash", "-lc", "..."]. Each command has a timeout argument (default 20 s,
at most {max_timeout} s, and never beyond the time left for the task); give long builds a larger one.
Nobody will answer questions. Work until the task is done, check the result yourself, then reply
with a short summary of what you did and what you verified."""

SUMMARY_FILE = "baseagent-summary.json"
STEP_LIMITS = {RunStatus.MAX_STEPS_EXCEEDED, RunStatus.MAX_MODEL_CALLS_EXCEEDED, RunStatus.MAX_TOOL_CALLS_EXCEEDED,
               RunStatus.TOOL_LOOP_DETECTED, RunStatus.ABORTED_BY_WATCHDOG}
BUDGET_STOPS = {RunStatus.MAX_TOKENS_EXCEEDED, RunStatus.USAGE_UNAVAILABLE, RunStatus.RESERVATION_UNAVAILABLE,
                RunStatus.REQUEST_BOUND_VIOLATED}


@dataclass
class RunConfig:
    max_steps: int = 60
    max_model_calls: int | None = None
    max_tool_calls: int = 200
    # Upper bound for one command; 60 s unless the run raises it. The remaining wall clock always caps it.
    max_command_timeout_sec: int = DEFAULT_COMMAND_TIMEOUT
    # Agent wall clock for this task (Harbor's agent timeout); None when it could not be determined.
    agent_timeout_sec: float | None = None
    deadline_source: str = "unknown"
    # Stop this many seconds before the wall clock so the run ends on its own terms.
    safety_margin_sec: float = 15.0
    budget_cny: Decimal | None = None
    input_cny_per_million: Decimal = INPUT_CNY_PER_MILLION
    output_cny_per_million: Decimal = OUTPUT_CNY_PER_MILLION
    # Explicit CNY->USD factor; without one cost_usd stays empty.
    usd_per_cny: Decimal | None = None
    provider_calls_are_billed: bool = True
    extra_metadata: dict = field(default_factory=dict)


class RunObserver(AgentMiddleware):
    """Outermost model wrapper: refuses dispatch after cancellation and freezes counted usage."""

    def __init__(self, cancellation: CancellationToken):
        self.cancellation = cancellation
        self.lock = threading.Lock()
        self.prompt_tokens = self.completion_tokens = 0
        self.dispatched = self.in_flight = 0
        self.unknown_usage_calls = 0
        self.permission_denials = 0
        self.frozen = None

    def wrap_model_call(self, request, handler):
        state = request.state
        with self.lock:
            if self.frozen is not None or self.cancellation.cancelled():
                raise Cancelled()
            self.in_flight += 1
        before = (state.model_calls, state.prompt_tokens, state.completion_tokens, state.unknown_usage_calls)
        try:
            return handler(request)
        finally:
            with self.lock:
                self.in_flight -= 1
                if state.model_calls != before[0]:
                    self.dispatched += 1
                    if self.frozen is None:
                        self.prompt_tokens += state.prompt_tokens - before[1]
                        self.completion_tokens += state.completion_tokens - before[2]
                        self.unknown_usage_calls += state.unknown_usage_calls - before[3]

    def after_tool(self, request, result):
        if result.error is not None and result.error.code == ErrorCode.PERMISSION_DENIED:
            with self.lock:
                self.permission_denials += 1

    def freeze(self):
        """Stop counting and dispatching; returns usage counted up to now."""
        with self.lock:
            self.cancellation.cancel()
            if self.frozen is None:
                self.frozen = {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                               "model_calls": self.dispatched, "in_flight_model_calls": self.in_flight,
                               "unknown_usage_calls": self.unknown_usage_calls}
            return dict(self.frozen)

    def usage(self):
        with self.lock:
            if self.frozen is not None:
                return dict(self.frozen)
            return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                    "model_calls": self.dispatched, "in_flight_model_calls": self.in_flight,
                    "unknown_usage_calls": self.unknown_usage_calls}


def stop_reason(state, budget) -> str:
    status = state.status
    exhausted = (state.metadata.get("shared_budget_exhausted") or {}).get("reason")
    if exhausted == "budget_cny":
        return "budget_stopped"
    if exhausted in {"max_steps", "max_model_calls"}:
        return "steps_exhausted"
    if status == RunStatus.COMPLETED:
        return "completed"
    if status in STEP_LIMITS:
        return "steps_exhausted"
    if status in BUDGET_STOPS:
        return "budget_stopped"
    if status == RunStatus.DEADLINE_EXCEEDED:
        return "timeout"
    if status == RunStatus.AWAITING_APPROVAL:
        return "permission_blocked"
    if status == RunStatus.CONTEXT_LIMIT_EXCEEDED:
        return "context_limit"
    return "infrastructure"


def model_secrets(model):
    """Credentials the model adapter holds, so error text can be scrubbed of them."""
    values = []
    for owner, name in ((getattr(model, "client", None), "api_key"), (model, "api_key"), (model, "_key")):
        value = getattr(owner, name, None) if owner is not None else None
        if isinstance(value, str) and value:
            values.append(value)
    return values


def redact(text, secrets=()) -> str:
    """Error text safe for metadata, logs and Harbor's result.json.

    Removes the model's key, every host environment value whose name looks like a credential
    (KEY / TOKEN / SECRET / PASSWORD), and ``sk-...`` / ``Bearer ...`` shapes.
    """
    from baseagent.testing.redaction import Redactor
    extra = [value for name, value in os.environ.items()
             if value and len(value) >= 6 and any(part in name.upper() for part in ("PASSWORD", "CREDENTIAL"))]
    return Redactor(secrets=[*secrets, *extra]).text(str(text))


async def stop_everything(environment, backend) -> bool:
    """Stop everything the agent started in the environment, from the event loop.

    Covers commands still running and children that finished commands left in the background.
    True only when the environment confirms nothing of the agent's is still alive and no earlier
    stop during the run failed (a process that was not stopped may have ended on its own since).
    """
    command = backend.stop_all_command() if backend is not None else None
    if command is None:
        return True
    try:
        result = await asyncio.wait_for(environment.exec(command, timeout_sec=int(STOP_TIMEOUT) + 5),
                                        STOP_TIMEOUT + 10)
        return result.return_code == 0 and not backend.stop_failed
    except Exception:  # noqa: BLE001 -- the environment may already be gone
        return False


async def discover_workdir(environment) -> str:
    """The task's working directory as the environment itself reports it (TB 2.0: /app)."""
    try:
        result = await environment.exec("pwd -P", timeout_sec=60)
    except Exception as exc:  # noqa: BLE001
        raise BaseAgentInfrastructureError(
            f"environment exec failed before the agent started: {type(exc).__name__}: {exc}") from None
    lines = (result.stdout or "").strip().splitlines()
    if result.return_code != 0 or not lines or not lines[-1].startswith("/"):
        raise BaseAgentInfrastructureError(f"could not determine the task working directory (exit {result.return_code})")
    return lines[-1].strip()


async def run_in_environment(instruction: str, environment, context, *, model, config: RunConfig,
                             logs_dir: Path | None = None, budget=None) -> str:
    """Run the agent for one task and fill ``context``. Returns the stop reason or raises.

    ``budget`` may be any ``TurnBudget``-compatible ledger (admit/charge/snapshot); by default
    each task gets its own ``TurnBudget`` with ``config.budget_cny``. Every exception that leaves
    this function is ``BudgetExceededError``, ``BaseAgentInfrastructureError`` or a cancellation,
    raised without its cause chain and with credentials removed from its text.
    """
    secrets = model_secrets(model)
    try:
        return await _run(instruction, environment, context, model=model, config=config, logs_dir=logs_dir,
                          budget=budget, secrets=secrets)
    except (BudgetExceededError, BaseAgentInfrastructureError, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001 -- anything else is the harness's own failure
        message = redact(f"{type(exc).__name__}: {exc}", secrets)
    raise BaseAgentInfrastructureError(message) from None


async def _run(instruction, environment, context, *, model, config, logs_dir, budget, secrets):
    started = time.monotonic()
    loop = asyncio.get_running_loop()
    cancellation = CancellationToken()
    observer = RunObserver(cancellation)
    max_steps = config.max_steps
    budget = budget or TurnBudget(config.max_model_calls or max_steps, max_steps, budget_cny=config.budget_cny,
                                  input_rate=config.input_cny_per_million, output_rate=config.output_cny_per_million)
    record = {"workdir": None, "deadline_source": config.deadline_source,
              "agent_timeout_sec": config.agent_timeout_sec,
              "max_command_timeout_sec": config.max_command_timeout_sec, **config.extra_metadata}

    def finish(reason, *, usage, run_status=None, error=None, steps=None, tool_calls=None, stopped=None):
        cost = (Decimal(usage["prompt_tokens"]) * Decimal(config.input_cny_per_million)
                + Decimal(usage["completion_tokens"]) * Decimal(config.output_cny_per_million)) / Decimal(1_000_000)
        if not config.provider_calls_are_billed:
            cost = Decimal(0)
        cost = cost.quantize(Decimal("0.000001"))
        context.n_input_tokens = usage["prompt_tokens"]
        context.n_output_tokens = usage["completion_tokens"]
        context.cost_usd = float(cost * config.usd_per_cny) if config.usd_per_cny is not None else None
        metadata = {"agent": "baseagent", "stop_reason": reason, "run_status": run_status,
                    "model_calls": usage["model_calls"],
                    "provider_calls": usage["model_calls"] if config.provider_calls_are_billed else 0,
                    "in_flight_model_calls_at_stop": usage["in_flight_model_calls"],
                    "unknown_usage_calls": usage["unknown_usage_calls"],
                    "steps": steps, "tool_calls": tool_calls, "permission_denials": observer.permission_denials,
                    "environment_commands": backend.commands if backend is not None else 0,
                    "cost_cny": str(cost), "cost_currency": "CNY",
                    "pricing_cny_per_million": {"input": str(config.input_cny_per_million),
                                                "output": str(config.output_cny_per_million)},
                    "budget_cny": None if config.budget_cny is None else str(config.budget_cny),
                    # True: everything the agent started was confirmed stopped; None: not attempted
                    # (a finished run keeps processes it left running, as a service the tests use would).
                    "agent_processes_stopped": stopped,
                    "elapsed_sec": round(time.monotonic() - started, 3), **record}
        if error:
            metadata["error"] = redact(error, secrets)[:2000]
        context.metadata = {**(context.metadata or {}), **metadata}
        if logs_dir is not None:
            try:
                Path(logs_dir).mkdir(parents=True, exist_ok=True)
                (Path(logs_dir) / SUMMARY_FILE).write_text(json.dumps(metadata, ensure_ascii=False, indent=2),
                                                         encoding="utf-8")
            except OSError:
                pass
        return metadata

    backend = None
    try:
        root = await discover_workdir(environment)
    except BaseAgentInfrastructureError as exc:
        message = redact(str(exc), secrets)
        finish("infrastructure", usage=observer.usage(), error=message)
        raise BaseAgentInfrastructureError(message) from None
    record["workdir"] = root
    backend = ExecBackend(AsyncExecChannel(environment, loop), root, cancellation=cancellation)
    workspace = Workspace(backend=backend, allow_write=True, allow_command=True,
                          max_command_timeout=config.max_command_timeout_sec)
    tools = coding_tools(workspace)
    max_duration = None
    if config.agent_timeout_sec is not None:
        max_duration = config.agent_timeout_sec - config.safety_margin_sec - (time.monotonic() - started)
        if max_duration <= 1:
            finish("timeout", usage=observer.usage(), error="no wall-clock time left before the agent started")
            return "timeout"
    record["max_duration_sec"] = None if max_duration is None else round(max_duration, 3)
    system_prompt = SYSTEM_PROMPT.format(root=root, max_timeout=config.max_command_timeout_sec)
    outcome: asyncio.Future = loop.create_future()

    def deliver(method, value):
        if not outcome.done():
            getattr(outcome, method)(value)

    def work():
        try:
            state = run_agent(model, instruction, tools=tools,
                              middleware=[observer, SharedBudgetMiddleware(budget, worker=False)],
                              system_prompt=system_prompt, max_steps=max_steps,
                              max_model_calls=config.max_model_calls, max_tool_calls=config.max_tool_calls,
                              max_duration_seconds=max_duration, runtime_config={"spill": {"enabled": False}},
                              cancellation=cancellation)
        except BaseException as exc:  # noqa: BLE001 -- handed to the event loop thread
            method, value = "set_exception", exc if isinstance(exc, Exception) else _Wrapped(exc)
        else:
            method, value = "set_result", state
        try:
            loop.call_soon_threadsafe(deliver, method, value)
        except RuntimeError:
            pass    # the trial already ended (outer cancellation); its context was filled then

    # Daemon thread: after an outer cancellation it stops at its next check and must never block exit.
    threading.Thread(target=work, name="baseagent-tbench", daemon=True).start()
    try:
        state = await outcome
    except asyncio.CancelledError:
        # Harbor's agent timeout (or job cancellation). Nothing new is dispatched after this point
        # and counted usage is frozen at the current total. Everything the agent started in the
        # container is stopped before Harbor moves on to grading.
        usage = observer.freeze()
        stopped = await stop_everything(environment, backend)
        if not stopped:
            # Grading beside processes that may still write would not be a real result.
            message = ("cancelled by the evaluation harness, and stopping the agent's processes in the "
                       "environment could not be confirmed")
            finish("infrastructure", usage=usage, error=message, stopped=False)
            raise BaseAgentInfrastructureError(message) from None
        finish("timeout", usage=usage, error="cancelled by the evaluation harness (agent timeout or job cancellation)",
               stopped=True)
        raise
    except Exception as exc:  # noqa: BLE001 -- includes _Wrapped BaseExceptions from the worker thread
        usage = observer.freeze()
        inner = exc.inner if isinstance(exc, _Wrapped) else exc
        message = redact(f"{type(inner).__name__}: {inner}", secrets)
        stopped = await stop_everything(environment, backend)
        finish("infrastructure", usage=usage, error=message, stopped=stopped)
        raise BaseAgentInfrastructureError(message) from None
    usage = observer.freeze()
    reason = stop_reason(state, budget)
    error = state.error if reason != "completed" else None
    stopped = None
    if reason in ("timeout", "budget_stopped", "infrastructure"):
        # Past the deadline (or not to be graded as a normal run): nothing the agent started,
        # including children it left in the background, may keep running or writing.
        stopped = await stop_everything(environment, backend)
        if not stopped and reason != "infrastructure":
            error = f"{reason}: stopping the agent's processes in the environment could not be confirmed"
            reason = "infrastructure"
    finish(reason, usage=usage, run_status=str(state.status), error=error, steps=state.step,
           tool_calls=state.tool_calls, stopped=stopped)
    if reason == "budget_stopped":
        raise BudgetExceededError(redact(state.error or "CNY budget exhausted", secrets))
    if reason == "infrastructure":
        raise BaseAgentInfrastructureError(redact(f"run ended with status {state.status}: {error}", secrets))
    return reason


class _Wrapped(Exception):
    """Carries a BaseException (EnvironmentUnavailable, Cancelled, ...) out of the worker thread."""

    def __init__(self, inner):
        super().__init__(f"{type(inner).__name__}: {inner}")
        self.inner = inner


__all__ = ["RunConfig", "RunObserver", "SYSTEM_PROMPT", "discover_workdir", "run_in_environment", "stop_reason",
           "EnvironmentUnavailable"]
