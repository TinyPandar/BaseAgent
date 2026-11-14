"""apply_patch through the agent loop: checkpoints and /undo, approvals, policy and hooks, backends.

README 施工段 AC 6, 7, 9-15, 19 (checkpoint part), 22, 24, 25. Fake models only; no provider calls.
"""

import json
import tempfile
from dataclasses import dataclass
import unittest
from pathlib import Path
from unittest.mock import patch

from checkpoint_helpers import Script, write_call
from patch_helpers import current_hash, operations, patch_call, patch_step, tree

from baseagent.agent import run_agent
from baseagent.agent.gateway import restrict
from baseagent.agent.router import WRITE_TOOLS
from baseagent.agent.state import RunStatus
from baseagent.backends import LocalBackend
from baseagent.capabilities import assemble_capabilities
from baseagent.main import main
from baseagent.middleware import AgentMiddleware
from baseagent.middleware.execution_guidance import ExecutionGuidanceMiddleware
from baseagent.session import SessionStore, list_checkpoints, rewind_session
from baseagent.session import checkpoints as checkpoint_module
from baseagent.tools.edit_policy import EditPolicy
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.workspace import Workspace, coding_tools

ORIGINAL = {"a.txt": b"one\ntwo\n", "b.txt": b"bee\r\n", "keep.txt": b"keep\n"}


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "work"
        self.root.mkdir()
        for name, data in ORIGINAL.items():
            (self.root / name).write_bytes(data)
        self.db = Path(self.temp.name).resolve() / "state" / "sessions.sqlite3"
        self.store = SessionStore(self.db)

    def workspace(self, **kwargs):
        return Workspace(self.root, allow_write=True, protected_paths=[self.store.path, self.store.lock_dir], **kwargs)

    def turn(self, steps, session="s", workspace=None, prompt="change", **kwargs):
        model = Script(steps)
        state = run_agent(model, prompt, tools=assemble_capabilities(workspace or self.workspace()), store=self.store,
                          session_id=session, workspace_root=self.root, **kwargs)
        return state, model

    def results(self, state):
        return [json.loads(message["content"]) for message in state.messages[state.turn_start:]
                if message.get("role") == "tool"]

    def three(self):
        return patch_step(self.root, ("delete", "b.txt"), ("add", "new/n.txt", "n\n"),
                          ("modify", "a.txt", [{"old_text": "two", "new_text": "TWO"}]))


class CheckpointUndoTests(SessionTestCase):
    def test_one_checkpoint_lists_created_modified_deleted_and_undo_restores_all(self):
        # AC 14, 15, 22.
        state, _ = self.turn([self.three(), {"content": "done"}])
        self.assertTrue(self.results(state)[0]["ok"])
        (checkpoint,) = list_checkpoints(self.store, "s")["checkpoints"]
        self.assertEqual({item["path"]: item["change"] for item in checkpoint["files"]},
                         {"b.txt": "deleted", "new/n.txt": "created", "a.txt": "modified"})
        text = checkpoint_module.describe(list_checkpoints(self.store, "s"))
        for line in ("删除 b.txt", "新建 new/n.txt", "修改 a.txt"):
            self.assertIn(line, text)
        result = rewind_session(self.store, self.workspace(), "s", "last")
        self.assertEqual({item["path"]: item["action"] for item in result["files"]},
                         {"a.txt": "restored", "b.txt": "restored", "new/n.txt": "deleted"})
        self.assertEqual(tree(self.root), ORIGINAL)

    def test_write_file_then_apply_patch_on_the_same_file_undoes_to_the_turn_start(self):
        state, _ = self.turn([lambda messages: {"tool_calls": [write_call(self.root, "w1", "a.txt", "written\n")]},
                              patch_step(self.root, ("modify", "a.txt", [{"old_text": "written", "new_text": "patched"}]),
                                         ("add", "other.txt", "o\n")),
                              {"content": "done"}])
        self.assertTrue(all(result["ok"] for result in self.results(state)))
        self.assertEqual((self.root / "a.txt").read_text(), "patched\n")
        rewind_session(self.store, self.workspace(), "s", "last")
        self.assertEqual(tree(self.root), ORIGINAL)

    def test_cli_rewind_last_undoes_the_batch(self):
        self.turn([self.three(), {"content": "done"}])
        code = main(["--root", str(self.root), "--db", str(self.db), "--session", "s", "--rewind", "last"])
        self.assertEqual(code, 0)
        self.assertEqual(tree(self.root), ORIGINAL)

    def test_rolled_back_batch_leaves_no_checkpoint_record_and_earlier_records_stay(self):
        # AC 6 and 19: an earlier write of this turn keeps its record; the failed batch leaves none.
        original = LocalBackend.write_file

        def write(backend, path, *args):
            if path == "fail.txt":
                raise OSError("injected")
            return original(backend, path, *args)

        with patch.object(LocalBackend, "write_file", write):
            state, _ = self.turn([lambda messages: {"tool_calls": [write_call(self.root, "w1", "keep.txt", "kept\n")]},
                                  patch_step(self.root, ("modify", "a.txt", [{"old_text": "two", "new_text": "2"}]),
                                             ("delete", "b.txt"), ("add", "fail.txt", "x")),
                                  {"content": "done"}])
        results = self.results(state)
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        self.assertTrue(results[1]["error"]["message"].startswith("fail.txt:"))
        self.assertEqual(tree(self.root), {**ORIGINAL, "keep.txt": b"kept\n"})
        (checkpoint,) = list_checkpoints(self.store, "s")["checkpoints"]
        self.assertEqual([item["path"] for item in checkpoint["files"]], ["keep.txt"])
        self.assertEqual(set(state.metadata["observed_files"]), {"keep.txt"})

    def test_failed_rollback_keeps_the_originals_and_undo_recovers(self):
        original = LocalBackend.write_file
        seen = []

        def write(backend, path, *args):
            seen.append(path)
            if path == "fail.txt" or (path == "a.txt" and seen.count("a.txt") == 2):
                raise OSError("injected")
            return original(backend, path, *args)

        with patch.object(LocalBackend, "write_file", write):
            state, _ = self.turn([patch_step(self.root, ("modify", "a.txt", [{"old_text": "two", "new_text": "2"}]),
                                             ("delete", "b.txt"), ("add", "fail.txt", "x")),
                                  {"content": "done"}])
        error = self.results(state)[0]["error"]
        self.assertIn("state unknown for a.txt", error["message"])
        self.assertEqual((self.root / "a.txt").read_text(), "one\n2\n")
        (checkpoint,) = list_checkpoints(self.store, "s")["checkpoints"]
        self.assertIn("a.txt", [item["path"] for item in checkpoint["files"]])
        rewind_session(self.store, self.workspace(), "s", "last")
        self.assertEqual(tree(self.root), ORIGINAL)

    def test_checkpoint_file_limit_rejects_the_whole_batch(self):
        with patch.object(checkpoint_module, "MAX_FILES_PER_TURN", 2):
            state, _ = self.turn([self.three(), {"content": "done"}])
        error = self.results(state)[0]["error"]
        self.assertEqual(error["code"], "limit_exceeded")
        self.assertIn("no file was written", error["message"])
        self.assertEqual(tree(self.root), ORIGINAL)

    def test_patch_applied_event_is_metadata_only(self):
        state, _ = self.turn([self.three(), {"content": "done"}])
        events = [item for item in self.store.events("s", limit=1000) if item["type"] == "patch_applied"]
        self.assertEqual(len(events), 1)
        data = events[0]["data"]
        self.assertEqual(data["call_id"], "p1")
        self.assertEqual(data["files"], [{"path": "b.txt", "op": "delete", "added": 0, "removed": 1},
                                         {"path": "new/n.txt", "op": "add", "added": 1, "removed": 0},
                                         {"path": "a.txt", "op": "modify", "added": 1, "removed": 1}])
        self.assertNotIn("TWO", json.dumps(events))


class ApprovalTests(SessionTestCase):
    POLICY = ToolPolicy({"apply_patch": "ask"})

    def pause(self, specs, session):
        state, _ = self.turn([patch_step(self.root, *specs)], session=session, tool_policy=self.POLICY)
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.assertEqual(tree(self.root), ORIGINAL)
        return state.metadata["approval_request"]

    def test_ask_waits_then_allow_applies_and_deny_writes_nothing(self):
        specs = (("delete", "b.txt"), ("modify", "a.txt", [{"old_text": "two", "new_text": "TWO"}]))
        for decision in ("allow", "deny"):
            with self.subTest(decision=decision):
                for name, data in ORIGINAL.items():
                    (self.root / name).write_bytes(data)
                session = f"ask-{decision}"
                request = self.pause(specs, session)
                self.assertEqual(request["name"], "apply_patch")
                self.store.decide_tool(session, request["call_id"], decision, request["request_digest"])
                state, _ = self.turn([{"content": "done"}], session=session, prompt=None, tool_policy=self.POLICY)
                self.assertEqual(state.status, RunStatus.COMPLETED)
                if decision == "allow":
                    self.assertFalse((self.root / "b.txt").exists())
                    self.assertEqual((self.root / "a.txt").read_text(), "one\nTWO\n")
                else:
                    self.assertEqual(tree(self.root), ORIGINAL)

    def test_digest_covers_every_operation(self):
        base = (("add", "n.txt", "content one"), ("modify", "a.txt", [{"old_text": "two", "new_text": "TWO"}]))
        first = self.pause(base, "d1")
        changed_content = self.pause((("add", "n.txt", "content two"), base[1]), "d2")
        changed_edit = self.pause((base[0], ("modify", "a.txt", [{"old_text": "two", "new_text": "2"}])), "d3")
        for session, request in (("d2", changed_content), ("d3", changed_edit)):
            self.assertNotEqual(request["request_digest"], first["request_digest"])
            with self.assertRaises(ValueError):
                self.store.decide_tool(session, request["call_id"], "allow", first["request_digest"])

    def test_approval_does_not_skip_validation_after_a_change(self):
        request = self.pause((("modify", "a.txt", [{"old_text": "two", "new_text": "TWO"}]), ("add", "n.txt", "n")),
                             "late")
        self.store.decide_tool("late", request["call_id"], "allow", request["request_digest"])
        (self.root / "a.txt").write_text("someone else\n")
        state, _ = self.turn([{"content": "done"}], session="late", prompt=None, tool_policy=self.POLICY)
        error = self.results(state)[0]["error"]
        self.assertEqual(error["code"], "conflict")
        self.assertTrue(error["message"].startswith("a.txt:"))
        self.assertFalse((self.root / "n.txt").exists())

    def test_no_policy_in_edit_mode_does_not_stop(self):
        state, _ = self.turn([self.three(), {"content": "done"}])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertNotIn("approval_request", state.metadata)

    def test_max_changed_lines_counts_the_whole_batch(self):
        # Each file changes 2 lines (under 3); together 4 lines (over 3): the batch waits for approval.
        workspace = self.workspace(edit_policy=EditPolicy(max_changed_lines=3))
        specs = (("modify", "a.txt", [{"old_text": "two", "new_text": "2"}]), ("add", "n.txt", "n1\nn2\n"))
        state, _ = self.turn([patch_step(self.root, *specs)], session="lines", workspace=workspace)
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        request = state.metadata["approval_request"]
        self.store.decide_tool("lines", request["call_id"], "allow", request["request_digest"])
        state, _ = self.turn([{"content": "done"}], session="lines", workspace=workspace, prompt=None)
        self.assertTrue(self.results(state)[0]["ok"])
        self.assertTrue((self.root / "n.txt").exists())
        # Library callers without an approval are refused before any write.
        result = coding_tools(workspace).execute("apply_patch", {"operations": operations(
            self.root, ("modify", "keep.txt", [{"old_text": "keep", "new_text": "k"}]), ("add", "m.txt", "1\n2\n3\n"))})
        self.assertEqual(result.error.code, "permission_denied")
        self.assertFalse((self.root / "m.txt").exists())

    def test_policy_deny_and_hooks_only_tighten(self):
        state, _ = self.turn([self.three(), {"content": "done"}], session="deny",
                             tool_policy=ToolPolicy({"apply_patch": "deny"}))
        self.assertEqual(self.results(state)[0]["error"]["code"], "permission_denied")
        self.assertEqual(tree(self.root), ORIGINAL)

        class Guard(AgentMiddleware):
            def __init__(self, action):
                self.action = action

            def before_tool(self, request):
                restrict(request, self.action, "test guard")

        state, _ = self.turn([self.three(), {"content": "done"}], session="hook-deny", middleware=[Guard("deny")])
        self.assertEqual(self.results(state)[0]["error"]["code"], "permission_denied")
        state, _ = self.turn([self.three()], session="hook-ask", middleware=[Guard("ask")])
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.assertEqual(tree(self.root), ORIGINAL)
        with self.assertRaises(ValueError):
            Guard("allow").before_tool(type("R", (), {"name": "apply_patch", "arguments": "{}", "call_id": "c",
                                                      "state": state})())


class PermissionTests(SessionTestCase):
    def test_read_only_hides_the_tool_and_a_forced_call_is_denied(self):
        workspace = Workspace(self.root, protected_paths=[self.store.path, self.store.lock_dir])
        call = patch_call("p1", operations(self.root, ("add", "n.txt", "x")))
        state, model = self.turn([{"tool_calls": [call]}, {"content": "done"}], workspace=workspace,
                                 middleware=[ExecutionGuidanceMiddleware(workspace)])
        self.assertNotIn("apply_patch", model.tools[0])
        self.assertIn("apply_patch", model.seen[0][1]["content"])     # listed under "Disabled tools"
        self.assertEqual(self.results(state)[0]["error"]["code"], "permission_denied")
        self.assertFalse((self.root / "n.txt").exists())
        state, model = self.turn([{"content": "done"}], session="edit",
                                 middleware=[ExecutionGuidanceMiddleware(self.workspace())])
        self.assertIn("apply_patch", model.tools[0])

    def test_guidance_text_is_unchanged_without_apply_patch(self):
        workspace = Workspace(self.root)
        captured = []
        tools = [spec for spec in coding_tools(workspace).specs() if spec["function"]["name"] != "apply_patch"]
        @dataclass
        class Fake:
            messages: list
            tools: list
            state: object

        ExecutionGuidanceMiddleware(workspace).wrap_model_call(
            Fake([{"role": "user", "content": "x"}], tools, type("S", (), {"messages": [], "turn_start": 0})()),
            lambda value: captured.append(value))
        self.assertIn("Disabled tools: edit_file, publish_memory, run_command, verify_command, withdraw_memory, write_file",
                      captured[0].messages[0]["content"])

    def test_counts_as_a_write_tool_for_workers(self):
        self.assertIn("apply_patch", WRITE_TOOLS)
        tools = coding_tools(self.workspace())
        readonly = [name for name in tools._tools if tools.retry_safe(name)]    # what a read-only worker gets
        self.assertNotIn("apply_patch", readonly)


class BackendTests(SessionTestCase):
    def test_local_backend_delete_is_compare_and_delete(self):
        backend = LocalBackend(self.root)
        digest = Workspace(self.root).get_instructions()["digest"]
        with self.assertRaises(Exception):
            backend.delete_file("a.txt", "0" * 64, digest)
        self.assertTrue((self.root / "a.txt").exists())
        result = backend.delete_file("a.txt", current_hash(self.root, "a.txt"), digest)
        self.assertEqual(result["sha256"], "missing")
        self.assertFalse((self.root / "a.txt").exists())

    def test_memory_backend_patch_never_touches_the_host(self):
        from test_backends import MemoryBackend
        backend = MemoryBackend(self.root / "absent-environment")
        workspace = Workspace(backend=backend, allow_write=True)
        digest = backend.get_instructions()["digest"]
        ops = [{"op": "modify", "path": "x.txt", "expected_sha256": backend.file_hash("x.txt"),
                "expected_instructions": digest, "edits": [{"old_text": "first", "new_text": "FIRST"}]},
               {"op": "add", "path": "y.txt", "expected_sha256": "missing", "expected_instructions": digest,
                "content": "why"}]
        with patch("pathlib.Path.open", side_effect=AssertionError("host I/O")):
            result = workspace.apply_patch(ops)
        self.assertEqual(backend.files["x.txt"], b"FIRST\r\nsecond\r\n")
        self.assertEqual(backend.files["y.txt"], b"why")
        self.assertEqual([item["op"] for item in result["files"]], ["modify", "add"])
        delete = [{"op": "delete", "path": "y.txt", "expected_sha256": backend.file_hash("y.txt"),
                   "expected_instructions": digest}]
        workspace.apply_patch(delete)
        self.assertNotIn("y.txt", backend.files)
        self.assertFalse(backend.root.exists())

    def test_docker_backend_delete_checks_the_hash_then_removes(self):
        from baseagent.backends import DockerBackend
        from baseagent.testing.docker import FakeDocker
        runner = FakeDocker()
        (self.root / "gone.txt").write_bytes(b"bye\n")
        backend = DockerBackend(self.root, runner=runner, owner=False)
        self.addCleanup(backend.close)
        with self.assertRaises(Exception):
            backend.delete_file("gone.txt", "0" * 64, None)
        self.assertTrue((self.root / "gone.txt").exists())
        deletes = [argv for argv in runner.calls if argv[1] == "exec" and "del" in argv]
        self.assertEqual(len(deletes), 1)                    # the hash is compared inside the container
        from hashlib import sha256
        self.assertEqual(backend.delete_file("gone.txt", sha256(b"bye\n").hexdigest(), None)["sha256"], "missing")
        self.assertFalse((self.root / "gone.txt").exists())

if __name__ == "__main__":
    unittest.main()
