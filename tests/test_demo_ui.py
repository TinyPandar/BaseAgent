"""scripts/demo_ui.py: the interview demo path (AC30) end to end, offline.

TUI: send one message -> two workers in the panel -> approve w1's write -> F2 cancels w2 -> quit.
Web: the same session -> snapshot shows the history and both workers -> one more message.
"""

import asyncio
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_helpers import HAS_TEXTUAL, HAS_WEB, NO_TEXTUAL, NO_WEB, wait_until
from test_main_agent import WAIT

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "demo_ui.py"


@unittest.skipUnless(HAS_TEXTUAL and HAS_WEB, f"{NO_TEXTUAL}; {NO_WEB}")
class DemoUiTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("demo_ui", SCRIPT)
        self.demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.demo)
        self.demo.STEP_SECONDS = 0.2
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name) / "demo"

    def test_tui_then_web_demo(self):
        from baseagent.tui.app import ApprovalScreen, ChatApp, ExitScreen

        def tui(session):
            async def drive():
                app = ChatApp(session, poll=0.02)
                async with app.run_test(size=(140, 40)) as pilot:
                    async def until(predicate):
                        deadline = time.monotonic() + WAIT
                        while not predicate():
                            self.assertLess(time.monotonic(), deadline)
                            await pilot.pause(0.02)

                    app.query_one("#input").value = "查一下 TODO，再把 VERSION 写成 1.0"
                    await pilot.press("enter")
                    await until(lambda: app.worker_ids == ["w1", "w2"])
                    await until(lambda: isinstance(app.screen, ApprovalScreen) and app.screen.query("#allow"))
                    self.assertEqual(app.screen.view["name"], "write_file")
                    await pilot.press("y")
                    await until(lambda: any("[w1 已完成]" in text for _, text in app.entries))
                    app.query_one("#workers").move_cursor(row=1)
                    await pilot.press("f2")
                    await until(lambda: session.workers.get("w2").status == "cancelled")
                    await pilot.press("ctrl+q")
                    await until(lambda: session.closed or isinstance(app.screen, ExitScreen))
            asyncio.run(drive())
            return 0

        with patch("baseagent.tui.app.run_tui", tui):
            self.assertEqual(self.demo.run(["--dir", str(self.dir), "--fresh"]), 0)
        self.assertEqual((self.dir / "workspace" / "VERSION").read_text(), "1.0\n")

        def web(session, **options):
            from fastapi.testclient import TestClient

            from baseagent.web.server import create_app
            self.assertEqual(options["port"], 8765)
            session.open()
            client = TestClient(create_app(session), base_url="http://127.0.0.1:8765")
            body = client.get("/events", params={"wait": 0}).text
            snapshot = json.loads(body.split("data: ", 1)[1].split("\n", 1)[0])["data"]["snapshot"]
            self.assertEqual(snapshot["history"][0], {"role": "user", "text": "查一下 TODO，再把 VERSION 写成 1.0"})
            self.assertEqual({item["id"]: item["status"] for item in snapshot["status"]["workers"]},
                             {"w1": "completed", "w2": "cancelled"})
            client.post("/messages", json={"text": "刚才做了什么？"})
            wait_until(lambda: not session.busy)
            # Pushes not yet attached to a message when the TUI exited are not re-sent after a restart
            # (same as the CLI); the registry and the snapshot still show both workers.
            self.assertEqual(session.history()[-1]["text"], "（离线演示模型）收到：刚才做了什么？。Worker 的进度看右侧面板。")
            session.exit(force=True)
            return 0

        with patch("baseagent.web.server.serve", web):
            self.assertEqual(self.demo.run(["--dir", str(self.dir), "--ui", "web"]), 0)


if __name__ == "__main__":
    unittest.main()
