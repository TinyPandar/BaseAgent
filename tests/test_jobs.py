"""Opt-in background jobs (tools/jobs.py, docs/background_jobs.md). Offline; children are this Python."""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

import config_isolation  # noqa: F401
from checkpoint_helpers import Script, tool_call

from baseagent.agent import run_agent
from baseagent.agent.cancellation import CancellationToken
from baseagent.agent.state import RunStatus
from baseagent.backends import DockerBackend
from baseagent.capabilities import assemble_capabilities
from baseagent.main import main
from baseagent.middleware.execution_guidance import ExecutionGuidanceMiddleware
from baseagent.session import SessionStore
from baseagent.testing.docker import FakeDocker
from baseagent.tools.jobs import JOB_TOOLS, NOTICE_HEADER, JobManager, JobNoticeMiddleware
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.workspace import Workspace, coding_tools

PY = sys.executable

SLEEPER = """
import sys, time
print("started", flush=True)
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 60)
print("finished", flush=True)
"""

# Parent writes "<parent pid> <grandchild pid>" and keeps running (or exits at once with "exit").
TREE = """
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(sys.argv[1] + ".tmp", "w") as handle:
    handle.write(f"{os.getpid()} {child.pid}")
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
if len(sys.argv) < 3:
    time.sleep(60)
"""


def alive(pid):
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie child of this process is not running
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def wait_until(predicate, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class JobsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "sleeper.py").write_text(SLEEPER, encoding="utf-8")
        (self.root / "tree.py").write_text(TREE, encoding="utf-8")
        self.managers = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for manager in self.managers:
            manager.close()
        self.temp.cleanup()

    def workspace(self, **kwargs):
        options = {"allow_command": True, "background_jobs": True, **kwargs}
        workspace = Workspace(self.root, **options)
        if workspace.jobs is not None:
            self.managers.append(workspace.jobs)
        return workspace

    def manager(self, **kwargs):
        manager = JobManager(self.root, **kwargs)
        self.managers.append(manager)
        return manager


class SurfaceTests(JobsCase):
    def test_flag_off_keeps_default_tools_and_contract(self):
        plain = Workspace(self.root, allow_command=True)
        self.assertIsNone(plain.jobs)
        for tools in (coding_tools(plain), assemble_capabilities(plain)):
            names = {spec["function"]["name"] for spec in tools.specs()}
            self.assertFalse(names & set(JOB_TOOLS))
            self.assertNotIn("background_jobs", tools.capability_contract)
        self.assertEqual(coding_tools(plain).contract(), coding_tools(Workspace(self.root, allow_command=True)).contract())

    def test_flag_on_adds_job_tools(self):
        workspace = self.workspace()
        for tools in (coding_tools(workspace), assemble_capabilities(workspace),
                      assemble_capabilities(workspace, packages=["memory"])):
            names = {spec["function"]["name"] for spec in tools.specs()}
            self.assertTrue(set(JOB_TOOLS) <= names)
            self.assertTrue(tools.capability_contract["background_jobs"])
            self.assertTrue(tools.retry_safe("job_list") and tools.retry_safe("job_output"))
            self.assertFalse(tools.retry_safe("start_job") or tools.retry_safe("job_kill"))
        # run_command keeps its schema.
        plain = {spec["function"]["name"]: spec for spec in coding_tools(Workspace(self.root, allow_command=True)).specs()}
        enabled = {spec["function"]["name"]: spec for spec in coding_tools(workspace).specs()}
        self.assertEqual(plain["run_command"], enabled["run_command"])

    def test_permission_gating_without_allow_command(self):
        workspace = self.workspace(allow_command=False)
        tools = coding_tools(workspace)
        result = tools.execute("start_job", {"argv": [PY, "sleeper.py", "1"]})
        self.assertEqual(result.error.code, "permission_denied")
        self.assertIn("--allow-command", result.error.message)
        self.assertEqual(tools.execute("job_kill", {"job_id": "j00000000"}).error.code, "permission_denied")
        self.assertEqual(tools.execute("job_list", {}).data, {"jobs": [], "running": 0})
        self.assertFalse((self.root / ".baseagent" / "jobs").exists())

    def test_disabled_tools_are_hidden_from_the_model_without_allow_command(self):
        workspace = self.workspace(allow_command=False)
        captured = []

        @dataclass
        class Fake:
            messages: list
            tools: list
            state: object

        ExecutionGuidanceMiddleware(workspace).wrap_model_call(
            Fake([{"role": "user", "content": "x"}], coding_tools(workspace).specs(),
                 type("S", (), {"messages": [], "turn_start": 0})()),
            lambda value: captured.append(value))
        names = {tool["function"]["name"] for tool in captured[0].tools}
        self.assertFalse(names & {"start_job", "job_kill", "run_command"})
        self.assertTrue({"job_list", "job_output"} <= names)
        self.assertIn("job_kill", captured[0].messages[0]["content"])

    def test_policy_rules_apply_to_start_job_argv(self):
        workspace = self.workspace()
        policy = ToolPolicy({"start_job": {"default": "deny", "rules": [
            {"action": "allow", "match": {"argv_exact": [PY, "sleeper.py", "1"]}}]}})
        allowed = json.dumps({"argv": [PY, "sleeper.py", "1"]})
        denied = json.dumps({"argv": [PY, "tree.py", "x"]})
        state = type("S", (), {"metadata": {}, "turn_id": 1})()
        self.assertEqual(policy.action(state, "c1", "start_job", allowed), "allow")
        self.assertEqual(policy.action(state, "c2", "start_job", denied), "deny")

    def test_invalid_arguments_and_paths(self):
        tools = coding_tools(self.workspace())
        self.assertEqual(tools.execute("start_job", {"argv": [PY, "sleeper.py"], "cwd": "../"}).error.code,
                         "permission_denied")
        self.assertEqual(tools.execute("start_job", {"argv": [PY, "sleeper.py"], "cwd": ".baseagent"}).error.code,
                         "permission_denied")
        self.assertEqual(tools.execute("start_job", {"argv": [PY], "cwd": "missing"}).error.code, "not_found")
        self.assertEqual(tools.execute("job_output", {"job_id": "j00000000"}).error.code, "not_found")
        self.assertEqual(tools.execute("start_job", {"argv": ["definitely-not-a-program-xyz"]}).error.code, "not_found")
        self.assertEqual(tools.execute("job_list", {}).data["jobs"], [])

    def test_docker_backend_refuses_job_tools(self):
        backend = DockerBackend(self.root, runner=FakeDocker(), start=False, owner=False)
        workspace = Workspace(backend=backend, allow_command=True, background_jobs=True)
        self.assertIsNone(workspace.jobs)
        tools = coding_tools(workspace)
        result = tools.execute("start_job", {"argv": [PY, "sleeper.py", "1"]})
        self.assertEqual(result.error.code, "permission_denied")
        self.assertIn("docker", result.error.message)
        self.assertEqual(tools.execute("job_list", {}).error.code, "permission_denied")

    def test_cli_refuses_background_jobs_with_docker(self):
        called = []
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--root", str(self.root), "--db", str(self.root / ".baseagent" / "s.sqlite3"),
                         "--session", "demo", "--backend", "docker", "--background-jobs", "--allow-command", "hi"],
                        model_factory=lambda args: Script([]),
                        backend_factory=lambda *args: called.append(args))
        self.assertEqual(code, 1)
        self.assertEqual(called, [])
        self.assertIn("--background-jobs is not supported with --backend docker", stderr.getvalue())

    def test_cli_one_shot_offers_job_tools_and_stops_jobs_at_exit(self):
        seen = {}

        def after_start(messages):
            seen.update(json.loads(messages[-1]["content"]))
            return {"content": "started"}

        model = Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": [PY, "sleeper.py", "60"]})]}, after_start])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--root", str(self.root), "--db", str(self.root / ".baseagent" / "s.sqlite3"),
                         "--session", "demo", "--allow-command", "--background-jobs", "train"],
                        model_factory=lambda args: model)
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertTrue(set(JOB_TOOLS) <= set(model.tools[0]))
        info = seen.get("data") or seen
        self.assertEqual(info["status"], "running")
        # The session (process) is over: its jobs ended with it.
        self.assertTrue(wait_until(lambda: not alive(info["pid"])))

    def test_config_file_maps_background_jobs(self):
        from baseagent.config import load_harness_config
        path = self.root / "harness.json"
        path.write_text(json.dumps({"version": 1, "run": {"background_jobs": True}}), encoding="utf-8")
        self.assertEqual(load_harness_config(path)["background_jobs"], True)
        path.write_text(json.dumps({"version": 1, "run": {"background_jobs": "yes"}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_harness_config(path)


class LifecycleTests(JobsCase):
    def test_start_list_output_paging_and_exit_code(self):
        script = "import sys\nfor i in range(400):\n    print(f'line {i:04d}')\nsys.exit(3)\n"
        tools = coding_tools(self.workspace())
        started = tools.execute("start_job", {"argv": [PY, "-c", script]})
        self.assertTrue(started.ok, started)
        job_id = started.data["job_id"]
        self.assertRegex(job_id, r"^j[0-9a-f]{8}$")
        self.assertEqual(started.data["status"], "running")
        self.assertEqual(started.data["log_path"], f".baseagent/jobs/{job_id}.log")
        manager = self.managers[0]
        final = manager.wait(job_id, timeout=30)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["exit_code"], 3)
        self.assertIsNotNone(final["ended_at"])
        listed = tools.execute("job_list", {}).data
        self.assertEqual([item["job_id"] for item in listed["jobs"]], [job_id])
        self.assertEqual(listed["running"], 0)
        pages, offset = [], 0
        while True:
            page = tools.execute("job_output", {"job_id": job_id, "offset": offset, "limit": 1000}).data
            pages.append(page["content"])
            self.assertEqual(page["offset"], offset)
            offset = page["next_offset"]
            if not page["more"]:
                self.assertTrue(page["complete"])
                break
        self.assertGreater(len(pages), 3)
        text = "".join(pages).replace("\r\n", "\n")
        self.assertEqual(text.splitlines(), [f"line {i:04d}" for i in range(400)])
        tail = tools.execute("job_output", {"job_id": job_id, "tail": 30}).data["content"].replace("\r\n", "\n")
        self.assertTrue(tail.endswith("line 0399\n"))
        self.assertEqual(tools.execute("job_output", {"job_id": job_id, "offset": 10**9}).data["content"], "")

    def test_successful_job_reports_exited_and_stderr_is_captured(self):
        manager = self.manager()
        job = manager.start([PY, "-c", "import sys; print('out'); print('err', file=sys.stderr)"])
        final = manager.wait(job["job_id"], timeout=30)
        self.assertEqual((final["status"], final["exit_code"]), ("exited", 0))
        content = manager.output(job["job_id"])["content"]
        self.assertIn("out", content)
        self.assertIn("err", content)

    def test_job_survives_turn_cancellation_and_turn_end(self):
        workspace = self.workspace()
        tools = coding_tools(workspace)
        token = CancellationToken()
        store = SessionStore(self.root / ".baseagent" / "sessions.sqlite3")
        started = {}

        def cancel_during_command(messages):
            started.update(json.loads(messages[-1]["content"]))
            threading.Timer(1.0, token.cancel).start()
            return {"tool_calls": [tool_call("c2", "run_command", {"argv": [PY, "sleeper.py", "30"], "timeout": 20})]}

        model = Script([{"tool_calls": [tool_call("c1", "start_job", {"argv": [PY, "sleeper.py", "60"]})]},
                        cancel_during_command, {"content": "unused"}])
        state = run_agent(model, "train", tools=tools, store=store, session_id="demo", cancellation=token,
                          workspace_root=str(self.root))
        self.assertEqual(state.status, RunStatus.CANCELLED)
        job_id = (started.get("data") or started)["job_id"]
        info = workspace.jobs.describe(job_id)
        self.assertEqual(info["status"], "running")
        self.assertTrue(alive(info["pid"]))
        time.sleep(0.5)
        self.assertEqual(workspace.jobs.describe(job_id)["status"], "running")
        killed = tools.execute("job_kill", {"job_id": job_id}).data
        self.assertTrue(killed["killed"])
        self.assertEqual(killed["status"], "killed")
        self.assertTrue(wait_until(lambda: not alive(info["pid"])))

    def test_job_kill_terminates_the_process_tree(self):
        tools = coding_tools(self.workspace())
        pids = self.root / "pids.txt"
        job = tools.execute("start_job", {"argv": [PY, "tree.py", str(pids)]}).data
        self.assertTrue(wait_until(pids.exists))
        parent, grandchild = map(int, pids.read_text().split())
        self.assertTrue(alive(parent) and alive(grandchild))
        result = tools.execute("job_kill", {"job_id": job["job_id"]})
        self.assertTrue(result.ok, result)
        self.assertEqual(result.data["status"], "killed")
        self.assertTrue(wait_until(lambda: not alive(parent)))
        self.assertTrue(wait_until(lambda: not alive(grandchild)), "grandchild survived job_kill")
        again = tools.execute("job_kill", {"job_id": job["job_id"]}).data
        self.assertFalse(again["killed"])
        self.assertEqual(again["status"], "killed")

    def test_leftover_descendants_stop_when_the_job_exits(self):
        manager = self.manager()
        pids = self.root / "pids2.txt"
        job = manager.start([PY, "tree.py", str(pids), "exit"])
        final = manager.wait(job["job_id"], timeout=30)
        self.assertEqual(final["status"], "exited")
        _, grandchild = map(int, pids.read_text().split())
        self.assertTrue(wait_until(lambda: not alive(grandchild)))

    def test_output_cap(self):
        manager = self.manager(max_log_bytes=1000)
        job = manager.start([PY, "-c", "import sys; sys.stdout.write('x' * 50000)"])
        final = manager.wait(job["job_id"], timeout=30)
        self.assertEqual(final["status"], "exited")
        self.assertEqual(final["log_bytes"], 1000)
        self.assertEqual(final["output_bytes"], 50000)
        self.assertTrue(final["log_truncated"])
        self.assertEqual((self.root / final["log_path"]).stat().st_size, 1000)

    def test_close_kills_running_jobs_and_refuses_new_ones(self):
        manager = self.manager()
        job = manager.start([PY, "sleeper.py", "60"])
        manager.close()
        info = manager.describe(job["job_id"])
        self.assertEqual(info["status"], "killed")
        self.assertTrue(wait_until(lambda: not alive(info["pid"])))
        with self.assertRaises(Exception):
            manager.start([PY, "sleeper.py", "1"])

    def test_running_limit(self):
        manager = self.manager(max_running=1)
        manager.start([PY, "sleeper.py", "60"])
        with self.assertRaisesRegex(Exception, "at most 1"):
            manager.start([PY, "sleeper.py", "60"])

    def test_completion_notice_is_injected_once(self):
        manager = self.manager()
        job = manager.start([PY, "-c", "print('done')"])
        manager.wait(job["job_id"], timeout=30)
        middleware = JobNoticeMiddleware(manager)
        state = type("S", (), {"messages": []})()
        middleware.before_model(state)
        self.assertEqual(len(state.messages), 1)
        self.assertTrue(state.messages[0]["content"].startswith(NOTICE_HEADER))
        self.assertIn(job["job_id"], state.messages[0]["content"])
        self.assertIn("exited", state.messages[0]["content"])
        middleware.before_model(state)
        self.assertEqual(len(state.messages), 1)


if __name__ == "__main__":
    unittest.main()
