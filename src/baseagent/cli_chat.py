"""Minimal interactive terminal frontend for the coding CLI.

This module contains no agent logic. Each line is handed to the existing
``run_agent`` composition; approvals reuse ``SessionStore.decide_tool`` and
the same ToolPolicy/workspace capability rules as the one-shot CLI.
"""

from __future__ import annotations

import json
import sqlite3
import sys

from baseagent.agent.state import RunStatus
from baseagent.session import SessionBusy

PROMPT = "> "
EXIT_COMMANDS = frozenset({"/exit", "/quit"})
APPROVAL_PROMPT = "Allow {name}? [y/N] "


def run_chat(*, run_turn, store, session_id=None, workspace, out=None, err=None) -> int:
    """Read lines until EOF or an exit command; one line starts one turn."""
    out = out or sys.stdout
    err = err or sys.stderr
    print(f"Interactive chat. Workspace: {workspace.root}", file=out)
    print("Type a task, /status, /exit or /quit. Ctrl+C cancels the current turn.", file=out)
    state = None
    if session_id:
        try:
            state = store.load(session_id)
        except (OSError, sqlite3.Error):
            state = None
    if state is not None and state.status == RunStatus.AWAITING_APPROVAL:
        state = _handle_approvals(run_turn, store, state, out, err)
        if state is not None:
            _report(state, out, err)
    while True:
        try:
            line = input(PROMPT)
        except EOFError:
            print(file=out)
            break
        except KeyboardInterrupt:
            print(file=out)
            break
        text = line.strip()
        if not text:
            continue
        if text in EXIT_COMMANDS:
            break
        if text == "/status":
            _print_status(store, state, session_id, workspace, out)
            continue
        state = _run_turn(run_turn, store, state, text, out, err)
    return 0


def _run_turn(run_turn, store, state, text, out, err):
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
        _report(state, out, err)
        if state.status != RunStatus.COMPLETED:
            print("The previous turn is still unfinished; inspect or resume it with "
                  "--session <id> --resume.", file=err)
            return state
    state = _invoke(run_turn, text, out, err)
    if state is None:
        return None
    state = _handle_approvals(run_turn, store, state, out, err)
    if state is not None:
        _report(state, out, err)
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
        call_id, digest = approval.get("call_id"), approval.get("request_digest")
        name = approval.get("name", "tool")
        if not call_id or not digest:
            print("Approval state is missing its exact request; inspect the session before continuing.", file=err)
            return state
        print(f"\nApproval required: {name}", file=out)
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


def _preview(value, limit):
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _report(state, out, err):
    if state.final_answer:
        print(state.final_answer, file=out)
    if state.status == RunStatus.COMPLETED or state.status == RunStatus.AWAITING_APPROVAL:
        return
    if state.status == RunStatus.INTERRUPTED:
        print("Turn cancelled. Type another message to continue.", file=err)
        return
    detail = state.error or "no error detail was recorded"
    print(_explain(state.status, detail), file=err)
    if state.blocked_tool_calls:
        print("Unresolved tool calls: " + ", ".join(state.blocked_tool_calls)
              + ". Inspect with --inspect-session and resolve them before resuming.", file=err)


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


def _print_status(store, state, session_id, workspace, out):
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
    print(f"Attempts: steps {current.step}, model {current.model_calls}, tools {current.tool_calls}", file=out)
    if current.max_total_tokens is not None:
        print(f"Token budget: {current.max_total_tokens}", file=out)
