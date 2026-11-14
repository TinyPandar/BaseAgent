"""Docker backend protocol tests. Real containers run only when Docker is present."""

import shutil
import tempfile
import unittest
from pathlib import Path

from baseagent.backends import DockerBackend, WorkspaceBackend
from baseagent.backends.docker import DockerError, explain_docker_failure
from baseagent.testing.docker import FakeDocker
from baseagent.tools.result import ErrorCode, ToolFailure


class FakeRunner(FakeDocker):
    """The offline fake Docker CLI with the two outage modes these tests need."""

    def __init__(self, *, fail=""):
        super().__init__(daemon=fail != "daemon", images=() if fail == "image" else ("python:3.12-slim",))


class DockerConfigTests(unittest.TestCase):
    def test_contract_defaults_to_no_network_and_rejects_bad_limits(self):
        root = Path(tempfile.mkdtemp())
        backend = DockerBackend(root, runner=FakeRunner(), start=False, name="baseagent-test")
        self.assertIsInstance(backend, WorkspaceBackend)
        self.assertEqual(backend.contract()["network"], "none")
        self.assertEqual(backend.contract()["isolation"], "container")
        with self.assertRaises(ValueError):
            DockerBackend(root, network="host", runner=FakeRunner(), start=False)
        daemon = explain_docker_failure(b"Cannot connect to the Docker daemon", image="python:3.12-slim")
        self.assertTrue(daemon.startswith("Docker daemon is not running"), daemon)
        self.assertIn("Start Docker", daemon)                      # says how to fix it (AC 13)
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
