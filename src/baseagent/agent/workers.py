"""Full-duplex workers for the main conversation agent.

``delegate_task`` returns at once with a worker id. Each worker runs in its own
thread as an ordinary single-agent session (``<main session>-w<n>``) in the same
SessionStore, so it reuses the existing ledger, approvals, cancellation and
spill rules. The main agent never waits: results are pushed back later as
notices that the chat prints between prompts and attaches to the next main
agent turn.

Concurrency rules: at most ``max_workers`` active (running or awaiting
approval) workers, and at most one of them may hold write tools. Extra requests
are rejected with an explicit reason, not queued.

Budget: every main-agent turn opens one ``TurnBudget``. The main agent's own
calls in that turn and all workers it started draw model calls and steps (and,
optionally, CNY) from it atomically. One model call stays reserved for the main
agent's reply.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from baseagent.middleware import AgentMiddleware
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.result import ErrorCode, ToolResult

from .cancellation import CancellationToken
from .errors import BudgetExceeded
from .events import event
from .router import WORKER_PROMPT, worker_report
from .state import RunStatus

DELEGATE_TOOL = "delegate_task"
MESSAGE_TOOL = "message_worker"
RUNNING, AWAITING, COMPLETED, FAILED, CANCELLED = "running", "awaiting_approval", "completed", "failed", "cancelled"
ACTIVE = frozenset({RUNNING, AWAITING})
TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})
# qwen-plus non-thinking rates already used by the evaluation metering (CNY per million tokens).
INPUT_CNY_PER_MILLION = Decimal("0.8")
OUTPUT_CNY_PER_MILLION = Decimal(2)
PUSH_HEADER = "[worker 推送：系统自动附加的结果，不是用户输入]"
USER_HEADER = "[用户消息]"
INBOX_HEADER = "[主 Agent 转达的补充要求]"


def worker_state(status):
    """Collapse a session RunStatus into the five worker states shown to users."""
    status = RunStatus(status)
    if status == RunStatus.COMPLETED:
        return COMPLETED
    if status == RunStatus.AWAITING_APPROVAL:
        return AWAITING
    if status == RunStatus.RUNNING:
        return RUNNING
    if status in (RunStatus.CANCELLED, RunStatus.INTERRUPTED):
        return CANCELLED
    return FAILED


class TurnBudget:
    """Thread-safe pool shared by one main-agent turn and the workers it starts.

    Admission is atomic, so concurrent callers never exceed ``max_model_calls``
    or ``max_steps`` in total. Starting a worker holds ``reserve`` model calls
    and steps for the main agent's reply: workers cannot take them, and the main
    agent's next call releases them. CNY is charged from reported usage after
    each call, so it is checked before dispatch; calls already in flight when
    the budget is crossed may still be billed.
    """

    def __init__(self, max_model_calls, max_steps, *, budget_cny=None, reserve=1,
                 input_rate=INPUT_CNY_PER_MILLION, output_rate=OUTPUT_CNY_PER_MILLION):
        if any(type(value) is not int or value < 1 for value in (max_model_calls, max_steps)):
            raise ValueError("shared budget limits must be positive integers")
        if type(reserve) is not int or reserve < 0:
            raise ValueError("reply reserve must be a nonnegative integer")
        self.max_model_calls, self.max_steps, self.reserve = max_model_calls, max_steps, reserve
        self.budget_cny = None if budget_cny is None else Decimal(str(budget_cny))
        if self.budget_cny is not None and not self.budget_cny > 0:
            raise ValueError("budget_cny must be positive")
        self.input_rate, self.output_rate = Decimal(input_rate), Decimal(output_rate)
        self.model_calls = self.steps = 0
        self.held_calls = self.held_steps = 0
        self.cost_cny = Decimal(0)
        self.usage_unknown = False
        self._lock = threading.Lock()

    def admit_step(self, *, worker):
        with self._lock:
            if self.steps >= self.max_steps - (self.held_steps if worker else 0):
                return "max_steps"
            self.steps += 1
            if not worker and self.held_steps:
                self.held_steps -= 1
            return None

    def admit_model(self, *, worker):
        with self._lock:
            if self.model_calls >= self.max_model_calls - (self.held_calls if worker else 0):
                return "max_model_calls"
            if self.budget_cny is not None and (self.usage_unknown or self.cost_cny >= self.budget_cny):
                return "budget_cny"
            self.model_calls += 1
            if not worker and self.held_calls:
                self.held_calls -= 1
            return None

    def refund_model(self):
        with self._lock:
            self.model_calls -= 1

    def charge(self, prompt_tokens, completion_tokens, *, unknown=False):
        with self._lock:
            self.cost_cny += (Decimal(prompt_tokens) * self.input_rate
                              + Decimal(completion_tokens) * self.output_rate) / Decimal(1_000_000)
            self.usage_unknown |= unknown

    def room(self):
        """Model calls a new worker may use: what is left after holding the main agent's reply."""
        with self._lock:
            return min(self.max_model_calls - self.model_calls, self.max_steps - self.steps) - self.reserve

    def hold_reply(self):
        with self._lock:
            self.held_calls = self.held_steps = self.reserve

    def snapshot(self):
        with self._lock:
            value = {"model_calls": self.model_calls, "max_model_calls": self.max_model_calls,
                     "steps": self.steps, "max_steps": self.max_steps}
            if self.budget_cny is not None:
                value.update(cost_cny=str(self.cost_cny.quantize(Decimal("0.000001"))), budget_cny=str(self.budget_cny))
            return value


class SharedBudgetMiddleware(AgentMiddleware):
    """Charge one TurnBudget at the real dispatch boundary (innermost model wrapper)."""

    STATUS = {"max_steps": RunStatus.MAX_STEPS_EXCEEDED, "max_model_calls": RunStatus.MAX_MODEL_CALLS_EXCEEDED,
              "budget_cny": RunStatus.MAX_TOKENS_EXCEEDED}

    def __init__(self, budget, *, worker):
        self.budget, self.worker = budget, worker

    def before_agent(self, state):
        # A flag from an earlier turn must not explain a later stop.
        state.metadata.pop("shared_budget_exhausted", None)

    def _refuse(self, state, reason):
        state.metadata["shared_budget_exhausted"] = {"reason": reason, **self.budget.snapshot()}
        raise BudgetExceeded(self.STATUS[reason])

    def before_model(self, state):
        reason = self.budget.admit_step(worker=self.worker)
        if reason:
            self._refuse(state, reason)

    def wrap_model_call(self, request, handler):
        state = request.state
        reason = self.budget.admit_model(worker=self.worker)
        if reason:
            self._refuse(state, reason)
        before = (state.model_calls, state.prompt_tokens, state.completion_tokens, state.unknown_usage_calls)
        try:
            return handler(request)
        except BaseException:
            # Refused by the node's own limits before dispatch: give the call back.
            if state.model_calls == before[0]:
                self.budget.refund_model()
            raise
        finally:
            if state.model_calls != before[0]:
                self.budget.charge(state.prompt_tokens - before[1], state.completion_tokens - before[2],
                                   unknown=state.unknown_usage_calls > before[3])


class SessionTagMiddleware(AgentMiddleware):
    """Record fixed session metadata (for example the session mode) without overwriting what a session has."""

    def __init__(self, **values):
        self.values = values

    def before_agent(self, state):
        for key, value in self.values.items():
            state.metadata.setdefault(key, value)


class InboxMiddleware(AgentMiddleware):
    """Inject requirements sent with message_worker or /tell before the worker's next model call."""

    def __init__(self, record):
        self.record = record

    def before_model(self, state):
        for text in self.record.take_inbox():
            state.messages.append({"role": "user", "content": f"{INBOX_HEADER}\n{text}"})


@dataclass
class WorkerRecord:
    id: str
    session_id: str
    task: str
    allow_writes: bool
    status: str = RUNNING
    detail: str | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    usage: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    approval: dict | None = None
    announced: str | None = None
    main_turn: int | None = None

    def __post_init__(self):
        self.thread = None
        self.budget = None
        self.cancellation = CancellationToken()
        self.cancel_requested = False
        self._inbox = []
        self._inbox_lock = threading.Lock()

    def take_inbox(self):
        with self._inbox_lock:
            items, self._inbox = self._inbox, []
            return items

    def put_inbox(self, text):
        with self._inbox_lock:
            self._inbox.append(text)

    def to_dict(self):
        value = asdict(self)
        value["task"] = value["task"][:500]
        return value

    @classmethod
    def from_dict(cls, value):
        names = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in names if key in value})

    def elapsed(self, now=None):
        end = self.finished_at if self.finished_at is not None else (now or time.time())
        return max(0.0, end - self.started_at)


class WorkerManager:
    """The main agent's delegation runtime in full-duplex chat.

    Bound into run_agent through the ``subtasks`` hook: the main agent gets
    ``delegate_task`` and ``message_worker`` only. ``runner`` starts (task) or
    resumes (None) one worker session; main.py supplies it with the CLI
    composition. Its keyword arguments: session_id, tools, system_prompt,
    max_steps, max_model_calls, max_tool_calls, middleware (innermost layers),
    cancellation, emit (trace lines, queued for the chat) and metadata (for a
    new worker session).
    """

    tool_name = DELEGATE_TOOL

    def __init__(self, tools, *, system_prompt, store, runner, max_workers=2, max_steps=12, max_tool_calls=24,
                 reply_reserve=1, summary_chars=4000, budget_factory=None, join_seconds=10.0):
        if DELEGATE_TOOL in tools._tools or MESSAGE_TOOL in tools._tools or "run_subtask" in tools._tools:
            raise ValueError("worker tools must not include delegation tools")
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if type(reply_reserve) is not int or reply_reserve < 1 or type(summary_chars) is not int or summary_chars < 200:
            raise ValueError("reply reserve must be positive and summary_chars at least 200")
        self.worker_tools, self.store, self.runner = tools, store, runner
        self.worker_prompt = system_prompt.rstrip() + "\n" + WORKER_PROMPT
        self.max_workers, self.max_steps, self.max_tool_calls = max_workers, max_steps, max_tool_calls
        self.reply_reserve, self.summary_chars = reply_reserve, summary_chars
        self.budget_factory = budget_factory or (lambda: TurnBudget(16, 16, reserve=reply_reserve))
        self.join_seconds = join_seconds
        self.session_id = None
        self.budget = None
        self.records = {}
        self._next = 1
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._approvals = deque()
        self._pushes = []
        self._display = deque()
        self.on_notice = None
        # Optional chat event hook (baseagent.chat): observer(kind, record, **data). It only
        # reports what already happened; it never changes limits, approvals or control flow.
        self.observer = None

    # ----- run_agent subtasks protocol -------------------------------------------------
    def contract(self):
        return {"version": 1, "kind": "duplex_workers", "max_depth": 1, "worker_prompt": self.worker_prompt,
                "worker_tools": sorted(self.worker_tools._tools), "reply_reserve": self.reply_reserve,
                "summary_chars": self.summary_chars}

    def bind_tools(self, tools):
        if tools is not self.worker_tools:
            raise ValueError("worker manager must bind the entry registry it was created with")
        bound = ToolRegistry(max_result_bytes=tools.max_result_bytes)
        bound.register(DELEGATE_TOOL, "Start one execution worker in the background and return at once with its id "
                       "(status running). Its result is pushed into the conversation later: summary, citations, paths, "
                       "call ids and spill refs. Read-only workers may run in parallel; at most one worker may write.",
                       {"type": "object", "properties": {
                           "task": {"type": "string", "minLength": 1, "maxLength": 16000,
                                    "description": "Complete, self-contained task for the worker"},
                           "allow_writes": {"type": "boolean", "default": False,
                                            "description": "true only when the user explicitly asked to change files or run commands"}},
                        "required": ["task"]}, self.delegate, contextual=True)
        bound.register(MESSAGE_TOOL, "Add a requirement to a running worker; it is injected before the worker's next step. "
                       "Fails for finished workers.",
                       {"type": "object", "properties": {
                           "task_id": {"type": "string", "minLength": 1, "maxLength": 32},
                           "text": {"type": "string", "minLength": 1, "maxLength": 4000}},
                        "required": ["task_id", "text"]}, self.message_tool)
        return bound

    def managed(self, context, record):
        return False

    def resume(self, context, record):
        raise RuntimeError("full-duplex delegation has no in-turn child to resume")

    # ----- delegation ------------------------------------------------------------------
    def delegate(self, task, allow_writes=False, *, context):
        if not task.strip():
            return ToolResult.failure(ErrorCode.INVALID_ARGUMENTS, "worker task must not be blank")
        with self._lock:
            self.session_id = self.session_id or context.state.session_id
            active = [record for record in self.records.values() if record.status in ACTIVE]
            if len(active) >= self.max_workers:
                ids = ", ".join(record.id for record in active)
                return ToolResult.failure(
                    ErrorCode.LIMIT_EXCEEDED,
                    f"rejected: max_workers ({len(active)}/{self.max_workers} active: {ids}); tell the user to wait for "
                    "a pushed result or /cancel a worker, then ask again",
                    data={"status": "rejected", "reason": "max_workers", "active": [record.id for record in active]})
            writer = next((record for record in active if record.allow_writes), None)
            if allow_writes and writer is not None:
                return ToolResult.failure(
                    ErrorCode.LIMIT_EXCEEDED,
                    f"rejected: single_writer ({writer.id} already holds write tools); only one worker may write at a "
                    "time. Tell the user to wait for its result or /cancel it",
                    data={"status": "rejected", "reason": "single_writer", "writer": writer.id})
            budget = self.budget or self.budget_factory()
            room = budget.room()
            tool_room = context.state.max_tool_calls - context.state.tool_calls
            if room < 1:
                return ToolResult.failure(ErrorCode.LIMIT_EXCEEDED, "not enough model calls left in this turn to start a "
                                          "worker; answer the user with what is known")
            number = self._next
            while self.store.load(self._session(number)) is not None:
                number += 1
            self._next = number + 1
            record = WorkerRecord(f"w{number}", self._session(number), task, bool(allow_writes))
            # File checkpoints: this worker's writes belong to the main-agent turn that dispatched it.
            from baseagent.session.checkpoints import turn_number
            record.main_turn = turn_number(context.state)
            record.usage = {"max_model_calls": min(self.max_steps, room), "max_steps": min(self.max_steps, room),
                            "max_tool_calls": min(self.max_tool_calls, max(tool_room, 0)),
                            "model_calls": 0, "steps": 0, "tool_calls": 0}
            record.budget = budget
            budget.hold_reply()
            self.records[record.id] = record
            context.state.metadata["workers"] = self.snapshot()
        self._start(record, task)
        self._notify("worker_started", record)
        return ToolResult(data={"task_id": record.id, "status": RUNNING, "allow_writes": record.allow_writes,
                                "limits": {key: record.usage[key] for key in ("max_model_calls", "max_steps", "max_tool_calls")}})

    def _session(self, number):
        return f"{self.session_id[:100]}-w{number}"

    def tools_for(self, record):
        names = [name for name in self.worker_tools._tools if record.allow_writes or self.worker_tools.retry_safe(name)]
        return self.worker_tools.subset(names)

    def _layers(self, record):
        # The runner puts these innermost, so every real dispatch (including retries) is charged.
        return [InboxMiddleware(record), SharedBudgetMiddleware(record.budget or self.budget_factory(), worker=True)]

    def _start(self, record, user_input):
        thread = threading.Thread(target=self._run, args=(record, user_input), name=f"worker-{record.id}", daemon=True)
        with self._lock:
            record.status, record.approval = RUNNING, None
            record.thread = thread
        thread.start()

    def _run(self, record, user_input):
        state, error = None, None
        limits = record.usage if user_input is not None else {}
        try:
            state = self.runner(user_input, session_id=record.session_id, tools=self.tools_for(record),
                                system_prompt=self.worker_prompt, max_steps=limits.get("max_steps"),
                                max_model_calls=limits.get("max_model_calls"), max_tool_calls=limits.get("max_tool_calls"),
                                middleware=self._layers(record), cancellation=record.cancellation,
                                emit=lambda line: self._progress(record, line),
                                metadata={"session_mode": "worker", "main_session": self.session_id, "worker_id": record.id,
                                          **({"main_turn": record.main_turn} if record.main_turn is not None else {})})
        except BaseException as exc:  # noqa: BLE001 -- a worker failure must not kill the chat.
            error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._settle(record, state, error)
            record.thread = None
            self._changed.notify_all()
        self._announce(record)

    def _settle(self, record, state, error):
        if state is not None:
            record.usage.update(model_calls=state.model_calls, max_model_calls=state.max_model_calls, steps=state.step,
                                max_steps=state.max_steps, tool_calls=state.tool_calls, max_tool_calls=state.max_tool_calls)
        if state is None:
            status, record.detail, record.error = FAILED, "failed", error
        else:
            status = worker_state(state.status)
            record.detail, record.error = str(state.status), state.error
            shared = state.metadata.get("shared_budget_exhausted")
            if shared and status == FAILED:
                # The kernel's generic limit text would name the wrong limit.
                record.detail, record.error = "shared_" + shared["reason"], None
        if record.cancel_requested and status != COMPLETED:
            status = CANCELLED
        record.status = status
        if status == AWAITING:
            request = state.metadata.get("approval_request") or {}
            record.approval = {"call_id": request.get("call_id"), "request_digest": request.get("request_digest"),
                               "name": request.get("name", "tool")}
            if record.id not in self._approvals:
                self._approvals.append(record.id)
            return
        if state is not None and status in TERMINAL:
            record.report = worker_report(state, limit=self.summary_chars if status == COMPLETED else 1000)
        if status in TERMINAL and record.finished_at is None:
            record.finished_at = time.time()

    # ----- pushes ----------------------------------------------------------------------
    def push_payload(self, record):
        payload = {"task_id": record.id, "status": record.status}
        if record.status == AWAITING:
            payload["tool"] = (record.approval or {}).get("name")
            return payload
        payload.update(record.report or {"summary": "", "summary_truncated": False, "citations": [], "paths": [],
                                         "call_ids": [], "spill_refs": []})
        if record.status != COMPLETED:
            payload["detail"] = record.detail
        return payload

    def _announce(self, record):
        with self._lock:
            if record.announced == record.status or record.status == RUNNING:
                return
            record.announced = record.status
            self._pushes.append(self.push_payload(record))
            stream, text = self.describe(record)
        self.post(stream, text)
        self._notify(OBSERVED.get(record.status, "worker_failed"), record, text=text)

    def _progress(self, record, line):
        self.post("err", f"[{record.id}] {line}")
        self._notify("worker_progress", record, line=line)

    def _notify(self, kind, record, **data):
        observer = self.observer
        if observer is not None:
            try:
                observer(kind, record, **data)
            except Exception:  # noqa: BLE001 -- a display hook must never break a worker.
                pass

    def describe(self, record):
        """Human-readable push for the terminal: summary fields only, never tool bodies."""
        if record.status == COMPLETED:
            report = record.report
            lines = [f"[{record.id} 已完成] {preview(report.get('summary') or '(没有汇报)', 600)}"]
            if report.get("citations"):
                lines.append("  引用: " + ", ".join(report["citations"][:10]))
            if report.get("paths"):
                lines.append("  路径: " + ", ".join(report["paths"][:10]))
            return "out", "\n".join(lines)
        if record.status == AWAITING:
            return "out", f"[{record.id} 等待审批] {(record.approval or {}).get('name', 'tool')}，下一次提示前会逐条询问。"
        return "err", worker_explanation(record)

    def post(self, stream, text):
        hook = self.on_notice
        if hook is not None and hook(stream, text):
            return
        with self._lock:
            self._display.append((stream, text))

    def take_display(self):
        with self._lock:
            items = list(self._display)
            self._display.clear()
            return items

    def take_pushes(self):
        with self._lock:
            items, self._pushes = self._pushes, []
            return items

    def compose(self, text):
        """Attach pending pushes to the next main-agent message, marked as system-added."""
        pushes = self.take_pushes()
        if not pushes:
            return text
        body = "\n".join(json.dumps(item, ensure_ascii=False) for item in pushes)
        return f"{PUSH_HEADER}\n{body}\n{USER_HEADER}\n{text}"

    # ----- control -----------------------------------------------------------------------
    def get(self, worker_id):
        with self._lock:
            return self.records.get((worker_id or "").strip())

    def message(self, worker_id, text):
        """Queue a requirement for the worker's next step. Returns (ok, message)."""
        with self._lock:
            record = self.records.get(worker_id.strip())
            if record is None:
                return False, f"unknown worker: {worker_id}"
            if record.status not in ACTIVE:
                return False, f"worker {record.id} already finished ({record.status}); start a new worker instead"
            record.put_inbox(text)
            return True, f"queued for {record.id}; it is injected before the worker's next step"

    def message_tool(self, task_id, text):
        ok, message = self.message(task_id, text)
        if ok:
            return ToolResult(data={"task_id": task_id, "queued": True, "note": message})
        return ToolResult.failure(ErrorCode.NOT_FOUND if message.startswith("unknown") else ErrorCode.EXECUTION_FAILED, message)

    def running(self):
        with self._lock:
            return [record for record in self.records.values() if record.thread is not None]

    def cancel(self, worker_id, *, reason="cancelled by the user"):
        """Stop one worker; run_command subprocesses are killed through the cancellation token."""
        with self._lock:
            record = self.records.get(worker_id.strip())
            if record is None:
                return False, f"unknown worker: {worker_id}"
            if record.status in TERMINAL:
                return False, f"worker {record.id} already finished ({record.status})"
            record.cancel_requested = True
            record.cancellation.cancel()
            thread = record.thread
            if thread is None:
                # Parked at an approval: no thread to stop; close its session.
                self._close_parked(record, reason)
        if thread is not None:
            thread.join(self.join_seconds)
            with self._lock:
                if record.status not in TERMINAL:
                    # Still inside one model call that cannot be interrupted.
                    record.status, record.detail = CANCELLED, "cancel_requested"
                    record.finished_at = record.finished_at or time.time()
        self._announce(record)
        return True, f"{record.id} cancelled"

    def _close_parked(self, record, reason):
        try:
            with self.store.exclusive(record.session_id):
                state = self.store.load(record.session_id)
                if state is not None and state.status == RunStatus.AWAITING_APPROVAL:
                    state.status, state.error = RunStatus.CANCELLED, reason
                    state.metadata.pop("approval_request", None)
                    self.store.save(state, event=event("worker_cancelled", worker=record.id))
        except Exception as exc:  # noqa: BLE001 -- the registry still records the cancellation.
            record.error = f"could not update worker session: {exc}"
        if record.id in self._approvals:
            self._approvals.remove(record.id)
        record.status, record.detail, record.approval = CANCELLED, "cancelled", None
        record.finished_at = time.time()

    def cancel_all(self, *, reason="cancelled by the user"):
        with self._lock:
            ids = [record.id for record in self.records.values() if record.status in ACTIVE]
        return [self.cancel(worker_id, reason=reason)[1] for worker_id in ids]

    def wait(self, timeout=None):
        """Block until no worker thread is running. Ctrl+C stops waiting, not the workers."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while any(record.thread is not None for record in self.records.values()):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._changed.wait(0.2 if remaining is None else min(0.2, remaining))
        return True

    # ----- approvals ---------------------------------------------------------------------
    def next_approval(self):
        with self._lock:
            while self._approvals:
                record = self.records.get(self._approvals[0])
                if record is not None and record.status == AWAITING and record.approval:
                    return record
                self._approvals.popleft()
            return None

    def decide(self, record, decision):
        """Record the decision for the displayed call only, then resume that worker in the background."""
        approval = record.approval or {}
        self.store.decide_tool(record.session_id, approval["call_id"], decision, approval["request_digest"])
        with self._lock:
            if self._approvals and self._approvals[0] == record.id:
                self._approvals.popleft()
            record.announced = None
        self._start(record, None)

    def skip_approval(self, record):
        """Leave the call pending (for example on EOF); it is asked again after a restart."""
        with self._lock:
            if record.id in self._approvals:
                self._approvals.remove(record.id)

    # ----- persistence -------------------------------------------------------------------
    def snapshot(self):
        with self._lock:
            return {record.id: record.to_dict() for record in self.records.values()}

    def restore(self, state):
        """Load the registry saved in the main session. Nothing runs after a restart except parked approvals."""
        if state is None:
            return
        with self._lock:
            self.session_id = state.session_id
            for value in (state.metadata.get("workers") or {}).values():
                record = WorkerRecord.from_dict(value)
                record.announced = record.status
                if record.status == RUNNING:
                    record.status, record.detail = CANCELLED, "process_exited"
                    record.finished_at = record.finished_at or time.time()
                elif record.status == AWAITING:
                    worker = self.store.load(record.session_id)
                    request = worker.metadata.get("approval_request") if worker is not None else None
                    if worker is not None and worker.status == RunStatus.AWAITING_APPROVAL and request:
                        record.approval = {"call_id": request["call_id"], "request_digest": request["request_digest"],
                                           "name": request.get("name", "tool")}
                        self._approvals.append(record.id)
                    else:
                        record.status = worker_state(worker.status) if worker is not None else FAILED
                self.records[record.id] = record
                self._next = max(self._next, int(record.id[1:]) + 1 if record.id[1:].isdigit() else self._next)

    def persist(self):
        """Write the registry into the main session. Called by the chat thread between main-agent turns."""
        with self._lock:
            if not self.session_id or not self.records:
                return
            snapshot = self.snapshot()
        with self.store.exclusive(self.session_id):
            state = self.store.load(self.session_id)
            if state is None or state.metadata.get("workers") == snapshot:
                return
            state.metadata["workers"] = snapshot
            self.store.save(state)

    def live_usage(self, record):
        """Current counters from the worker session (the thread checkpoints after every step)."""
        if record.status != RUNNING:
            return record.usage
        try:
            state = self.store.load(record.session_id)
        except Exception:  # noqa: BLE001 -- status display must not fail.
            state = None
        if state is None:
            return record.usage
        return {**record.usage, "model_calls": state.model_calls, "steps": state.step, "tool_calls": state.tool_calls}


# WorkerManager status -> chat event kind (baseagent.chat.events).
OBSERVED = {COMPLETED: "worker_done", FAILED: "worker_failed", CANCELLED: "worker_cancelled",
            AWAITING: "approval_requested"}

WORKER_LABELS = {RUNNING: "运行中", AWAITING: "等待审批", COMPLETED: "已完成", CANCELLED: "已取消"}

WORKER_REASONS = {
    "max_steps_exceeded": "用完了自己的步数上限",
    "max_model_calls_exceeded": "用完了自己的模型调用上限",
    "max_tool_calls_exceeded": "用完了自己的工具调用上限",
    "tool_loop_detected": "连续重复同样的工具调用，被循环保护停下",
    "context_limit_exceeded": "上下文超出上限",
    "failed": "模型或工具出错",
    "needs_recovery": "有结果不确定的工具调用，需要先核对",
    "usage_unavailable": "模型用量未知，无法核对 token 预算",
    "max_tokens_exceeded": "token 预算用完",
    "deadline_exceeded": "超过时长上限",
    "workspace_changed": "工作区文件在执行期间被改动",
    "shared_max_model_calls": "这一轮的共享模型调用额度（主 Agent + 所有 worker 共用 --max-model-calls）用完",
    "shared_max_steps": "这一轮的共享步数额度（主 Agent + 所有 worker 共用 --max-steps）用完",
    "shared_budget_cny": "这一轮的共享 --budget-cny 额度用完（或模型没有报告用量，无法核对花费）",
}


def worker_label(record):
    if record.status == FAILED:
        return f"失败（{record.detail or 'failed'}）"
    return WORKER_LABELS.get(record.status, record.status)


def worker_explanation(record):
    """Explain in Chinese which worker stopped, roughly why, and whether chat can retry."""
    if record.status == CANCELLED:
        how = "进程退出时它仍在运行" if record.detail == "process_exited" else "被取消"
        return (f"执行子 Agent（worker {record.id}）没有完成：{how}。已完成的工具调用留在它的会话 {record.session_id} 里；"
                "需要时可以在对话里让主 Agent 重新派一个。")
    reason = WORKER_REASONS.get(record.detail or "failed", f"停在 {record.detail}")
    lines = [f"执行子 Agent（worker {record.id}）没有完成：{reason}。"]
    if record.error:
        lines.append(f"  详情：{preview(record.error, 300)}")
    lines.append("  这个子 Agent 已结束，主 Agent 会在你的下一条消息里收到失败结果。可以在对话里重试："
                 "把任务说得更具体或拆小，让主 Agent 重新派一个。")
    if (record.detail or "").startswith("shared_"):
        lines.append("  共享额度按主 Agent 的一轮计算；下一条消息会开新的一轮，也可以退出后用更大的 "
                     "--max-steps / --max-model-calls / --budget-cny 和同一个 --session 重新进入对话。")
    return "\n".join(lines)


def preview(value, limit):
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."
