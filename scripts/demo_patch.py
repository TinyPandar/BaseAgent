"""Offline apply_patch demo. No provider calls, cost 0, no git needed.

Drives the real chat entry (``baseagent --mode edit --no-main-agent``) with scripted input
and a scripted fake model, in a temporary workspace:

1. one ``apply_patch`` call changes three files at once: modify ``app.py`` (edit_file-style
   ``edits``), add ``notes.md`` (full content, ``expected_sha256="missing"``), delete ``old.txt``;
   no approval stop in ``--mode edit``; the whole-batch unified diff prints under the result;
2. ``/checkpoints`` lists the batch in turn 1 (modified / created / deleted);
3. ``/undo`` restores all three in one step, byte for byte.

The transcript goes to stdout (tool trace and diff to stderr); the last stdout line is a JSON summary.
"""

from __future__ import annotations

import contextlib
import io
import json
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

FILES = {"app.py": 'def greet():\n    return "hello"\n', "old.txt": "obsolete\n"}
LINES = ["问候改成中文，加一份 notes.md，删掉 old.txt", "/checkpoints", "/undo", "/exit"]


def _hash(path):
    return sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


class Script:
    """Turn 1: one apply_patch call with three operations, then a short answer."""

    def __init__(self, root):
        self.root = root
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            digest = Workspace(self.root).get_instructions()["digest"]
            operations = [
                {"op": "modify", "path": "app.py", "expected_sha256": _hash(self.root / "app.py"),
                 "expected_instructions": digest, "edits": [{"old_text": '"hello"', "new_text": '"你好"'}]},
                {"op": "add", "path": "notes.md", "expected_sha256": "missing", "expected_instructions": digest,
                 "content": "- 问候改成了中文\n- 删除了 old.txt\n"},
                {"op": "delete", "path": "old.txt", "expected_sha256": _hash(self.root / "old.txt"),
                 "expected_instructions": digest},
            ]
            return {"tool_calls": [{"id": "p1", "type": "function", "function": {
                "name": "apply_patch", "arguments": json.dumps({"operations": operations}, ensure_ascii=False)}}]}
        return {"content": "一次 apply_patch 改好了三个文件：app.py、notes.md、old.txt。"}


class Tee(io.StringIO):
    """Keep stderr live on the terminal and remember it for the summary."""

    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    def write(self, text):
        self.stream.write(text)
        self.stream.flush()
        return super().write(text)


def run(directory):
    root = Path(directory).resolve() / "workspace"
    root.mkdir()
    for name, text in FILES.items():
        (root / name).write_text(text, encoding="utf-8")
    db = Path(directory).resolve() / "state" / "sessions.sqlite3"
    model = Script(root)
    lines = iter(LINES)
    after = {}

    def typed(prompt=""):
        line = next(lines)
        if line == "/checkpoints":
            after.update({name: (root / name).exists() for name in ("app.py", "notes.md", "old.txt")})
        print(f"{prompt}{line}", flush=True)
        return line

    trace = Tee(sys.stderr)
    with patch("builtins.input", side_effect=typed), contextlib.redirect_stderr(trace):
        code = main(["--root", str(root), "--db", str(db), "--session", "demo", "--mode", "edit", "--no-main-agent"],
                    model_factory=lambda args: model)
    checkpoints = SessionStore(db).checkpoints("demo")["checkpoints"]
    restored = all((root / name).read_text(encoding="utf-8") == text for name, text in FILES.items())
    return {"exit_code": code, "model_calls": model.calls,
            "diff_printed": "+++ b/notes.md" in trace.getvalue() and "--- a/old.txt" in trace.getvalue(),
            "after_patch": after,
            "checkpoints": [{"turn": item["turn"], "files": [f"{entry['change']} {entry['path']}" for entry in item["files"]],
                             "restored": item["restored"]} for item in checkpoints],
            "files_restored": restored and not (root / "notes.md").exists()}


def main_demo():
    with tempfile.TemporaryDirectory() as directory:
        summary = run(directory)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["files_restored"] and summary["diff_printed"] else 1


if __name__ == "__main__":
    raise SystemExit(main_demo())
