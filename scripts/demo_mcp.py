"""Offline MCP demo. No provider calls, no network, cost 0.

Drives the real chat entry (``baseagent --mode edit --no-main-agent``) with scripted
input, a scripted fake model and the in-repo fake MCP server
(``tests/fixtures/fake_mcp_server.py``), in a temporary workspace with a ``.mcp.json``:

0. first use of the project ``.mcp.json``: the chat lists its servers (command, arguments, env key
   names) and asks ``Trust this .mcp.json and start these servers? [y/N]``; the demo answers ``y``
   (the trust record goes to a temporary ``BASEAGENT_CONFIG_HOME``, never the real one);
1. start-up lists the registered ``mcp__demo__…`` tools on stderr;
2. the agent calls the read-only ``mcp__demo__echo`` — no approval;
3. the agent calls ``mcp__demo__add_note`` — the chat asks ``Allow mcp__demo__add_note? [y/N]``,
   the note is written only after ``y``;
4. the server process is gone when the chat exits.

The transcript goes to stdout; the last line is a JSON summary.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from baseagent.main import main  # noqa: E402
from baseagent.session import SessionStore  # noqa: E402

SERVER = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"
LINES = ["用 MCP 的 echo 回显 hello，再记一条笔记：买牛奶", "/exit"]


def _call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class Script:
    def __init__(self):
        self.calls = 0
        self.offered = []

    def complete(self, messages, tools):
        self.calls += 1
        self.offered = sorted(tool["function"]["name"] for tool in tools if tool["function"]["name"].startswith("mcp__"))
        if self.calls == 1:
            return {"tool_calls": [_call("c1", "mcp__demo__echo", {"text": "hello"})]}
        if self.calls == 2:
            return {"tool_calls": [_call("c2", "mcp__demo__add_note", {"text": "买牛奶"})]}
        result = json.loads(messages[-1]["content"])
        return {"content": "echo 回了 hello；笔记" + ("已记下。" if result["ok"] else "没有记：" + result["error"]["message"])}


def _gone(pid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        time.sleep(0.05)
    return False


def run(directory, answer="y"):
    base = Path(directory).resolve()
    root, notes, pid_file = base / "workspace", base / "notes.txt", base / "server.pid"
    root.mkdir()
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"demo": {
        "command": sys.executable, "args": [str(SERVER), "--notes", str(notes), "--pid-file", str(pid_file)],
        "tools": {"echo": {"readOnly": True}}}}}, indent=2), encoding="utf-8")
    model = Script()
    lines = iter(LINES)
    prompts = []

    def typed(prompt=""):
        prompts.append(prompt)
        line = answer if prompt.startswith(("Allow ", "Trust ")) else next(lines)
        print(f"{prompt}{line}", flush=True)
        return line

    stderr = io.StringIO()
    # The demo stands in for a person at a terminal; trust is stored under the temporary directory.
    with patch("builtins.input", side_effect=typed), contextlib.redirect_stderr(stderr), \
         patch("baseagent.mcp.trust.stdin_is_terminal", lambda: True), \
         patch.dict(os.environ, {"BASEAGENT_CONFIG_HOME": str(base / "config")}):
        code = main(["--root", str(root), "--db", str(base / "state" / "sessions.sqlite3"), "--session", "demo",
                     "--mode", "edit", "--no-main-agent"], model_factory=lambda args: model)
    startup = [line for line in stderr.getvalue().splitlines() if line.startswith(("MCP", "  - ", "      env keys"))]
    for line in startup:
        print("[stderr] " + line.replace(str(base), "<tmp>").replace(sys.executable, "python").replace(str(ROOT), "<repo>"))
    state = SessionStore(base / "state" / "sessions.sqlite3").load("demo")
    results = [json.loads(message["content"]) for message in state.messages if message.get("role") == "tool"]
    return {"exit_code": code, "mcp_tools": model.offered,
            "trust_prompts": [prompt for prompt in prompts if prompt.startswith("Trust ")],
            "approval_prompts": [prompt for prompt in prompts if prompt.startswith("Allow ")],
            "echo": results[0]["data"]["text"] if results and results[0]["ok"] else None,
            "note_written": notes.exists() and notes.read_text(encoding="utf-8") == "买牛奶\n",
            "server_stopped": _gone(int(pid_file.read_text(encoding="utf-8")))}


def main_demo():
    with tempfile.TemporaryDirectory() as directory:
        summary = run(directory)
    print(json.dumps(summary, ensure_ascii=False))
    ok = summary["exit_code"] == 0 and summary["note_written"] and summary["server_stopped"] and summary["echo"] == "hello"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main_demo())
