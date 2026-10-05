import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_subtasks as delegation
import test_scope as fixtures
from baseagent.agent import run_agent
from baseagent.agent.cancellation import CancellationToken
from baseagent.agent.state import RunStatus
from baseagent.agent.subtasks import SubtaskDefinition, SubtaskRuntime
from baseagent.session import MemorySessionStore, SessionBusy, SessionStore
from baseagent.tools.context import ToolContext
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.result import ToolFailure
from baseagent.tools.shared_memory import _scope


class MemoryDelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MemorySessionStore()
        self.addCleanup(self.store.close)
        self.tools = ToolRegistry()
        self.effects = []
        self.tools.register("read", "effect", {"type": "object"}, lambda: self.effects.append(True) or "ok")

    runtime = delegation.SubtaskTests.runtime
    run_turn = delegation.SubtaskTests.run_turn


# Exercise the existing behavioral contracts against a genuinely different
# storage backend; subprocess durability and CLI SQLite tests remain separate.
for _name in dir(delegation.SubtaskTests):
    if _name.startswith("test_") and _name not in {
        "test_hard_exit_after_completed_child_recovers_cached_delivery",
        "test_cli_configuration_is_explicit_strict_and_refuses_unsupported_reservation",
        "test_actual_delegation_node_approval_and_resume_across_processes",
        "test_hard_exit_during_child_model_retains_ancestor_quote_and_unknown_usage",
        "test_hard_exit_after_delegated_child_effect_requires_reconcile_without_replay",
    }:
        setattr(MemoryDelegationTests, _name, getattr(delegation.SubtaskTests, _name))


class MemoryStoreTests(unittest.TestCase):
    def runtime(self, child):
        return SubtaskRuntime([SubtaskDefinition("reader", (), model=child)])

    def test_implicit_delegation_has_no_files_and_returns_node_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            child = fixtures.Model([{"content": "private child answer"}])
            root = fixtures.Model([{"tool_calls": [delegation.delegate()]}, {"content": "done"}])
            with patch.object(Path, "mkdir", side_effect=AssertionError("unexpected filesystem write")):
                state = run_agent(root, "task", tools=ToolRegistry(), workspace_root=directory, subtasks=self.runtime(child))
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(state.status, RunStatus.COMPLETED)
            self.assertEqual((state.model_calls, state.tool_calls, state.total_tokens), (3, 1, 12))
            self.assertEqual(state.metadata["execution_storage"], {"durable": False, "resumable": False})
            node = state.metadata["subtask_tree"][0]
            self.assertEqual(node["state"]["final_answer"], "private child answer")
            self.assertEqual(node["state"]["session_id"], state.session_id)

    def test_implicit_parent_budget_pauses_child_with_snapshot(self):
        state = run_agent(fixtures.Model([{"tool_calls": [delegation.delegate()]}]), "task", tools=ToolRegistry(),
                          subtasks=self.runtime(fixtures.Model([{"content": "never"}])), max_model_calls=1)
        self.assertEqual(state.status, RunStatus.MAX_MODEL_CALLS_EXCEEDED)
        self.assertEqual(state.metadata["subtask_tree"][0]["state"]["model_calls"], 0)

    def test_implicit_cancellation_from_child_stops_entire_tree(self):
        token = CancellationToken()
        child = fixtures.Model([{"content": "child"}])
        child.observe = token.cancel
        state = run_agent(fixtures.Model([{"tool_calls": [delegation.delegate()]}]), "task", tools=ToolRegistry(),
                          subtasks=self.runtime(child), cancellation=token)
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(state.model_calls, 2)
        self.assertEqual(len(state.metadata["subtask_tree"]), 1)

    def test_explicit_backend_export_and_backup_preserve_nodes(self):
        with tempfile.TemporaryDirectory() as directory, MemorySessionStore() as store:
            state = run_agent(fixtures.Model([{"tool_calls": [delegation.delegate()]}, {"content": "done"}]), "task",
                              tools=ToolRegistry(), store=store, session_id="demo", subtasks=self.runtime(fixtures.Model([{"content": "child"}])))
            target = Path(directory)
            store.export_session("demo", target / "export.json")
            store.backup(target / "backup.sqlite3")
            exported = json.loads((target / "export.json").read_text(encoding="utf-8"))
            self.assertEqual(len(exported["task_nodes"]), 1)
            restored = SessionStore(target / "backup.sqlite3")
            self.assertEqual(restored.load("demo").to_dict(), state.to_dict())
            self.assertEqual(restored.task_tree("demo"), store.task_tree("demo"))

    def test_lock_ownership_cross_thread_close_and_transaction_rollback(self):
        with MemorySessionStore() as store:
            errors = []
            def contender():
                try:
                    with store.exclusive("demo"):
                        pass
                except SessionBusy:
                    errors.append("busy")
                errors.append(getattr(store._lock_owners, "sessions", set()))
            with store.exclusive("demo"):
                thread = threading.Thread(target=contender)
                thread.start()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, ["busy", set()])
                with self.assertRaises(SessionBusy):
                    store.close()
                with self.assertRaises(SessionBusy):
                    with store.exclusive("demo"):
                        pass
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with store._connection() as connection:
                    connection.execute("INSERT INTO sessions(id,state_json) VALUES('rollback','{}')")
                    raise RuntimeError("rollback")
            self.assertIsNone(store.load("rollback"))
            with store._connection():
                with self.assertRaisesRegex(RuntimeError, "nested"):
                    store.load("demo")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            store.load("demo")

    def test_shared_publication_requires_durable_storage(self):
        with tempfile.TemporaryDirectory() as directory, MemorySessionStore() as store:
            state = run_agent(fixtures.Model([{"content": "done"}]), "task", tools=ToolRegistry(), store=store,
                              session_id="demo", workspace_root=directory)
            from baseagent.tools.workspace import Workspace
            workspace = Workspace(directory, allow_memory_publish=True)
            with self.assertRaises(ToolFailure):
                _scope(workspace, ToolContext(state, lambda *args: None, store=store), publish=True)

    def test_implicit_does_not_accept_named_session_without_host_store(self):
        with self.assertRaisesRegex(ValueError, "requires"):
            run_agent(fixtures.Model(), "task", tools=ToolRegistry(), session_id="demo", subtasks=self.runtime(fixtures.Model()))


if __name__ == "__main__":
    unittest.main()
