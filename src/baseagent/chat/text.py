"""Chat text shared by every chat frontend: stop explanations, approval previews, /status lines.

Moved verbatim from ``cli_chat`` so the CLI, TUI and Web show the same words for
the same situation. ``cli_chat`` re-exports the public names.
"""

from __future__ import annotations

import json
import sqlite3

from baseagent.agent.router import is_main_agent_session
from baseagent.agent.state import RunStatus
from baseagent.agent.workers import ACTIVE, preview, worker_label as duplex_worker_label


def arguments_preview(store, state, call_id, limit=300):
    try:
        request = json.loads(store.call(state, call_id)["request_json"])
        arguments = request["function"]["arguments"]
    except (ValueError, KeyError, TypeError, sqlite3.Error):
        return ""
    return _preview(arguments, limit)


def node_arguments_preview(node, call_id, limit=300):
    for record in node.get("tool_calls", ()):
        if record.get("call_id") == call_id:
            try:
                return _preview(json.loads(record["request_json"])["function"]["arguments"], limit)
            except (ValueError, KeyError, TypeError):
                return ""
    return ""


def worker_nodes(store, state):
    if store is None or not state.session_id or not state.turn_id:
        return []
    try:
        return store.task_tree(state.session_id, state.turn_id)["nodes"]
    except (ValueError, OSError, sqlite3.Error):
        return []


def pending_worker(store, state):
    for node in reversed(worker_nodes(store, state)):
        metadata = node["state"]["metadata"]
        if node["state"]["status"] == RunStatus.AWAITING_APPROVAL and metadata.get("approval_request"):
            return node
    return None


def _preview(value, limit):
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


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



def status_lines(store, state, session_id, workspace, workers=None):
    """The /status block, one string per printed line."""
    out = []
    sid = state.session_id if state is not None else session_id
    if not sid:
        out.append(f"Session: not started\nWorkspace: {workspace.root}")
        return out
    try:
        current = store.load(sid)
    except (OSError, sqlite3.Error) as exc:
        out.append(f"Session: {sid}\nCould not read session: {exc}")
        return out
    if current is None:
        out.append(f"Session: {sid} (not saved yet)\nWorkspace: {workspace.root}")
        return out
    out.append(f"Session: {current.session_id}")
    out.append(f"Status: {current.status.value}")
    out.append(f"Workspace: {current.workspace_root or workspace.root}")
    out.append(f"Turn: {current.turn_id or '(none)'}")
    out.append(f"Tokens: {current.total_tokens} reported; {current.unknown_usage_calls} unknown usage calls")
    out.append(f"Attempts: steps {current.step}/{current.max_steps}, model {current.model_calls}/{current.max_model_calls}, "
               f"tools {current.tool_calls}/{current.max_tool_calls}")
    if current.max_total_tokens is not None:
        out.append(f"Token budget: {current.max_total_tokens}")
    if workers is not None:
        out.extend(worker_lines(workers))
        return out
    nodes = worker_nodes(store, current)
    if nodes:
        node = nodes[-1]
        child = node["state"]
        label = "已结束（失败）" if child["metadata"].get("closed_by_parent") else worker_label(child["status"])
        out.append(f"Worker: {node['task_id'][:8]} {label}; steps {child['step']}/{child['max_steps']}, "
                   f"model {child['model_calls']}/{child['max_model_calls']}, tools {child['tool_calls']}/{child['max_tool_calls']}"
                   + (f" ({len(nodes)} workers this turn)" if len(nodes) > 1 else ""))
    elif is_main_agent_session(current):
        out.append("Worker: none in this turn")
    return out


def worker_lines(workers):
    """AC9: every worker with id, state, elapsed time and model calls used."""
    out = []
    with workers._lock:
        records = list(workers.records.values())
    if workers.budget is not None:
        budget = workers.budget.snapshot()
        out.append(f"Turn budget (latest main-agent turn + workers it started): "
                   f"model {budget['model_calls']}/{budget['max_model_calls']}, "
                   f"steps {budget['steps']}/{budget['max_steps']}"
                   + (f", CNY {budget['cost_cny']}/{budget['budget_cny']}" if "budget_cny" in budget else ""))
    if not records:
        out.append("Workers: none in this session")
        return out
    active = sum(record.status in ACTIVE for record in records)
    out.append(f"Workers: {len(records)} ({active} active, at most {workers.max_workers}; one writer at a time)")
    for record in records:
        usage = workers.live_usage(record)
        out.append(f"  {record.id} {record.status} {duplex_worker_label(record)}; {record.elapsed():.1f}s; "
                   f"model {usage.get('model_calls', 0)}/{usage.get('max_model_calls', '?')}, "
                   f"steps {usage.get('steps', 0)}/{usage.get('max_steps', '?')}, "
                   f"tools {usage.get('tool_calls', 0)}/{usage.get('max_tool_calls', '?')}; "
                   f"{'writes' if record.allow_writes else 'read-only'}; {preview(record.task, 60)}")
    return out
