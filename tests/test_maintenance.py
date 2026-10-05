from contextlib import contextmanager, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.agent.events import event
from baseagent.agent.state import RunStatus, State
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.main import main
from baseagent.session import SessionBusy, SessionStore
from baseagent.tools.registry import ToolRegistry


class Model:
    def complete(self, messages, tools):
        return ModelResponse({"content": "private answer"}, TokenUsage(3, 1, 4))


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.sqlite3")
        self.tools = ToolRegistry()
        self.create("demo")

    def create(self, identifier):
        return run_agent(Model(), "private prompt", tools=self.tools, store=self.store, session_id=identifier)

    def age(self, identifier):
        with self.store._connection() as connection:
            connection.execute("UPDATE sessions SET updated_at='2000-01-01 00:00:00' WHERE id=?", (identifier,))

    def test_list_is_paged_without_transcripts(self):
        self.create("zeta")
        first = self.store.list_sessions(limit=1)
        self.assertEqual(first["sessions"][0]["id"], "demo")
        self.assertEqual(first["next_after"], "demo")
        second = self.store.list_sessions(after_id=first["next_after"], limit=1)
        self.assertEqual(second["sessions"][0]["id"], "zeta")
        self.assertIsNone(second["next_after"])
        self.assertNotIn("private", json.dumps(first))

    def test_export_is_one_snapshot_even_if_writer_commits_between_queries(self):
        state = self.store.load("demo")
        original = state.to_dict()
        original_events = self.store.events("demo")
        dump = json.dump
        committed = []

        def dump_with_writer(value, output, **kwargs):
            if not committed:
                with self.store.exclusive("demo"):
                    state.metadata["writer_commit"] = True
                    state.status = RunStatus.RUNNING
                    call = {"id": "pending", "type": "function", "function": {"name": "read", "arguments": "{}"}}
                    state.messages.append({"role": "assistant", "tool_calls": [call]})
                    self.store.save(state, [call], event=event("writer_commit"))
                committed.append(True)
            return dump(value, output, **kwargs)

        target = self.root / "archive.json"
        with patch("baseagent.session.maintenance.json.dump", side_effect=dump_with_writer):
            self.store.export_session("demo", target)
        archive = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(archive["session"], original)
        self.assertEqual(archive["tool_calls"], [])
        self.assertEqual([item["sequence"] for item in archive["events"]], [item["sequence"] for item in original_events])
        self.assertTrue(self.store.load("demo").metadata["writer_commit"])
        self.assertEqual(len(self.store.calls("demo", state.turn_id)), 1)

    def test_backup_can_reopen_with_identical_state_and_events(self):
        target = self.root / "backup.sqlite3"
        self.store.backup(target)
        restored = SessionStore(target)
        self.assertEqual(restored.load("demo").to_dict(), self.store.load("demo").to_dict())
        self.assertEqual(restored.events("demo"), self.store.events("demo"))
        unused = Model()
        with patch.object(unused, "complete", side_effect=AssertionError("cached backup must not execute")):
            state = run_agent(unused, tools=self.tools, store=restored, session_id="demo")
            self.assertEqual(state.final_answer, "private answer")

    def test_inspection_state_and_ledger_share_one_snapshot(self):
        state = self.store.load("demo")
        original = state.to_dict()
        from_dict = State.from_dict

        def commit_after_state(value):
            state.status = RunStatus.RUNNING
            call = {"id": "pending", "function": {"name": "read", "arguments": "{}"}}
            self.store.save(state, [call])
            return from_dict(value)

        with patch("baseagent.session.maintenance.State.from_dict", side_effect=commit_after_state):
            inspected, ledger = self.store.inspect_session("demo")
        self.assertEqual(inspected.to_dict(), original)
        self.assertEqual(ledger, [])
        self.assertEqual(len(self.store.calls("demo", state.turn_id)), 1)

    def test_backup_is_consistent_while_another_connection_writes(self):
        ready, stop = threading.Event(), threading.Event()
        errors = []

        def writer():
            try:
                state = self.store.load("demo")
                with self.store.exclusive("demo"):
                    for generation in range(1, 100):
                        state.metadata["generation"] = generation
                        self.store.save(state, event=event("generation", generation=generation))
                        ready.set()
                        if stop.wait(0.005):
                            break
            except BaseException as exc:
                errors.append(exc)
                ready.set()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            target = self.root / "live-backup.sqlite3"
            self.store.backup(target)
        finally:
            stop.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        restored = SessionStore(target)
        self.assertEqual(restored.events("demo")[-1]["data"]["generation"], restored.load("demo").metadata["generation"])

    def test_artifacts_never_overwrite_existing_or_protected_storage(self):
        existing = self.root / "existing.json"
        existing.write_text("preserve")
        for operation in [lambda target: self.store.export_session("demo", target), self.store.backup]:
            with self.assertRaises(FileExistsError):
                operation(existing)
            for target in [self.store.path, Path(str(self.store.path) + "-wal"), self.store.lock_dir / "lock"]:
                with self.assertRaises(ValueError):
                    operation(target)
        self.assertEqual(existing.read_text(), "preserve")
        self.assertEqual(self.store.load("demo").status, RunStatus.COMPLETED)

    def test_failed_or_racing_export_does_not_publish_partial_archive(self):
        target = self.root / "archive.json"
        with self.assertRaises(ValueError):
            self.store.export_session("missing", target)
        self.assertFalse(target.exists())
        link = os.link

        def race(source, destination):
            Path(destination).write_text("concurrent artifact")
            return link(source, destination)

        with patch("baseagent.session.maintenance.os.link", side_effect=race):
            with self.assertRaises(FileExistsError):
                self.store.export_session("demo", target)
        self.assertEqual(target.read_text(), "concurrent artifact")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_backup_timeout_leaves_no_published_destination(self):
        target = self.root / "timeout.sqlite3"
        with patch("baseagent.session.maintenance.time.monotonic", side_effect=[0, 31]):
            with self.assertRaises(TimeoutError):
                self.store.backup(target, timeout=30)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_delete_removes_related_rows_and_retains_lock_file(self):
        self.store.delete_session("demo")
        self.assertIsNone(self.store.load("demo"))
        self.assertEqual(self.store.events("demo"), [])
        with self.store._connection() as connection:
            self.assertIsNone(connection.execute("PRAGMA foreign_key_check").fetchone())
        self.assertEqual(len(list(self.store.lock_dir.glob("*.lock"))), 1)

    def test_unfinished_requires_discard_and_busy_session_cannot_be_discarded(self):
        state = self.store.load("demo")
        state.status = RunStatus.INTERRUPTED
        self.store.save(state)
        with self.assertRaisesRegex(ValueError, "explicit discard"):
            self.store.delete_session("demo")
        with self.store.exclusive("demo"):
            with self.assertRaises(SessionBusy):
                self.store.delete_session("demo", discard_unfinished=True)
        self.store.delete_session("demo", discard_unfinished=True)
        self.assertIsNone(self.store.load("demo"))

    def test_delete_transaction_rolls_back_if_related_row_delete_fails(self):
        state = self.store.load("demo")
        call = {"id": "a", "function": {"name": "read", "arguments": "{}"}}
        self.store.save(state, [call])
        with self.store._connection() as connection:
            connection.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON tool_calls BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        old_events = self.store.events("demo")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_session("demo", discard_unfinished=True)
        self.assertIsNotNone(self.store.load("demo"))
        self.assertEqual(self.store.events("demo"), old_events)
        self.assertEqual(len(self.store.calls("demo", state.turn_id)), 1)

    def test_cleanup_previews_then_deletes_only_old_complete_idle_sessions(self):
        self.create("busy")
        self.create("unfinished")
        state = self.store.load("unfinished")
        state.status = RunStatus.INTERRUPTED
        self.store.save(state)
        for identifier in ["demo", "busy", "unfinished"]:
            self.age(identifier)
        preview = self.store.cleanup(86400)
        self.assertEqual(preview["eligible"], ["busy", "demo"])
        self.assertIsNotNone(self.store.load("demo"))
        with self.store.exclusive("busy"):
            applied = self.store.cleanup(86400, dry_run=False)
        self.assertEqual(applied["deleted"], ["demo"])
        self.assertEqual(applied["skipped"], [{"id": "busy", "reason": "busy"}])
        self.assertIsNotNone(self.store.load("unfinished"))

    def test_cleanup_rechecks_age_after_candidate_query(self):
        self.age("demo")
        exclusive = self.store.exclusive

        @contextmanager
        def refreshed(identifier):
            state = self.store.load(identifier)
            self.store.save(state)
            with exclusive(identifier):
                yield

        with patch.object(self.store, "exclusive", side_effect=refreshed):
            result = self.store.cleanup(86400, dry_run=False)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["skipped"], [{"id": "demo", "reason": "changed"}])
        self.assertIsNotNone(self.store.load("demo"))

    def test_cleanup_cursor_can_continue_to_next_page(self):
        self.create("zeta")
        self.age("demo")
        self.age("zeta")
        first = self.store.cleanup(86400, limit=1)
        self.assertEqual(first["eligible"], ["demo"])
        second = self.store.cleanup(86400, after_id=first["next_after"], limit=1)
        self.assertEqual(second["eligible"], ["zeta"])
        self.assertIsNone(second["next_after"])

    def test_cli_maintenance_needs_no_provider(self):
        base = ["baseagent", "--db", str(self.store.path)]
        operations = [["--list-sessions"], ["--session", "demo", "--export-session", str(self.root / "export.json")],
                      ["--backup-db", str(self.root / "cli-backup.sqlite3")], ["--cleanup-days", "30"],
                      ["--session", "demo", "--delete-session"]]
        for flags in operations:
            with patch.object(sys, "argv", base + flags), patch("baseagent.main.Model", side_effect=AssertionError("provider must not initialize")):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(), 0, flags)


if __name__ == "__main__":
    unittest.main()
