"""Minimal interactive terminal frontend for the coding CLI.

This module contains no agent logic. Each line is handed to the existing
``run_agent`` composition; approvals reuse ``SessionStore.decide_tool`` and
the same ToolPolicy/workspace capability rules as the one-shot CLI.

In main-agent chat, workers run in the background (``WorkerManager``). Their
pushes and trace lines are printed only between prompts: queued while a line
is being read, printed before the next prompt. On a real terminal a push may
also print above an *empty* prompt line and redraw the prompt; it never lands
inside text the user is typing.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading

from baseagent.agent.router import is_main_agent_session
from baseagent.agent.state import RunStatus
from baseagent.agent.workers import ACTIVE, preview, worker_label as duplex_worker_label
from baseagent.session import SessionBusy

PROMPT = "> "
EXIT_COMMANDS = frozenset({"/exit", "/quit"})
APPROVAL_PROMPT = "Allow {name}? [y/N] "
WORKER_APPROVAL_PROMPT = "Allow {name} for worker {worker}? [y/N] "
EXIT_PROMPT = "输入 c 取消它们并退出，输入 w 继续等待（留在对话里）[c/w] "


class PromptLine:
    """Read input lines and decide whether a background push may print right now.

    A push prints immediately only on a live terminal while a prompt is shown
    and its line buffer is empty; otherwise it is queued for the next prompt.
    """

    def __init__(self, out, *, live=None, line_buffer=None):
        if line_buffer is None and live is not False:
            line_buffer = _readline_buffer() if live or _interactive(out) else None
        self.out, self.line_buffer = out, line_buffer
        self.live = bool(line_buffer) if live is None else bool(live and line_buffer)
        self._lock = threading.Lock()
        self._prompt = None

    def read(self, prompt):
        with self._lock:
            self._prompt = prompt
        try:
            return input(prompt)
        finally:
            with self._lock:
                self._prompt = None

    def try_print(self, stream, text):
        if not self.live:
            return False
        with self._lock:
            if self._prompt is None or self.line_buffer() != "":
                return False
            self.out.write("\r\x1b[K" + text + "\n" + self._prompt)
            self.out.flush()
            return True


def _interactive(out):
    try:
        return out is sys.stdout and sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _readline_buffer():
    try:
        import readline
    except ImportError:
        return None
    return readline.get_line_buffer


def run_chat(*, run_turn, store, session_id=None, workspace, out=None, err=None, main_agent=False, workers=None,
             live=None) -> int:
    """Read lines until EOF or an exit command; one line starts one turn."""
    out = out or sys.stdout
    err = err or sys.stderr
    print(f"Interactive chat. Workspace: {workspace.root}", file=out)
    if workers is not None:
        print(f"Main agent: answers directly and runs workspace work in background workers "
              f"(up to {workers.max_workers} at once, one writer at a time); results are pushed here.", file=out)
        print("Type a task, /status, /wait, /cancel <id|all>, /tell <id> <text>, /exit or /quit. "
              "Ctrl+C cancels the current turn.", file=out)
    else:
        if main_agent:
            print("Main agent: answers directly and hands workspace work to one worker at a time.", file=out)
        print("Type a task, /status, /exit or /quit. Ctrl+C cancels the current turn.", file=out)
    state = None
    if session_id:
        try:
            state = store.load(session_id)
        except (OSError, sqlite3.Error):
            state = None
    line = PromptLine(out, live=live)
    if workers is not None:
        workers.restore(state)
        workers.on_notice = line.try_print
    if state is not None and state.status == RunStatus.AWAITING_APPROVAL:
        state = _handle_approvals(run_turn, store, state, out, err)
        if state is not None:
            _report(state, out, err, store)
    compose = workers.compose if workers is not None else None
    try:
        while True:
            if workers is not None:
                _deliver(workers, store, line, out, err)
            try:
                text = line.read(PROMPT).strip()
            except EOFError:
                print(file=out)
                if workers is not None:
                    finish_workers(workers, out, err, line=line)
                break
            except KeyboardInterrupt:
                print(file=out)
                if workers is not None and workers.running():
                    _cancel_all(workers, out, "Ctrl+C")
                break
            if not text:
                continue
            if text in EXIT_COMMANDS:
                if workers is not None and workers.running() and not _confirm_exit(workers, line, out):
                    continue
                break
            if text == "/status":
                _print_status(store, state, session_id, workspace, out, workers)
                continue
            if workers is not None and _worker_command(workers, text, out, err):
                continue
            state = _run_turn(run_turn, store, state, text, out, err, compose=compose)
            if workers is not None:
                _persist(workers, err)
    finally:
        if workers is not None:
            workers.on_notice = None
            _flush(workers, out, err)
            _persist(workers, err)
    return 0


def finish_workers(workers, out, err, *, line=None):
    """No more input: wait for running workers, print their pushes, keep parked approvals for later."""
    if workers.running():
        print("输入已结束：等待运行中的 worker 完成后退出（Ctrl+C 取消它们）。", file=err)
        try:
            workers.wait()
        except KeyboardInterrupt:
            print(file=out)
            _cancel_all(workers, out, "Ctrl+C")
    _flush(workers, out, err)
    parked = [record.id for record in workers.records.values() if record.status == "awaiting_approval"]
    if parked:
        print(f"{', '.join(parked)} 仍在等待审批；用同一 --session 再进入对话会逐条询问。", file=err)
    _persist(workers, err)


def _flush(workers, out, err):
    for stream, text in workers.take_display():
        print(text, file=out if stream == "out" else err)


def _persist(workers, err):
    try:
        workers.persist()
    except (ValueError, SessionBusy, OSError, sqlite3.Error) as exc:
        print(f"Could not save worker status: {exc}", file=err)


def _deliver(workers, store, line, out, err):
    """Between prompts: print queued pushes, then ask pending approvals one by one."""
    _flush(workers, out, err)
    while (record := workers.next_approval()) is not None:
        if not _ask_worker_approval(workers, store, record, line, out, err):
            break
        _flush(workers, out, err)
    _persist(workers, err)


def _ask_worker_approval(workers, store, record, line, out, err):
    """Ask allow/deny for one displayed worker call; the answer applies to that call only."""
    approval = record.approval
    name = approval.get("name", "tool")
    try:
        worker_state = store.load(record.session_id)
    except (OSError, sqlite3.Error):
        worker_state = None
    print(f"\nApproval required: {name}", file=out)
    print(f"  requested by: worker {record.id} (session {record.session_id})", file=out)
    print(f"  task: {preview(record.task, 120)}", file=out)
    arguments = _arguments_preview(store, worker_state, approval["call_id"]) if worker_state is not None else ""
    if arguments:
        print(f"  arguments: {arguments}", file=out)
    print(f"  call: {approval['call_id']}", file=out)
    waiting = sum(1 for other in workers.records.values() if other.status == "awaiting_approval") - 1
    if waiting > 0:
        print(f"  ({waiting} more pending; they are asked one at a time after this one)", file=out)
    try:
        answer = line.read(WORKER_APPROVAL_PROMPT.format(name=name, worker=record.id))
    except EOFError:
        print(file=out)
        workers.skip_approval(record)
        print(f"输入已结束：{record.id} 的这条审批保持待定，下次用同一 --session 进入对话会再问。", file=err)
        return False
    except KeyboardInterrupt:
        print(file=out)
        print("Approval prompt interrupted; denying the tool call.", file=err)
        answer = ""
    decision = "allow" if answer.strip().lower() in {"y", "yes", "allow"} else "deny"
    try:
        workers.decide(record, decision)
    except (ValueError, SessionBusy, OSError, sqlite3.Error) as exc:
        workers.skip_approval(record)
        print(f"Could not record the approval decision for {record.id}: {exc}", file=err)
        return True
    print(f"Tool call {'allowed' if decision == 'allow' else 'denied'}. Worker {record.id} continues in the background.",
          file=out)
    return True


def _confirm_exit(workers, line, out):
    running = workers.running()
    print(f"还有 {len(running)} 个 worker 在运行（{', '.join(record.id for record in running)}）。", file=out)
    try:
        answer = line.read(EXIT_PROMPT)
    except (EOFError, KeyboardInterrupt):
        print(file=out)
        answer = "c"
    if answer.strip().lower() in {"c", "cancel", "y", "yes", "取消"}:
        _cancel_all(workers, out, "exit")
        return True
    print("继续等待：worker 完成后会自动推送；/wait 可以阻塞等待，/status 查看进度。", file=out)
    return False


def _cancel_all(workers, out, why):
    for message in workers.cancel_all(reason=f"cancelled at {why}"):
        print(message, file=out)


def _worker_command(workers, text, out, err):
    """/wait, /cancel <id|all>, /tell <id> <text>. Returns False for ordinary chat lines."""
    command, _, rest = text.partition(" ")
    rest = rest.strip()
    if command == "/wait":
        if not workers.running():
            print("No worker is running.", file=out)
            return True
        print("Waiting for running workers (Ctrl+C stops waiting; workers keep running).", file=out)
        try:
            workers.wait()
        except KeyboardInterrupt:
            print(file=out)
            print("Stopped waiting; workers keep running.", file=out)
        return True
    if command == "/cancel":
        if not rest:
            print("Usage: /cancel <id> or /cancel all", file=err)
        elif rest == "all":
            messages = workers.cancel_all()
            print("\n".join(messages) if messages else "No active worker.", file=out)
        else:
            ok, message = workers.cancel(rest)
            print(message, file=out if ok else err)
        return True
    if command == "/tell":
        worker_id, _, note = rest.partition(" ")
        if not worker_id or not note.strip():
            print("Usage: /tell <id> <text>", file=err)
            return True
        ok, message = workers.message(worker_id, note.strip())
        print(message, file=out if ok else err)
        return True
    return False


def _run_turn(run_turn, store, state, text, out, err, *, compose=None):
    # The kernel only appends a prompt to a completed turn. If the previous
    # turn was interrupted/failed, resume it first so the conversation remains
    # usable after Ctrl+C instead of exiting the process.
    if state is not None and state.status != RunStatus.COMPLETED:
        resumed = _invoke(run_turn, None, out, err)
        if resumed is None:
            return state
        state = resumed
        if state.status == RunStatus.AWAITING_APPROVAL:
            state = _handle_approvals(run_turn, store, state, out, err)
            if state is None:
                return None
        _report(state, out, err, store)
        if state.status != RunStatus.COMPLETED:
            print("The previous turn is still unfinished; your new message was not sent. Inspect or "
                  f"resume it with --session {state.session_id or '<id>'} --resume.", file=err)
            return state
    state = _invoke(run_turn, compose(text) if compose else text, out, err)
    if state is None:
        return None
    state = _handle_approvals(run_turn, store, state, out, err)
    if state is not None:
        _report(state, out, err, store)
    return state


def _invoke(run_turn, user_input, out, err):
    try:
        return run_turn(user_input)
    except KeyboardInterrupt:
        print(file=out)
        print("Turn cancelled. Type another message to continue.", file=err)
        return None
    except (ValueError, SessionBusy, OSError, sqlite3.Error) as exc:
        print(f"Session error: {exc}", file=err)
        return None


def _handle_approvals(run_turn, store, state, out, err):
    """Ask allow/deny for the exact pending call, then resume in-process."""
    while state is not None and state.status == RunStatus.AWAITING_APPROVAL:
        approval = state.metadata.get("approval_request") or {}
        # A worker's pending call lives in its node; the root only records the pause.
        node = None if approval else _pending_worker(store, state)
        if node is not None:
            approval = node["state"]["metadata"]["approval_request"]
        call_id, digest = approval.get("call_id"), approval.get("request_digest")
        name = approval.get("name", "tool")
        if not call_id or not digest:
            print("Approval state is missing its exact request; inspect the session before continuing.", file=err)
            return state
        print(f"\nApproval required: {name}", file=out)
        if node is not None:
            print(f"  requested by: worker {node['task_id'][:8]}", file=out)
            arguments = _node_arguments_preview(node, call_id)
        else:
            arguments = _arguments_preview(store, state, call_id)
        if arguments:
            print(f"  arguments: {arguments}", file=out)
        print(f"  call: {call_id}", file=out)
        eof = False
        try:
            answer = input(APPROVAL_PROMPT.format(name=name))
        except EOFError:
            print(file=out)
            answer, eof = "", True
        except KeyboardInterrupt:
            print(file=out)
            print("Approval prompt interrupted; denying the tool call.", file=err)
            answer = ""
        decision = "allow" if answer.strip().lower() in {"y", "yes", "allow"} else "deny"
        try:
            if node is not None:
                store.decide_task_tool(state.session_id, node["task_id"], call_id, decision, digest)
            else:
                store.decide_tool(state.session_id, call_id, decision, digest)
        except (ValueError, SessionBusy, OSError, sqlite3.Error) as exc:
            print(f"Could not record the approval decision: {exc}", file=err)
            return state
        print(f"Tool call {'allowed' if decision == 'allow' else 'denied'}.", file=out)
        if eof:
            return state
        state = _invoke(run_turn, None, out, err)
    return state


def _arguments_preview(store, state, call_id, limit=300):
    try:
        request = json.loads(store.call(state, call_id)["request_json"])
        arguments = request["function"]["arguments"]
    except (ValueError, KeyError, TypeError, sqlite3.Error):
        return ""
    return _preview(arguments, limit)


def _node_arguments_preview(node, call_id, limit=300):
    for record in node.get("tool_calls", ()):
        if record.get("call_id") == call_id:
            try:
                return _preview(json.loads(record["request_json"])["function"]["arguments"], limit)
            except (ValueError, KeyError, TypeError):
                return ""
    return ""


def _worker_nodes(store, state):
    if store is None or not state.session_id or not state.turn_id:
        return []
    try:
        return store.task_tree(state.session_id, state.turn_id)["nodes"]
    except (ValueError, OSError, sqlite3.Error):
        return []


def _pending_worker(store, state):
    for node in reversed(_worker_nodes(store, state)):
        metadata = node["state"]["metadata"]
        if node["state"]["status"] == RunStatus.AWAITING_APPROVAL and metadata.get("approval_request"):
            return node
    return None


def _preview(value, limit):
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _report(state, out, err, store=None):
    if state.final_answer:
        print(state.final_answer, file=out)
    if state.status == RunStatus.AWAITING_APPROVAL:
        return
    nodes = _worker_nodes(store, state)
    if state.status == RunStatus.COMPLETED:
        for node in nodes:
            if node["state"]["metadata"].get("closed_by_parent"):
                print(worker_explanation(node, paused=False), file=err)
        return
    active = state.metadata.get("active_subtask")
    node = next((node for node in nodes if node["task_id"] == active), None)
    if node is not None and node["state"]["status"] != RunStatus.COMPLETED:
        print(worker_explanation(node, paused=True, session_id=state.session_id), file=err)
        return
    if state.status == RunStatus.INTERRUPTED:
        print("Turn cancelled. Type another message to continue.", file=err)
        return
    print(stop_explanation(state, interactive=True), file=err)
    if state.blocked_tool_calls:
        print("Unresolved tool calls: " + ", ".join(state.blocked_tool_calls)
              + ". Inspect with --inspect-session and resolve them before resuming.", file=err)


WORKER_STATUS = {
    RunStatus.RUNNING: "运行中",
    RunStatus.AWAITING_APPROVAL: "等待审批",
    RunStatus.COMPLETED: "已完成",
}

WORKER_REASONS = {
    RunStatus.MAX_STEPS_EXCEEDED: "用完了自己的步数上限",
    RunStatus.MAX_MODEL_CALLS_EXCEEDED: "用完了自己的模型调用上限",
    RunStatus.MAX_TOOL_CALLS_EXCEEDED: "用完了自己的工具调用上限",
    RunStatus.TOOL_LOOP_DETECTED: "连续重复同样的工具调用，被循环保护停下",
    RunStatus.CONTEXT_LIMIT_EXCEEDED: "上下文超出上限",
    RunStatus.FAILED: "模型或工具出错",
    RunStatus.CANCELLED: "被取消",
    RunStatus.INTERRUPTED: "被中断（Ctrl+C）",
    RunStatus.NEEDS_RECOVERY: "有结果不确定的工具调用，需要先核对",
    RunStatus.USAGE_UNAVAILABLE: "模型用量未知，无法核对 token 预算",
    RunStatus.MAX_TOKENS_EXCEEDED: "token 预算用完",
    RunStatus.DEADLINE_EXCEEDED: "超过本轮时长上限",
    RunStatus.WORKSPACE_CHANGED: "工作区文件在执行期间被改动",
}


def worker_label(status):
    status = RunStatus(status)
    return WORKER_STATUS.get(status, f"失败（{status.value}）")


def worker_explanation(node, *, paused, session_id=None):
    """Explain in Chinese which worker stopped, roughly why, and whether chat can retry."""
    child = node["state"]
    status = RunStatus(child["status"])
    reason = WORKER_REASONS.get(status, f"停在 {status.value}")
    detail = (child.get("error") or "").strip()
    lines = [f"执行子 Agent（worker {node['task_id'][:8]}）没有完成：{reason}。"]
    if detail:
        lines.append(f"  详情：{_preview(detail, 300)}")
    if not paused:
        lines.append("  这个子 Agent 已结束，主 Agent 已收到失败结果。可以在对话里把任务说得更具体或拆小后再试。")
    elif status in (RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED):
        lines.append("  可以在对话里重试：发送下一条消息时会先继续这个子 Agent，已完成的工具调用不会重做。")
    else:
        lines.append("  暂时不能在对话里直接重试：需要先处理这个子节点。用 /exit 退出后运行 "
                     f"baseagent --session {session_id or '<id>'} --task-tree 查看详情。")
    return "\n".join(lines)


# Turn limits: status -> (CLI option, used counter, limit field).
LIMITS = {
    RunStatus.MAX_STEPS_EXCEEDED: ("max-steps", "step", "max_steps"),
    RunStatus.MAX_MODEL_CALLS_EXCEEDED: ("max-model-calls", "model_calls", "max_model_calls"),
    RunStatus.MAX_TOOL_CALLS_EXCEEDED: ("max-tool-calls", "tool_calls", "max_tool_calls"),
}


def stop_explanation(state, *, interactive):
    """Explain why an unfinished turn stopped and how to continue it."""
    status = RunStatus(state.status)
    if status in LIMITS:
        return _limit_explanation(state, status, interactive)
    shared = state.metadata.get("shared_budget_exhausted")
    if shared and status == RunStatus.MAX_TOKENS_EXCEEDED:
        return (f"Shared --budget-cny exhausted (main agent + workers): CNY {shared.get('cost_cny')}/{shared.get('budget_cny')}, "
                "or a model call reported no usage so the spend cannot be checked. Send another message to resume this "
                "turn with a fresh turn budget, or exit and re-enter with a larger --budget-cny and the same --session.")
    detail = state.error or _usage_detail(state)
    text = _explain(status, detail)
    if status == RunStatus.USAGE_UNAVAILABLE:
        text += ("\nAutomatic model retries (--model-retries) stop here: a failed attempt (for example a "
                 "transport timeout) reported no usage, so the token budget cannot be checked before another "
                 "billed request. Verify provider usage, record it with --resolve-usage, then --resume.")
    return text


def _usage_detail(state):
    """Used/limit counters and how to continue, for stops that recorded no error text."""
    sid = state.session_id or "<id>"
    return (f"used steps {state.step}/{state.max_steps}, model calls {state.model_calls}/{state.max_model_calls}, "
            f"tool calls {state.tool_calls}/{state.max_tool_calls}. Send another message to resume this turn in chat, "
            f"or exit and run: baseagent --session {sid} --resume (raise --max-steps / --max-model-calls / "
            "--max-tool-calls above the used values if one of them is the reason).")


def _limit_explanation(state, status, interactive):
    option, used_field, limit_field = LIMITS[status]
    used, limit = getattr(state, used_field), getattr(state, limit_field)
    sid = state.session_id or "<id>"
    if status == RunStatus.MAX_TOOL_CALLS_EXCEEDED:
        raise_options = f"--max-tool-calls {max(used, limit) * 2}"
    else:
        # Steps and model calls advance together; raise both so the other
        # limit does not stop the resumed turn immediately.
        raise_options = (f"--max-steps {max(state.step, state.max_steps) * 2} "
                         f"--max-model-calls {max(state.model_calls, state.max_model_calls) * 2}")
    lines = [
        f"{_explain(status, '')}{option} {used}/{limit} used "
        f"(steps {state.step}/{state.max_steps}, model calls {state.model_calls}/{state.max_model_calls}, "
        f"tool calls {state.tool_calls}/{state.max_tool_calls}).",
        "The turn is unfinished. --resume continues this same turn and keeps the used counts, so raise "
        f"the limit above the used value: baseagent --session {sid} --resume {raise_options}",
    ]
    shared = state.metadata.get("shared_budget_exhausted")
    if shared:
        lines.insert(1, f"The shared turn budget ran out first (main agent + workers): model calls "
                        f"{shared['model_calls']}/{shared['max_model_calls']}, steps {shared['steps']}/{shared['max_steps']}"
                        + (f", CNY {shared['cost_cny']}/{shared['budget_cny']}" if "budget_cny" in shared else "") + ".")
    if interactive:
        lines.append("Sending another message here resumes the same turn with the same limits and stops "
                     "again; exit (/exit) and run the command above. /status shows the session ID and usage.")
    return "\n".join(lines)


def _explain(status, detail):
    prefixes = {
        RunStatus.FAILED: "Model or tool error: ",
        RunStatus.CANCELLED: "Turn cancelled: ",
        RunStatus.MAX_STEPS_EXCEEDED: "Step limit reached: ",
        RunStatus.MAX_MODEL_CALLS_EXCEEDED: "Model-call limit reached: ",
        RunStatus.MAX_TOOL_CALLS_EXCEEDED: "Tool-call limit reached: ",
        RunStatus.MAX_TOKENS_EXCEEDED: "Token budget exhausted: ",
        RunStatus.DEADLINE_EXCEEDED: "Turn deadline exceeded: ",
        RunStatus.USAGE_UNAVAILABLE: "Model usage is unknown, so the token budget cannot be verified: ",
        RunStatus.RESERVATION_UNAVAILABLE: "Model request reservation is unavailable: ",
        RunStatus.REQUEST_BOUND_VIOLATED: "Model request bound violated: ",
        RunStatus.CONTEXT_LIMIT_EXCEEDED: "Context limit exceeded: ",
        RunStatus.TOOL_LOOP_DETECTED: "Repeated tool loop stopped: ",
        RunStatus.WORKSPACE_CHANGED: "Workspace changed: ",
        RunStatus.NEEDS_RECOVERY: "Unresolved tool execution requires recovery: ",
        RunStatus.ABORTED_BY_WATCHDOG: "Watchdog stopped the turn: ",
    }
    return prefixes.get(status, f"Turn stopped ({status.value}): ") + detail


def _print_status(store, state, session_id, workspace, out, workers=None):
    sid = state.session_id if state is not None else session_id
    if not sid:
        print(f"Session: not started\nWorkspace: {workspace.root}", file=out)
        return
    try:
        current = store.load(sid)
    except (OSError, sqlite3.Error) as exc:
        print(f"Session: {sid}\nCould not read session: {exc}", file=out)
        return
    if current is None:
        print(f"Session: {sid} (not saved yet)\nWorkspace: {workspace.root}", file=out)
        return
    print(f"Session: {current.session_id}", file=out)
    print(f"Status: {current.status.value}", file=out)
    print(f"Workspace: {current.workspace_root or workspace.root}", file=out)
    print(f"Turn: {current.turn_id or '(none)'}", file=out)
    print(f"Tokens: {current.total_tokens} reported; {current.unknown_usage_calls} unknown usage calls", file=out)
    print(f"Attempts: steps {current.step}/{current.max_steps}, model {current.model_calls}/{current.max_model_calls}, "
          f"tools {current.tool_calls}/{current.max_tool_calls}", file=out)
    if current.max_total_tokens is not None:
        print(f"Token budget: {current.max_total_tokens}", file=out)
    if workers is not None:
        _print_workers(workers, out)
        return
    nodes = _worker_nodes(store, current)
    if nodes:
        node = nodes[-1]
        child = node["state"]
        label = "已结束（失败）" if child["metadata"].get("closed_by_parent") else worker_label(child["status"])
        print(f"Worker: {node['task_id'][:8]} {label}; steps {child['step']}/{child['max_steps']}, "
              f"model {child['model_calls']}/{child['max_model_calls']}, tools {child['tool_calls']}/{child['max_tool_calls']}"
              + (f" ({len(nodes)} workers this turn)" if len(nodes) > 1 else ""), file=out)
    elif is_main_agent_session(current):
        print("Worker: none in this turn", file=out)


def _print_workers(workers, out):
    """AC9: every worker with id, state, elapsed time and model calls used."""
    records = list(workers.records.values())
    if workers.budget is not None:
        budget = workers.budget.snapshot()
        print(f"Turn budget (latest main-agent turn + workers it started): "
              f"model {budget['model_calls']}/{budget['max_model_calls']}, "
              f"steps {budget['steps']}/{budget['max_steps']}"
              + (f", CNY {budget['cost_cny']}/{budget['budget_cny']}" if "budget_cny" in budget else ""), file=out)
    if not records:
        print("Workers: none in this session", file=out)
        return
    active = sum(record.status in ACTIVE for record in records)
    print(f"Workers: {len(records)} ({active} active, at most {workers.max_workers}; one writer at a time)", file=out)
    for record in records:
        usage = workers.live_usage(record)
        print(f"  {record.id} {record.status} {duplex_worker_label(record)}; {record.elapsed():.1f}s; "
              f"model {usage.get('model_calls', 0)}/{usage.get('max_model_calls', '?')}, "
              f"steps {usage.get('steps', 0)}/{usage.get('max_steps', '?')}, "
              f"tools {usage.get('tool_calls', 0)}/{usage.get('max_tool_calls', '?')}; "
              f"{'writes' if record.allow_writes else 'read-only'}; {preview(record.task, 60)}", file=out)
