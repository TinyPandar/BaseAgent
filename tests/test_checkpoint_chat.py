"""/checkpoints, /undo and /rewind in chat (AC 4, 5, 8, 22, 25). Fake models only.

The commands live in ``ChatSession.command``, so the CLI, the Textual TUI (pilot) and the
Web server (TestClient) run the same code. Worker writes are checked against the main-agent
turn that dispatched the worker, not the turn during which the file was written.
"""

import asyncio
import json
import time
import unittest

from chat_helpers import (
    HAS_TEXTUAL,
    HAS_WEB,
    NO_TEXTUAL,
    NO_WEB,
    ChatTestCase,
    wait_until,
)
from checkpoint_helpers import Script, write_call, writes
from test_main_agent import WAIT, Duplex, Gate, delegate

if HAS_TEXTUAL:
    from textual.widgets import Input

    from baseagent.tui.app import ChatApp
if HAS_WEB:
    from fastapi.testclient import TestClient

    from baseagent.web.server import create_app

SINGLE = ["--session", "demo", "--allow-write", "--no-main-agent"]
MAIN = ["--session", "demo", "--allow-write"]


def three_writes(root):
    return Script([writes(root, ("a.txt", "A1\n"), ("b.txt", "B1\n"), ("new.txt", "N\n")), {"content": "改好了 3 个文件"},
                   {"content": "知道了"}])


class CheckpointChatTestCase(ChatTestCase):
    def setUp(self):
        super().setUp()
        (self.root / "a.txt").write_text("A0\n")
        (self.root / "b.txt").write_text("B0\n")

    def assert_restored(self):
        self.assertEqual((self.root / "a.txt").read_text(), "A0\n")
        self.assertEqual((self.root / "b.txt").read_text(), "B0\n")
        self.assertFalse((self.root / "new.txt").exists())

    def listing(self, session="demo"):
        return self.store().checkpoints(session)["checkpoints"]


class CliCheckpointTests(CheckpointChatTestCase):
    def test_cli_checkpoints_then_undo(self):
        code, out, err = self.invoke(SINGLE, three_writes(self.root), ["改三个文件", "/checkpoints", "/undo", "/checkpoints", "/exit"])
        self.assertEqual(code, 0, err)
        self.assertIn("第 1 轮", out)
        for line in ("修改 a.txt", "修改 b.txt", "新建 new.txt"):
            self.assertIn(line, out)
        self.assertIn("已撤回第 1 轮的文件改动：恢复 2 个文件，删除 1 个新建文件。", out)
        self.assertIn("3 个文件  已撤销", out)
        self.assert_restored()

    def test_undo_conflict_needs_force(self):
        def hand_edit(prompt):
            (self.root / "b.txt").write_text("hand\n")
            return "/undo"

        model = Script([writes(self.root, ("a.txt", "A1\n")), {"content": "t1"},
                        writes(self.root, ("b.txt", "B2\n")), {"content": "t2"}])
        code, out, err = self.invoke(SINGLE, model, ["一", "二", hand_edit, "/undo --force", "/undo", "/exit"])
        self.assertEqual(code, 0, err)
        self.assertIn("撤销被拒绝：这些文件在 agent 写入之后又被改动过：b.txt。没有写回任何文件", err)
        self.assertIn("已撤回第 2 轮的文件改动：恢复 1 个文件，删除 0 个新建文件。（--force：覆盖了冲突文件）", out)
        self.assertIn("已撤回第 1 轮的文件改动", out)
        self.assertEqual([(item["turn"], item["restored"]) for item in self.listing()], [(1, True), (2, True)])
        self.assertEqual((self.root / "a.txt").read_text(), "A0\n")
        self.assertEqual((self.root / "b.txt").read_text(), "B0\n")

    def test_rewind_after_exit_and_reentry(self):
        self.invoke(SINGLE, Script([writes(self.root, ("a.txt", "A1\n")), {"content": "t1"},
                                    writes(self.root, ("b.txt", "B2\n"), ("new.txt", "N\n")), {"content": "t2"}]),
                    ["一", "二", "/exit"])
        self.assertEqual((self.root / "a.txt").read_text(), "A1\n")
        code, out, err = self.invoke(SINGLE, Script([]), ["/checkpoints", "/rewind 1", "/exit"])
        self.assertEqual(code, 0, err)
        self.assertIn("已撤回第 2、1 轮的文件改动", out)
        self.assert_restored()

    def test_usage_errors(self):
        code, out, err = self.invoke(SINGLE, Script([]), ["/undo", "/rewind", "/rewind zero", "/checkpoints extra", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("没有可撤销的检查点：这个对话还没有开始第一轮。", err)
        self.assertIn("Usage: /rewind <N> [--force]", err)
        self.assertIn("Usage: /checkpoints", err)


class WorkerCheckpointTests(CheckpointChatTestCase):
    def test_worker_write_belongs_to_the_main_turn_that_dispatched_it(self):
        gate = Gate(lambda messages: {"tool_calls": [write_call(self.root, "c1", "a.txt", "by w1\n")]})
        self.gates = [gate]
        model = Duplex(main=[delegate("d1", "change a.txt", allow_writes=True), {"content": "已派出 w1。"},
                             {"content": "在聊天。"}, {"content": "收到。"}],
                       worker=[gate, {"content": "wrote a.txt"}])

        def release(prompt):
            gate.release()
            return "/wait"

        code, out, err = self.invoke(MAIN, model, ["改 a.txt", "随便聊聊", release, "/checkpoints", "/undo", "/exit"])
        self.assertEqual(code, 0, err)
        (checkpoint,) = self.listing()
        self.assertEqual(checkpoint["turn"], 1)     # dispatched in turn 1, written during turn 2
        self.assertEqual(checkpoint["workers"], ["w1"])
        self.assertEqual(checkpoint["files"][0]["worker_id"], "w1")
        self.assertIn("修改 a.txt  (worker w1)", out)
        self.assertIn("已撤回第 1 轮的文件改动", out)
        self.assertEqual((self.root / "a.txt").read_text(), "A0\n")
        self.assertEqual(self.worker().metadata["main_turn"], 1)

    def test_undo_is_refused_while_a_writable_worker_is_active(self):
        gate = Gate(lambda messages: {"tool_calls": [write_call(self.root, "c1", "a.txt", "by w1\n")]})
        self.gates = [gate]
        model = Duplex(main=[delegate("d1", "change a.txt", allow_writes=True), {"content": "已派出 w1。"}],
                       worker=[gate, {"content": "wrote a.txt"}])

        def release(prompt):
            gate.release()
            return "/wait"

        code, out, err = self.invoke(MAIN, model, ["改 a.txt", "/undo", release, "/undo", "/exit"])
        self.assertEqual(code, 0, err)
        self.assertIn("撤销被拒绝：可写的 worker w1（运行中）还没结束。", err)
        self.assertIn("已撤回第 1 轮的文件改动", out)
        self.assertEqual((self.root / "a.txt").read_text(), "A0\n")


@unittest.skipUnless(HAS_TEXTUAL, NO_TEXTUAL)
class TuiCheckpointTests(CheckpointChatTestCase):
    def test_tui_checkpoints_then_undo(self):
        session = self.session(SINGLE, three_writes(self.root), ui="tui")

        async def drive():
            app = ChatApp(session, poll=0.02)
            async with app.run_test(size=(140, 40)) as pilot:
                async def type_line(text):
                    field = app.query_one("#input", Input)
                    field.focus()
                    field.value = text
                    await pilot.press("enter")

                async def until(predicate):
                    deadline = time.monotonic() + WAIT
                    while not predicate():
                        self.assertLess(time.monotonic(), deadline)
                        await pilot.pause(0.02)

                texts = lambda: [text for _, text in app.entries]
                await type_line("改三个文件")
                await until(lambda: "改好了 3 个文件" in texts() and not session.busy)
                await type_line("/checkpoints")
                await until(lambda: any("新建 new.txt" in text for text in texts()))
                await type_line("/undo")
                await until(lambda: any("已撤回第 1 轮" in text for text in texts()))

        asyncio.run(drive())
        self.assert_restored()


@unittest.skipUnless(HAS_WEB, NO_WEB)
class WebCheckpointTests(CheckpointChatTestCase):
    def test_web_checkpoints_then_undo(self):
        session = self.session(SINGLE, three_writes(self.root), ui="web")
        session.open()
        client = TestClient(create_app(session), base_url="http://127.0.0.1:8765")
        self.assertEqual(client.post("/messages", json={"text": "改三个文件"}).json(), {"result": "sent"})
        self.idle(session)
        self.assertEqual(client.post("/messages", json={"text": "/checkpoints"}).json(), {"result": "command"})
        self.assertEqual(client.post("/messages", json={"text": "/undo"}).json(), {"result": "command"})
        body = client.get("/events", params={"wait": 0, "after": "0"}).text
        notices = [json.loads(line[6:])["data"]["text"] for line in body.splitlines()
                   if line.startswith("data: ") and '"kind":"notice"' in line]
        self.assertTrue(any("新建 new.txt" in text for text in notices))
        self.assertTrue(any("已撤回第 1 轮的文件改动" in text for text in notices))
        self.assert_restored()
        wait_until(lambda: not session.busy)


if __name__ == "__main__":
    unittest.main()
