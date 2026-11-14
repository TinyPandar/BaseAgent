"""Offline demo: project setup on first use. No provider calls, no network, cost 0.

Drives the real chat entry (``baseagent --no-main-agent``) with scripted input, a scripted fake
model and the in-repo fake MCP server, in a temporary workspace that has an ``AGENTS.md`` and a
``.mcp.json``:

1. first use of the project ``.mcp.json``: the chat lists the server (command, arguments, env key
   names - never values) and asks ``Trust this .mcp.json and start these servers? [y/N]``; the demo
   answers ``y`` (the trust record goes to a temporary ``BASEAGENT_CONFIG_HOME``);
2. ``/status`` shows ``Project rules: AGENTS.md loaded (… bytes)`` and the turn start time in UTC;
3. a turn stops at its step limit (``--max-steps 3``) and ``/continue`` finishes it in the chat,
   without repeating the tool calls that already ran;
4. a second chat in the same workspace starts the server without asking again.

The transcript goes to stdout; the last line is a JSON summary.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from baseagent.main import main  # noqa: E402
from baseagent.session import SessionStore  # noqa: E402

SERVER = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"
RULES = "# 项目约定\n- 代码用四个空格缩进。\n- 提交信息用中文。\n"


class Script:
    """Turn 1 answers; turn 2 lists files four times (one step too many for --max-steps 3), then answers."""

    def __init__(self):
        self.calls = 0
        self.saw_rules = False
        self.mcp_tools = []

    def complete(self, messages, tools):
        self.calls += 1
        self.saw_rules = self.saw_rules or any("代码用四个空格缩进" in str(m.get("content")) for m in messages)
        self.mcp_tools = sorted(t["function"]["name"] for t in tools if t["function"]["name"].startswith("mcp__"))
        if self.calls == 1:
            return {"content": "你好！我读到了项目约定：四个空格缩进、中文提交信息。"}
        if 2 <= self.calls <= 5:
            index = self.calls - 1
            return {"tool_calls": [{"id": f"g{index}", "type": "function", "function": {
                "name": "glob", "arguments": json.dumps({"pattern": f"*{index}*"})}}]}
        return {"content": "四次查找都做完了。"}


def chat(root, base, lines, model, prompts, extra=()):
    lines = iter(lines)

    def typed(prompt=""):
        prompts.append(prompt)
        line = "y" if prompt.startswith("Trust ") else next(lines)
        print(f"{prompt}{line}", flush=True)
        return line

    stderr = io.StringIO()
    with patch("builtins.input", side_effect=typed), contextlib.redirect_stderr(stderr), \
         patch("baseagent.mcp.trust.stdin_is_terminal", lambda: True), \
         patch.dict(os.environ, {"BASEAGENT_CONFIG_HOME": str(base / "config")}):
        code = main(["--root", str(root), "--db", str(base / "state" / "sessions.sqlite3"), "--session", "demo",
                     "--no-main-agent", *extra], model_factory=lambda args: model)
    for line in stderr.getvalue().splitlines():
        if line.startswith(("MCP", "  - ", "      env keys", "Step limit")) or "/continue" in line:
            print("[stderr] " + line.replace(str(base), "<tmp>").replace(sys.executable, "python")
                  .replace(str(ROOT), "<repo>"))
    return code


def run(directory):
    base = Path(directory).resolve()
    root = base / "workspace"
    root.mkdir()
    (root / "AGENTS.md").write_text(RULES, encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"demo": {
        "command": sys.executable, "args": [str(SERVER), "--pid-file", str(base / "server.pid")],
        "env": {"DEMO_TOKEN": "not-shown"}, "tools": {"echo": {"readOnly": True}}}}}, indent=2), encoding="utf-8")
    model, prompts = Script(), []
    print("== 第一次进入：先问是否信任 .mcp.json ==")
    first = chat(root, base, ["你好", "/status", "把名字里带 1-4 的文件各找一遍", "/continue", "/exit"],
                 model, prompts, extra=["--max-steps", "3"])
    print("== 第二次进入：同一个 .mcp.json 不再问 ==")
    again_prompts = []
    second = chat(root, base, ["/exit"], Script(), again_prompts)
    state = SessionStore(base / "state" / "sessions.sqlite3").load("demo")
    return {"exit_codes": [first, second],
            "trust_prompts": [p for p in prompts if p.startswith("Trust ")],
            "trust_prompts_second_run": [p for p in again_prompts if p.startswith("Trust ")],
            "model_saw_rules": model.saw_rules, "mcp_tools": model.mcp_tools,
            "turn_status": str(state.status), "steps": f"{state.step}/{state.max_steps}",
            "tool_calls": state.tool_calls}


def main_demo():
    with tempfile.TemporaryDirectory() as directory:
        summary = run(directory)
    print(json.dumps(summary, ensure_ascii=False))
    ok = (summary["exit_codes"] == [0, 0] and len(summary["trust_prompts"]) == 1
          and not summary["trust_prompts_second_run"] and summary["model_saw_rules"]
          and summary["turn_status"] == "completed" and summary["tool_calls"] == 4)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main_demo())
