"""Offline ``--backend docker`` demo. Scripted fake model, no provider call, cost 0.

Drives the real chat entry (``baseagent --backend docker --mode full --no-main-agent``):

1. the agent writes ``hello.txt`` (inside the container);
2. runs a command that reads it back;
3. tries to reach http://example.com, which fails: the container has no network;
4. ``/undo`` removes ``hello.txt`` again (byte-exact undo through the container);
5. ``/exit`` removes the container.

Needs Docker and the default image (``docker pull python:3.12-slim``). Without Docker the CLI
stops before the model with exit code 3 and says why; the demo then prints that error and exits 3.
``--fake-docker`` runs the same flow on the offline fake Docker runner (no isolation, for CI).
The transcript goes to stdout; the last line is a JSON summary.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from baseagent.main import main  # noqa: E402
from baseagent.session import SessionStore  # noqa: E402
from baseagent.tools.workspace import Workspace  # noqa: E402

LINES = ["写一个 hello.txt，读回来，再试试能不能访问外网", "/undo", "/exit"]
NETWORK = "import urllib.request; urllib.request.urlopen('http://example.com', timeout=5); print('online')"


def _call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class Script:
    """Turn 1: write, read back with a command, try the network; then report."""

    def __init__(self, root):
        self.root = root
        self.calls = 0
        self.results = []

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            digest = Workspace(self.root).get_instructions("hello.txt")["digest"]
            return {"tool_calls": [
                _call("c1", "write_file", {"path": "hello.txt", "content": "hello from the container\n",
                                           "expected_sha256": "missing", "expected_instructions": digest}),
                _call("c2", "run_command", {"argv": ["sh", "-c", "cat hello.txt && uname -s"]}),
                _call("c3", "run_command", {"argv": ["python3", "-c", NETWORK], "timeout": 20})]}
        self.results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        blocked = not self.results[2]["ok"]
        return {"content": "写好了 hello.txt 并读回来了；访问外网" + ("失败：容器没有网络。" if blocked else "居然成功了。")}


def run(directory, *, fake=False, image="python:3.12-slim"):
    root = Path(directory).resolve() / "workspace"
    root.mkdir()
    db = root / ".baseagent" / "sessions.sqlite3"
    model = Script(root)
    lines = iter(LINES)
    runner = None
    factory = None
    if fake:
        from baseagent.backends import DockerBackend
        from baseagent.testing.docker import FakeDocker
        runner = FakeDocker(images=(image,))

        def factory(args, workspace_root, protected):
            return DockerBackend(workspace_root, image=image, protected_paths=protected, runner=runner)

    def typed(prompt=""):
        line = next(lines)
        print(f"{prompt}{line}", flush=True)
        return line

    errors = io.StringIO()
    with patch("builtins.input", side_effect=typed), contextlib.redirect_stderr(errors):
        code = main(["--root", str(root), "--db", str(db), "--session", "demo", "--backend", "docker",
                     "--docker-image", image, "--mode", "full", "--no-main-agent"],
                    model_factory=lambda args: model, backend_factory=factory)
    stderr = errors.getvalue()
    if code == 3:
        print(stderr.strip(), file=sys.stderr)
        error = next((line for line in stderr.splitlines() if line.startswith("Docker error")), stderr.strip())
        return {"exit_code": code, "status": "docker_unavailable", "error": error, "cost_cny": 0}
    container = next((line.rsplit("container ", 1)[-1] for line in stderr.splitlines() if line.startswith("Backend:")),
                     None)
    if runner is not None:
        removed = container in runner.removed() and not runner.containers
    else:
        listed = subprocess.run(["docker", "ps", "-aq", "-f", f"name={container}"], capture_output=True, text=True,
                                check=False)
        removed = listed.stdout.strip() == ""
    results = model.results
    checkpoints = SessionStore(db).checkpoints("demo")["checkpoints"]
    return {"exit_code": code, "docker": "fake" if fake else "real", "container": container,
            "file_written": bool(results and results[0]["ok"]),
            "command_output": (results[1].get("data") or {}).get("stdout", "").strip() if results else None,
            "network_blocked": bool(results and not results[2]["ok"]),
            "undo_restored": not (root / "hello.txt").exists() and bool(checkpoints and checkpoints[0]["restored"]),
            "container_removed": removed, "cost_cny": 0}


def main_demo(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake-docker", action="store_true", help="Use the offline fake Docker runner (no isolation)")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as directory:
        summary = run(directory, fake=args.fake_docker)
    print(json.dumps(summary, ensure_ascii=False))
    if summary["exit_code"] == 3:
        return 3
    ok = summary["file_written"] and summary["network_blocked"] and summary["undo_restored"] and summary["container_removed"]
    return 0 if ok and summary["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main_demo())
