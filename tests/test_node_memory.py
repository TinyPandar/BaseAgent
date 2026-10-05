from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import test_scope as fixtures
from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.agent.subtasks import SubtaskDefinition, SubtaskRuntime
from baseagent.session import SessionStore
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.result import ToolFailure
from baseagent.tools.workspace import Workspace, coding_tools


def call(name, arguments, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class NodeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.sqlite3")

    def publish(self, *, allow=True, policy=None):
        self.tools = coding_tools(Workspace(self.root, allow_memory_publish=allow))
        child = fixtures.Model([{"tool_calls": [
            call("remember", {"key": "decision", "content": "Node reference", "source_messages": [1], "expected_revision": 0}, "remember"),
            call("publish_memory", {"key": "decision", "expected_revision": 0}, "publish")
        ]}, {"content": "published"}])
        self.runtime = SubtaskRuntime([SubtaskDefinition("publisher", ("remember", "publish_memory", "withdraw_memory"), model=child)])
        request = call("run_subtask", {"name": "publisher", "prompt": "child source"}, "delegate")
        self.state = run_agent(fixtures.Model([{"tool_calls": [request]}, {"content": "done"}]), "root source", tools=self.tools,
                               store=self.store, session_id="demo", workspace_root=self.root, subtasks=self.runtime, tool_policy=policy)
        self.assertEqual(self.state.status, RunStatus.COMPLETED)
        return self.store.task_tree("demo")["nodes"][0]

    def test_node_publication_is_bound_to_node_sources_and_private_notes_stay_isolated(self):
        node = self.publish()
        reference = self.store.search_shared_memory(self.root)["notes"][0]
        self.assertEqual(reference["source_status"], "source_matches")
        self.assertEqual(reference["source_task_id"], node["task_id"])
        self.assertEqual(reference["source_turn_id"], node["state"]["turn_id"])
        self.assertEqual(reference["source_session_id"], "demo")
        self.assertEqual(self.state.memory_notes, {})
        self.state.memory_notes["decision"] = {"content": "root changed"}
        self.store.save(self.state)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "source_matches")
        events = [entry for entry in self.store.events("demo") if entry["type"] == "memory_published"]
        self.assertEqual(events[0]["task_id"], node["task_id"])
        self.assertNotIn("Node reference", json.dumps(events))

    def test_node_note_changes_and_source_changes_are_detected(self):
        node = self.publish()
        with self.store.exclusive("demo"):
            view = self.store.task_node(self.state, node["task_id"])
            original = deepcopy(view.state.memory_notes["decision"])
            view.state.memory_notes["decision"]["content"] = "changed"
            view.save(view.state)
            self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "source_note_changed")
            del view.state.memory_notes["decision"]
            view.save(view.state)
            self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "source_note_missing")
            view.state.memory_notes["decision"] = original
            view.state.messages[1]["content"] = "source changed"
            view.save(view.state)
            self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "source_changed")

    def test_root_capability_and_policy_cannot_be_upgraded_by_node(self):
        for allow, policy in ((False, None), (True, ToolPolicy({"publish_memory": "deny"}))):
            # Independent stores keep the cases separate.
            self.store = SessionStore(self.root / ("allowed.sqlite3" if allow else "denied.sqlite3"))
            node = self.publish(allow=allow, policy=policy)
            self.assertEqual(self.store.search_shared_memory(self.root)["notes"], [])
            publication = next(record for record in node["tool_calls"] if record["call_id"] == "publish")
            self.assertEqual(json.loads(publication["result_json"])["error"]["code"], "permission_denied")

    def test_archived_node_source_remains_valid_after_root_new_turn(self):
        node = self.publish()
        with self.store.exclusive("demo"):
            old_view = self.store.task_node(self.state, node["task_id"])
        run_agent(fixtures.Model([{"content": "other done"}]), "new root turn", tools=self.tools, store=self.store, session_id="demo", workspace_root=self.root, subtasks=self.runtime)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "source_matches")
        with self.store.exclusive("demo"):
            with self.assertRaises(ToolFailure):
                old_view.publish_memory(old_view.state, self.root, "decision", 1)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["revision"], 1)

    def test_node_publication_requires_held_root_lock_and_correct_node_view(self):
        node = self.publish()
        with self.store.exclusive("demo"):
            view = self.store.task_node(self.state, node["task_id"])
            with self.assertRaises(ToolFailure):
                self.store.publish_memory(view.state, self.root, "decision", 1)
        with self.assertRaises(ValueError):
            view.publish_memory(view.state, self.root, "decision", 1)

    def test_publication_event_failure_rolls_back_reference_and_revision(self):
        node = self.publish()
        with self.store.exclusive("demo"):
            view = self.store.task_node(self.state, node["task_id"])
            with self.store._connection() as connection:
                connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'injected'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                view.publish_memory(view.state, self.root, "decision", 1)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["revision"], 1)

    def test_export_backup_source_deletion_and_no_root_fallback(self):
        node = self.publish()
        reference = self.store.search_shared_memory(self.root)["notes"][0]
        archive, backup = self.root / "archive.json", self.root / "backup.sqlite3"
        self.store.export_session("demo", archive)
        saved = json.loads(archive.read_text())
        self.assertEqual(saved["published_references"][0]["source_note"]["source_task_id"], node["task_id"])
        self.store.backup(backup)
        self.assertEqual(SessionStore(backup).search_shared_memory(self.root)["notes"][0], reference)
        note = deepcopy(node["state"]["memory_notes"]["decision"])
        self.store.delete_session("demo")
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "source_missing")
        replacement = run_agent(fixtures.Model([{"content": "done"}]), "child source", tools=self.tools, store=self.store, session_id="demo", workspace_root=self.root)
        replacement.memory_notes["decision"] = note
        self.store.save(replacement)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "source_missing")

    def test_node_can_withdraw_authorized_reference_with_cas(self):
        node = self.publish()
        with self.store.exclusive("demo"):
            view = self.store.task_node(self.state, node["task_id"])
            with self.assertRaises(ToolFailure):
                view.withdraw_memory(view.state, self.root, "decision", 0)
            self.assertEqual(view.withdraw_memory(view.state, self.root, "decision", 1)["revision"], 2)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "withdrawn")


if __name__ == "__main__":
    unittest.main()
