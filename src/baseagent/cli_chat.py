"""Minimal interactive terminal frontend for the coding CLI.

This module contains no agent logic and no chat rules: turns, approvals, worker
pushes and the /status /wait /cancel /tell /exit commands live in
``baseagent.chat.ChatSession``, which the TUI and Web frontends share. This
module reads lines, prints what the session reports and asks y/N questions.

In main-agent chat, workers run in the background (``WorkerManager``). Their
pushes and trace lines are printed only between prompts: queued while a line
is being read, printed before the next prompt. On a real terminal a push may
also print above an *empty* prompt line and redraw the prompt; it never lands
inside text the user is typing.
"""

from __future__ import annotations

import sqlite3
import sys
import threading

from baseagent.chat.session import ERRORS, ChatSession, exit_question
# Re-exported: stop and worker explanations moved to the shared chat layer unchanged.
from baseagent.chat.text import LIMITS, WORKER_REASONS, WORKER_STATUS, stop_explanation, worker_explanation, worker_label  # noqa: F401
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


def run_chat(*, run_turn=None, store=None, session_id=None, workspace=None, out=None, err=None, main_agent=False,
             workers=None, live=None, session=None) -> int:
    """Read lines until EOF or an exit command; one line starts one turn."""
    out = out or sys.stdout
    err = err or sys.stderr
    if session is None:
        session = ChatSession(run_turn=run_turn, store=store, workspace=workspace, session_id=session_id,
                              workers=workers, main_agent=main_agent)
    session.printer = lambda stream, text: print(text, file=out if stream == "out" else err)
    session.status_events = False
    workers, workspace = session.workers, session.workspace
    print(f"Interactive chat. Workspace: {workspace.root}", file=out)
    if workers is not None:
        print(f"Main agent: answers directly and runs workspace work in background workers "
              f"(up to {workers.max_workers} at once, one writer at a time); results are pushed here.", file=out)
        print("Type a task, /status, /wait, /cancel <id|all>, /tell <id> <text>, /exit or /quit. "
              "Ctrl+C cancels the current turn.", file=out)
    else:
        if session.main_agent:
            print("Main agent: answers directly and hands workspace work to one worker at a time.", file=out)
        print("Type a task, /status, /exit or /quit. Ctrl+C cancels the current turn.", file=out)
    print("File checkpoints: /checkpoints lists them, /undo or /rewind <N> restores files written by the agent.", file=out)
    session.start()
    line = PromptLine(out, live=live)
    if workers is not None:
        workers.on_notice = line.try_print

    def ask(view):
        return _ask_root_approval(view, out, err)

    session.resolve_pending(ask)
    try:
        while True:
            if workers is not None:
                _deliver(session, line, out, err)
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
                    session.cancel_all("Ctrl+C")
                break
            if not text:
                continue
            if text in EXIT_COMMANDS:
                if workers is not None and workers.running() and not _confirm_exit(session, line, out):
                    continue
                break
            if session.command(text):
                continue
            session.turn(text, ask)
    finally:
        if workers is not None:
            workers.on_notice = None
            _flush(session, out, err)
        session.close()
    return 0


def finish_workers(workers, out, err, *, line=None):
    """No more input: wait for running workers, print their pushes, keep parked approvals for later."""
    if workers.running():
        print("输入已结束：等待运行中的 worker 完成后退出（Ctrl+C 取消它们）。", file=err)
        try:
            workers.wait()
        except KeyboardInterrupt:
            print(file=out)
            for message in workers.cancel_all(reason="cancelled at Ctrl+C"):
                print(message, file=out)
    _flush(workers, out, err)
    parked = [record.id for record in workers.records.values() if record.status == "awaiting_approval"]
    if parked:
        print(f"{', '.join(parked)} 仍在等待审批；用同一 --session 再进入对话会逐条询问。", file=err)
    try:
        workers.persist()
    except (ValueError, SessionBusy, OSError, sqlite3.Error) as exc:
        print(f"Could not save worker status: {exc}", file=err)


def _flush(source, out, err):
    """Print queued pushes and worker trace lines (source: ChatSession or WorkerManager)."""
    for stream, text in source.take_display():
        print(text, file=out if stream == "out" else err)


def _deliver(session, line, out, err):
    """Between prompts: print queued pushes, then ask pending approvals one by one."""
    _flush(session, out, err)
    while (view := session.next_worker_approval()) is not None:
        if not _ask_worker_approval(session, view, line, out, err):
            break
        _flush(session, out, err)
    session.persist()


def _ask_worker_approval(session, view, line, out, err):
    """Ask allow/deny for one displayed worker call; the answer applies to that call only."""
    name, worker = view["name"], view["worker"]
    print(f"\nApproval required: {name}", file=out)
    print(f"  requested by: worker {worker} (session {view['session_id']})", file=out)
    print(f"  task: {view['task']}", file=out)
    if view["arguments"]:
        print(f"  arguments: {view['arguments']}", file=out)
    print(f"  call: {view['call_id']}", file=out)
    if view["pending"] > 0:
        print(f"  ({view['pending']} more pending; they are asked one at a time after this one)", file=out)
    try:
        answer = line.read(WORKER_APPROVAL_PROMPT.format(name=name, worker=worker))
    except EOFError:
        print(file=out)
        session.skip(view)
        print(f"输入已结束：{worker} 的这条审批保持待定，下次用同一 --session 进入对话会再问。", file=err)
        return False
    except KeyboardInterrupt:
        print(file=out)
        print("Approval prompt interrupted; denying the tool call.", file=err)
        answer = ""
    decision = "allow" if answer.strip().lower() in {"y", "yes", "allow"} else "deny"
    try:
        session.decide(view["call_id"], view["digest"], decision, worker=worker)
    except ERRORS as exc:
        session.skip(view)
        print(f"Could not record the approval decision for {worker}: {exc}", file=err)
    return True


def _ask_root_approval(view, out, err):
    """Show the exact pending main-agent call and read y/N. Returns (decision, stop)."""
    name = view["name"]
    print(f"\nApproval required: {name}", file=out)
    if view["node"] is not None:
        print(f"  requested by: worker {view['node'][:8]}", file=out)
    if view["arguments"]:
        print(f"  arguments: {view['arguments']}", file=out)
    print(f"  call: {view['call_id']}", file=out)
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
    return ("allow" if answer.strip().lower() in {"y", "yes", "allow"} else "deny"), eof


def _confirm_exit(session, line, out):
    print(exit_question(session.running()), file=out)
    try:
        answer = line.read(EXIT_PROMPT)
    except (EOFError, KeyboardInterrupt):
        print(file=out)
        answer = "c"
    if answer.strip().lower() in {"c", "cancel", "y", "yes", "取消"}:
        session.cancel_all("exit")
        return True
    print("继续等待：worker 完成后会自动推送；/wait 可以阻塞等待，/status 查看进度。", file=out)
    return False
