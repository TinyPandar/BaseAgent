"""File checkpoints and undo (README plan A, AC 1-10 and 22). Fake models only; no provider calls.

Turns run through the real agent loop and workspace tools, so every write goes through
``Workspace._write_file``. Restores are checked byte for byte and by SHA-256.
"""

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from checkpoint_helpers import Script, current_hash, tool_call, writes

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.capabilities import assemble_capabilities
from baseagent.session import (
    CheckpointError,
    SessionStore,
    list_checkpoints,
    rewind_session,
)
from baseagent.session import checkpoints as checkpoint_module
from baseagent.tools.edit_policy import EditPolicy
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.result import ErrorCode
from baseagent.tools.workspace import Workspace, parse_git_status

HAS_GIT = shutil.which("git") is not None


def digest(data):
    return sha256(data).hexdigest()


class CheckpointTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "work"
        self.root.mkdir()
        self.db = Path(self.temp.name).resolve() / "state" / "sessions.sqlite3"
        self.store = SessionStore(self.db)

    def protected(self):
        return [self.store.path, self.store.lock_dir]

    def workspace(self, **kwargs):
        return Workspace(self.root, allow_write=True, protected_paths=self.protected(), **kwargs)

    def turn(self, steps, prompt="change files", session="s", workspace=None, **kwargs):
        workspace = workspace or self.workspace()
        model = Script(steps)
        state = run_agent(model, prompt, tools=assemble_capabilities(workspace), store=self.store,
                          session_id=session, workspace_root=self.root, **kwargs)
        return state, model

    def results(self, state):
        """Tool results of the current turn only."""
        return [json.loads(message["content"]) for message in state.messages[state.turn_start:]
                if message.get("role") == "tool"]

    def counts(self):
        with self.store._connection() as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("checkpoints", "checkpoint_files", "checkpoint_blobs")}


class CaptureAndUndoTests(CheckpointTestCase):
    def test_undo_restores_three_files_bytes_hashes_and_modes_and_deletes_a_new_file(self):
        originals = {"a.bin": b"\xff\xfe raw bytes\r\nsecond line\r\n", "b.txt": b"plain\n"}
        for name, data in originals.items():
            (self.root / name).write_bytes(data)
        os.chmod(self.root / "a.bin", 0o640)
        state, _ = self.turn([writes(self.root, ("a.bin", "A2\n"), ("b.txt", "B2\n"), ("dir/new.txt", "N\n")),
                              {"content": "changed 3 files"}])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertTrue(all(result["ok"] for result in self.results(state)))
        self.assertEqual((self.root / "a.bin").read_bytes(), b"A2\n")
        listing = list_checkpoints(self.store, "s")
        (checkpoint,) = listing["checkpoints"]
        self.assertEqual((checkpoint["turn"], checkpoint["file_count"], checkpoint["restored"]), (1, 3, False))
        self.assertEqual({item["path"]: item["change"] for item in checkpoint["files"]},
                         {"a.bin": "modified", "b.txt": "modified", "dir/new.txt": "created"})
        self.assertEqual({item["path"]: item["before_sha256"] for item in checkpoint["files"]},
                         {"a.bin": digest(originals["a.bin"]), "b.txt": digest(originals["b.txt"]), "dir/new.txt": "missing"})

        result = rewind_session(self.store, self.workspace(), "s", "last")
        self.assertEqual(result["restored_turns"], [1])
        for name, data in originals.items():
            self.assertEqual((self.root / name).read_bytes(), data)
            self.assertEqual(current_hash(self.root, name), digest(data))
        self.assertEqual(os.stat(self.root / "a.bin").st_mode & 0o777, 0o640)
        self.assertFalse((self.root / "dir" / "new.txt").exists())
        self.assertEqual({item["path"]: item["action"] for item in result["files"]},
                         {"a.bin": "restored", "b.txt": "restored", "dir/new.txt": "deleted"})
        self.assertTrue(list_checkpoints(self.store, "s")["checkpoints"][0]["restored"])
        self.assertEqual(list(self.root.rglob(".baseagent-edit-*")), [])

    def test_second_write_in_the_same_turn_keeps_the_first_original(self):
        (self.root / "a.txt").write_text("original\n")
        state, _ = self.turn([writes(self.root, ("a.txt", "first\n")), writes(self.root, ("a.txt", "second\n"), prefix="x"),
                              {"content": "done"}])
        (entry,) = list_checkpoints(self.store, "s")["checkpoints"][0]["files"]
        self.assertEqual(entry["before_sha256"], digest(b"original\n"))
        self.assertEqual(entry["after_sha256"], digest(b"second\n"))
        self.assertEqual(self.counts(), {"checkpoints": 1, "checkpoint_files": 1, "checkpoint_blobs": 1})
        self.store.rewind(self.workspace(), "s")
        self.assertEqual((self.root / "a.txt").read_text(), "original\n")

    def test_only_files_written_by_tools_are_stored_and_identical_originals_once(self):
        for name in ("a.txt", "b.txt", "untouched.txt"):
            (self.root / name).write_text("same\n")
        self.turn([writes(self.root, ("a.txt", "A\n"), ("b.txt", "B\n")), {"content": "ok"}])
        self.assertEqual(self.counts(), {"checkpoints": 1, "checkpoint_files": 2, "checkpoint_blobs": 1})
        paths = [item["path"] for item in list_checkpoints(self.store, "s")["checkpoints"][0]["files"]]
        self.assertNotIn("untouched.txt", paths)

    def test_failed_write_and_post_edit_check_rollback_leave_no_record(self):
        (self.root / "a.txt").write_text("A\n")
        stale = tool_call("c1", "write_file", {"path": "a.txt", "content": "x", "expected_sha256": "0" * 64,
                                               "expected_instructions": Workspace(self.root).get_instructions("a.txt")["digest"]})
        state, _ = self.turn([{"tool_calls": [stale]}, {"content": "conflict"}])
        self.assertEqual(self.results(state)[0]["error"]["code"], ErrorCode.CONFLICT)
        self.assertEqual(self.counts(), {"checkpoints": 0, "checkpoint_files": 0, "checkpoint_blobs": 0})
        failing = self.workspace(edit_policy=EditPolicy(post_edit_check=[sys.executable, "-c", "raise SystemExit(1)"]))
        state, _ = self.turn([writes(self.root, ("a.txt", "B\n")), {"content": "rolled back"}], workspace=failing)
        self.assertIn("rolled back", self.results(state)[0]["error"]["message"])
        self.assertEqual((self.root / "a.txt").read_text(), "A\n")
        self.assertEqual(self.counts(), {"checkpoints": 0, "checkpoint_files": 0, "checkpoint_blobs": 0})

    def test_unsaved_original_fails_closed_without_writing(self):
        (self.root / "a.txt").write_text("A" * 64)
        with patch.object(checkpoint_module, "MAX_BLOB_BYTES", 16):
            state, _ = self.turn([writes(self.root, ("a.txt", "small\n")), {"content": "no"}])
        error = self.results(state)[0]["error"]
        self.assertEqual(error["code"], ErrorCode.LIMIT_EXCEEDED)
        self.assertIn("checkpoint could not save the original file, so it was not written", error["message"])
        self.assertEqual((self.root / "a.txt").read_text(), "A" * 64)

        def broken(*args, **kwargs):
            raise OSError("disk read failed")

        with patch("baseagent.backends.local.LocalBackend.read_bytes", broken):
            state, _ = self.turn([writes(self.root, ("a.txt", "small\n")), {"content": "no"}], prompt="again")
        self.assertIn("so it was not written", self.results(state)[0]["error"]["message"])
        self.assertEqual((self.root / "a.txt").read_text(), "A" * 64)

        @contextlib.contextmanager
        def failing(store):
            raise sqlite3.OperationalError("database is locked")
            yield  # pragma: no cover

        original = checkpoint_module.capture

        def capture(workspace, path, content, context):
            with patch.object(SessionStore, "_connection", failing):
                return original(workspace, path, content, context)

        with patch.object(checkpoint_module, "capture", capture):
            state, _ = self.turn([writes(self.root, ("new.txt", "x\n")), {"content": "no"}], prompt="third")
        self.assertIn("checkpoint could not be recorded", self.results(state)[0]["error"]["message"])
        self.assertFalse((self.root / "new.txt").exists())

    def test_file_limit_per_turn_matches_the_observed_limit(self):
        self.assertEqual(checkpoint_module.MAX_FILES_PER_TURN, 500)
        with patch.object(checkpoint_module, "MAX_FILES_PER_TURN", 2):
            state, _ = self.turn([writes(self.root, ("a.txt", "a"), ("b.txt", "b"), ("c.txt", "c")), {"content": "ok"}])
        results = self.results(state)
        self.assertEqual([result["ok"] for result in results], [True, True, False])
        self.assertIn("checkpoint file limit is 2 per turn", results[2]["error"]["message"])
        self.assertFalse((self.root / "c.txt").exists())

    def test_tools_without_a_session_store_write_without_checkpoints(self):
        workspace = self.workspace()
        model = Script([writes(self.root, ("a.txt", "x")), {"content": "ok"}])
        # Spill off: a store-less run would otherwise leave a temporary directory for the garbage collector.
        state = run_agent(model, "write", tools=assemble_capabilities(workspace), runtime_config={"spill": {"enabled": False}})
        self.assertTrue(self.results(state)[0]["ok"])
        self.assertEqual(self.counts()["checkpoint_files"], 0)


class UndoRulesTests(CheckpointTestCase):
    def three_turns(self):
        (self.root / "a.txt").write_text("A0\n")
        (self.root / "b.txt").write_text("B0\n")
        self.turn([writes(self.root, ("a.txt", "A1\n")), {"content": "t1"}], prompt="one")
        self.turn([writes(self.root, ("a.txt", "A2\n"), ("b.txt", "B2\n")), {"content": "t2"}], prompt="two")
        self.turn([writes(self.root, ("c.txt", "C3\n")), {"content": "t3"}], prompt="three")

    def test_manual_change_is_refused_for_the_whole_batch_until_force(self):
        (self.root / "a.txt").write_text("A0\n")
        self.turn([writes(self.root, ("a.txt", "A1\n"), ("b.txt", "B1\n")), {"content": "ok"}])
        (self.root / "a.txt").write_text("edited by hand\n")
        with self.assertRaises(CheckpointError) as refused:
            self.store.rewind(self.workspace(), "s")
        self.assertEqual(refused.exception.reason, "conflict")
        self.assertEqual(refused.exception.conflicts, ["a.txt"])
        self.assertIn("--force", str(refused.exception))
        self.assertEqual((self.root / "a.txt").read_text(), "edited by hand\n")
        self.assertEqual((self.root / "b.txt").read_text(), "B1\n")   # nothing in the batch was written
        self.assertFalse(list_checkpoints(self.store, "s")["checkpoints"][0]["restored"])
        result = self.store.rewind(self.workspace(), "s", force=True)
        self.assertTrue(result["forced"])
        self.assertEqual((self.root / "a.txt").read_text(), "A0\n")
        self.assertFalse((self.root / "b.txt").exists())

    def test_rewind_n_undoes_that_turn_and_every_later_one_then_undo_takes_the_rest(self):
        self.three_turns()
        result = self.store.rewind(self.workspace(), "s", 2)
        self.assertEqual(result["restored_turns"], [3, 2])
        self.assertEqual((self.root / "a.txt").read_text(), "A1\n")
        self.assertEqual((self.root / "b.txt").read_text(), "B0\n")
        self.assertFalse((self.root / "c.txt").exists())
        listing = list_checkpoints(self.store, "s")["checkpoints"]
        self.assertEqual([(item["turn"], item["restored"]) for item in listing], [(1, False), (2, True), (3, True)])
        self.assertEqual(self.store.rewind(self.workspace(), "s", "last")["restored_turns"], [1])
        self.assertEqual((self.root / "a.txt").read_text(), "A0\n")
        with self.assertRaises(CheckpointError) as nothing:
            self.store.rewind(self.workspace(), "s")
        self.assertEqual(nothing.exception.reason, "nothing_to_undo")
        restored = [item for item in self.store.events("s", limit=1000) if item["type"] == "checkpoint_restored"]
        self.assertEqual([item["data"]["turn"] for item in restored], [3, 2, 1])
        turn2 = restored[1]["data"]
        self.assertEqual(turn2["file_count"], 2)
        self.assertEqual(turn2["files"][0], {"path": "a.txt", "from_sha256": digest(b"A2\n"), "to_sha256": digest(b"A1\n")})
        self.assertNotIn("A2", json.dumps(restored))      # hashes only, never contents

    def test_rewind_number_validation(self):
        self.three_turns()
        with self.assertRaises(CheckpointError) as late:
            self.store.rewind(self.workspace(), "s", 4)
        self.assertEqual(late.exception.reason, "nothing_to_undo")
        self.assertEqual(checkpoint_module.parse_target("LAST"), "last")
        self.assertEqual(checkpoint_module.parse_target("2"), 2)
        for bad in ("0", "-1", "x", "1.5"):
            with self.assertRaises(ValueError):
                checkpoint_module.parse_target(bad)

    def test_undo_after_reopening_the_database_in_a_new_process(self):
        self.three_turns()
        script = ("import sys, json; from baseagent.session import SessionStore; from baseagent.tools.workspace import Workspace;"
                  "store = SessionStore(sys.argv[1]); ws = Workspace(sys.argv[2], protected_paths=[store.path, store.lock_dir]);"
                  "print(json.dumps(store.rewind(ws, 's', 'last')))")
        completed = subprocess.run([sys.executable, "-c", script, str(self.db), str(self.root)], capture_output=True,
                                   text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["restored_turns"], [3])
        self.assertFalse((self.root / "c.txt").exists())

    def test_next_turn_tells_the_agent_which_turn_was_undone(self):
        self.three_turns()
        self.store.rewind(self.workspace(), "s", 2)
        state, model = self.turn([{"content": "noted"}], prompt="four")
        notices = [message["content"] for message in model.seen[0] if message["role"] == "system"][1:]
        self.assertEqual(len(notices), 1)
        self.assertIn("第 2、3 轮的文件改动已撤回", notices[0])
        self.assertNotIn("checkpoint_notices", state.metadata)
        _, later = self.turn([{"content": "again"}], prompt="five")
        self.assertEqual(sum("已撤回" in message["content"] for message in later.seen[0] if message["role"] == "system"), 1)

    def test_turn_numbers_continue_after_undo_and_an_undone_turn_stays_closed(self):
        self.three_turns()
        self.store.rewind(self.workspace(), "s")
        self.turn([writes(self.root, ("d.txt", "D\n")), {"content": "t4"}], prompt="four")
        turns = [item["turn"] for item in list_checkpoints(self.store, "s")["checkpoints"]]
        self.assertEqual(turns, [1, 2, 3, 4])
        self.assertEqual(checkpoint_module.turn_number(self.store.load("s")), 4)

    def test_refused_while_the_turn_is_unfinished_awaiting_approval_or_running(self):
        (self.root / "a.txt").write_text("A0\n")
        self.turn([writes(self.root, ("a.txt", "A1\n")), {"content": "t1"}], prompt="one")
        policy = ToolPolicy.from_dict({"default": "allow", "tools": {"write_file": "ask"}})
        state, _ = self.turn([writes(self.root, ("a.txt", "A2\n"))], prompt="two", tool_policy=policy)
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        with self.assertRaises(CheckpointError) as waiting:
            self.store.rewind(self.workspace(), "s")
        self.assertEqual(waiting.exception.reason, "turn_unfinished")
        self.assertIn("等待审批", str(waiting.exception))
        self.assertEqual((self.root / "a.txt").read_text(), "A1\n")
        state = self.store.load("s")
        state.status = RunStatus.COMPLETED
        self.store.save(state)
        with self.store.exclusive("s"):
            with self.assertRaises(CheckpointError) as running:
                self.store.rewind(self.workspace(), "s")
        self.assertEqual(running.exception.reason, "turn_running")

    def test_refused_while_a_persisted_writable_worker_still_runs_elsewhere(self):
        (self.root / "a.txt").write_text("A0\n")
        self.turn([writes(self.root, ("a.txt", "A1\n")), {"content": "t1"}])
        state = self.store.load("s")
        state.metadata["workers"] = {"w1": {"id": "w1", "session_id": "s-w1", "allow_writes": True, "status": "running"},
                                     "w2": {"id": "w2", "session_id": "s-w2", "allow_writes": False, "status": "running"}}
        self.store.save(state)
        with self.store.exclusive("s-w1"):     # another process is running w1
            with self.assertRaises(CheckpointError) as busy:
                self.store.rewind(self.workspace(), "s")
        self.assertEqual(busy.exception.reason, "writer_active")
        self.assertIn("w1（运行中）", str(busy.exception))
        # The lock is free: that process is gone and the worker can no longer write.
        self.assertEqual(self.store.rewind(self.workspace(), "s")["restored_turns"], [1])

    def test_write_back_stays_inside_the_workspace_and_protected_paths(self):
        (self.root / "a.txt").write_text("A0\n")
        self.turn([writes(self.root, ("a.txt", "A1\n")), {"content": "t1"}])
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("A1\n")
        (self.root / "a.txt").unlink()
        (self.root / "a.txt").symlink_to(outside)
        with self.assertRaises(CheckpointError) as refused:
            self.store.rewind(self.workspace(), "s", force=True)
        self.assertEqual(refused.exception.reason, "protected_path")
        self.assertEqual(outside.read_text(), "A1\n")
        other = Path(self.temp.name) / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "different workspace"):
            self.store.rewind(Workspace(other), "s")

    def test_delete_session_and_cleanup_collect_unreferenced_blocks(self):
        (self.root / "shared.txt").write_text("same original\n")
        (self.root / "only.txt").write_text("only in s\n")
        self.turn([writes(self.root, ("shared.txt", "x\n"), ("only.txt", "y\n")), {"content": "ok"}])
        (self.root / "shared.txt").write_text("same original\n")
        self.turn([writes(self.root, ("shared.txt", "z\n")), {"content": "ok"}], session="other")
        self.assertEqual(self.counts(), {"checkpoints": 2, "checkpoint_files": 3, "checkpoint_blobs": 2})
        self.store.delete_session("s")
        self.assertEqual(self.counts(), {"checkpoints": 1, "checkpoint_files": 1, "checkpoint_blobs": 1})
        with self.store._connection() as connection:
            connection.execute("UPDATE sessions SET updated_at='2000-01-01 00:00:00' WHERE id='other'")
        self.assertEqual(self.store.cleanup(1, dry_run=False)["deleted"], ["other"])
        self.assertEqual(self.counts(), {"checkpoints": 0, "checkpoint_files": 0, "checkpoint_blobs": 0})


class MigrationTests(CheckpointTestCase):
    def test_v6_database_upgrades_to_v7_without_losing_sessions(self):
        (self.root / "a.txt").write_text("A0\n")
        self.turn([writes(self.root, ("a.txt", "A1\n")), {"content": "t1"}], prompt="one")
        before = (self.store.load("s").to_dict(), self.store.calls("s", self.store.load("s").turn_id), self.store.events("s", limit=1000))
        with self.store._connection() as connection:
            for table in ("checkpoint_files", "checkpoints", "checkpoint_blobs"):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("PRAGMA user_version=6")
            self.assertEqual({row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {"sessions", "tool_calls", "events", "sqlite_sequence", "cancellation_requests",
                              "event_retention", "shared_memories", "task_nodes"})
        upgraded = SessionStore(self.db)
        with upgraded._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 7)
        state = upgraded.load("s")
        self.assertEqual((state.to_dict(), upgraded.calls("s", state.turn_id), upgraded.events("s", limit=1000)), before)
        self.assertEqual(upgraded.checkpoints("s")["checkpoints"], [])
        # The old session continues: its next turn is turn 2 and can be undone.
        self.store = upgraded
        self.turn([writes(self.root, ("a.txt", "A2\n")), {"content": "t2"}], prompt="two")
        self.assertEqual([item["turn"] for item in upgraded.checkpoints("s")["checkpoints"]], [2])
        upgraded.rewind(self.workspace(), "s")
        self.assertEqual((self.root / "a.txt").read_text(), "A1\n")
        with upgraded._connection() as connection:
            connection.execute("PRAGMA user_version=8")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            SessionStore(self.db)


class CommandLineTests(CheckpointTestCase):
    def cli(self, *argv):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("DEEPSEEK_", "BASEAGENT_LLM_", "OPENAI_", "ANTHROPIC_"))}
        return subprocess.run([sys.executable, "-m", "baseagent", "--db", str(self.db), *argv], capture_output=True,
                              text=True, timeout=30, env=env, cwd=self.temp.name)

    def test_checkpoints_and_rewind_run_without_a_model_or_keys(self):
        (self.root / "a.txt").write_text("A0\n")
        self.turn([writes(self.root, ("a.txt", "A1\n"), ("new.txt", "N\n")), {"content": "t1"}], prompt="one")
        self.turn([writes(self.root, ("a.txt", "A2\n")), {"content": "t2"}], prompt="two")
        listed = self.cli("--session", "s", "--checkpoints")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([item["turn"] for item in json.loads(listed.stdout)["checkpoints"]], [1, 2])
        (self.root / "a.txt").write_text("hand edit\n")
        refused = self.cli("--session", "s", "--rewind", "last")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("Session error: 撤销被拒绝", refused.stderr)
        self.assertEqual(refused.stdout, "")
        forced = self.cli("--session", "s", "--rewind", "last", "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(json.loads(forced.stdout)["restored_turns"], [2])
        self.assertEqual((self.root / "a.txt").read_text(), "A1\n")
        rewound = self.cli("--session", "s", "--rewind", "1")
        self.assertEqual(rewound.returncode, 0, rewound.stderr)
        self.assertEqual((self.root / "a.txt").read_text(), "A0\n")
        self.assertFalse((self.root / "new.txt").exists())
        events = self.cli("--session", "s", "--events", "--event-limit", "1000")
        self.assertEqual([item["data"]["turn"] for item in json.loads(events.stdout) if item["type"] == "checkpoint_restored"], [2, 1])

    def test_argument_errors(self):
        self.turn([{"content": "t1"}])
        for argv, message in ((["--session", "s", "--rewind", "0"], "positive turn number"),
                              (["--session", "s", "--checkpoints", "--force"], "--force applies to --rewind"),
                              (["--checkpoints"], "require --session")):
            completed = self.cli(*argv)
            self.assertEqual(completed.returncode, 2, argv)
            self.assertIn(message, completed.stderr)
        empty = self.cli("--session", "s", "--rewind", "last")
        self.assertEqual(empty.returncode, 1)
        self.assertIn("没有可撤销的检查点", empty.stderr)


class GitStatusTests(CheckpointTestCase):
    def git(self, *argv):
        subprocess.run(["git", *argv], cwd=self.root, check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_git_status_in_a_repository(self):
        self.git("init", "-q", "-b", "main")
        (self.root / "tracked.txt").write_text("one\n")
        (self.root / "gone.txt").write_text("bye\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "init")
        tools = assemble_capabilities(Workspace(self.root))
        clean = tools.execute("git_status", {})
        self.assertTrue(clean.ok, clean.error)
        self.assertEqual((clean.data["clean"], clean.data["entries"], clean.data["branch"]), (True, [], "main"))
        (self.root / "tracked.txt").write_text("two\n")
        (self.root / "gone.txt").unlink()
        (self.root / "new file.txt").write_text("new\n")
        (self.root / ".baseagent").mkdir()
        (self.root / ".baseagent" / "lock").write_text("harness state")
        dirty = tools.execute("git_status", {})
        self.assertFalse(dirty.data["clean"])
        self.assertEqual({(item["path"], item["status"]) for item in dirty.data["entries"]},
                         {("tracked.txt", "modified"), ("gone.txt", "deleted"), ("new file.txt", "untracked")})
        self.assertEqual(dirty.data["harness_paths_hidden"], 1)

    def test_git_status_outside_a_repository_or_without_git_fails_readably(self):
        tools = assemble_capabilities(Workspace(self.root))
        if HAS_GIT:
            with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root.parent)}):
                outside = tools.execute("git_status", {})
            self.assertFalse(outside.ok)
            self.assertEqual(outside.error.message, "workspace is not a git repository")
        with patch("baseagent.backends.local.run_process", side_effect=FileNotFoundError("git")):
            missing = tools.execute("git_status", {})
        self.assertEqual((missing.error.code, missing.error.message), (ErrorCode.NOT_FOUND, "git is not installed or not on PATH"))

    def test_git_status_is_read_only_and_available_to_read_only_workers(self):
        tools = assemble_capabilities(Workspace(self.root))
        self.assertTrue(tools.retry_safe("git_status"))
        from baseagent.agent.workers import WorkerManager, WorkerRecord
        manager = WorkerManager(tools, system_prompt="p", store=self.store, runner=lambda *a, **k: None)
        reader = manager.tools_for(WorkerRecord("w1", "s-w1", "look", False))
        self.assertIn("git_status", reader._tools)
        self.assertNotIn("write_file", reader._tools)

    def test_porcelain_parser_handles_renames_branch_and_unmerged(self):
        output = "## main...origin/main [ahead 1]\0R  new.py\0old.py\0 M a b.txt\0UU conflict.txt\0?? dir/\0A  staged.py\0"
        parsed = parse_git_status(output)
        self.assertEqual(parsed["branch"], "main...origin/main [ahead 1]")
        self.assertEqual([(item["path"], item["status"]) for item in parsed["entries"]],
                         [("new.py", "renamed"), ("a b.txt", "modified"), ("conflict.txt", "unmerged"),
                          ("dir/", "untracked"), ("staged.py", "added")])
        self.assertEqual(parsed["entries"][0]["orig_path"], "old.py")
        self.assertEqual(parse_git_status("", truncated=True)["clean"], False)


if __name__ == "__main__":
    unittest.main()
