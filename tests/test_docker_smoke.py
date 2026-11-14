"""Real-Docker smoke tests for ``--backend docker`` (AC 5, 6, 8, 10, 15). Fake models only.

Skipped when there is no ``docker`` command, the daemon is not reachable, or the image is not
available and cannot be pulled. Pulling the image is the only network use; the containers
themselves run with ``--network none``. Image: ``BASEAGENT_DOCKER_TEST_IMAGE`` (default
python:3.12-slim, the CLI default).
"""

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from checkpoint_helpers import Script, tool_call, write_call

from baseagent.main import main

IMAGE = os.getenv("BASEAGENT_DOCKER_TEST_IMAGE", "python:3.12-slim")
ROOT = Path(__file__).resolve().parents[1]


def docker_ready():
    """None when real Docker can run IMAGE, otherwise the reason to skip."""
    if not shutil.which("docker"):
        return "Docker CLI is not installed"
    try:
        if subprocess.run(["docker", "version"], capture_output=True, timeout=20).returncode != 0:
            return "Docker daemon is not reachable"
        if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=20).returncode != 0:
            if subprocess.run(["docker", "pull", IMAGE], capture_output=True, timeout=600).returncode != 0:
                return f"Docker image {IMAGE} is not available and could not be pulled"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Docker is not usable: {exc}"
    return None


REASON = docker_ready()


def containers(label_value):
    listed = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=baseagent.workspace={label_value}"],
                            capture_output=True, text=True, check=False)
    return listed.stdout.split()


@unittest.skipIf(REASON, REASON or "")
class DockerSmokeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name).resolve()
        self.root = self.parent / "work"
        self.root.mkdir()
        (self.root / "a.txt").write_text("A0\n")
        (self.parent / "secret.txt").write_text("host-secret\n")
        self.db = self.root / ".baseagent" / "sessions.sqlite3"
        from hashlib import sha256
        self.key = sha256(str(self.root).encode()).hexdigest()[:12]
        self.addCleanup(lambda: [subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                                 for name in containers(self.key)])

    def cli(self, argv, model, lines=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch("builtins.input", side_effect=[*lines, EOFError()]):
            code = main(["--root", str(self.root), "--db", str(self.db), "--backend", "docker", "--docker-image", IMAGE,
                         *argv], model_factory=lambda args: model)
        return code, stdout.getvalue(), stderr.getvalue()

    def command_results(self, session):
        from baseagent.session import SessionStore
        state = SessionStore(self.db).load(session)
        return [json.loads(message["content"]) for message in state.messages if message.get("role") == "tool"]

    def test_isolation_network_ownership_undo_and_cleanup(self):
        commands = [
            ["sh", "-c", f"cat {self.parent / 'secret.txt'}; ls /"],                      # AC 5: host path absent
            ["python", "-c", "import urllib.request; urllib.request.urlopen('http://example.com', timeout=5)"],  # AC 6
            ["sh", "-c", "mkdir -p made/deep && echo root > made/deep/file.txt && ln -s /etc/passwd link"],    # AC 8
            ["sh", "-c", "echo hacked > .baseagent/sessions.sqlite3; touch .git/hooks-test 2>&1; echo done"],
        ]
        (self.root / ".git").mkdir()
        model = Script([lambda messages: {"tool_calls": [
            write_call(self.root, "w1", "a.txt", "A1\n"),
            *[tool_call(f"c{index}", "run_command", {"argv": argv}) for index, argv in enumerate(commands)],
            tool_call("r1", "read_file", {"path": "link"}),
            tool_call("r2", "read_file", {"path": "../secret.txt"})]}, {"content": "done"}])
        code, out, err = self.cli(["--mode", "full", "--session", "smoke", "go"], model)
        self.assertEqual(code, 0, err)
        write, outside, network, created, harness, link, escape = self.command_results("smoke")
        self.assertTrue(write["ok"])
        self.assertNotIn("host-secret", outside["data"]["stdout"])
        self.assertNotIn(self.parent.name, outside["data"]["stdout"])
        self.assertFalse(network["ok"])                               # no network inside the container
        self.assertTrue(created["ok"], created)
        self.assertFalse(link["ok"])
        self.assertEqual(link["error"]["code"], "permission_denied")
        self.assertFalse(escape["ok"])
        # The container's harness directory is a tmpfs: the host database is intact.
        from baseagent.session import SessionStore
        self.assertIsNotNone(SessionStore(self.db).load("smoke"))
        self.assertFalse((self.root / ".git" / "hooks-test").exists())     # .git is read-only in the container
        if sys.platform.startswith("linux"):
            for path in (self.root / "made", self.root / "made" / "deep", self.root / "made" / "deep" / "file.txt"):
                self.assertEqual(path.lstat().st_uid, os.getuid(), path)   # AC 8: no root-owned leftovers
            shutil.rmtree(self.root / "made")
        self.assertEqual(containers(self.key), [])                         # AC 15: removed at normal end
        # AC 10: undo restores the exact bytes.
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["--root", str(self.root), "--db", str(self.db), "--session", "smoke",
                                   "--rewind", "last"]), 0)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"A0\n")

    def test_chat_undo_inside_the_container(self):
        model = Script([lambda messages: {"tool_calls": [write_call(self.root, "w1", "a.txt", "A1\n")]},
                        {"content": "ok"}])
        code, out, err = self.cli(["--mode", "edit", "--no-main-agent", "--session", "chat"], model,
                                  ["改", "/undo", "/exit"])
        self.assertEqual(code, 0, err)
        self.assertIn("已撤回第 1 轮的文件改动", out)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"A0\n")
        self.assertEqual(containers(self.key), [])

    def test_ctrl_c_exits_130_and_removes_the_container(self):
        script = textwrap.dedent(f"""
            import sys, time
            sys.path[:0] = [{str(ROOT / 'src')!r}, {str(ROOT / 'tests')!r}]
            from baseagent.main import main
            class Slow:
                def complete(self, messages, tools):
                    print("model-called", flush=True)
                    time.sleep(120)
            sys.exit(main(["--root", {str(self.root)!r}, "--db", {str(self.db)!r}, "--backend", "docker",
                           "--docker-image", {IMAGE!r}, "--session", "interrupt", "go"],
                          model_factory=lambda args: Slow()))
        """)
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "model-called")
            self.assertEqual(len(containers(self.key)), 1)
            process.send_signal(signal.SIGINT)
            code = process.wait(60)
        finally:
            if process.poll() is None:
                process.kill()
        self.assertEqual(code, 130)
        deadline = time.monotonic() + 10
        while containers(self.key) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(containers(self.key), [])


if __name__ == "__main__":
    unittest.main()
