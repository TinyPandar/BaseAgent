import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.middleware import AgentMiddleware
from baseagent.session import SessionStore
from baseagent.tools.result import ToolFailure
from baseagent.tools.workspace import Workspace, coding_tools


class Model:
    def __init__(self, messages=()):
        self.messages = iter(messages)

    def complete(self, messages, tools):
        return next(self.messages)


def call(name, args, identifier="a"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class SharedMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / ".baseagent/sessions.sqlite3")
        self.tools = coding_tools(Workspace(self.root, allow_memory_publish=True))

    def run_turn(self, model, prompt=None, identifier="publisher", tools=None, **kwargs):
        return run_agent(model, prompt, tools=tools or self.tools, store=self.store, session_id=identifier, workspace_root=self.root, **kwargs)

    def create(self, identifier="publisher", content="Use Python", publish=True):
        calls = [call("remember", {"key": "language", "content": content, "source_messages": [1], "expected_revision": 0})]
        if publish:
            calls.append(call("publish_memory", {"key": "language", "expected_revision": 0}, "b"))
        return self.run_turn(Model([{"tool_calls": calls}, {"content": "done"}]), "Python project", identifier)

    def test_publish_and_read_across_sessions(self):
        self.create()
        state = self.run_turn(Model([{"tool_calls": [call("search_shared_memory", {"query": "python"})]}, {"content": "found"}]), "recall", "reader")
        data = json.loads(state.messages[-2]["content"])["data"]
        self.assertEqual(data["notes"][0]["content"], "Use Python")
        self.assertEqual(data["notes"][0]["source_status"], "source_matches")
        self.assertEqual(state.memory_notes, {})

    def test_publication_capability_does_not_follow_file_write(self):
        denied = coding_tools(Workspace(self.root, allow_write=True))
        self.run_turn(Model([{"tool_calls": [call("remember", {"key": "language", "content": "Use Python", "source_messages": [1], "expected_revision": 0}),
                                            call("publish_memory", {"key": "language", "expected_revision": 0}, "b")]}, {"content": "done"}]), "task", tools=denied)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"], [])

    def test_unpublished_session_note_is_not_shared(self):
        self.create(publish=False)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"], [])

    def test_workspace_namespaces_are_isolated(self):
        self.create()
        other = self.root / "other"
        other.mkdir()
        self.assertEqual(self.store.search_shared_memory(other)["notes"], [])
        source = self.store.load("publisher")
        with self.assertRaises(ToolFailure):
            self.store.publish_memory(source, other, "language", 0)

    def test_concurrent_publish_compare_and_swap(self):
        first = self.create(publish=False)
        second = self.create(identifier="second", content="Use Rust", publish=False)
        barrier = threading.Barrier(2)
        successes, conflicts = [], []
        def publish(state):
            barrier.wait()
            try:
                successes.append(self.store.publish_memory(state, self.root, "language", 0))
            except ToolFailure as exc:
                conflicts.append(exc.code)
        workers = [threading.Thread(target=publish, args=(state,)) for state in [first, second]]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(successes), 1)
        self.assertEqual(conflicts, ["conflict"])
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["revision"], 1)

    def test_withdraw_tombstone_prevents_stale_recreate(self):
        state = self.create()
        self.store.withdraw_memory(state, self.root, "language", 1)
        note = self.store.search_shared_memory(self.root)["notes"][0]
        self.assertTrue(note["withdrawn"])
        self.assertIsNone(note["content"])
        self.assertEqual(note["revision"], 2)
        with self.assertRaises(ToolFailure):
            self.store.publish_memory(state, self.root, "language", 0)
        self.store.publish_memory(state, self.root, "language", 2)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["revision"], 3)

    def test_source_delete_or_change_is_reported(self):
        state = self.create()
        original_note = dict(state.memory_notes["language"])
        state.memory_notes["language"]["content"] = "changed note"
        self.store.save(state)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "source_note_changed")
        del state.memory_notes["language"]
        self.store.save(state)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "source_note_missing")
        state.memory_notes["language"] = original_note
        state.messages[1]["content"] = "changed source"
        self.store.save(state)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "source_changed")
        self.store.delete_session("publisher")
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["source_status"], "source_missing")

    def test_publish_event_failure_rolls_back_reference(self):
        state = self.create(publish=False)
        with self.store._connection() as connection:
            connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.publish_memory(state, self.root, "language", 0)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"], [])

    def test_export_backup_and_source_deletion(self):
        self.create()
        archive = self.root / "session.json"
        self.store.export_session("publisher", archive)
        self.assertEqual(json.loads(archive.read_text())["published_references"][0]["content"], "Use Python")
        self.store.backup(self.root / "backup.sqlite3")
        self.assertEqual(SessionStore(self.root / "backup.sqlite3").search_shared_memory(self.root), self.store.search_shared_memory(self.root))

    def test_interrupted_publication_is_conservative_and_not_replayed(self):
        self.create(publish=False)
        class Interrupt(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                handler(request)
                raise KeyboardInterrupt()
        state = self.run_turn(Model([{"tool_calls": [call("publish_memory", {"key": "language", "expected_revision": 0})]}]), "publish", middleware=[Interrupt()])
        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        self.assertEqual(self.store.search_shared_memory(self.root)["notes"][0]["revision"], 1)
        self.assertEqual(self.run_turn(Model()).status, RunStatus.NEEDS_RECOVERY)

    def test_cli_inspects_shared_memory_without_model(self):
        self.create()
        result = subprocess.run([sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "publisher", "--shared-memory", "--memory-query", "python"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["notes"][0]["source_status"], "source_matches")

    def test_v4_migration_preserves_existing_session(self):
        before = self.create(publish=False)
        with self.store._connection() as connection:
            connection.execute("DROP TABLE shared_memories")
            connection.execute("PRAGMA user_version=4")
        upgraded = SessionStore(self.store.path)
        self.assertEqual(upgraded.load("publisher").to_dict(), before.to_dict())
        with upgraded._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)


if __name__ == "__main__":
    unittest.main()
