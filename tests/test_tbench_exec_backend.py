"""ExecBackend over a fake Harbor environment: no Docker, no Harbor, no provider."""

import os
import threading
import time
import unittest
from hashlib import sha256
from pathlib import Path, PurePosixPath

from tbench_helpers import LoopThread, MappedShellEnvironment, ShellEnvironment, temporary_workdir

from baseagent.backends import LocalBackend
from baseagent.backends.exec_backend import EnvironmentUnavailable, ExecBackend
from baseagent.kernel.cancellation import CancellationToken, Cancelled
from baseagent.tbench.channel import AsyncExecChannel
from baseagent.tools.result import ErrorCode, ToolFailure
from baseagent.tools.workspace import Workspace, coding_tools


def _timeout_maximum(registry):
    spec = next(spec for spec in registry.specs() if spec["function"]["name"] == "run_command")
    return spec["function"]["parameters"]["properties"]["timeout"]["maximum"]


class ExecBackendTests(unittest.TestCase):
    def setUp(self):
        self.workdir = temporary_workdir(self)
        self.env = ShellEnvironment(self.workdir)
        self.loop_thread = LoopThread()
        self.loop = self.loop_thread.__enter__()
        self.addCleanup(self.loop_thread.__exit__, None, None, None)
        self.token = CancellationToken()
        self.backend = ExecBackend(AsyncExecChannel(self.env, self.loop), str(self.workdir), cancellation=self.token)

    def assertFailure(self, code, function, *args):
        with self.assertRaises(ToolFailure) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_read_write_and_compare_and_swap(self):
        digest = self.backend.get_instructions("notes/a.txt")["digest"]
        written = self.backend.write_file("notes/a.txt", "first 'q' \"d\"\n", "missing", digest)
        self.assertEqual(written["sha256"], sha256("first 'q' \"d\"\n".encode()).hexdigest())
        self.assertEqual(written["instruction_digest"], digest)
        self.assertEqual((self.workdir / "notes/a.txt").read_text(), "first 'q' \"d\"\n")
        self.assertEqual(self.backend.read_bytes("notes/a.txt"), b"first 'q' \"d\"\n")
        os.chmod(self.workdir / "notes/a.txt", 0o750)
        self.backend.write_file("notes/a.txt", "second\n", written["sha256"], digest)
        self.assertEqual((self.workdir / "notes/a.txt").read_text(), "second\n")
        self.assertEqual(os.stat(self.workdir / "notes/a.txt").st_mode & 0o777, 0o750)
        # Stale version, and create-only on an existing file, are both conflicts that write nothing.
        self.assertFailure(ErrorCode.CONFLICT, self.backend.write_file, "notes/a.txt", "x", written["sha256"], digest)
        self.assertFailure(ErrorCode.CONFLICT, self.backend.write_file, "notes/a.txt", "x", "missing", digest)
        self.assertFailure(ErrorCode.CONFLICT, self.backend.write_file, "notes/a.txt", "x",
                           self.backend.file_hash("notes/a.txt"), "0" * 64)
        self.assertEqual((self.workdir / "notes/a.txt").read_text(), "second\n")
        current = self.backend.file_hash("notes/a.txt")
        self.assertFailure(ErrorCode.CONFLICT, self.backend.delete_file, "notes/a.txt", "0" * 64, None)
        self.assertEqual(self.backend.delete_file("notes/a.txt", current, None)["sha256"], "missing")
        self.assertFalse((self.workdir / "notes/a.txt").exists())
        # No temporary or framework files are left in the workspace.
        self.assertEqual([path.name for path in self.workdir.rglob("*")], ["notes"])

    def test_large_content_is_staged_in_chunks(self):
        content = "".join(f"line {index:06d} {'x' * 40}\n" for index in range(3000))   # ~150 KB
        self.backend.write_file("big.txt", content, "missing", None)
        self.assertEqual((self.workdir / "big.txt").read_text(), content)
        self.assertEqual(self.backend.read_bytes("big.txt"), content.encode())

    def test_missing_file_and_limits(self):
        self.assertFailure(ErrorCode.NOT_FOUND, self.backend.read_bytes, "absent.txt")
        self.assertEqual(self.backend.file_hash("absent.txt"), "missing")
        self.assertFailure(ErrorCode.CONFLICT, self.backend.delete_file, "absent.txt", "0" * 64, None)
        (self.workdir / "big.bin").write_bytes(b"a" * 1001)
        self.assertFailure(ErrorCode.LIMIT_EXCEEDED, self.backend.read_bytes, "big.bin", 1000)
        self.assertEqual(len(self.backend.read_bytes("big.bin", 1001)), 1001)
        self.assertFailure(ErrorCode.LIMIT_EXCEEDED, self.backend.write_file, "huge.txt", "x" * 200_001, "missing", None)
        result = self.backend.execute(["sh", "-c", "head -c 5000 /dev/zero | tr '\\0' a; echo err >&2"], timeout=10,
                                      output_limit=100)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["stdout"], "a" * 100)
        self.assertTrue(result.data["stdout_truncated"])
        self.assertFalse(result.data["stderr_truncated"])

    def test_path_escapes_and_protected_paths(self):
        outside = self.workdir.parent / "outside.txt"
        outside.write_text("secret")
        for path in ("../outside.txt", "/etc/passwd", "a/../../outside.txt", str(outside)):
            self.assertFailure(ErrorCode.PERMISSION_DENIED, self.backend.read_bytes, path)
            self.assertFailure(ErrorCode.PERMISSION_DENIED, self.backend.write_file, path, "x", None, None)
        for path in (".git/config", ".env", ".env.local", "sub/.baseagent/x", ".venv/bin/python"):
            self.assertFailure(ErrorCode.PERMISSION_DENIED, self.backend.read_bytes, path)
            self.assertFailure(ErrorCode.PERMISSION_DENIED, self.backend.write_file, path, "x", "missing", None)
        # Links that leave the workspace are refused inside the environment.
        (self.workdir / "link.txt").symlink_to(outside)
        (self.workdir / "dirlink").symlink_to(self.workdir.parent)
        self.assertFailure(ErrorCode.PERMISSION_DENIED, self.backend.read_bytes, "link.txt")
        self.assertFailure(ErrorCode.PERMISSION_DENIED, self.backend.read_bytes, "dirlink/outside.txt")
        self.assertFailure(ErrorCode.PERMISSION_DENIED, self.backend.write_file, "dirlink/new.txt", "x", "missing", None)
        self.assertEqual(outside.read_text(), "secret")
        self.assertFalse((self.workdir.parent / "new.txt").exists())
        # An absolute path inside the workspace is the same file as the relative one.
        (self.workdir / "inside.txt").write_text("in")
        self.assertEqual(self.backend.read_bytes(str(self.workdir / "inside.txt")), b"in")

    def test_commands_run_in_the_workdir_with_literal_arguments(self):
        result = self.backend.execute(["pwd", "-P"], timeout=10)
        self.assertEqual(result.data["stdout"].strip(), str(self.workdir))
        argv = ["printf", "<%s>", "a b", "it's", '"double"', "$HOME", "a; touch injected", "*"]
        result = self.backend.execute(argv, timeout=10)
        self.assertEqual(result.data["stdout"], "<a b><it's><\"double\"><$HOME><a; touch injected><*>")
        self.assertFalse((self.workdir / "injected").exists())
        failed = self.backend.execute(["sh", "-c", "exit 3"], timeout=10)
        self.assertEqual(failed.error.code, ErrorCode.EXECUTION_FAILED)
        self.assertEqual(failed.data["exit_code"], 3)
        self.assertTrue(all(call["cwd"] == str(self.workdir) for call in self.env.calls))
        self.assertTrue(all(call["env"] is None for call in self.env.calls))

    def test_timeout_stops_the_command_and_cancellation_stops_later_actions(self):
        started = time.monotonic()
        result = self.backend.execute(["sh", "-c", "sleep 3; touch late"], timeout=1)
        self.assertEqual(result.error.code, ErrorCode.TIMEOUT)
        self.assertLess(time.monotonic() - started, 2.5)
        time.sleep(3)
        self.assertFalse((self.workdir / "late").exists(), "timed-out command kept running")
        threading.Timer(0.5, self.token.cancel).start()
        result = self.backend.execute(["sh", "-c", "sleep 3; touch cancelled"], timeout=30)
        self.assertEqual(result.error.code, ErrorCode.CANCELLED)
        commands = len(self.env.calls)
        with self.assertRaises(Cancelled):
            self.backend.execute(["touch", "after"], timeout=10)
        with self.assertRaises(Cancelled):
            self.backend.read_bytes("anything.txt")
        time.sleep(3)
        self.assertEqual(len(self.env.calls), commands, "an action reached the environment after cancellation")
        self.assertFalse((self.workdir / "cancelled").exists())
        self.assertFalse((self.workdir / "after").exists())

    def test_environment_failure_is_infrastructure(self):
        self.env.fail_with = RuntimeError("container is gone")
        with self.assertRaises(EnvironmentUnavailable):
            self.backend.read_bytes("a.txt")
        with self.assertRaises(EnvironmentUnavailable):
            self.backend.execute(["true"], timeout=5)

    def test_instructions_match_the_local_backend(self):
        (self.workdir / "AGENTS.md").write_text("root rules\n")
        (self.workdir / "pkg").mkdir()
        (self.workdir / "pkg/AGENTS.md").write_text("package rules\n")
        (self.workdir / "pkg/mod.py").write_text("x = 1\n")
        local = LocalBackend(self.workdir)
        for path in (".", "pkg", "pkg/mod.py", "pkg/new.py", "other.txt"):
            remote, native = self.backend.get_instructions(path), local.get_instructions(path)
            self.assertEqual(remote["digest"], native["digest"], path)
            self.assertEqual(remote["scope"], native["scope"], path)
            self.assertEqual(remote["instructions"], native["instructions"], path)

    def test_search_stays_local_only(self):
        self.assertFailure(ErrorCode.EXECUTION_FAILED, self.backend.search_files, "x")


class WorkspaceOverExecBackendTests(unittest.TestCase):
    def test_container_root_is_never_a_host_path(self):
        if Path("/app").exists():
            self.skipTest("this host has a real /app")
        workdir = temporary_workdir(self)
        env = MappedShellEnvironment(workdir, alias="/app")
        with LoopThread() as loop:
            backend = ExecBackend(AsyncExecChannel(env, loop), "/app")
            workspace = Workspace(backend=backend, allow_write=True, allow_command=True)
            self.assertEqual(workspace.root, PurePosixPath("/app"))
            self.assertNotIsInstance(workspace.root, Path)
            digest = workspace.get_instructions("answer.txt")["digest"]
            workspace.write_file("answer.txt", "ok\n", "missing", digest)
            self.assertEqual(workspace.read_file("/app/answer.txt")["content"], "ok\n")
            self.assertEqual(workspace.run_command(["cat", "answer.txt"], 10).data["stdout"], "ok\n")
            self.assertEqual(workspace.run_command(["pwd"], 10).data["stdout"].strip(), "/app")
            patched = workspace.apply_patch([{"op": "modify", "path": "answer.txt",
                                              "expected_sha256": sha256(b"ok\n").hexdigest(),
                                              "expected_instructions": digest,
                                              "edits": [{"old_text": "ok", "new_text": "fine"}]}])
            self.assertTrue(patched)
        self.assertEqual((workdir / "answer.txt").read_text(), "fine\n")
        self.assertFalse(Path("/app").exists())

    def test_command_timeout_limit_defaults_to_60_and_can_be_raised(self):
        workdir = temporary_workdir(self)
        with LoopThread() as loop:
            backend = ExecBackend(AsyncExecChannel(ShellEnvironment(workdir), loop), str(workdir))
            default = Workspace(backend=backend, allow_command=True)
            self.assertEqual(_timeout_maximum(coding_tools(default)), 60)
            with self.assertRaises(ToolFailure):
                default.run_command(["true"], 61)
            raised = Workspace(backend=backend, allow_command=True, max_command_timeout=900)
            self.assertEqual(_timeout_maximum(coding_tools(raised)), 900)
            self.assertTrue(raised.run_command(["true"], 900).ok)


if __name__ == "__main__":
    unittest.main()
