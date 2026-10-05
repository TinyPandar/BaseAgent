from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from uuid import uuid4

from baseagent.agent.events import event
from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus, State
from baseagent.session import SessionStore
from baseagent.tools.result import ToolResult
from baseagent.tools.registry import ToolRegistry


def call(identifier):
    return {"id": identifier, "type": "function", "function": {"name": "delegate", "arguments": "{}"}}


class TaskPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.sqlite3")
        self.state = State(session_id="demo", turn_id="root-turn", workspace_root=str(self.root),
                           messages=[{"role": "system", "content": "system"}, {"role": "user", "content": "task"}])
        self.store.save(self.state, [call("delegate")])
        self.store.start_call(self.state, "delegate")

    def candidate(self):
        return State(session_id="demo", turn_id=uuid4().hex, workspace_root=str(self.root),
                     messages=[{"role": "system", "content": "child-system"}, {"role": "user", "content": "private child task"}])

    def create(self, **kwargs):
        return self.store.create_task(self.state, call_id="delegate", name="reader", state=self.candidate(), **kwargs)

    def finish_parent(self):
        self.store.complete_call(self.state, "delegate", ToolResult(data="done"))
        self.state.status = RunStatus.COMPLETED
        self.store.save(self.state)

    def test_creation_is_stable_and_changed_request_refuses_replacement(self):
        with self.store.exclusive("demo"):
            record = self.create()
            self.assertEqual(self.create()["task_id"], record["task_id"])
            view = self.store.task_node(self.state, record["task_id"])
            view.state.status = RunStatus.COMPLETED
            view.state.final_answer = "cached"
            view.save(view.state)
            self.assertEqual(self.create()["state"]["final_answer"], "cached")
            changed = self.candidate()
            changed.messages[-1]["content"] = "changed"
            with self.assertRaises(ValueError):
                self.store.create_task(self.state, call_id="delegate", name="reader", state=changed)
        self.assertEqual(len(self.store.task_tree("demo")["nodes"]), 1)
        self.assertNotIn("private child task", json.dumps(self.store.events("demo")))

    def test_nested_checkpoints_commit_ancestors_and_isolate_call_ids(self):
        with self.store.exclusive("demo"):
            parent = self.store.task_node(self.state, self.create()["task_id"])
            parent.save(parent.state, [call("delegate")])
            parent.start_call(parent.state, "delegate")
            record = self.store.create_task(self.state, parent_task_id=parent.task_id, call_id="delegate", name="reader", state=self.candidate())
            child = self.store.task_node(self.state, record["task_id"])
            child.parent.state.model_calls = 2
            self.state.model_calls = 3
            child.state.model_calls = 1
            child.save(child.state, [call("delegate")], event=event("model_started", attempt=1))
            child.start_call(child.state, "delegate")
            child.record_attempt(child.state, "delegate", {"name": "read", "arguments": {}})
            result = ToolResult(data="ok")
            child.record_terminal_result(child.state, "delegate", result)
            child.state.messages.append({"role": "tool", "tool_call_id": "delegate", "content": json.dumps(result.to_dict())})
            child.complete_call(child.state, "delegate", result)
            self.assertEqual(child.call(child.state, "delegate")["status"], "completed")
            self.assertEqual(parent.call(parent.state, "delegate")["status"], "running")
            self.assertEqual(self.store.call(self.state, "delegate")["status"], "running")
        tree = self.store.task_tree("demo")
        self.assertEqual(tree["current_root_state"]["model_calls"], 3)
        self.assertEqual([node["state"]["model_calls"] for node in tree["nodes"]], [2, 1])
        self.assertEqual(tree["nodes"][1]["tool_calls"][0]["attempts"], 1)
        entry = self.store.events("demo")[-1]
        self.assertEqual(entry["task_id"], child.task_id)
        self.assertEqual(entry["turn_id"], self.state.turn_id)

    def test_node_event_failure_rolls_back_root_child_and_ledger(self):
        with self.store.exclusive("demo"):
            view = self.store.task_node(self.state, self.create()["task_id"])
            view.save(view.state, [call("read")])
            before = self.store.task_tree("demo")
            with self.store._connection() as connection:
                connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'injected'); END")
            self.state.model_calls = 7
            view.state.model_calls = 4
            with self.assertRaises(sqlite3.IntegrityError):
                view.start_call(view.state, "read")
            self.assertEqual(self.store.task_tree("demo"), before)

    def test_creation_event_failure_rolls_back_root_and_node(self):
        with self.store.exclusive("demo"):
            before = self.store.load("demo").to_dict()
            with self.store._connection() as connection:
                connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'injected'); END")
            self.state.model_calls = 9
            with self.assertRaises(sqlite3.IntegrityError):
                self.create()
            self.assertEqual(self.store.load("demo").to_dict(), before)
            self.assertEqual(self.store.task_tree("demo")["nodes"], [])

    def test_stale_node_view_cannot_overwrite_new_checkpoint(self):
        with self.store.exclusive("demo"):
            record = self.create()
            first = self.store.task_node(self.state, record["task_id"])
            stale = self.store.task_node(self.state, record["task_id"])
            first.state.model_calls = 3
            first.save(first.state)
            self.state.model_calls = 10
            stale.state.model_calls = 1
            with self.assertRaises(ValueError):
                stale.save(stale.state)
            self.assertEqual(self.store.load("demo").model_calls, 0)
            self.assertEqual(self.store.task_tree("demo")["nodes"][0]["state"]["model_calls"], 3)

    def test_failed_commit_keeps_snapshot_usable_for_retry(self):
        with self.store.exclusive("demo"):
            view = self.store.task_node(self.state, self.create()["task_id"])
            with self.store._connection() as connection:
                connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'injected'); END")
            view.state.model_calls = 2
            with self.assertRaises(sqlite3.IntegrityError):
                view.save(view.state, event=event("test"))
            with self.store._connection() as connection:
                connection.execute("DROP TRIGGER fail_event")
            view.save(view.state, event=event("test"))
            self.assertEqual(self.store.task_tree("demo")["nodes"][0]["state"]["model_calls"], 2)

    def test_lock_thread_turn_workspace_and_caller_requirements(self):
        with self.assertRaises(ValueError):
            self.create()
        with self.store.exclusive("demo"):
            errors = []
            def other_thread():
                try:
                    self.create()
                except ValueError:
                    errors.append(True)
            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join()
            self.assertEqual(errors, [True])
            record = self.create()
            view = self.store.task_node(self.state, record["task_id"])
            bad = deepcopy(view.state)
            bad.turn_id = self.state.turn_id
            with self.assertRaises(ValueError):
                view.save(bad)
            with self.assertRaises(ValueError):
                self.store.create_task(self.state, call_id="missing", name="reader", state=self.candidate())
            bad = self.candidate()
            bad.workspace_root = "elsewhere"
            with self.assertRaises(ValueError):
                self.store.create_task(self.state, call_id="delegate", name="reader", state=bad)
            stale = deepcopy(self.state)
            stale.turn_id = "old"
            with self.assertRaises(ValueError):
                self.store.create_task(stale, call_id="delegate", name="reader", state=self.candidate())
        with self.assertRaises(ValueError):
            view.save(view.state)

    def test_depth_and_quantity_limits_prevent_node_creation(self):
        with self.store.exclusive("demo"):
            view = self.store.task_node(self.state, self.create(max_nodes=1)["task_id"])
            view.save(view.state, [call("nested")])
            view.start_call(view.state, "nested")
            for limits in ({"max_depth": 1}, {"max_nodes": 1}):
                with self.assertRaises(ValueError):
                    self.store.create_task(self.state, parent_task_id=view.task_id, call_id="nested", name="reader", state=self.candidate(), **limits)
        self.assertEqual(len(self.store.task_tree("demo")["nodes"]), 1)

    def test_export_backup_delete_and_cleanup_include_unfinished_nodes(self):
        with self.store.exclusive("demo"):
            record = self.create()
            view = self.store.task_node(self.state, record["task_id"])
            view.save(view.state, [call("read")])
            view.start_call(view.state, "read")
        self.finish_parent()
        with self.store._connection() as connection:
            connection.execute("UPDATE sessions SET updated_at='2000-01-01 00:00:00' WHERE id='demo'")
        self.assertEqual(self.store.cleanup(1)["eligible"], [])
        with self.assertRaises(ValueError):
            self.store.delete_session("demo")
        archive, backup = self.root / "archive.json", self.root / "backup.sqlite3"
        self.store.export_session("demo", archive)
        data = json.loads(archive.read_text())
        self.assertEqual(data["task_nodes"][0]["task_id"], record["task_id"])
        self.assertEqual(len(data["tool_calls"]), 2)
        self.store.backup(backup)
        self.assertEqual(SessionStore(backup).task_tree("demo"), self.store.task_tree("demo"))
        self.store.delete_session("demo", discard_unfinished=True)
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM task_nodes").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM tool_calls").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_active_node_without_pending_calls_still_blocks_cleanup(self):
        with self.store.exclusive("demo"):
            view = self.store.task_node(self.state, self.create()["task_id"])
        self.finish_parent()
        with self.store._connection() as connection:
            connection.execute("UPDATE sessions SET updated_at='2000-01-01 00:00:00' WHERE id='demo'")
        self.assertEqual(self.store.cleanup(1)["eligible"], [])
        with self.assertRaises(ValueError):
            self.store.delete_session("demo")
        with self.store.exclusive("demo"):
            view.state.status = RunStatus.COMPLETED
            view.save(view.state)
        with self.store._connection() as connection:
            connection.execute("UPDATE sessions SET updated_at='2000-01-01 00:00:00' WHERE id='demo'")
        self.assertEqual(self.store.cleanup(1, dry_run=False)["deleted"], ["demo"])

    def test_v5_migration_preserves_sessions_ledger_and_events(self):
        state, calls, events = self.state.to_dict(), self.store.calls("demo", "root-turn"), self.store.events("demo")
        with self.store._connection() as connection:
            connection.execute("DROP TABLE task_nodes")
            connection.execute("PRAGMA user_version=5")
        upgraded = SessionStore(self.store.path)
        self.assertEqual(upgraded.load("demo").to_dict(), state)
        self.assertEqual(upgraded.calls("demo", "root-turn"), calls)
        self.assertEqual(upgraded.events("demo"), events)
        self.assertEqual(upgraded.task_tree("demo")["nodes"], [])
        with upgraded._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)

    def test_cli_tree_reads_without_provider_credentials(self):
        with self.store.exclusive("demo"):
            self.create()
        result = subprocess.run([sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo", "--task-tree"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["nodes"]), 1)

    def test_root_recovery_cannot_ignore_active_child_or_start_new_turn(self):
        with self.store.exclusive("demo"):
            self.create()
        self.finish_parent()
        original_turn = self.state.turn_id

        class NeverDispatch:
            def complete(self, messages, tools):
                raise AssertionError("must not dispatch")

        for prompt in (None, "new task"):
            saved = run_agent(NeverDispatch(), prompt, tools=ToolRegistry(), store=self.store, session_id="demo")
            self.assertEqual(saved.status, RunStatus.NEEDS_RECOVERY)
            self.assertEqual(saved.turn_id, original_turn)
            self.assertNotIn({"role": "user", "content": "new task"}, saved.messages)
            self.assertEqual(saved.model_calls, 0)
        self.assertEqual(self.store.task_tree("demo")["nodes"][0]["state"]["status"], RunStatus.RUNNING)

    def test_hard_exit_preserves_uncertain_node_tool_and_actual_side_effect(self):
        with self.store.exclusive("demo"):
            view = self.store.task_node(self.state, self.create()["task_id"])
            view.save(view.state, [call("effect")])
        effect = self.root / "effect.txt"
        script = """
import os, sys
from pathlib import Path
from baseagent.session import SessionStore
from baseagent.tools.result import ToolResult
store = SessionStore(sys.argv[1])
with store.exclusive('demo'):
    root = store.load('demo')
    view = store.task_node(root, sys.argv[2])
    view.start_call(view.state, 'effect')
    root.tool_calls += 1
    view.state.tool_calls += 1
    view.record_attempt(view.state, 'effect', {'name': 'write', 'arguments': {}})
    Path(sys.argv[3]).write_text('once')
    view.record_terminal_result(view.state, 'effect', ToolResult(data='written'))
    os._exit(41)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.store.path), view.task_id, str(effect)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 41, result.stderr)
        tree = self.store.task_tree("demo")
        self.assertEqual(effect.read_text(), "once")
        self.assertEqual(tree["current_root_state"]["tool_calls"], 1)
        self.assertEqual(tree["nodes"][0]["state"]["tool_calls"], 1)
        ledger = tree["nodes"][0]["tool_calls"][0]
        self.assertEqual(ledger["status"], "running")
        self.assertEqual(ledger["attempts"], 1)
        self.assertEqual(json.loads(ledger["terminal_result_json"])["data"], "written")

    def test_delete_failure_rolls_back_tree_ledger_and_session(self):
        with self.store.exclusive("demo"):
            self.create()
        before = self.store.task_tree("demo")
        with self.store._connection() as connection:
            connection.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON task_nodes BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_session("demo", discard_unfinished=True)
        self.assertEqual(self.store.task_tree("demo"), before)
        self.assertEqual(self.store.call(self.state, "delegate")["status"], "running")


if __name__ == "__main__":
    unittest.main()
