"""Offline checkpoint/undo demo. No provider calls, cost 0.

Drives the real chat entry (``baseagent --mode edit --no-main-agent``) with scripted
input and a scripted fake model, in a temporary git repository:

1. the agent changes 3 files (two edits, one new file) through write_file;
2. ``/checkpoints`` lists turn 1 with the three files;
3. ``/undo`` restores the two files byte for byte and deletes the new one;
4. the next turn starts with the note that turn 1 was undone; the agent calls
   ``git_status`` and reports a clean working tree.

The transcript goes to stdout; the last line is a JSON summary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from baseagent.main import main  # noqa: E402
from baseagent.session import SessionStore  # noqa: E402
from baseagent.tools.workspace import Workspace  # noqa: E402

FILES = {"app.py": 'print("hello")\n', "README.md": "# Demo\n\nPrints hello.\n"}
LINES = ["把 app.py 的问候改成中文，README 跟着改，再加一份 notes.md", "/checkpoints", "/undo",
         "现在 git 状态干净吗？", "/exit"]


def _call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _write(root, identifier, path, content):
    target = root / path
    expected = sha256(target.read_bytes()).hexdigest() if target.exists() else "missing"
    return _call(identifier, "write_file", {"path": path, "content": content, "expected_sha256": expected,
                                            "expected_instructions": Workspace(root).get_instructions(path)["digest"]})


class Script:
    """Turn 1 writes three files; turn 2 checks git status and reads its result."""

    def __init__(self, root):
        self.root = root
        self.calls = 0
        self.notice_seen = False
        self.git_status = None

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {"tool_calls": [_write(self.root, "c1", "app.py", 'print("你好")\n'),
                                   _write(self.root, "c2", "README.md", "# Demo\n\n打印「你好」。\n"),
                                   _write(self.root, "c3", "notes.md", "- 问候改成了中文\n")]}
        if self.calls == 2:
            return {"content": "改好了：app.py、README.md，并新建了 notes.md。"}
        if self.calls == 3:
            self.notice_seen = any(message["role"] == "system" and "第 1 轮的文件改动已撤回" in message["content"]
                                   for message in messages)
            return {"tool_calls": [_call("c4", "git_status", {})]}
        self.git_status = json.loads(messages[-1]["content"])
        data = self.git_status.get("data") or {}
        if data.get("clean"):
            return {"content": f"干净：分支 {data.get('branch')} 上没有改动，第 1 轮的修改已经撤回。"}
        return {"content": "还有改动：" + json.dumps(data.get("entries"), ensure_ascii=False)}


def _git(root, *argv):
    env = {**os.environ, "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.invalid",
           "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.invalid"}
    subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True, env=env)


def run(directory):
    root = Path(directory).resolve() / "workspace"
    root.mkdir()
    for name, text in FILES.items():
        (root / name).write_text(text, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "demo")
    db = Path(directory).resolve() / "state" / "sessions.sqlite3"
    model = Script(root)
    lines = iter(LINES)

    def typed(prompt=""):
        line = next(lines)
        print(f"{prompt}{line}", flush=True)
        return line

    with patch("builtins.input", side_effect=typed):
        code = main(["--root", str(root), "--db", str(db), "--session", "demo", "--mode", "edit", "--no-main-agent"],
                    model_factory=lambda args: model)
    checkpoints = SessionStore(db).checkpoints("demo")["checkpoints"]
    restored = all((root / name).read_text(encoding="utf-8") == text for name, text in FILES.items())
    return {"exit_code": code, "files_restored": restored and not (root / "notes.md").exists(),
            "checkpoints": [{"turn": item["turn"], "files": [f"{entry['change']} {entry['path']}" for entry in item["files"]],
                             "restored": item["restored"]} for item in checkpoints],
            "undo_notice_seen_by_agent": model.notice_seen,
            "git_clean": bool(model.git_status and (model.git_status.get("data") or {}).get("clean"))}


def main_demo():
    if shutil.which("git") is None:
        print(json.dumps({"skipped": "git is not installed"}))
        return 0
    with tempfile.TemporaryDirectory() as directory:
        summary = run(directory)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["files_restored"] and summary["git_clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main_demo())
