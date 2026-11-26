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

On a terminal (stdin and stdout are TTYs, or ``concurrent=True``) the main
agent's turn runs in a background thread while this thread keeps reading lines:
a plain line typed meanwhile is steered into the running turn (delivered before
its next model call), ``/queue <text>`` waits for the next turn, and while a
main-agent approval is pending the next typed line answers it (y = allow,
anything else = deny). Piped or scripted input keeps the sequential behaviour:
each line waits for the previous turn. See docs/inbox.md.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque

from baseagent.chat.session import ERRORS, QUEUE_COMMAND, ChatSession, _is_queue, exit_question, is_side_command
# Re-exported: stop and worker explanations moved to the shared chat layer unchanged.
from baseagent.chat.text import LIMITS, WORKER_REASONS, WORKER_STATUS, stop_explanation, worker_explanation, worker_label  # noqa: F401

PROMPT = "> "
EXIT_COMMANDS = frozenset({"/exit", "/quit"})
APPROVAL_PROMPT = "Allow {name}? [y/N] "
WORKER_APPROVAL_PROMPT = "Allow {name} for worker {worker}? [y/N] "
EXIT_PROMPT = "输入 c 取消它们并退出，输入 w 继续等待（留在对话里）[c/w] "
THREADED_APPROVAL = "（下一行输入 y 允许，n 或直接回车拒绝；输入其它内容 = 拒绝，并把这句话作为补充消息交给主 Agent）"
ALLOW_WORDS = frozenset({"y", "yes", "allow"})
DENY_WORDS = frozenset({"", "n", "no", "deny"})
DENIED_WITH_LINE = "已拒绝这次调用；你输入的这句话会作为补充消息交给主 Agent。"
EOF_ANSWER, EXIT_ANSWER = object(), object()


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

    def emit(self, file, text):
        """Print a line from another thread without splitting the prompt: on a live terminal the prompt and
        what is typed so far are redrawn under it; elsewhere it is an ordinary print."""
        with self._lock:
            if self.live and self._prompt is not None and file is self.out:
                self.out.write("\r\x1b[K" + text + "\n" + self._prompt + (self.line_buffer() or ""))
                self.out.flush()
                return
        print(text, file=file)

    def try_print(self, stream, text):
        if not self.live:
            return False
        with self._lock:
            if self._prompt is None or self.line_buffer() != "":
                return False
            self.out.write("\r\x1b[K" + text + "\n" + self._prompt)
            self.out.flush()
            return True


class ApprovalLines:
    """Main-agent approvals asked by the turn thread and answered by the next line the main thread reads.

    Questions are answered in the order they were shown. ``close`` (EOF or /exit while a turn runs)
    answers every waiting and later question at once: EOF denies as before, /exit leaves the call pending.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._waiting = deque()
        self.closed = None

    def pending(self):
        with self._lock:
            return bool(self._waiting)

    def ask(self):
        slot = {"event": threading.Event(), "answer": None}
        with self._lock:
            if self.closed is not None:
                return self.closed
            self._waiting.append(slot)
        try:
            while not slot["event"].wait(0.1):   # short waits: a Ctrl+C (interrupt) can still reach this thread
                pass
        finally:
            with self._lock:
                if slot in self._waiting:
                    self._waiting.remove(slot)
        return slot["answer"]

    def answer(self, text):
        with self._lock:
            if not self._waiting:
                return False
            slot = self._waiting.popleft()
        slot["answer"] = text
        slot["event"].set()
        return True

    def close(self, answer):
        with self._lock:
            self.closed = answer
            waiting, self._waiting = list(self._waiting), deque()
        for slot in waiting:
            slot["answer"] = answer
            slot["event"].set()

    def reopen(self):
        with self._lock:
            self.closed = None


SIDE_PREFIX = "[侧聊] "
SIDE_HELP = ("Side chat: /side <question> asks about the current context without tools (answers are shown here only); "
             "/side-stop, /side-retry, /side-end.")


class SidePrinter:
    """Print side chat events as their own lines, each prefixed ``[侧聊]``, apart from the main agent's output.

    Streamed text is printed line by line as it arrives; the rest when the answer ends or is stopped.
    It only displays: replaying an event never sends anything.
    """

    def __init__(self, write):
        self.write = write
        self.lock = threading.Lock()
        self.pending, self.received = {}, {}

    def __call__(self, event):
        try:
            with self.lock:
                self.show(event.kind, event.data)
        except Exception:  # noqa: BLE001 -- display must never break the side chat
            pass

    def lines(self, attempt, text, final=False):
        buffer = self.pending.get(attempt, "") + text
        *done, rest = buffer.split("\n")
        for line in done:
            self.write(SIDE_PREFIX + line)
        if final:
            if rest:
                self.write(SIDE_PREFIX + rest)
            rest = ""
        self.pending[attempt] = rest

    def show(self, kind, data):
        attempt = data.get("attempt")
        if kind == "side_opened":
            origin = "主任务最近一次真实请求" if data["origin"] == "live" else "已保存的会话（文本化）"
            self.write(f"{SIDE_PREFIX}已打开：快照取自主会话第 {data['turn']} 轮，{origin}；之后主任务的进展不会同步进来。")
            for note in data.get("notes") or ():
                self.write(f"{SIDE_PREFIX}· {note}")
        elif kind == "side_delta":
            self.received[attempt] = self.received.get(attempt, 0) + len(data["text"])
            self.lines(attempt, data["text"])
        elif kind == "side_answer":
            rest = data["text"][self.received.get(attempt, 0):]
            self.lines(attempt, rest, final=True)
        elif kind == "side_stopped":
            self.lines(attempt, "", final=True)
            self.write(f"{SIDE_PREFIX}已停止（主任务、Worker 和后台任务不受影响）。已显示的部分保留；这次请求的用量若之后到达仍会记录。")
        elif kind == "side_failed":
            self.lines(attempt, "", final=True)
            self.write(f"{SIDE_PREFIX}请求失败：{data.get('error')}。/side-retry 重新发送（按新请求计费）。")
        elif kind == "side_usage":
            if data.get("usage") == "unknown":
                self.write(f"{SIDE_PREFIX}这次请求的用量未知（不按 0 计）。")
            elif data.get("late"):
                self.write(f"{SIDE_PREFIX}已停止的那一问的用量后到：输入 {data.get('prompt_tokens')} / 输出 "
                           f"{data.get('completion_tokens')} tokens，已记入。")
        elif kind == "side_notice":
            self.write(SIDE_PREFIX + data["text"])
        elif kind == "side_ended":
            known = data["prompt_tokens"] + data["completion_tokens"]
            line = f"{SIDE_PREFIX}侧聊已结束：{data['attempts']} 次请求，已知用量 {known} tokens"
            if data.get("unknown"):
                line += f"，{data['unknown']} 次用量未知"
            if data.get("pending"):
                line += f"，{data['pending']} 次还在等待用量（到达后照常记录）"
            self.write(line + "。")


def _stop_side(session):
    side = getattr(session, "side", None)
    return side is not None and not side.ended and side.stop()["result"] == "stopped"


def _wait_side(session):
    """Sequential (piped) input: a side question is answered before the next line is read. Ctrl+C stops it."""
    side = getattr(session, "side", None)
    while side is not None and side.active is not None:
        try:
            time.sleep(0.02)
        except KeyboardInterrupt:
            _stop_side(session)


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
             workers=None, live=None, session=None, concurrent=None) -> int:
    """Read lines until EOF or an exit command; one line starts one turn.

    ``concurrent`` (default: stdin and stdout are a terminal) runs turns in a background thread and keeps
    reading lines meanwhile (steer, /queue, approvals); otherwise each line waits for the previous turn.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    if concurrent is None:
        concurrent = _interactive(out)
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
        print("Type a task, /status, /continue [N], /wait, /cancel <id|all>, /tell <id> <text>, /queue <text>, "
              "/exit or /quit. Ctrl+C cancels the current turn.", file=out)
    else:
        if session.main_agent:
            print("Main agent: answers directly and hands workspace work to one worker at a time.", file=out)
        print("Type a task, /status, /continue [N], /queue <text>, /exit or /quit. Ctrl+C cancels the current turn.",
              file=out)
    print("File checkpoints: /checkpoints lists them, /undo or /rewind <N> restores files written by the agent.", file=out)
    print(SIDE_HELP, file=out)
    # Looked up at print time: the threaded CLI replaces session.printer below.
    session.side_events.subscribe(SidePrinter(lambda text: session.printer("out", text)))
    session.start()
    line = PromptLine(out, live=live)
    if workers is not None:
        workers.on_notice = line.try_print

    def ask(view):
        return _ask_root_approval(view, out, err)

    session.resolve_pending(ask)
    if concurrent:
        print("While the agent works you can keep typing: a line is added to the current turn (delivered before "
              "its next model call); /queue <text> sends it as the next turn.", file=out)
        return _run_threaded(session, line, out, err)
    try:
        while True:
            if workers is not None:
                _deliver(session, line, out, err)
            try:
                text = line.read(PROMPT).strip()
            except EOFError:
                print(file=out)
                if workers is not None:
                    session.finish_workers()
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
            if is_side_command(text):
                session.command(text)
                _wait_side(session)
                continue
            if _is_queue(text):
                # Sequential input: nothing runs now, so a queued line is simply the next turn.
                text = text[len(QUEUE_COMMAND):].strip()
                if not text:
                    print("Usage: /queue <text>", file=err)
                    continue
            elif session.command(text, ask=ask):
                continue
            session.turn(text, ask)
    finally:
        if workers is not None:
            workers.on_notice = None
            _flush(session, out, err)
        session.close()
    return 0


def _run_threaded(session, line, out, err):
    """Turns run in the background; this (main) thread reads every line and routes it."""
    workers = session.workers
    approvals = ApprovalLines()
    session.printer = lambda stream, text: line.emit(out if stream == "out" else err, text)

    def ask(view):
        _show_root_approval(view, lambda text: line.emit(out, text))
        line.emit(out, APPROVAL_PROMPT.format(name=view["name"]) + THREADED_APPROVAL)
        answer = approvals.ask()
        if answer is EOF_ANSWER:
            return "deny", True          # as the sequential CLI does at EOF
        if answer is EXIT_ANSWER:
            return None, True            # /exit while answering: the call stays pending for next time
        word = answer.strip().lower()
        if word in ALLOW_WORDS:
            return "allow", False
        if word not in DENY_WORDS:
            # The user typed something else while the question was up (often a correction meant for the
            # agent): deny the call, and keep the line instead of discarding it.
            session.interject(answer.strip())
            line.emit(err, DENIED_WITH_LINE)
        return "deny", False

    try:
        while True:
            if workers is not None and not approvals.pending():
                _deliver(session, line, out, err, threaded=True)
            try:
                text = line.read(PROMPT)
            except EOFError:
                print(file=out)
                approvals.close(EOF_ANSWER)
                _join_turn(session, err)
                if workers is not None:
                    session.finish_workers()
                break
            except KeyboardInterrupt:
                print(file=out)
                if approvals.answer(""):
                    print("Approval prompt interrupted; denying the tool call.", file=err)
                    continue
                if session.busy:
                    if session.interrupt():
                        print("Cancelling the current turn (Ctrl+C)...", file=err)
                    continue
                if _stop_side(session):
                    continue          # the main agent is idle: Ctrl+C stops only the side chat's answer
                if workers is not None and workers.running():
                    session.cancel_all("Ctrl+C")
                break
            if is_side_command(text):
                # A side chat line never answers a pending approval; that question stays open.
                session.command(text.strip())
                if approvals.pending():
                    line.emit(out, "（主 Agent 的审批仍在等待：下一行 y 允许，n 或回车拒绝。）")
                continue
            if approvals.answer(text):
                continue
            text = text.strip()
            if not text:
                continue
            if text in EXIT_COMMANDS:
                if session.busy:
                    print("主 Agent 还在回复这一轮：等它结束后退出（Ctrl+C 取消这一轮；待答的审批保持待定）。", file=err)
                    approvals.close(EXIT_ANSWER)
                    _join_turn(session, err)
                    approvals.reopen()
                if workers is not None and workers.running() and not _confirm_exit(session, line, out):
                    continue
                break
            if not _is_queue(text) and session.command(text, ask=ask, background=True):
                continue
            session.send(text, ask=ask)
    finally:
        approvals.close(EXIT_ANSWER)
        _join_turn(session, err)
        if workers is not None:
            workers.on_notice = None
            _flush(session, out, err)
        session.close()
    return 0


def _join_turn(session, err):
    """Wait for the background turn; Ctrl+C cancels it instead of leaving it running."""
    while session.busy:
        try:
            time.sleep(0.05)
        except KeyboardInterrupt:
            if session.interrupt():
                print("Cancelling the current turn (Ctrl+C)...", file=err)


def finish_workers(source, out, err, *, line=None):
    """No more input: wait for running workers, print their pushes, keep parked approvals for later.

    The rules live in ``ChatSession.finish_workers``; ``source`` is a ChatSession, or (older callers)
    a WorkerManager that gets a printing ChatSession around it.
    """
    session = source if isinstance(source, ChatSession) else ChatSession(
        run_turn=None, store=source.store, workspace=None, session_id=source.session_id, workers=source,
        main_agent=True, status_events=False)
    session.printer = lambda stream, text: print(text, file=out if stream == "out" else err)
    session.finish_workers()


def _flush(source, out, err):
    """Print queued pushes and worker trace lines (source: ChatSession or WorkerManager)."""
    for stream, text in source.take_display():
        print(text, file=out if stream == "out" else err)


def _deliver(session, line, out, err, *, threaded=False):
    """Between prompts: print queued pushes, then ask pending approvals one by one.

    ``threaded``: a turn may hold the session, so the registry is saved only when it is free.
    """
    _flush(session, out, err)
    while (view := session.next_worker_approval()) is not None:
        if not _ask_worker_approval(session, view, line, out, err):
            break
        _flush(session, out, err)
    if threaded:
        session.persist_idle()
    else:
        session.persist()


def _ask_worker_approval(session, view, line, out, err):
    """Ask allow/deny for one displayed worker call; the answer applies to that call only."""
    name, worker = view["name"], view["worker"]
    print(f"\nApproval required: {name}", file=out)
    print(f"  requested by: worker {worker} (session {view['session_id']})", file=out)
    print(f"  task: {view['task']}", file=out)
    if view["arguments"]:
        print(f"  arguments: {view['arguments']}", file=out)
    if view.get("patch"):
        print(view["patch"], file=out)
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


def _show_root_approval(view, write):
    write(f"\nApproval required: {view['name']}")
    if view["node"] is not None:
        write(f"  requested by: worker {view['node'][:8]}")
    if view["arguments"]:
        write(f"  arguments: {view['arguments']}")
    if view.get("patch"):
        write(view["patch"])
    write(f"  call: {view['call_id']}")


def _ask_root_approval(view, out, err):
    """Show the exact pending main-agent call and read y/N. Returns (decision, stop)."""
    name = view["name"]
    _show_root_approval(view, lambda text: print(text, file=out))
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
