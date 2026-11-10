"""Offline TUI/Web demo with a scripted fake model. No provider calls, cost 0.

Runs the real ``baseagent --ui tui`` / ``--ui web`` entry on a fixed demo workspace
(``.baseagent/demo-ui`` by default) and session ``demo``, so you can start in the
TUI and continue the same session in the browser:

    python scripts/demo_ui.py --fresh            # TUI; Ctrl+Q to leave
    python scripts/demo_ui.py --ui web           # same session at http://127.0.0.1:8765/

The fake main agent answers the first message by starting two workers:
w1 writes VERSION (allow_writes, so write_file waits for your approval) and
w2 is a slow read-only scan (one step every few seconds) that you can cancel.
Later messages get a short reply that relays any pushed worker results.
Needs the optional extras:  uv sync --extra frontends
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from baseagent.agent.cancellation import Cancelled  # noqa: E402
from baseagent.agent.workers import PUSH_HEADER, USER_HEADER  # noqa: E402
from baseagent.main import main  # noqa: E402
from baseagent.tools.workspace import Workspace  # noqa: E402

VERSION_TASK = "Write 1.0 into the VERSION file"
SCAN_TASK = "Scan every file in the workspace for TODO markers, one directory at a time"
STEP_SECONDS = 3.0


def _call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class DemoModel:
    """Deterministic stand-in for the provider. Thread-safe; main-agent calls carry delegate_task."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.lock = threading.Lock()
        self.counter = 0

    def _id(self, prefix):
        with self.lock:
            self.counter += 1
            return f"{prefix}{self.counter}-{int(time.time() * 1000) % 100000}"

    def complete_with_control(self, messages, tools, *, cancellation=None, timeout=None):
        names = {tool["function"]["name"] for tool in tools}
        if "delegate_task" in names:
            return self._main(messages)
        task = next(message["content"] for message in messages if message["role"] == "user")
        return self._worker(task, messages, cancellation)

    def _main(self, messages):
        last = messages[-1]
        if last["role"] == "tool":
            return {"content": "已派出 w1（写 VERSION，需要你审批）和 w2（只读扫描，比较慢）。两个都在后台跑，你可以继续聊。"}
        text = last.get("content") or ""
        pushes = []
        if text.startswith(PUSH_HEADER):
            body, _, text = text.partition(f"\n{USER_HEADER}\n")
            for line in body.splitlines()[1:]:
                try:
                    pushes.append(json.loads(line))
                except ValueError:
                    pass
        if not any(message["role"] == "assistant" for message in messages):
            return {"tool_calls": [_call(self._id("d"), "delegate_task", {"task": VERSION_TASK, "allow_writes": True}),
                                   _call(self._id("d"), "delegate_task", {"task": SCAN_TASK})]}
        if pushes:
            parts = [f"{item['task_id']} {item['status']}" + (f"：{item['summary']}" if item.get("summary") else "")
                     for item in pushes]
            return {"content": "（离线演示模型）收到 Worker 推送：" + "；".join(parts)}
        return {"content": f"（离线演示模型）收到：{text.strip()[:80]}。Worker 的进度看右侧面板。"}

    def _worker(self, task, messages, cancellation):
        steps = sum(1 for message in messages if message["role"] == "assistant")
        if task == VERSION_TASK:
            if steps == 0:
                digest = Workspace(self.workspace).get_instructions("VERSION")["digest"]
                return {"tool_calls": [_call(self._id("v"), "write_file", {
                    "path": "VERSION", "content": "1.0\n", "expected_sha256": "missing",
                    "expected_instructions": digest})]}
            result = json.loads(messages[-1]["content"])
            return {"content": "Wrote VERSION (1.0). VERSION:1-1" if result.get("ok")
                    else "The write was denied; VERSION is unchanged."}
        deadline = time.monotonic() + STEP_SECONDS
        while time.monotonic() < deadline:
            if cancellation is not None and cancellation.cancelled():
                raise Cancelled()
            time.sleep(0.05)
        return {"tool_calls": [_call(self._id("s"), "glob", {"pattern": f"**/*{steps}*"})]}


def prepare(directory, fresh):
    if fresh and directory.exists():
        shutil.rmtree(directory)
    workspace = directory / "workspace"
    if not workspace.exists():
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "app.py").write_text("def main():\n    return 'hello'  # TODO: real CLI\n", encoding="utf-8")
        (workspace / "tests").mkdir()
        (workspace / "tests" / "test_app.py").write_text(
            "from src.app import main\n\n\ndef test_main():\n    assert main()\n", encoding="utf-8")
    policy = directory / "policy.json"
    policy.write_text(json.dumps({"default": "allow", "tools": {"write_file": "ask"}}), encoding="utf-8")
    return workspace, policy


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ui", choices=["tui", "web"], default="tui")
    parser.add_argument("--dir", type=Path, default=ROOT / ".baseagent" / "demo-ui")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fresh", action="store_true", help="delete the demo workspace and session first")
    args = parser.parse_args(argv)
    workspace, policy = prepare(args.dir.resolve(), args.fresh)
    model = DemoModel(workspace)
    options = ["--root", str(workspace), "--db", str(args.dir.resolve() / "sessions.sqlite3"), "--session", "demo",
               "--max-workers", "2", "--allow-write", "--tool-policy", str(policy), "--ui", args.ui]
    if args.ui == "web":
        options += ["--port", str(args.port)]
    return main(options, model_factory=lambda parsed: model)


if __name__ == "__main__":
    raise SystemExit(run())
