"""Docker backend protocol tests. Real containers run only when Docker is present."""

import shutil
import tempfile
import unittest
from pathlib import Path

from baseagent.backends import DockerBackend, WorkspaceBackend
from baseagent.backends.docker import DockerError, explain_docker_failure
from baseagent.tools.result import ErrorCode, ToolFailure


class FakeRunner:
    def __init__(self, *, fail=""):
        self.fail = fail
        self.calls = []
        self.files = {}

    def run(self, argv, *, input_bytes=None, timeout=60):
        self.calls.append(argv)
        if self.fail == "daemon" and argv[1] == "run":
            return 1, b"", b"Cannot connect to the Docker daemon"
        if self.fail == "image" and argv[1] == "run":
            return 1, b"", b"Unable to find image 'missing:latest' locally"
        if argv[1] == "run":
            return 0, b"container-id\n", b""
        if argv[1] == "rm":
            return 0, b"baseagent-test\n", b""
        if argv[1] == "exec":
            command = argv[5:]
            if command[:2] == ["sh", "-c"] and command[2] == "cat \"$1\"":
                relative = command[4]
                if relative not in self.files:
                    return 1, b"", b"cat: can't open: No such file"
                return 0, self.files[relative], b""
            if command == ["echo", "ok"]:
                return 0, b"ok\n", b""
            if command[:1] == ["wget"]:
                return 1, b"", b"wget: bad address"
            return 1, b"", b"unexpected"
        if argv[1] == "cp":
            relative = argv[3].split("/workspace/", 1)[1]
            self.files[relative] = Path(argv[2]).read_bytes()
            return 0, b"", b""
        return 1, b"", b"unknown"


class DockerConfigTests(unittest.TestCase):
    def test_contract_defaults_to_no_network_and_rejects_bad_limits(self):
        root = Path(tempfile.mkdtemp())
        backend = DockerBackend(root, runner=FakeRunner(), start=False, name="baseagent-test")
        self.assertIsInstance(backend, WorkspaceBackend)
        self.assertEqual(backend.contract()["network"], "none")
        self.assertEqual(backend.contract()["isolation"], "container")
        with self.assertRaises(ValueError):
            DockerBackend(root, network="host", runner=FakeRunner(), start=False)
        self.assertEqual(explain_docker_failure(b"Cannot connect to the Docker daemon", image="python:3.12-slim"),
                         "Docker is not running")
        self.assertIn("missing", explain_docker_failure(b"Unable to find image 'missing:latest' locally", image="missing:latest"))

    def test_missing_daemon_and_image_are_explicit(self):
        root = Path(tempfile.mkdtemp())
        with self.assertRaises(DockerError) as caught:
            DockerBackend(root, runner=FakeRunner(fail="daemon"), name="baseagent-test")
        self.assertIn("not running", str(caught.exception))
        with self.assertRaises(DockerError) as missing:
            DockerBackend(root, image="missing:latest", runner=FakeRunner(fail="image"), name="baseagent-test")
        self.assertIn("missing", str(missing.exception))

    def test_escape_is_denied_before_a_container_call(self):
        root = Path(tempfile.mkdtemp())
        backend = DockerBackend(root, runner=FakeRunner(), start=False, name="baseagent-test")
        backend.container_id = "container-id"
        with self.assertRaises(ToolFailure) as caught:
            backend.read_bytes("../secret.txt")
        self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)
        self.assertEqual(backend.runner.calls, [])


@unittest.skipUnless(shutil.which("docker"), "Docker CLI is not installed")
class DockerIsolationTests(unittest.TestCase):
    def test_container_runs_blocks_network_and_is_removed(self):
        parent = Path(tempfile.mkdtemp())
        workspace = parent / "workspace"
        workspace.mkdir()
        (workspace / "hello.txt").write_text("hello\n", encoding="utf-8")
        (parent / "secret.txt").write_text("host-secret\n", encoding="utf-8")
        image = "nginx:alpine"
        try:
            backend = DockerBackend(workspace, image=image, name="baseagent-iso-test")
        except DockerError as exc:
            self.skipTest(str(exc))
        try:
            ran = backend.execute(["echo", "ok"], timeout=20)
            self.assertTrue(ran.ok, ran)
            self.assertIn("ok", ran.data["stdout"])
            self.assertEqual(backend.read_bytes("hello.txt").replace(b"\r\n", b"\n"), b"hello\n")
            outside = backend.execute(["sh", "-c", "cat /secret.txt"], timeout=20)
            self.assertFalse(outside.ok)
            self.assertNotIn("host-secret", outside.data["stdout"])
            blocked = backend.execute(["wget", "-q", "-T", "3", "-O", "-", "http://example.com"], timeout=20)
            self.assertFalse(blocked.ok)
        finally:
            name = backend.name
            backend.close()
        import subprocess
        remaining = subprocess.run(["docker", "ps", "-aq", "-f", f"name={name}"], capture_output=True, text=True, check=False)
        self.assertEqual(remaining.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
