"""Textual frontend for continuous chat (``baseagent --ui tui``).

Layout: transcript (scrolls) and input on the left, worker panel on the right,
status bar (session id, budget used/limit) and key bindings at the bottom.
The app only calls ``ChatSession`` commands and renders its events; it reads
events with the same ``events_after(after_id)`` cursor the Web server uses, so
a gap in the buffer produces the same ``resync`` snapshot.

Blocking calls (cancel waits up to 10 s for a worker thread, exit cancels
workers) run in Textual thread workers so the screen never freezes. A worker
push only appends to the transcript; the input box is never touched.
"""

from __future__ import annotations

import time
from collections import OrderedDict

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, Static

from baseagent.chat.text import job_label

POLL_SECONDS = 0.05
TOOL_CHARS = 140   # tool previews are 300 chars; the transcript shows a shorter line
LIST_HEADER_CHARS = 300   # glob/grep count line (total, shown, omitted, tool truncation) is never cut
STYLES = {"user": "bold", "assistant": "", "system": "dim", "tool": "dim italic", "warn": "yellow",
          "push": "green", "failed": "red"}
PREFIX = {"user": "你 › ", "assistant": "主 Agent › "}
QUEUED_SUFFIX = {"steer": "  （已加入本轮，等待送达）", "queue": "  （已排队：本轮结束后发送）"}


class ApprovalScreen(ModalScreen[str]):
    """One pending call: worker, tool, argument preview and digest; allow or deny."""

    BINDINGS = [Binding("y", "decide('allow')", "批准"), Binding("n", "decide('deny')", "拒绝")]

    def __init__(self, view, waiting=0):
        super().__init__()
        self.view = view
        self.waiting = waiting

    def compose(self) -> ComposeResult:
        view = self.view
        who = f"worker {view['worker']}（会话 {view['session_id']}）" if view.get("worker") else "主 Agent"
        lines = [f"请求者：{who}", f"工具：{view['name']}"]
        if view.get("task"):
            lines.append(f"任务：{view['task']}")
        lines += [f"参数：{view.get('arguments') or '（无预览）'}", f"call：{view['call_id']}", f"digest：{view['digest']}"]
        if self.waiting:
            lines.append(f"（后面还有 {self.waiting} 条待审批，依次弹出）")
        with Vertical(id="dialog"):
            yield Label(f"需要审批：{view['name']}", id="title")
            yield Static(Text("\n".join(lines)), id="details")
            with Horizontal(id="buttons"):
                yield Button("批准 (y)", id="allow", variant="success")
                yield Button("拒绝 (n)", id="deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)

    def action_decide(self, decision: str) -> None:
        self.dismiss(decision)


class ExitScreen(ModalScreen[bool]):
    """Running workers (or a running turn) at exit: cancel and leave, or keep waiting — as in the CLI."""

    BINDINGS = [Binding("c", "choose(True)", "取消并退出"), Binding("w", "choose(False)", "继续等待"),
                Binding("escape", "choose(False)", "继续等待")]

    def __init__(self, question):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("退出？", id="title")
            yield Static(Text(self.question), id="details")
            with Horizontal(id="buttons"):
                yield Button("取消它们并退出 (c)", id="leave", variant="error")
                yield Button("继续等待 (w)", id="stay", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "leave")

    def action_choose(self, leave: bool) -> None:
        self.dismiss(leave)


class ChatApp(App):
    TITLE = "BaseAgent"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #main { height: 1fr; }
    #chat { width: 1fr; }
    #log { height: 1fr; border: round $primary-darken-2; padding: 0 1; }
    #input { dock: bottom; }
    #side { width: 50; }
    #workers { height: 1fr; border: round $secondary-darken-2; }
    #sidechat { height: 2fr; border: round $accent-darken-2; }
    #sidechat-title { padding: 0 1; text-style: bold; }
    #side-log { height: 1fr; padding: 0 1; }
    #side-live { height: auto; max-height: 12; padding: 0 1; color: $text; }
    #worker-detail { height: auto; max-height: 8; border: round $secondary-darken-2; padding: 0 1; color: $text-muted; }
    #side-title { padding: 0 1; text-style: bold; }
    #statusbar { height: 1; background: $boost; color: $text-muted; padding: 0 1; }
    ApprovalScreen, ExitScreen { align: center middle; }
    #dialog { width: 80; height: auto; border: thick $warning; background: $panel; padding: 1 2; }
    #title { text-style: bold; padding-bottom: 1; }
    #buttons { height: auto; padding-top: 1; }
    #buttons Button { margin-right: 2; }
    """
    BINDINGS = [
        Binding("f2", "cancel_selected", "取消选中 worker"),
        Binding("f3", "cancel_all", "取消全部"),
        Binding("f5", "refresh", "刷新状态"),
        Binding("ctrl+q", "quit_chat", "退出", priority=True),
    ]

    def __init__(self, session, *, poll=POLL_SECONDS):
        super().__init__()
        self.session = session
        self.poll = poll
        self.cursor = None
        self.entries = []                     # (kind, text) shown in the transcript; tests read it
        self.approvals = OrderedDict()        # "<worker|main>:<call_id>" -> view, oldest first
        self.asking = None                    # key of the approval shown in the modal
        self.worker_rows = []
        self.worker_ids = []
        self.progress = {}
        self.last_status = None
        self.leaving = False
        self.shown_keys = set()               # mid-turn replies a resync already put on screen
        # Side chat panel: its own cursor on the side event buffer, its own transcript and input.
        self.side_cursor = None
        self.side_entries = []                # (kind, text) shown in the side panel; tests read it
        self.side_live = {}                   # attempt -> text that arrived so far

    # ----- layout ----------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="chat"):
                yield RichLog(id="log", wrap=True, markup=False, highlight=False, auto_scroll=True)
                yield Input(placeholder="输入任务或问题；/status、/continue [N]、/checkpoints、/undo、/cancel <id|all>、/tell <id> <text>、/queue <text>、/exit（回复中发送 = 追加到本轮）", id="input")
            with Vertical(id="side"):
                yield Label("Workers", id="side-title")
                yield DataTable(id="workers", cursor_type="row", zebra_stripes=True)
                yield Static("", id="worker-detail")
                with Vertical(id="sidechat"):
                    yield Label("侧聊（只看创建时的上下文，不执行工具，不回到主任务）", id="sidechat-title")
                    yield RichLog(id="side-log", wrap=True, markup=False, highlight=False, auto_scroll=True)
                    yield Static("", id="side-live")
                    yield Input(placeholder="侧聊提问（Tab 切换输入框；/side-stop 停止，/side-end 结束）", id="side-input")
        yield Static("", id="statusbar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#workers", DataTable)
        table.add_columns("id", "状态", "时间", "模型", "任务")
        self.set_interval(1.0, self.render_detail)
        self.session.open()
        self.apply(self.session.events_after(None))
        self.apply_side(self.session.side_events_after(None))
        self.query_one("#input", Input).focus()
        self.set_interval(self.poll, self.pump)
        self.set_interval(1.0, self.render_workers)

    # ----- events ----------------------------------------------------------------------
    def pump(self) -> None:
        if not self.leaving:
            self.apply(self.session.events_after(self.cursor))
            self.apply_side(self.session.side_events_after(self.side_cursor))

    # ----- side chat panel ---------------------------------------------------------------
    def apply_side(self, items) -> None:
        for item in items:
            self.side_cursor = item["id"]
            self.handle_side(item["kind"], item["data"])

    def side_write(self, kind, text) -> None:
        self.side_entries.append((kind, text))
        prefix = {"user": "你 › ", "assistant": "侧聊 › "}.get(kind, "")
        self.query_one("#side-log", RichLog).write(Text(prefix + text, style=STYLES.get(kind, "")))

    def render_side_live(self) -> None:
        text = "\n".join(self.side_live.values())
        self.query_one("#side-live", Static).update(Text(("侧聊 › " + text) if text else ""))

    def handle_side(self, kind, data) -> None:
        attempt = data.get("attempt")
        if kind == "resync":
            self.query_one("#side-log", RichLog).clear()
            self.side_entries.clear()
            self.side_live.clear()
            view = data["snapshot"].get("side")
            if view:
                for note in view["notes"]:
                    self.side_write("system", note)
                for item in view["attempts"]:
                    self.side_write("user", item["question"])
                    if item["status"] in ("dispatching", "streaming"):
                        self.side_live[item["attempt"]] = item["text"]
                    else:
                        self.side_write("assistant", item["text"] + {"stopped": "（已停止）",
                                                                     "failed": "（失败）"}.get(item["status"], ""))
            self.render_side_live()
        elif kind == "side_opened":
            origin = "主任务最近一次真实请求" if data["origin"] == "live" else "已保存的会话（文本化）"
            self.side_write("system", f"已打开：主会话第 {data['turn']} 轮的快照（{origin}）")
            for note in data.get("notes") or ():
                self.side_write("system", note)
        elif kind == "side_question":
            self.side_write("user", data["text"])
            self.side_live[attempt] = ""
            self.render_side_live()
        elif kind == "side_delta":
            self.side_live[attempt] = self.side_live.get(attempt, "") + data["text"]
            self.render_side_live()
        elif kind in ("side_answer", "side_stopped", "side_failed"):
            self.side_live.pop(attempt, None)
            suffix = {"side_stopped": "（已停止）", "side_failed": f"（失败：{data.get('error')}）"}.get(kind, "")
            self.side_write("assistant" if kind == "side_answer" else "warn", (data.get("text") or "") + suffix)
            self.render_side_live()
        elif kind == "side_usage":
            if data.get("usage") == "unknown":
                self.side_write("system", "（这次请求的用量未知，不按 0 计）")
            elif data.get("late"):
                self.side_write("system", f"（已停止那一问的用量后到：{data.get('prompt_tokens')}+"
                                          f"{data.get('completion_tokens')} tokens，已记入）")
        elif kind == "side_notice":
            self.side_write("warn", data["text"])
        elif kind == "side_ended":
            self.side_live.clear()
            self.render_side_live()
            self.side_write("system", f"侧聊已结束：{data['attempts']} 次请求，未知用量 {data['unknown']} 次")

    @work(thread=True)
    def side_submit(self, text) -> None:
        line = text if text.startswith("/side") else "/side " + text
        result = self.session.side_command(line)
        if result.get("result") in ("refused", "busy") and not text.startswith("/side-"):
            self.call_from_thread(self.restore_side_input, text)

    def restore_side_input(self, text) -> None:
        field = self.query_one("#side-input", Input)
        if not field.value:
            field.value = text

    def apply(self, items) -> None:
        for item in items:
            self.cursor = item["id"]
            self.handle(item["kind"], item["data"])
            if item["kind"] == "assistant_message" and item["data"].get("interim"):
                self.confirm_shown([item["id"]])

    def confirm_shown(self, ids) -> None:
        """Report mid-turn replies as shown once the refresh that contains them has been drawn."""
        if ids:
            self.call_after_refresh(self.session.mark_shown, ids)

    def handle(self, kind, data) -> None:
        if kind == "resync":
            self.resync(data["snapshot"])
        elif kind == "user_message":
            # A line sent while the agent works waits in the inbox: steer joins this turn, queue the next one.
            suffix = QUEUED_SUFFIX.get(data.get("mode"), "") if data.get("queued") else ""
            self.write("user", data["text"] + suffix)
        elif kind == "inbox_delivered":
            if "user" in data.get("sources", ()):
                self.write("system", "（已送达主 Agent" + ("：这一步就会看到）" if data.get("how") == "model_call"
                                                         else "：作为新的一轮）"))
        elif kind == "assistant_message":
            # After a resync the snapshot may already hold a mid-turn reply whose event follows.
            if not (data.get("interim") and data.get("key") in self.shown_keys):
                self.write("assistant", data["text"])
        elif kind == "tool_call":
            self.write("tool", _clip(f"{_who(data)}→ {data['name']} {data['arguments']}"))
        elif kind == "tool_result":
            head, *items = data["preview"].split("\n")
            # A list preview keeps its count line whole; each listed item is clipped like other tool lines.
            line = f"{_who(data)}← {data['name']} {'ok' if data['ok'] else '失败'}: {head}"
            self.write("tool", "\n".join([_clip(line, LIST_HEADER_CHARS if items else TOOL_CHARS),
                                           *(_clip(item) for item in items)]))
        elif kind == "worker_started":
            mode = "可写" if data["allow_writes"] else "只读"
            self.write("system", f"已派出 {data['worker']}（{mode}）：{data['task']}")
        elif kind == "worker_progress":
            self.progress[data["worker"]] = data["line"]
        elif kind == "worker_done":
            self.write("push", data["text"])
        elif kind in {"worker_failed", "worker_cancelled"}:
            self.write("failed", data["text"])
        elif kind == "approval_requested":
            self.approvals[_key(data)] = data
            self.write("warn", f"{data.get('worker') or '主 Agent'} 等待审批：{data['name']}")
            self.ask_next()
        elif kind == "approval_decided":
            self.approvals.pop(_key(data), None)
            self.write("system", data["text"])
        elif kind == "status":
            self.show_status(data)
        elif kind == "notice":
            self.write("warn" if data["stream"] == "err" else "system", data["text"])
        elif kind == "error":
            self.write("warn", data["message"])

    def resync(self, snapshot) -> None:
        log = self.query_one("#log", RichLog)
        log.clear()
        self.entries.clear()
        self.shown_keys = {item["key"] for item in snapshot["history"] if item.get("key")}
        for item in snapshot["history"]:
            self.write(item["role"], item["text"])
        for answer in snapshot.get("interim", ()):
            if answer["key"] not in self.shown_keys:
                self.shown_keys.add(answer["key"])
                self.write("assistant", answer["text"])
        self.confirm_shown([answer["id"] for answer in snapshot.get("interim", ())])
        self.approvals = OrderedDict((_key(view), view) for view in snapshot["approvals"])
        self.show_status(snapshot["status"])
        self.ask_next()

    def write(self, kind, text) -> None:
        self.entries.append((kind, text))
        style = STYLES.get(kind, "")
        self.query_one("#log", RichLog).write(Text(PREFIX.get(kind, "") + text, style=style))

    # ----- worker panel and status bar --------------------------------------------------
    def show_status(self, status) -> None:
        self.last_status = status
        self.worker_rows = status.get("workers") or []
        budget = status.get("budget")
        parts = [f"会话 {status.get('session_id') or '（第一条消息创建）'}"]
        if budget:
            text = (f"本轮预算 模型 {budget['model_calls']}/{budget['max_model_calls']} "
                    f"步数 {budget['steps']}/{budget['max_steps']}")
            if "budget_cny" in budget:
                text += f" ¥{budget['cost_cny']}/{budget['budget_cny']}"
            parts.append(text)
        parts.append(f"tokens {status.get('tokens', 0)}")
        if status.get("busy"):
            parts.append("主 Agent 回复中…")
        parts.extend(job_label(job) for job in status.get("jobs") or () if job["waiting"] or job["status"] == "running")
        self.query_one("#statusbar", Static).update(" · ".join(parts))
        self.render_workers()

    def render_workers(self) -> None:
        table = self.query_one("#workers", DataTable)
        row = table.cursor_row
        table.clear()
        self.worker_ids = []
        now = time.time()
        for worker in self.worker_rows:
            end = worker.get("finished_at") or now
            table.add_row(worker["id"] + (" ✎" if worker.get("allow_writes") else ""), worker["label"][:12],
                          f"{max(0.0, end - worker['started_at']):.0f}s",
                          f"{worker['model_calls']}/{worker.get('max_model_calls') or '?'}", worker["task"][:16],
                          key=worker["id"])
            self.worker_ids.append(worker["id"])
        if self.worker_ids:
            table.move_cursor(row=min(max(row, 0), len(self.worker_ids) - 1))
        self.render_detail()

    def render_detail(self) -> None:
        """The selected worker: its task, and its latest trace line while it runs."""
        worker_id = self.selected_worker()
        worker = next((item for item in self.worker_rows if item["id"] == worker_id), None)
        if worker is None:
            self.query_one("#worker-detail", Static).update("还没有 worker" if not self.worker_rows else "")
            return
        lines = [f"{worker['id']} {worker['label']} · {'可写' if worker.get('allow_writes') else '只读'} · "
                 f"步数 {worker['steps']}/{worker.get('max_steps') or '?'} · 工具 {worker['tool_calls']}/"
                 f"{worker.get('max_tool_calls') or '?'}", f"任务：{worker['task']}"]
        if worker["status"] == "running" and self.progress.get(worker_id):
            lines.append(f"最近：{self.progress[worker_id].splitlines()[0]}")
        elif worker.get("summary"):
            lines.append(f"汇报：{worker['summary']}")
        self.query_one("#worker-detail", Static).update(Text("\n".join(lines)))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.render_detail()

    def selected_worker(self):
        table = self.query_one("#workers", DataTable)
        if not self.worker_ids:
            return None
        return self.worker_ids[min(max(table.cursor_row, 0), len(self.worker_ids) - 1)]

    # ----- approvals -------------------------------------------------------------------
    def ask_next(self) -> None:
        if self.asking is not None or not self.approvals or self.leaving:
            return
        key, view = next(iter(self.approvals.items()))
        self.asking = key
        self.push_screen(ApprovalScreen(view, waiting=len(self.approvals) - 1),
                         lambda decision, view=view: self.answered(view, decision))

    def answered(self, view, decision) -> None:
        self.approvals.pop(_key(view), None)
        self.asking = None
        self.decide(view, decision)
        self.call_later(self.ask_next)

    @work(thread=True)
    def decide(self, view, decision) -> None:
        answer = self.session.approve if decision == "allow" else self.session.deny
        answer(view["call_id"], view["digest"], worker=view.get("worker"))

    # ----- input and actions -----------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if event.input.id == "side-input":
            event.input.clear()
            self.side_submit(text)
            return
        if text in {"/exit", "/quit"}:
            event.input.clear()
            self.action_quit_chat()
            return
        event.input.clear()
        self.submit(text)

    @work(thread=True)
    def submit(self, text) -> None:
        if self.session.submit(text) == "rejected":
            self.call_from_thread(self.restore_input, text)

    def restore_input(self, text) -> None:
        field = self.query_one("#input", Input)
        if not field.value:
            field.value = text

    def action_cancel_selected(self) -> None:
        worker = self.selected_worker()
        if worker is None:
            self.write("warn", "没有选中的 worker。")
            return
        self.cancel(worker)

    def action_cancel_all(self) -> None:
        self.cancel("all")

    @work(thread=True)
    def cancel(self, target) -> None:
        self.session.cancel(target)

    def action_refresh(self) -> None:
        self.session.publish_status(force=True)

    def action_quit_chat(self) -> None:
        result = self.session.exit()
        if result["exited"]:
            self.leaving = True
            self.exit()
            return
        self.push_screen(ExitScreen(result["question"]), self.confirm_exit)

    def confirm_exit(self, leave) -> None:
        if leave:
            self.leaving = True
            self.leave()
        else:
            self.write("system", "继续等待：worker 完成后会自动推送；F5 刷新状态。")

    @work(thread=True)
    def leave(self) -> None:
        self.session.exit(force=True, why="exit")
        self.call_from_thread(self.exit)


def _key(view):
    return f"{view.get('worker') or 'main'}:{view['call_id']}"


def _clip(text, limit=TOOL_CHARS):
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _who(data):
    return f"[{data['worker']}] " if data.get("worker") else ""


def run_tui(session):
    """Run the TUI until exit; anything still running afterwards is cancelled and recorded, as on Ctrl+C."""
    try:
        ChatApp(session).run()
    finally:
        if not session.closed:
            session.exit(force=True, why="Ctrl+C")
    return 0
