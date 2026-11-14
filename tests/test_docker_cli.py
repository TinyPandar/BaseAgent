"""``--backend docker`` in the CLI (README plan "Docker 执行后端接入 CLI", AC 1-16). Offline.

``FakeDocker`` (baseagent.testing.docker) plays the docker CLI: ``docker exec`` really runs the
command with /workspace mapped to the test workspace, so the in-container file script is
exercised. Models are fake. Real-Docker smoke tests are in test_docker_smoke.py.
"""

import contextlib
import io
import json
import os
import sys
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from chat_helpers import RUNNERS, ChatTestCase
from checkpoint_helpers import Script, current_hash, tool_call, write_call, writes
from test_main_agent import Duplex, Gate, delegate

from baseagent.backends import DockerBackend
from baseagent.backends.docker import MOUNT
from baseagent.backends.docker_fs import FS_SCRIPT
from baseagent.main import BACKEND_UNAVAILABLE_EXIT, FULL_MODE_WARNING, main
from baseagent.testing.docker import FakeDocker
from baseagent.tools.edit_policy import EditPolicy
from baseagent.tools.result import ErrorCode, ToolFailure
from baseagent.tools.workspace import Workspace

DOCKER = ["--backend", "docker"]
SINGLE = ["--session", "demo", "--mode", "edit", "--no-main-agent"]


class DockerCliCase(ChatTestCase):
    """Chat/one-shot runs through the real ``main`` with the fake Docker CLI behind ``--backend docker``."""

    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.db = self.root / ".baseagent" / "sessions.sqlite3"
        (self.root / "a.txt").write_text("A0\n")
        (self.root / "b.txt").write_text("B0\n")
        self.fake = FakeDocker()
        self.backends = []
        self.enterContext(patch("baseagent.main._make_backend", self.factory))

    def factory(self, args, root, protected):
        backend = DockerBackend(root, image=args.docker_image or "python:3.12-slim",
                                network=args.docker_network or "none", protected_paths=protected,
                                runner=self.fake, owner=False)
        self.backends.append(backend)
        return backend

    def run_cli(self, argv, model=None, lines=()):
        """(exit code, stdout, stderr) of one ``baseagent`` invocation with this workspace and database."""
        model = model if model is not None else Script([])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch("builtins.input", side_effect=[*lines, EOFError()]):
            try:
                code = main(["--root", str(self.root), "--db", str(self.db), *argv], model_factory=lambda args: model)
            except SystemExit as exit:
                code = exit.code
        return code, stdout.getvalue(), stderr.getvalue()

    def containers(self):
        return self.fake.started()

    def assert_all_removed(self):
        self.assertTrue(self.containers())
        self.assertEqual(sorted(self.fake.removed()), sorted(self.containers()))
        self.assertEqual(self.fake.containers, {})


def run_and_write(root, name="out.txt", content="x\n", argv=("sh", "-c", "echo ran-in-container")):
    return Script([lambda messages: {"tool_calls": [write_call(root, "c1", name, content),
                                                    tool_call("c2", "run_command", {"argv": list(argv)})]},
                   {"content": "done"}])


# ----- A. CLI ---------------------------------------------------------------------------------
class CliOptionTests(DockerCliCase):
    def test_docker_options_need_the_docker_backend(self):
        for argv in (["--docker-image", "x"], ["--backend", "local", "--docker-network", "bridge"],
                     ["--backend", "local", "--docker-image", "alpine"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli([*argv, "hi"])
                self.assertEqual(code, 2)
                self.assertIn("applies to --backend docker", err)
        code, _, err = self.run_cli([*DOCKER, "--session", "s", "--checkpoints"])
        self.assertEqual(code, 2)
        self.assertIn("--backend docker applies to one-shot prompts", err)
        self.assertEqual(self.containers(), [])

    def test_explicit_local_matches_the_default_byte_for_byte(self):
        outputs = []
        for argv in ([], ["--backend", "local"]):
            for fmt in ("text", "json"):
                (self.root / "out.txt").unlink(missing_ok=True)
                session = f"s{len(outputs)}"
                code, out, err = self.run_cli([*argv, "--mode", "full", "--session", session, "--output-format", fmt,
                                               "make it"], run_and_write(self.root, argv=["echo", "hi"]))
                self.assertEqual(code, 0, err)
                outputs.append((fmt, out.replace(session, "S"), err.replace(session, "S")))
        self.assertEqual(outputs[:2], outputs[2:])
        self.assertIn(FULL_MODE_WARNING, outputs[0][2])
        self.assertNotIn('"backend"', outputs[1][1])                  # no field means local
        self.assertNotIn("Backend:", outputs[0][2])
        self.assertEqual(self.containers(), [])

    def test_one_shot_runs_commands_and_writes_in_the_container(self):
        code, out, err = self.run_cli([*DOCKER, "--mode", "full", "--session", "s", "make it"], run_and_write(self.root))
        self.assertEqual(code, 0, err)
        self.assertEqual((self.root / "out.txt").read_text(), "x\n")
        state = self.store().load("s")
        results = self.tool_results(state)
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertIn("ran-in-container", results[1]["data"]["stdout"])
        name = self.containers()[0]
        execs = [argv for argv in self.fake.calls if argv[:2] == ["docker", "exec"] and name in argv]
        self.assertTrue(any("put" in argv and "out.txt" in argv for argv in execs))          # the write
        self.assertTrue(any(argv[-3:] == ["sh", "-c", "echo ran-in-container"] for argv in execs))  # the command
        self.assertEqual(state.metadata["execution_backend"], {"kind": "docker", "image": "python:3.12-slim",
                                                                "network": "none"})
        self.assert_all_removed()

    def test_full_mode_explains_the_container_instead_of_the_local_warning(self):
        for network in ("none", "bridge"):
            with self.subTest(network=network):
                code, _, err = self.run_cli([*DOCKER, "--docker-network", network, "--mode", "full",
                                             "--session", network, "hi"], Script([{"content": "hi"}]))
                self.assertEqual(code, 0, err)
                self.assertNotIn(FULL_MODE_WARNING, err)
                self.assertNotIn("LocalBackend is not a sandbox", err)
                self.assertIn("runs commands inside the Docker container", err)
                self.assertIn("no network access" if network == "none" else "network enabled", err)

    def test_permission_modes_and_tool_policy_are_unchanged_under_docker(self):
        for mode, write, command in (("read-only", False, False), ("edit", True, False), ("full", True, True)):
            with self.subTest(mode=mode):
                (self.root / "out.txt").unlink(missing_ok=True)
                code, _, err = self.run_cli([*DOCKER, "--mode", mode, "--session", mode, "make it"],
                                            run_and_write(self.root))
                self.assertEqual(code, 0, err)
                wrote, ran = (result["ok"] for result in self.tool_results(self.store().load(mode)))
                self.assertEqual((wrote, ran), (write, command))
                self.assertEqual((self.root / "out.txt").exists(), write)
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"run_command": "deny"}}))
        calls = len(self.fake.calls)
        code, _, err = self.run_cli([*DOCKER, "--mode", "full", "--tool-policy", str(policy), "--session", "denied",
                                     "make it"], run_and_write(self.root, name="p.txt"))
        self.assertEqual(code, 0, err)
        results = self.tool_results(self.store().load("denied"))
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        self.assertFalse(any(argv[-3:] == ["sh", "-c", "echo ran-in-container"] for argv in self.fake.calls[calls:]))

    def test_resume_uses_a_new_container_and_backend_switches_are_refused(self):
        policy = self.root / "ask.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"run_command": "ask"}}))
        model = Script([lambda messages: {"tool_calls": [tool_call("c1", "run_command", {"argv": ["echo", "ok"]})]},
                        {"content": "finished"}])
        code, _, err = self.run_cli([*DOCKER, "--mode", "full", "--tool-policy", str(policy), "--session", "r", "go"],
                                    model)
        self.assertEqual(code, 1, err)
        state = self.store().load("r")
        self.assertEqual(state.status, "awaiting_approval")
        request = state.metadata["approval_request"]
        self.assertEqual(self.run_cli(["--session", "r", "--approve-tool", request["call_id"], "--approval-digest",
                                       request["request_digest"]])[0], 0)
        # Resuming the docker turn on the local backend (or another image) is refused before any container starts.
        for argv in (["--backend", "local"], [], [*DOCKER, "--docker-image", "alpine:3"]):
            with self.subTest(argv=argv):
                started = len(self.containers())
                code, _, err = self.run_cli([*argv, "--mode", "full", "--tool-policy", str(policy), "--session", "r",
                                             "--resume"], model)
                self.assertEqual(code, 1)
                self.assertIn("has an unfinished turn that ran with --backend docker", err)
                self.assertEqual(len(self.containers()), started)
        code, out, err = self.run_cli([*DOCKER, "--mode", "full", "--tool-policy", str(policy), "--session", "r",
                                       "--resume"], model)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "finished")
        self.assertEqual(len(self.containers()), 2)               # one container per run
        self.assert_all_removed()

    def test_a_local_unfinished_turn_cannot_continue_in_docker(self):
        policy = self.root / "ask.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"run_command": "ask"}}))
        model = Script([lambda messages: {"tool_calls": [tool_call("c1", "run_command", {"argv": ["echo", "ok"]})]}])
        self.assertEqual(self.run_cli(["--mode", "full", "--tool-policy", str(policy), "--session", "l", "go"], model)[0], 1)
        code, _, err = self.run_cli([*DOCKER, "--mode", "full", "--tool-policy", str(policy), "--session", "l",
                                     "--resume"], model)
        self.assertEqual(code, 1)
        self.assertIn("has an unfinished turn that ran with --backend local", err)
        self.assertEqual(self.containers(), [])

    def test_chat_turns_and_workers_share_one_container(self):
        gate = Gate(lambda messages: {"tool_calls": [write_call(self.root, "c1", "a.txt", "by w1\n"),
                                                     tool_call("c2", "run_command", {"argv": ["echo", "worker-ran"]})]})
        self.gates = [gate]
        model = Duplex(main=[delegate("d1", "change a.txt", allow_writes=True), {"content": "已派出 w1。"},
                             {"content": "收到。"}],
                       worker=[gate, {"content": "wrote a.txt"}])

        def release(prompt):
            gate.release()
            return "/wait"

        code, out, err = self.invoke([*DOCKER, "--session", "demo", "--mode", "full"], model,
                                     ["改 a.txt", release, "/status", "/exit"])
        self.assertEqual(code, 0, err)
        self.assertEqual((self.root / "a.txt").read_text(), "by w1\n")
        self.assertEqual(len(self.containers()), 1)
        self.assertIn("worker-ran", json.dumps(self.tool_results(self.worker())))
        self.assertIn("Backend: docker, image python:3.12-slim, no network access", out)
        self.assert_all_removed()


# ----- B. isolation and file boundary -----------------------------------------------------------
class IsolationTests(DockerCliCase):
    def backend(self, **options):
        backend = DockerBackend(self.root, runner=self.fake, owner=options.pop("owner", False), **options)
        self.addCleanup(backend.close)
        return backend

    def test_only_the_workspace_is_mounted_and_the_network_is_off_by_default(self):
        subprocess_git = self.root / ".git"
        subprocess_git.mkdir()
        backend = self.backend()
        run = next(argv for argv in self.fake.calls if argv[1] == "run")
        mounts = [run[index + 1] for index, value in enumerate(run) if value == "--mount"]
        self.assertEqual(mounts, [f"type=bind,source={self.root},target={MOUNT}",
                                  f"type=bind,source={self.root / '.git'},target={MOUNT}/.git,readonly",
                                  f"type=tmpfs,destination={MOUNT}/.baseagent"])
        self.assertNotIn("-v", run)
        self.assertNotIn("--privileged", run)
        self.assertEqual(run[run.index("--network") + 1], "none")
        self.assertEqual(run[run.index("--pull") + 1], "never")
        self.assertEqual(backend.contract()["network"], "none")
        self.assertNotIn("container", backend.contract())        # resume-stable contract
        bridged = DockerBackend(self.root, runner=self.fake, owner=False, network="bridge")
        self.addCleanup(bridged.close)
        run = [argv for argv in self.fake.calls if argv[1] == "run"][-1]
        self.assertEqual(run[run.index("--network") + 1], "bridge")
        with self.assertRaises(ValueError):
            DockerBackend(self.root, runner=self.fake, network="host", start=False)

    def test_escapes_are_refused_like_local(self):
        outside = self.root.parent / f"{self.root.name}-outside.txt"
        outside.write_text("host secret\n")
        self.addCleanup(outside.unlink)
        os.symlink(outside, self.root / "link.txt")
        os.symlink(self.root.parent, self.root / "updir")
        workspace = Workspace(backend=self.backend(), allow_write=True)
        local = Workspace(self.root, allow_write=True)
        digest = local.get_instructions()["digest"]
        for path in ("../x.txt", "/etc/hosts", "link.txt", f"updir/{outside.name}", "sub/../../x.txt"):
            for name, target in (("docker", workspace), ("local", local)):
                with self.subTest(path=path, backend=name):
                    with self.assertRaises(ToolFailure) as read:
                        target.read_file(path)
                    self.assertEqual(read.exception.code, ErrorCode.PERMISSION_DENIED)
                    with self.assertRaises(ToolFailure) as wrote:
                        target.write_file(path, "pwned", "missing", digest)
                    self.assertEqual(wrote.exception.code, ErrorCode.PERMISSION_DENIED)
        self.assertEqual(outside.read_text(), "host secret\n")

    def test_the_container_script_checks_links_itself(self):
        # Defence in depth: even a path the host accepted is resolved again inside the container.
        import subprocess
        os.symlink("/etc", self.root / "etc")
        (self.root / "dir").mkdir()
        os.symlink("../a.txt", self.root / "dir" / "inside")
        probe = subprocess.run(["sh", "-c", FS_SCRIPT, "baseagent-fs", str(self.root), "probe", "100",
                                "r:etc/hosts", "r:dir/inside", "i:dir/inside"], capture_output=True)
        statuses = [line.split()[1] for line in probe.stderr.decode().splitlines() if line.startswith("@@")]
        self.assertEqual(statuses, ["escape", "ok", "link"])     # links inside the workspace read like local
        put = subprocess.run(["sh", "-c", FS_SCRIPT, "baseagent-fs", str(self.root), "put", "etc/x", "missing", "-"],
                             input=b"x", capture_output=True)
        self.assertEqual(put.returncode, 5)
        self.assertFalse(Path("/etc/x").exists())

    def test_protected_paths_are_refused_and_harness_state_is_hidden(self):
        workspace = Workspace(backend=self.backend(protected_paths=[self.root / ".baseagent" / "s.sqlite3"]),
                              allow_write=True)
        digest = workspace.get_instructions()["digest"]
        for path in (".git/config", ".env", ".env.local", ".baseagent/s.sqlite3", ".venv/x", "a/.git/hooks/x"):
            with self.subTest(path=path):
                with self.assertRaises(ToolFailure) as caught:
                    workspace.write_file(path, "x", "missing", digest)
                self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)
                with self.assertRaises(ToolFailure):
                    workspace.apply_patch([{"op": "add", "path": path, "content": "x", "expected_sha256": "missing",
                                            "expected_instructions": digest}])
        with self.assertRaises(ValueError) as caught:
            DockerBackend(self.root, runner=self.fake, start=False, protected_paths=[self.root / "state.sqlite3"])
        self.assertIn("session database must live outside the workspace", str(caught.exception))

    def test_database_inside_the_workspace_is_refused_with_exit_2(self):
        code, _, err = self.run_cli(["--db", str(self.root / "state.sqlite3"), *DOCKER, "hi"])
        self.assertEqual(code, 2)
        self.assertIn("session database must live outside the workspace", err)

    def test_files_go_back_to_the_host_user_on_linux(self):
        backend = self.backend(owner=(1234, 5678))
        workspace = Workspace(backend=backend, allow_write=True, allow_command=True)
        workspace.write_file("new.txt", "x", "missing", workspace.get_instructions()["digest"])
        workspace.run_command(["echo", "hi"])
        file_ops = [argv for argv in self.fake.calls if argv[1] == "exec" and FS_SCRIPT in argv
                    and argv[argv.index(FS_SCRIPT) + 3] in ("put", "probe")]
        self.assertTrue(file_ops)
        self.assertTrue(all(argv[argv.index("-u") + 1] == "1234:5678" for argv in file_ops))
        command = next(argv for argv in self.fake.calls if argv[-2:] == ["echo", "hi"])
        self.assertNotIn("-u", command)                           # commands keep the image user (apt, pip)
        after = [argv for argv in self.fake.calls if FS_SCRIPT in argv and argv[argv.index(FS_SCRIPT) + 3] == "after"]
        self.assertEqual(after[-1][-2:], ["1234", "5678"])
        self.assertNotIn("-u", after[-1])                         # chown runs as the image user (root)
        backend.close()
        closing = [argv for argv in self.fake.calls if FS_SCRIPT in argv and argv[argv.index(FS_SCRIPT) + 3] == "after"][-1]
        self.assertEqual(closing[-3:], ["-", "1234", "5678"])

    def test_batched_drift_check_matches_file_by_file_answers(self):
        (self.root / "AGENTS.md").write_text("root rules\n")
        (self.root / "sub" / "deeper").mkdir(parents=True)
        (self.root / "sub" / "AGENTS.md").write_text("sub rules\n")
        (self.root / "sub" / "deeper" / "x.py").write_text("x\n")
        (self.root / "big.bin").write_bytes(b"0" * 200_001)
        (self.root / "sub" / "deeper" / "AGENTS.md").write_text("y" * 20_001)
        os.symlink("../a.txt", self.root / "sub" / "alias.txt")
        backend = self.backend()
        workspace = Workspace(backend=backend)
        paths = ["a.txt", "sub/AGENTS.md", "sub/deeper/x.py", "gone.txt", "sub/new/n.txt", "big.bin", "sub",
                 "sub/alias.txt", "../escape.txt", ".git/config"]
        expected = {}
        for path in paths:
            try:
                expected[path] = (workspace.file_hash(path), workspace.get_instructions(path)["digest"])
            except (OSError, ToolFailure):
                expected[path] = ("unavailable", "unavailable")
        calls = len(self.fake.calls)
        self.assertEqual(workspace.observe_many(paths), expected)
        self.assertEqual(len(self.fake.calls) - calls, 1)                 # one exec for the whole list
        local = Workspace(self.root)
        self.assertEqual(local.observe_many(paths), expected)             # and the same answers as local

    def test_two_sessions_on_one_workspace_do_not_collide(self):
        first, second = self.backend(), self.backend()
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(set(self.fake.containers), {first.name, second.name})
        second.close()
        self.assertIn(first.name, self.fake.containers)           # the live session keeps its container
        self.assertTrue(Workspace(backend=first).read_file("a.txt")["content"] == "A0\n")


# ----- C. checkpoints, undo and atomic patches --------------------------------------------------
class UndoTests(DockerCliCase):
    def test_chat_checkpoints_undo_and_rewind_restore_exact_bytes(self):
        (self.root / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
        model = Script([writes(self.root, ("a.txt", "A1\n"), ("new.txt", "N\n")), {"content": "第一轮"},
                        writes(self.root, ("b.txt", "B1\n")), {"content": "第二轮"},
                        lambda messages: {"tool_calls": [tool_call("p", "apply_patch", {"operations": [
                            {"op": "modify", "path": "crlf.txt", "expected_sha256": current_hash(self.root, "crlf.txt"),
                             "expected_instructions": Workspace(self.root).get_instructions()["digest"],
                             "edits": [{"old_text": "two", "new_text": "TWO"}]}]})]},
                        {"content": "第三轮"}, {"content": "ok"}])
        code, out, err = self.invoke([*DOCKER, *SINGLE], model,
                                     ["一", "二", "三", "/checkpoints", "/undo", "/rewind 1", "/checkpoints", "/exit"])
        self.assertEqual(code, 0, err)
        self.assertIn("第 3 轮", out)
        self.assertIn("已撤回第 3 轮的文件改动", out)
        self.assertIn("已撤回第 2、1 轮的文件改动", out)
        self.assertEqual((self.root / "crlf.txt").read_bytes(), b"one\r\ntwo\r\n")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"A0\n")
        self.assertEqual((self.root / "b.txt").read_bytes(), b"B0\n")
        self.assertFalse((self.root / "new.txt").exists())
        self.assertNotIn("unsupported_backend", out + err)
        self.assert_all_removed()

    def test_drift_force_and_pending_approval_behave_like_local(self):
        def drift(prompt):
            (self.root / "a.txt").write_text("edited by hand\n")
            return "/undo"

        model = Script([writes(self.root, ("a.txt", "A1\n")), {"content": "写好了"}])
        code, out, err = self.invoke([*DOCKER, *SINGLE], model, ["改", drift, "/undo --force", "/exit"])
        self.assertEqual(code, 0, err)
        self.assertIn("这些文件在 agent 写入之后又被改动过：a.txt", err)
        self.assertIn("--force：覆盖了冲突文件", out)
        self.assertEqual((self.root / "a.txt").read_text(), "A0\n")
        # A turn waiting for approval cannot be undone.
        policy = self.root / "ask.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"run_command": "ask"}}))
        model = Script([writes(self.root, ("b.txt", "B1\n")), {"content": "b"},
                        lambda messages: {"tool_calls": [tool_call("c9", "run_command", {"argv": ["echo"]})]}])
        code, out, err = self.invoke([*DOCKER, "--session", "ask", "--mode", "full", "--no-main-agent",
                                      "--tool-policy", str(policy)], model, ["改 b", "跑命令", "n", "/undo", "/exit"])
        self.assertIn("撤销被拒绝：当前这一轮", err)
        self.assertEqual((self.root / "b.txt").read_text(), "B1\n")

    def test_cli_checkpoints_and_rewind_last_work_for_a_docker_session_without_docker(self):
        code, _, err = self.run_cli([*DOCKER, "--mode", "edit", "--session", "s", "改"],
                                    Script([writes(self.root, ("a.txt", "A1\n"), ("n.txt", "N\n")), {"content": "ok"}]))
        self.assertEqual(code, 0, err)
        self.fake.installed = False                 # restoring needs no container (or Docker at all)
        code, out, _ = self.run_cli(["--session", "s", "--checkpoints"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["checkpoints"][0]["files"]), 2)
        code, out, err = self.run_cli(["--session", "s", "--rewind", "last"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["restored_turns"], [1])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"A0\n")
        self.assertFalse((self.root / "n.txt").exists())

    def test_apply_patch_rolls_back_when_the_second_file_fails(self):
        backend = DockerBackend(self.root, runner=self.fake, owner=False)
        self.addCleanup(backend.close)
        (self.root / "c.txt").write_bytes(b"C0\r\n")
        os.chmod(self.root / "c.txt", 0o750)
        workspace = Workspace(backend=backend, allow_write=True)
        digest = workspace.get_instructions()["digest"]
        operations = [
            {"op": "modify", "path": "a.txt", "expected_sha256": current_hash(self.root, "a.txt"),
             "expected_instructions": digest, "edits": [{"old_text": "A0", "new_text": "A1"}]},
            {"op": "add", "path": "deep/new.txt", "expected_sha256": "missing", "expected_instructions": digest,
             "content": "new"},
            {"op": "delete", "path": "c.txt", "expected_sha256": current_hash(self.root, "c.txt"),
             "expected_instructions": digest}]
        before = {name: (self.root / name).read_bytes() for name in ("a.txt", "b.txt", "c.txt")}
        puts = []

        def fail_second(argv):
            if FS_SCRIPT in argv and argv[argv.index(FS_SCRIPT) + 3] == "put" and "force" not in argv:
                puts.append(argv)
                if len(puts) == 2:
                    return 7, b"", b"baseagent-fs: write failed (injected)"
            return None

        self.fake.hook = fail_second
        with self.assertRaises(ToolFailure) as caught:
            workspace.apply_patch(operations)
        self.assertIn("patch rolled back, no file changed", str(caught.exception))
        self.assertEqual({name: (self.root / name).read_bytes() for name in before}, before)
        self.assertFalse((self.root / "deep" / "new.txt").exists())
        self.assertEqual(os.stat(self.root / "c.txt").st_mode & 0o777, 0o750)
        # Batch validation failure: nothing is written at all.
        self.fake.hook = None
        bad = [operations[0], {**operations[2], "expected_sha256": "0" * 64}]
        calls = len(self.fake.calls)
        with self.assertRaises(ToolFailure):
            workspace.apply_patch(bad)
        self.assertFalse(any(FS_SCRIPT in argv and argv[argv.index(FS_SCRIPT) + 3] in ("put", "del")
                             for argv in self.fake.calls[calls:]))
        self.assertEqual((self.root / "a.txt").read_bytes(), before["a.txt"])

    def test_patch_rollback_in_a_session_leaves_no_checkpoint(self):
        digest = Workspace(self.root).get_instructions()["digest"]

        def patch_call(messages):
            return {"tool_calls": [tool_call("p1", "apply_patch", {"operations": [
                {"op": "modify", "path": "a.txt", "expected_sha256": current_hash(self.root, "a.txt"),
                 "expected_instructions": digest, "edits": [{"old_text": "A0", "new_text": "A1"}]},
                {"op": "modify", "path": "b.txt", "expected_sha256": current_hash(self.root, "b.txt"),
                 "expected_instructions": digest, "edits": [{"old_text": "B0", "new_text": "B1"}]}]})]}

        count = []

        def fail_second(argv):
            if FS_SCRIPT in argv and argv[argv.index(FS_SCRIPT) + 3] == "put" and "force" not in argv:
                count.append(1)
                if len(count) == 2:
                    return 7, b"", b"baseagent-fs: write failed (injected)"
            return None

        self.fake.hook = fail_second
        code, _, err = self.run_cli([*DOCKER, "--mode", "edit", "--session", "p", "改"],
                                    Script([patch_call, {"content": "失败了"}]))
        self.assertEqual(code, 0, err)
        self.assertEqual((self.root / "a.txt").read_text(), "A0\n")
        self.assertEqual(self.store().checkpoints("p")["checkpoints"], [])

    def test_post_edit_check_and_diff_limit_work_in_the_container(self):
        backend = DockerBackend(self.root, runner=self.fake, owner=False)
        self.addCleanup(backend.close)
        failing = Workspace(backend=backend, allow_write=True, edit_policy=EditPolicy(post_edit_check=["sh", "-c", "exit 3"]))
        digest = failing.get_instructions()["digest"]
        with self.assertRaises(ToolFailure) as caught:
            failing.write_file("a.txt", "broken\n", current_hash(self.root, "a.txt"), digest)
        self.assertIn("post-edit check failed; edit rolled back", str(caught.exception))
        self.assertEqual((self.root / "a.txt").read_text(), "A0\n")
        with self.assertRaises(ToolFailure):
            failing.apply_patch([{"op": "delete", "path": "b.txt", "expected_sha256": current_hash(self.root, "b.txt"),
                                  "expected_instructions": digest}])
        self.assertEqual((self.root / "b.txt").read_text(), "B0\n")
        passing = Workspace(backend=backend, allow_write=True,
                            edit_policy=EditPolicy(post_edit_check=["grep", "-q", "fine", "a.txt"]))
        passing.write_file("a.txt", "fine\n", current_hash(self.root, "a.txt"), digest)
        self.assertEqual((self.root / "a.txt").read_text(), "fine\n")
        limited = Workspace(backend=backend, allow_write=True, edit_policy=EditPolicy(max_changed_lines=1))
        with self.assertRaises(ToolFailure) as caught:
            limited.write_file("a.txt", "1\n2\n3\n", current_hash(self.root, "a.txt"), digest)
        self.assertIn("max_changed_lines", str(caught.exception))
        self.assertEqual((self.root / "a.txt").read_text(), "fine\n")

    def test_backends_that_cannot_roll_back_refuse_post_edit_check_at_startup(self):
        from test_backends import MemoryBackend
        with self.assertRaises(ValueError) as caught:
            Workspace(backend=MemoryBackend(self.root / "env"), edit_policy=EditPolicy(post_edit_check=["true"]))
        self.assertIn("post_edit_check needs a backend that can roll an edit back", str(caught.exception))


# ----- D. failures and cleanup -----------------------------------------------------------------
class FailureTests(DockerCliCase):
    def test_docker_unavailable_fails_before_the_model_with_exit_3(self):
        cases = {"cli_missing": {"installed": False}, "daemon_unreachable": {"daemon": False},
                 "image_missing": {"images": ()}}
        for kind, options in cases.items():
            for fmt in ("text", "json", "jsonl"):
                with self.subTest(kind=kind, fmt=fmt):
                    self.fake = FakeDocker(**options)
                    model = Script([AssertionError("the model must not be called")])
                    code, out, err = self.run_cli([*DOCKER, "--output-format", fmt, "--session", f"{kind}-{fmt}", "hi"],
                                                  model)
                    self.assertEqual(code, BACKEND_UNAVAILABLE_EXIT)
                    self.assertEqual(model.seen, [])
                    self.assertIn(f"Docker error ({kind})", err)
                    self.assertIn({"cli_missing": "Install Docker", "daemon_unreachable": "Start Docker",
                                   "image_missing": "docker pull python:3.12-slim"}[kind], err)
                    self.assertIsNone(self.store().load(f"{kind}-{fmt}"))
                    if fmt == "text":
                        self.assertEqual(out, "")
                        continue
                    lines = [json.loads(line) for line in out.splitlines()]
                    value = lines[-1] if fmt == "json" else lines[-1]["data"]
                    self.assertEqual(value["status"], "error")
                    self.assertEqual(value["exit_code"], 3)
                    self.assertEqual(value["backend"]["unavailable"], kind)
                    self.assertEqual(value["backend"]["image"], "python:3.12-slim")
                    if fmt == "jsonl":
                        self.assertEqual([line["type"] for line in lines], ["output_started", "result"])

    def test_a_dead_container_fails_the_turn_readably_and_resume_continues(self):
        def kill_then_command(messages):
            self.fake.kill(self.backends[-1].name, oom=True)
            return {"tool_calls": [tool_call("c1", "run_command", {"argv": ["echo", "hi"]})]}

        code, out, err = self.run_cli([*DOCKER, "--mode", "full", "--session", "dead", "go"],
                                      Script([kill_then_command, {"content": "never"}]))
        self.assertEqual(code, 1)
        state = self.store().load("dead")
        # The workspace check before the next tool or model call sees the dead container and stops the turn.
        self.assertEqual(state.status, "failed")
        self.assertIn("is no longer running (out of memory, removed, or Docker restarted)", state.error)
        self.assertIn("--resume", state.error)
        self.assertIn("Agent error: DockerError", out)
        # Docker fixed: --resume starts a new container and finishes the turn.
        code, out, err = self.run_cli([*DOCKER, "--mode", "full", "--session", "dead", "--resume"],
                                      Script([{"content": "recovered"}]))
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "recovered")
        self.assertEqual(len(self.containers()), 2)

    def test_a_command_in_a_dead_container_returns_a_clear_tool_error(self):
        backend = DockerBackend(self.root, runner=self.fake, owner=False)
        self.addCleanup(backend.close)
        workspace = Workspace(backend=backend, allow_command=True)
        self.fake.kill(backend.name, oom=True)
        result = workspace.run_command(["echo", "hi"])
        self.assertFalse(result.ok)
        self.assertIn(f"Docker container {backend.name} is no longer running (out of memory", result.error.message)
        from baseagent.backends.docker import DockerError
        with self.assertRaises(DockerError) as caught:          # file tools raise; the turn then stops
            workspace.read_file("a.txt")
        self.assertIn("continue with --resume", str(caught.exception))
        self.fake.containers.pop(backend.name)                  # removed from outside
        self.assertFalse(workspace.run_command(["echo", "hi"]).ok)

    def test_container_is_removed_on_every_exit_path(self):
        cases = {
            "normal end": (["one-shot"], Script([{"content": "ok"}]), 0),
            "ctrl-c": (["one-shot"], Script([KeyboardInterrupt()]), 130),
            "chat /exit": (["chat", "/exit"], Script([]), 0),
            "chat ctrl-c at the prompt": (["chat", KeyboardInterrupt()], Script([]), 0),
            "chat ctrl-c during a turn": (["chat", "go", "/exit"], Script([KeyboardInterrupt()]), 0),
        }
        for label, (lines, model, expected) in cases.items():
            with self.subTest(label=label):
                before = len(self.containers())
                argv = [*DOCKER, "--session", label.replace(" ", "-").replace("/", "")]
                if lines[0] == "one-shot":
                    code, _, err = self.run_cli([*argv, "go"], model)
                else:
                    code, _, err = self.run_cli([*argv, "--no-main-agent"], model, lines[1:])
                self.assertEqual(code, expected, err)
                self.assertEqual(len(self.containers()), before + 1)
                self.assertIn(self.containers()[-1], self.fake.removed())
        self.assertEqual(self.fake.containers, {})

    def test_uncaught_exception_and_frontend_close_remove_the_container(self):
        def boom(session, **options):
            raise RuntimeError("frontend crashed")

        with patch(RUNNERS["cli"], boom), self.assertRaises(RuntimeError):
            main(["--root", str(self.root), "--db", str(self.db), *DOCKER, "--session", "boom"],
                 model_factory=lambda args: Script([]))
        self.assertIn(self.containers()[-1], self.fake.removed())
        for ui in ("tui", "web"):
            with self.subTest(ui=ui):
                seen = {}

                def frontend(session, **options):
                    session.open()
                    session.turn("改 a", ask=lambda view: (None, True))
                    seen["status"] = session.status()
                    seen["undo"] = session.command("/undo")
                    session.exit(force=True, why="window closed")
                    return 0

                model = Script([writes(self.root, ("a.txt", f"{ui}\n")), {"content": "ok"}])
                with patch(RUNNERS[ui], frontend), patch("baseagent.main._missing_frontend", return_value=None):
                    code = main(["--root", str(self.root), "--db", str(self.db), *DOCKER, "--session", ui,
                                 "--mode", "edit", "--no-main-agent", "--ui", ui], model_factory=lambda args: model)
                self.assertEqual(code, 0)
                self.assertEqual(seen["status"]["backend"]["kind"], "docker")
                self.assertEqual((self.root / "a.txt").read_text(), "A0\n")          # undone inside the frontend
                self.assertIn(self.containers()[-1], self.fake.removed())
        self.assertEqual(self.fake.containers, {})

    def test_leftover_containers_are_reaped_but_live_ones_are_kept(self):
        self.fake.add_stale("baseagent-old-dead", self.root, pid=2 ** 22 + 12345)     # no such process
        self.fake.add_stale("baseagent-old-live", self.root, pid=os.getpid())         # a live session
        self.fake.add_stale("baseagent-other-host", self.root, pid=2 ** 22 + 1, host="elsewhere")
        code, _, err = self.run_cli([*DOCKER, "--session", "reap", "hi"], Script([{"content": "ok"}]))
        self.assertEqual(code, 0, err)
        self.assertIn("Removed leftover container baseagent-old-dead", err)
        self.assertIn("baseagent-old-dead", self.fake.removed())
        self.assertIn("baseagent-old-live", self.fake.containers)
        self.assertIn("baseagent-other-host", self.fake.containers)


# ----- E. visibility ---------------------------------------------------------------------------
class VisibilityTests(DockerCliCase):
    def test_json_jsonl_status_and_stderr_name_the_backend(self):
        code, out, err = self.run_cli([*DOCKER, "--output-format", "json", "--session", "j", "hi"],
                                      Script([{"content": "ok"}]))
        self.assertEqual(code, 0, err)
        value = json.loads(out)
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["backend"], {"kind": "docker", "image": "python:3.12-slim", "network": "none",
                                            "container": self.containers()[-1], "cpus": 1, "memory": "512m",
                                            "pids": 128})
        self.assertIn("Backend: docker", err)
        code, out, err = self.run_cli([*DOCKER, "--docker-network", "bridge", "--output-format", "jsonl",
                                       "--session", "l", "hi"], Script([{"content": "ok"}]))
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([line["type"] for line in lines[:2]], ["output_started", "backend_ready"])
        self.assertEqual(lines[1]["data"]["network"], "bridge")
        self.assertEqual(lines[-1]["data"]["backend"]["network"], "bridge")
        code, out, _ = self.run_cli([*DOCKER, "--no-main-agent", "--session", "st"], Script([]), ["/status", "/exit"])
        self.assertIn("Backend: docker, image python:3.12-slim, no network access, container ", out)


class TerminalBenchTests(unittest.TestCase):
    def test_fixture_runs_through_the_docker_backend_offline(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import evaluate_terminal_bench
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = evaluate_terminal_bench.main(["--fixture", "--backend", "docker"])
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["success_rate"], 1)
        task = report["tasks"][0]
        self.assertEqual(task["backend"], "docker")
        self.assertEqual(task["docker"], "fake")
        self.assertTrue(task["container_removed"])
        self.assertEqual(report["provider_calls"], 0)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            evaluate_terminal_bench.main(["--live", "--budget-cny", "15"])
        plan = json.loads(stdout.getvalue())
        self.assertIn("--backend docker", plan["agent_command"])
        self.assertFalse(plan["executed"])


if __name__ == "__main__":
    unittest.main()
