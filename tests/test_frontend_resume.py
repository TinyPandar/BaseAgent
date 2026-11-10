"""One session, several frontends (AC20): CLI -> TUI, TUI -> Web, Web -> CLI with the same --session.

Each step uses the real ``baseagent.main`` composition and the same SQLite file; the next
frontend must show the earlier conversation and workers, and its next turn must reach the
model with the earlier messages. Fake models only.
"""

import asyncio
import contextlib
import io
import time
import unittest
from unittest.mock import patch

from baseagent.main import main
from chat_helpers import HAS_TEXTUAL, HAS_WEB, NO_TEXTUAL, NO_WEB, ChatTestCase, wait_until
from test_main_agent import WAIT, Duplex, delegate, feed

if HAS_TEXTUAL:
    from textual.widgets import Input

    from baseagent.tui.app import ChatApp
if HAS_WEB:
    from fastapi.testclient import TestClient

    from baseagent.web.server import create_app

OPTIONS = ["--session", "demo"]


class ResumeAcrossFrontendsTests(ChatTestCase):
    def cli(self, model, lines):
        stdout = io.StringIO()
        with patch("builtins.input", side_effect=feed(lines)), contextlib.redirect_stdout(stdout):
            code = main(["--root", str(self.root), "--db", str(self.db), *OPTIONS], model_factory=lambda args: model)
        self.assertEqual(code, 0)
        return stdout.getvalue()

    def tui(self, model, text):
        """Open the TUI on the session, send one message, wait for the reply, quit. Returns the transcript."""
        session = self.session(OPTIONS, model, ui="tui")
        entries = []

        async def drive():
            app = ChatApp(session, poll=0.02)
            async with app.run_test(size=(120, 36)) as pilot:
                deadline = time.monotonic() + WAIT
                before = len(app.entries)
                entries.extend(app.entries)
                field = app.query_one("#input", Input)
                field.value = text
                await pilot.press("enter")
                while not (any(kind == "assistant" for kind, _ in app.entries[before:]) and not session.busy
                           and not session.running()):
                    self.assertLess(time.monotonic(), deadline, "TUI turn never finished")
                    await pilot.pause(0.02)
                entries[:] = list(app.entries)
                await pilot.press("ctrl+q")
                while not session.closed:
                    self.assertLess(time.monotonic(), deadline, "TUI never exited")
                    await pilot.pause(0.02)

        asyncio.run(drive())
        return entries

    def web(self, model, text):
        session = self.session(OPTIONS, model, ui="web")
        session.open()
        client = TestClient(create_app(session), base_url="http://127.0.0.1:8765")
        snapshot = client.get("/events", params={"wait": 0}).text
        self.assertEqual(client.post("/messages", json={"text": text}).json(), {"result": "sent"})
        self.idle(session)
        session.exit(force=True)
        return session, snapshot

    @unittest.skipUnless(HAS_TEXTUAL, NO_TEXTUAL)
    def test_cli_then_tui(self):
        first = Duplex(main=[delegate("d1", "Read README.md"), {"content": "已派出 w1。"}],
                       worker=[{"content": "README is long. README.md:1-1"}])
        self.cli(first, ["读 README", "/wait", "/exit"])
        second = Duplex(main=[{"content": "刚才 w1 读了 README。"}])
        entries = self.tui(second, "刚才做了什么？")
        self.assertEqual(entries[:2], [("user", "读 README"), ("assistant", "已派出 w1。")])
        self.assertIn(("assistant", "刚才 w1 读了 README。"), entries)
        seen = [message.get("content") for message in second.main_seen[0]]
        self.assertIn("读 README", seen)
        self.assertIn("已派出 w1。", seen)
        self.assertEqual(self.registry()["w1"]["status"], "completed")

    @unittest.skipUnless(HAS_TEXTUAL and HAS_WEB, f"{NO_TEXTUAL}; {NO_WEB}")
    def test_tui_then_web(self):
        first = Duplex(main=[delegate("d1", "Read README.md"), {"content": "已派出 w1。"}],
                       worker=[{"content": "README is long. README.md:1-1"}])
        self.tui(first, "读 README")
        second = Duplex(main=[{"content": "继续：w1 已完成。"}])
        session, snapshot = self.web(second, "继续")
        self.assertIn('"text":"读 README"', snapshot)
        self.assertIn('"id":"w1","status":"completed"', snapshot)
        self.assertEqual([item["text"] for item in session.history()],
                         ["读 README", "已派出 w1。", "继续", "继续：w1 已完成。"])
        self.assertIn("读 README", [message.get("content") for message in second.main_seen[0]])

    @unittest.skipUnless(HAS_WEB, NO_WEB)
    def test_web_then_cli(self):
        first = Duplex(main=[delegate("d1", "Read README.md"), {"content": "已派出 w1。"}],
                       worker=[{"content": "README is long. README.md:1-1"}])
        self.web(first, "读 README")
        wait_until(lambda: self.registry().get("w1", {}).get("status") == "completed")
        second = Duplex(main=[{"content": "CLI 里接着聊。"}])
        stdout = self.cli(second, ["/status", "接着", "/exit"])
        self.assertIn("w1 completed 已完成", stdout)
        self.assertIn("CLI 里接着聊。", stdout)
        seen = [message.get("content") for message in second.main_seen[0]]
        self.assertIn("读 README", seen)
        self.assertIn("已派出 w1。", seen)


if __name__ == "__main__":
    unittest.main()
