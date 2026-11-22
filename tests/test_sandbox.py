"""OS sandbox for command processes (backends/sandbox.py, docs/sandbox.md).

Only Windows is available to run these tests here: macOS (Seatbelt) and Linux (bubblewrap) are covered
by profile/argv generation and selection with fakes, not by running a real confined process.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import config_isolation  # noqa: F401
from checkpoint_helpers import Script, tool_call

from baseagent.backends import LocalBackend
from baseagent.backends import sandbox as sb
from baseagent.config import load_harness_config
from baseagent.main import BACKEND_UNAVAILABLE_EXIT, main
from baseagent.tools.result import ErrorCode, ToolResult
from baseagent.tools.workspace import Workspace, coding_tools

# POSIX paths: the generated profiles/argv are for macOS/Linux, whatever host runs the tests.
ROOT = PurePosixPath("/work/project")
TEMP = PurePosixPath("/tmp/baseagent-sandbox-x")
PROTECTED = sb.protected_dirs(ROOT, [ROOT / ".baseagent" / "s.sqlite3", PurePosixPath("/elsewhere/db")])


def linux(**options):
    """Options for Sandbox(...) that select bubblewrap without touching the host."""
    return {"platform": "linux", "which": lambda name: f"/usr/bin/{name}", "exists": lambda path: False,
            "probe": None, **options}


class PolicyTests(unittest.TestCase):
    def test_modes_and_validation(self):
        self.assertEqual(sb.MODES, ("off", "read-only", "workspace-write"))
        self.assertFalse(sb.SandboxPolicy().confined)
        self.assertTrue(sb.SandboxPolicy("read-only").confined)
        with self.assertRaises(ValueError):
            sb.SandboxPolicy("danger-full-access")
        with self.assertRaises(ValueError):
            sb.SandboxPolicy("read-only", network="yes")

    def test_protected_dirs(self):
        self.assertEqual(PROTECTED, sorted([ROOT / ".baseagent", ROOT / ".baseagent" / "s.sqlite3", ROOT / ".git",
                                            ROOT / ".venv"], key=str))


class SeatbeltProfileTests(unittest.TestCase):
    def test_workspace_write(self):
        profile = sb.seatbelt_profile(sb.SandboxPolicy("workspace-write"), ROOT, TEMP, PROTECTED)
        lines = profile.splitlines()
        self.assertEqual(lines[:3], ["(version 1)", "(allow default)", "(deny file-write*)"])
        allow = profile.index("(allow file-write*")
        deny = profile.index("(deny file-write*\n")
        self.assertLess(allow, deny)  # last matching rule wins: protected denies come after the allows
        self.assertIn(f'(subpath "{ROOT}")', profile[allow:deny])
        self.assertIn(f'(subpath "{TEMP}")', profile[allow:deny])
        for part in (".git", ".venv", ".baseagent"):
            self.assertIn(f'(subpath "{ROOT / part}")', profile[deny:])
        self.assertTrue(profile.rstrip().endswith("(deny network*)"))

    def test_read_only_and_network(self):
        profile = sb.seatbelt_profile(sb.SandboxPolicy("read-only", network=True), ROOT, TEMP, PROTECTED)
        self.assertNotIn(f'(subpath "{ROOT}")', profile)
        self.assertIn(f'(subpath "{TEMP}")', profile)
        self.assertNotIn("network", profile)

    def test_quoting_and_argv(self):
        profile = sb.seatbelt_profile(sb.SandboxPolicy("workspace-write"), PurePosixPath('/a "b"\\c'), TEMP)
        self.assertIn('(subpath "/a \\"b\\"\\\\c")', profile)
        argv = sb.seatbelt_argv(sb.SandboxPolicy("read-only"), ROOT, TEMP)
        self.assertEqual((argv[0], argv[1], argv[-1]), (sb.SANDBOX_EXEC, "-p", "--"))


class BubblewrapArgvTests(unittest.TestCase):
    def pairs(self, argv, flag):
        return [tuple(argv[i + 1:i + 3]) for i, item in enumerate(argv) if item == flag]

    def test_workspace_write(self):
        argv = sb.bwrap_argv(sb.SandboxPolicy("workspace-write"), ROOT, ROOT, TEMP, PROTECTED)
        self.assertEqual(argv[:5], ["bwrap", "--ro-bind", "/", "/", "--dev"])
        self.assertIn((str(ROOT), str(ROOT)), self.pairs(argv, "--bind"))
        self.assertIn((str(TEMP), str(TEMP)), self.pairs(argv, "--bind"))
        protected = self.pairs(argv, "--ro-bind-try")
        for part in (".git", ".venv", ".baseagent"):
            self.assertIn((str(ROOT / part), str(ROOT / part)), protected)
        # Protected re-binds come after the writable workspace bind, so they win.
        self.assertGreater(argv.index("--ro-bind-try"), argv.index(str(ROOT)))
        self.assertGreater(argv.index(str(ROOT)), argv.index("--tmpfs"))
        for flag in ("--unshare-net", "--unshare-pid", "--die-with-parent", "--new-session"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-3:], ["--chdir", str(ROOT), "--"])

    def test_read_only_and_network(self):
        argv = sb.bwrap_argv(sb.SandboxPolicy("read-only", network=True), ROOT, ROOT, TEMP, PROTECTED)
        self.assertIn((str(ROOT), str(ROOT)), self.pairs(argv, "--ro-bind"))
        self.assertEqual(self.pairs(argv, "--bind"), [(str(TEMP), str(TEMP))])
        self.assertNotIn("--ro-bind-try", argv)
        self.assertNotIn("--unshare-net", argv)


class SelectionTests(unittest.TestCase):
    def test_platforms(self):
        nothing = {"which": lambda name: None, "exists": lambda path: False}
        self.assertEqual(sb.detect("read-only", platform="darwin", which=lambda n: None, exists=lambda p: True),
                         (sb.SEATBELT, sb.SANDBOX_EXEC))
        self.assertEqual(sb.detect("read-only", platform="darwin", which=lambda n: "/opt/sandbox-exec",
                                   exists=lambda p: False), (sb.SEATBELT, "/opt/sandbox-exec"))
        self.assertEqual(sb.detect("read-only", platform="linux", which=lambda n: "/usr/bin/bwrap"),
                         (sb.BUBBLEWRAP, "/usr/bin/bwrap"))
        for platform in ("darwin", "linux", "win32", "cygwin", "freebsd14"):
            with self.subTest(platform=platform), self.assertRaises(sb.SandboxUnavailable) as caught:
                sb.detect("workspace-write", platform=platform, **nothing)
            self.assertTrue(str(caught.exception).startswith("SANDBOX_UNAVAILABLE: --sandbox workspace-write"))
            self.assertEqual(caught.exception.code, "SANDBOX_UNAVAILABLE")

    def test_off_creates_nothing(self):
        self.assertIsNone(sb.create(sb.SandboxPolicy("off"), ROOT))
        self.assertIsNone(sb.create(None, ROOT))
        self.assertEqual(sb.wrap(["x"], ROOT, {"A": "1"}, None), (["x"], {"A": "1"}))


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def test_wrap_and_close(self):
        box = sb.Sandbox(sb.SandboxPolicy("workspace-write"), self.root, **linux())
        self.assertTrue(box.temp_dir.is_dir())
        argv, env = sb.wrap(["make", "test"], self.root, {"PATH": "p"}, box)
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        self.assertEqual(argv[-3:], ["--", "make", "test"])
        self.assertEqual(env["PATH"], "p")
        self.assertEqual({env[name] for name in ("TMPDIR", "TMP", "TEMP")}, {str(box.temp_dir)})
        self.assertEqual(box.describe(), {"kind": "local", "sandbox": "workspace-write", "network": "none",
                                          "implementation": "bubblewrap"})
        box.close()
        self.assertFalse(box.temp_dir.exists())
        with self.assertRaises(sb.SandboxUnavailable):
            box.wrap(["true"])

    def test_failed_probe_fails_closed(self):
        seen = []

        def probe(argv, env):
            seen.append(argv)
            return 1, "bwrap: No permissions to create new namespace"
        made = []
        real = tempfile.mkdtemp
        with patch.object(sb.tempfile, "mkdtemp", lambda **kw: made.append(real(**kw)) or made[-1]), \
                self.assertRaises(sb.SandboxUnavailable) as caught:
            sb.Sandbox(sb.SandboxPolicy("read-only"), self.root, **linux(probe=probe))
        self.assertIn("No permissions", str(caught.exception))
        self.assertEqual(seen[0][0], "/usr/bin/bwrap")
        self.assertFalse(Path(made[0]).exists())

    def test_local_backend_wraps_execute(self):
        box = sb.Sandbox(sb.SandboxPolicy("workspace-write"), self.root, **linux())
        backend = LocalBackend(self.root, sandbox=box)
        calls = []
        with patch("baseagent.backends.local.run_process",
                   lambda argv, cwd, **kw: calls.append((argv, cwd, kw)) or ToolResult(data={})):
            backend.execute(["echo", "hi"], timeout=5)
            backend.close()
            result = backend.execute(["echo", "again"], timeout=5)
        argv, cwd, kw = calls[0]
        self.assertEqual((argv[0], argv[-2:], cwd), ("/usr/bin/bwrap", ["echo", "hi"], self.root))
        self.assertEqual(kw["env"]["TMPDIR"], str(box.temp_dir))
        self.assertEqual(len(calls), 1)  # closed sandbox: refused, never run unconfined
        self.assertEqual(result.error.code, ErrorCode.PERMISSION_DENIED)
        self.assertIn("SANDBOX_UNAVAILABLE", result.error.message)

    @unittest.skipUnless(sys.platform == "win32", "native Windows is the real unavailable case")
    def test_windows_host_refuses_confined_modes(self):
        for mode in sb.CONFINED_MODES:
            with self.subTest(mode=mode), self.assertRaises(sb.SandboxUnavailable):
                LocalBackend(self.root, sandbox=sb.SandboxPolicy(mode))

    def test_default_contract_unchanged(self):
        plain = LocalBackend(self.root)
        off = LocalBackend(self.root, sandbox=sb.SandboxPolicy("off"))
        self.assertIsNone(off.sandbox)
        expected = {"kind": "local", "version": 1, "root": str(self.root), "isolation": "none", "protected_paths": []}
        self.assertEqual(plain.contract(), expected)
        self.assertEqual(json.dumps(off.contract(), sort_keys=True), json.dumps(expected, sort_keys=True))
        self.assertEqual(coding_tools(Workspace(self.root)).contract(),
                         coding_tools(Workspace(backend=off)).contract())
        boxed = LocalBackend(self.root, sandbox=sb.Sandbox(sb.SandboxPolicy("read-only"), self.root, **linux()))
        self.addCleanup(boxed.close)
        self.assertEqual(boxed.contract(), {**expected, "sandbox": {"version": 1, "mode": "read-only", "network": False,
                                                                    "protected_parts": [".git", ".venv", ".baseagent"]}})

    def test_background_jobs_spawn_through_the_sandbox(self):
        from baseagent.tools import jobs
        box = sb.Sandbox(sb.SandboxPolicy("workspace-write"), self.root, **linux())
        boxed = LocalBackend(self.root, sandbox=box)
        self.addCleanup(boxed.close)
        workspace = Workspace(backend=boxed, background_jobs=True)
        self.addCleanup(workspace.jobs.close)
        self.assertIs(workspace.jobs.sandbox, box)
        seen = []

        class Spawned(Exception):
            pass

        def popen(argv, **kw):
            seen.append((argv, kw))
            raise Spawned()

        with patch.object(jobs.subprocess, "Popen", popen), self.assertRaises(Spawned):
            jobs.spawn_job_process(["python", "train.py"], self.root, sandbox=box)
        argv, kw = seen[0]
        self.assertEqual((argv[0], argv[-2:]), ("/usr/bin/bwrap", ["python", "train.py"]))
        self.assertEqual(kw["env"]["TMPDIR"], str(box.temp_dir))
        # The same command without a sandbox is unchanged and inherits the environment.
        with patch.object(jobs.subprocess, "Popen", popen), self.assertRaises(Spawned):
            jobs.spawn_job_process(["python", "train.py"], self.root)
        self.assertEqual((seen[1][0], seen[1][1]["env"]), (["python", "train.py"], None))

    def test_background_jobs_never_run_unconfined(self):
        from baseagent.tools import jobs
        from baseagent.tools.result import ToolFailure
        box = sb.Sandbox(sb.SandboxPolicy("read-only"), self.root, **linux())
        boxed = LocalBackend(self.root, sandbox=box)
        workspace = Workspace(backend=boxed, background_jobs=True)
        self.addCleanup(workspace.jobs.close)
        spawned = []
        with patch.object(jobs.subprocess, "Popen", lambda *a, **kw: spawned.append(a) or 1 / 0):
            # keep_running: the runner writes into .baseagent (read-only) and outlives the sandbox temp dir.
            with self.assertRaises(ToolFailure) as caught:
                workspace.jobs.start(["python", "train.py"], keep_running=True)
            self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)
            self.assertEqual(workspace.jobs.list()["jobs"], [])
            boxed.close()  # a closed sandbox refuses instead of falling back to the host
            with self.assertRaises(ToolFailure) as caught:
                workspace.jobs.start(["python", "train.py"])
            self.assertIn("SANDBOX_UNAVAILABLE", str(caught.exception))
        self.assertEqual(spawned, [])


class ConfigTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "c.json"
            path.write_text(json.dumps({"version": 1, **data}))
            return load_harness_config(path)

    def test_section(self):
        self.assertEqual(self.load({"sandbox": {"mode": "workspace-write", "network": True}}),
                         {"sandbox": "workspace-write", "sandbox_network": True})
        for bad in ({"mode": "full"}, {"network": "yes"}, {"other": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.load({"sandbox": bad})


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.db = self.root / ".baseagent" / "s.sqlite3"

    def run_cli(self, argv, model):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = main(["--root", str(self.root), "--db", str(self.db), *argv], model_factory=lambda args: model)
            except SystemExit as exit:
                code = exit.code
        return code, stdout.getvalue(), stderr.getvalue()

    def unavailable(self, mode, **kw):
        raise sb.SandboxUnavailable(mode, "test host has no sandbox")

    def test_unavailable_exits_3_before_the_model(self):
        for fmt in ("text", "json"):
            with self.subTest(fmt=fmt), patch.object(sb, "detect", self.unavailable):
                model = Script([AssertionError("the model must not be called")])
                code, out, err = self.run_cli(["--sandbox", "workspace-write", "--mode", "full", "--output-format", fmt,
                                               "--session", f"s-{fmt}", "hi"], model)
                self.assertEqual(code, BACKEND_UNAVAILABLE_EXIT)
                self.assertEqual(model.seen, [])
                self.assertIn("Sandbox error: SANDBOX_UNAVAILABLE", err)
                if fmt == "json":
                    result = json.loads(out)
                    self.assertEqual(result["exit_code"], BACKEND_UNAVAILABLE_EXIT)
                    self.assertEqual(result["backend"], {"kind": "local", "sandbox": "workspace-write",
                                                         "network": "none", "unavailable": "SANDBOX_UNAVAILABLE"})

    @unittest.skipUnless(sys.platform == "win32", "native Windows is the real unavailable case")
    def test_real_windows_host_exits_3(self):
        model = Script([AssertionError("the model must not be called")])
        code, out, err = self.run_cli(["--sandbox", "read-only", "--session", "w", "hi"], model)
        self.assertEqual(code, BACKEND_UNAVAILABLE_EXIT)
        self.assertIn("native Windows", err)

    def test_flag_errors(self):
        for argv in (["--sandbox-network", "hi"], ["--sandbox", "off", "--sandbox-network", "hi"],
                     ["--backend", "docker", "--sandbox", "read-only", "hi"], ["--sandbox", "full", "hi"]):
            with self.subTest(argv=argv):
                code, _, _ = self.run_cli(argv, Script([]))
                self.assertEqual(code, 2)

    def test_background_jobs_allowed_with_sandbox(self):
        with patch.object(sb, "detect", lambda mode, **kw: (sb.BUBBLEWRAP, "bwrap")), \
                patch.object(sb, "_probe", lambda argv, env: (0, "")):
            code, _, err = self.run_cli(["--sandbox", "read-only", "--background-jobs", "--allow-command",
                                         "--session", "j", "hi"], Script([{"content": "done"}]))
        self.assertEqual(code, 0, err)

    def test_commands_run_wrapped(self):
        calls = []
        model = Script([{"tool_calls": [tool_call("c1", "run_command", {"argv": ["echo", "hi"]})]},
                        {"content": "done"}])
        with patch.object(sb, "detect", lambda mode, **kw: (sb.BUBBLEWRAP, "bwrap")), \
                patch.object(sb, "_probe", lambda argv, env: (0, "")), \
                patch("baseagent.backends.local.run_process",
                      lambda argv, cwd, **kw: calls.append(argv) or ToolResult(data={"exit_code": 0, "stdout": "hi\n",
                                                                                     "stderr": ""})):
            code, out, err = self.run_cli(["--sandbox", "workspace-write", "--sandbox-network", "--mode", "full",
                                           "--auto-approve", "--output-format", "json", "--session", "ok", "hi"], model)
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0][0], "bwrap")
        self.assertNotIn("--unshare-net", calls[0])
        self.assertEqual(calls[0][-2:], ["echo", "hi"])
        self.assertEqual(json.loads(out)["backend"], {"kind": "local", "sandbox": "workspace-write",
                                                      "network": "enabled", "implementation": "bubblewrap"})
        self.assertIn("Backend: local, sandbox workspace-write (bubblewrap), network enabled", err)
        self.assertNotIn("LocalBackend is not a sandbox", err)

    def test_default_output_has_no_backend(self):
        code, out, err = self.run_cli(["--output-format", "json", "--session", "d", "hi"], Script([{"content": "ok"}]))
        self.assertEqual(code, 0, err)
        self.assertNotIn("backend", json.loads(out))


if __name__ == "__main__":
    unittest.main()
