"""Regenerate the TUI screenshots in docs/images/ offline. No provider calls, cost 0.

Runs the real ``baseagent --ui tui`` composition with a scripted fake model in a
temporary workspace, drives it with Textual's run_test() pilot and saves SVG
screenshots:

- docs/images/tui-approval.svg: three workers (two read-only, one writer); the
  writer's write_file call waits in the approval modal;
- docs/images/tui-workers.svg: after the approval; w2 and w3 pushed results,
  w1 is still running and shows in the worker panel.

Needs the optional 'tui' extra:  uv sync --extra tui && python scripts/ui_screenshots.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from baseagent.agent.cancellation import Cancelled  # noqa: E402
from baseagent.main import main  # noqa: E402
from baseagent.tools.workspace import Workspace  # noqa: E402

WAIT = 30
SIZE = (150, 38)


def _call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class Gate:
    """Hold one scripted worker response until released; cancellation ends the wait."""

    def __init__(self, response):
        self.response = response
        self.opened = threading.Event()

    def wait(self, cancellation):
        deadline = time.monotonic() + WAIT
        while not self.opened.wait(0.01):
            if cancellation is not None and cancellation.cancelled():
                raise Cancelled()
            if time.monotonic() > deadline:
                raise RuntimeError("screenshot gate was never released")
        return self.response


class Script:
    """Main-agent calls carry delegate_task; worker calls are keyed by their task text."""

    def __init__(self, main, workers):
        self.main = iter(main)
        self.workers = {task: iter(steps) for task, steps in workers.items()}
        self.lock = threading.Lock()

    def complete_with_control(self, messages, tools, *, cancellation=None, timeout=None):
        names = [tool["function"]["name"] for tool in tools]
        with self.lock:
            if "delegate_task" in names:
                response = next(self.main)
            else:
                task = next(message["content"] for message in messages if message["role"] == "user")
                response = next(self.workers[task])
        return response.wait(cancellation) if isinstance(response, Gate) else response


async def until(pilot, predicate, what):
    deadline = time.monotonic() + WAIT
    while not predicate():
        if time.monotonic() > deadline:
            raise RuntimeError(f"timed out waiting for {what}")
        await pilot.pause(0.05)


def run(out_dir=ROOT / "docs" / "images"):
    from baseagent.tui.app import ApprovalScreen, ChatApp, ExitScreen

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "app.py").write_text("def main():\n    return 'hello'\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_app.py").write_text("from app import main\n\n\ndef test_main():\n    assert main()\n",
                                                   encoding="utf-8")
        (root / "policy.json").write_text(json.dumps({"default": "allow", "tools": {"write_file": "ask"}}),
                                          encoding="utf-8")
        digest = Workspace(root).get_instructions("VERSION")["digest"]
        entry, tests, version = ("Find the CLI entry point", "Check whether this repository has tests",
                                 "Write 1.0 into VERSION")
        slow = Gate({"content": "never reached: cancelled at exit"})
        model = Script(
            main=[{"tool_calls": [_call("d1", "delegate_task", {"task": entry}),
                                  _call("d2", "delegate_task", {"task": tests}),
                                  _call("d3", "delegate_task", {"task": version, "allow_writes": True})]},
                  {"content": "已派出 w1（找入口）、w2（查测试）两个只读 worker，w3 写 VERSION（需要你审批）。可以继续聊。"}],
            workers={
                entry: [{"tool_calls": [_call("g1", "grep", {"pattern": "def main"})]}, slow],
                tests: [{"tool_calls": [_call("t1", "glob", {"pattern": "tests/*.py"})]},
                        {"content": "Found one test file, tests/test_app.py:1-5, testing app.main."}],
                version: [{"tool_calls": [_call("v1", "write_file", {
                    "path": "VERSION", "content": "1.0\n", "expected_sha256": "missing",
                    "expected_instructions": digest})]}, {"content": "Wrote VERSION (1.0)."}]})
        captured = {}

        def capture(session):
            captured["session"] = session
            return 0

        with patch("baseagent.tui.app.run_tui", capture):
            code = main(["--root", str(root), "--db", str(root / ".state" / "sessions.sqlite3"), "--session", "demo",
                         "--max-workers", "3", "--allow-write", "--tool-policy", str(root / "policy.json"), "--ui", "tui"],
                        model_factory=lambda args: model)
        if code:
            raise SystemExit(code)
        session = captured["session"]

        async def drive():
            app = ChatApp(session)
            async with app.run_test(size=SIZE) as pilot:
                field = app.query_one("#input")
                field.value = "找一下入口和测试，再把 VERSION 写成 1.0"
                await pilot.press("enter")
                await until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.screen.query("#allow"),
                            "the approval modal")
                await until(pilot, lambda: any("[w2 已完成]" in text for _, text in app.entries), "w2's push")
                await pilot.pause(0.3)
                app.save_screenshot("tui-approval.svg", str(out_dir))
                await pilot.press("y")
                await until(pilot, lambda: any("[w3 已完成]" in text for _, text in app.entries), "w3's push")
                field.focus()
                field.value = "w1 还在跑吗？"
                await pilot.pause(1.2)
                app.save_screenshot("tui-workers.svg", str(out_dir))
                field.value = ""
                await pilot.press("ctrl+q")
                await until(pilot, lambda: isinstance(app.screen, ExitScreen), "the exit question")
                await pilot.press("c")
                await until(pilot, lambda: session.closed, "exit")

        asyncio.run(drive())
        print(json.dumps({"written": ["docs/images/tui-approval.svg", "docs/images/tui-workers.svg"],
                          "version_file": (root / "VERSION").read_text(encoding="utf-8").strip(),
                          "w1": session.workers.get("w1").status}, ensure_ascii=False))


if __name__ == "__main__":
    run()
