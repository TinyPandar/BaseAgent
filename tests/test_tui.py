"""Textual TUI (``baseagent --ui tui``) driven with App.run_test() pilots. Fake models only.

Skipped with a reason when the optional 'tui' extra (textual) is not installed.
"""

import asyncio
import contextlib
import io
import time
import unittest

from chat_helpers import HAS_TEXTUAL, NO_TEXTUAL, ChatTestCase
from test_main_agent import WAIT, Duplex, Gate, delegate, delegation, feed, tool_call

if HAS_TEXTUAL:
    from textual.widgets import Input, Static

    from baseagent.main import main
    from baseagent.tui.app import ApprovalScreen, ChatApp, ExitScreen
    from unittest.mock import patch

SIZE = (140, 40)


async def until(pilot, predicate, message="condition was never met"):
    deadline = time.monotonic() + WAIT
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        await pilot.pause(0.02)


@unittest.skipUnless(HAS_TEXTUAL, NO_TEXTUAL)
class TuiTests(ChatTestCase):
    def run_app(self, session, script):
        async def drive():
            app = ChatApp(session, poll=0.02)
            async with app.run_test(size=SIZE) as pilot:
                await script(app, pilot)
        asyncio.run(drive())

    def texts(self, app, kind=None):
        return [text for entry_kind, text in app.entries if kind is None or entry_kind == kind]

    async def type(self, app, pilot, text):
        field = app.query_one("#input", Input)
        field.focus()
        field.value = text
        await pilot.press("enter")

    def test_message_reply_and_status_bar(self):
        session = self.session(["--session", "demo"], Duplex(main=[{"content": "你好，我在。"}]), ui="tui")

        async def script(app, pilot):
            await self.type(app, pilot, "你好")
            await until(pilot, lambda: "你好，我在。" in self.texts(app, "assistant"))
            self.assertIn("你好", self.texts(app, "user"))
            self.assertEqual(app.query_one("#input", Input).value, "")
            await until(pilot, lambda: "会话 demo" in str(app.query_one("#statusbar", Static).render()))
            self.assertIn("本轮预算 模型", str(app.query_one("#statusbar", Static).render()))

        self.run_app(session, script)

    def test_worker_push_appears_without_touching_the_input(self):
        gate = Gate({"content": "pushed result"})
        self.gates = [gate]
        model = Duplex(main=[delegate("d1", "slow"), {"content": "已派出 w1。"}], worker=[gate])
        session = self.session(["--session", "demo"], model, ui="tui")

        async def script(app, pilot):
            await self.type(app, pilot, "慢任务")
            await until(pilot, lambda: "已派出 w1。" in self.texts(app, "assistant"))
            await until(pilot, lambda: app.worker_ids == ["w1"], "worker panel never listed w1")
            field = app.query_one("#input", Input)
            field.focus()
            await pilot.press(*"half typ")
            gate.release()
            await until(pilot, lambda: any("[w1 已完成] pushed result" in text for text in self.texts(app, "push")))
            self.assertEqual(field.value, "half typ")
            self.assertTrue(field.has_focus)
            await pilot.press(*"ed")
            self.assertEqual(field.value, "half typed")

        self.run_app(session, script)

    def test_approval_modals_come_one_at_a_time_allow_then_deny(self):
        (self.root / "B.md").write_text("bee\n", encoding="utf-8")
        gates = {"A": Gate({"tool_calls": [tool_call("ra", "read_file", {"path": "README.md"})]}),
                 "B": Gate({"tool_calls": [tool_call("rb", "read_file", {"path": "B.md"})]})}
        self.gates = list(gates.values())
        model = Duplex(main=[{"tool_calls": [delegation("d1", "A"), delegation("d2", "B")]}, {"content": "ok"}],
                       by_task={"A": [gates["A"], {"content": "A finished"}], "B": [gates["B"], {"content": "B finished"}]})
        session = self.session(["--session", "demo", "--tool-policy", self.policy(["read_file"])], model, ui="tui")
        shown = []

        async def script(app, pilot):
            await self.type(app, pilot, "读两个文件")
            await until(pilot, lambda: gates["A"].entered.is_set() and gates["B"].entered.is_set())
            for gate in gates.values():
                gate.release()
            await until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.screen.query("#allow"),
                        "no approval modal")
            await until(pilot, lambda: len(app.approvals) == 2)
            first = app.screen
            details = str(first.query_one("#details", Static).render())
            for text in (f"worker {first.view['worker']}", "工具：read_file", "参数：", first.view["digest"]):
                self.assertIn(text, details)
            shown.append(first.view["worker"])
            await until(pilot, lambda: first.query_one("#allow").region.width > 0, "allow button never laid out")
            await pilot.click("#allow")
            await until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.screen is not first
                        and app.screen.query("#deny"), "second approval never popped")
            shown.append(app.screen.view["worker"])
            await pilot.press("n")
            await until(pilot, lambda: not isinstance(app.screen, ApprovalScreen))
            await until(pilot, lambda: len(self.texts(app, "push")) == 2, "both workers should finish")

        self.run_app(session, script)
        self.assertEqual(sorted(shown), ["w1", "w2"])
        allowed, denied = shown
        self.assertTrue(self.tool_result(allowed)["ok"])
        self.assertEqual(self.tool_result(denied)["error"]["code"], "permission_denied")

    def test_f2_cancels_the_selected_worker_and_f3_cancels_all(self):
        gates = [Gate({"content": "never"}), Gate({"content": "never"})]
        self.gates = gates
        model = Duplex(main=[{"tool_calls": [delegation("d1", "A"), delegation("d2", "B")]}, {"content": "ok"}],
                       by_task={"A": [gates[0]], "B": [gates[1]]})
        session = self.session(["--session", "demo"], model, ui="tui")

        async def script(app, pilot):
            await self.type(app, pilot, "两个")
            await until(pilot, lambda: all(gate.entered.is_set() for gate in gates))
            await until(pilot, lambda: app.worker_ids == ["w1", "w2"])
            app.query_one("#workers").move_cursor(row=1)
            await pilot.press("f2")
            await until(pilot, lambda: session.workers.get("w2").status == "cancelled")
            self.assertEqual(session.workers.get("w1").status, "running")
            await until(pilot, lambda: any("worker w2" in text for text in self.texts(app, "failed")))
            await pilot.press("f3")
            await until(pilot, lambda: session.workers.get("w1").status == "cancelled")
            await pilot.press("f5")

        self.run_app(session, script)
        footer_keys = {binding.key for binding in ChatApp.BINDINGS}
        self.assertEqual(footer_keys, {"f2", "f3", "f5", "ctrl+q"})

    def test_exit_with_a_running_worker_asks_then_cancels(self):
        gate = Gate({"content": "never"})
        self.gates = [gate]
        model = Duplex(main=[delegate("d1", "slow"), {"content": "ok"}], worker=[gate])
        session = self.session(["--session", "demo"], model, ui="tui")

        async def script(app, pilot):
            await self.type(app, pilot, "慢任务")
            await until(pilot, lambda: gate.entered.is_set() and not session.busy)
            await pilot.press("ctrl+q")
            await until(pilot, lambda: isinstance(app.screen, ExitScreen))
            self.assertIn("还有 1 个 worker 在运行（w1）", str(app.screen.query_one("#details", Static).render()))
            await pilot.press("w")
            await until(pilot, lambda: not isinstance(app.screen, ExitScreen))
            self.assertEqual(session.workers.get("w1").status, "running")
            await pilot.press("ctrl+q")
            await until(pilot, lambda: isinstance(app.screen, ExitScreen))
            await pilot.press("c")
            await until(pilot, lambda: session.closed)

        self.run_app(session, script)
        self.assertEqual(self.registry()["w1"]["status"], "cancelled")
        self.assertEqual(self.worker().status, "cancelled")

    def test_session_resume_shows_history_and_asks_the_pending_approval(self):
        # AC12 + AC20 (CLI -> TUI): the CLI leaves a parked approval; the TUI shows history and asks it.
        policy = ["--session", "demo", "--allow-write", "--tool-policy", self.policy(["write_file"])]
        first = Duplex(main=[delegate("d1", "Create out.txt", allow_writes=True), {"content": "已派出 w1。"}],
                       worker=[{"tool_calls": [tool_call("w1c", "write_file", self.write_request())]}])
        with patch("builtins.input", side_effect=feed(["create out.txt", "/wait", "/exit"], approvals=[EOFError()])), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--root", str(self.root), "--db", str(self.db), *policy],
                                  model_factory=lambda args: first), 0)
        self.assertEqual(self.worker().status, "awaiting_approval")
        session = self.session(policy, Duplex(worker=[{"content": "Created out.txt."}]), ui="tui")

        async def script(app, pilot):
            await until(pilot, lambda: isinstance(app.screen, ApprovalScreen), "pending approval was not asked")
            self.assertEqual(self.texts(app, "user"), ["create out.txt"])
            self.assertEqual(self.texts(app, "assistant"), ["已派出 w1。"])
            self.assertEqual((app.screen.view["worker"], app.screen.view["call_id"]), ("w1", "w1c"))
            await pilot.press("y")
            await until(pilot, lambda: any("[w1 已完成]" in text for text in self.texts(app, "push")))

        self.run_app(session, script)
        self.assertEqual((self.root / "out.txt").read_text(), "hello")
        self.assertEqual(self.worker().status, "completed")


if __name__ == "__main__":
    unittest.main()
