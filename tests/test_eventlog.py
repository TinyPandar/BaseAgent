import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from baseagent.agent import run_agent
from baseagent.agent.cancellation import CancellationToken, Cancelled
from baseagent.agent.events import event
from baseagent.session import SessionStore, SessionBusy
from baseagent.tools.registry import ToolRegistry


class Model:
    def complete(self, messages, tools):
        return {"content": "done"}


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.sqlite3")
        self.state = run_agent(Model(), "private task", tools=ToolRegistry(), store=self.store, session_id="demo")

    def preview(self, keep=2):
        return self.store.prune_events("demo", through=1000000, keep_latest=keep)

    def apply(self, preview):
        return self.store.prune_events("demo", through=preview["requested_through"], keep_latest=preview["keep_latest"], expected_digest=preview["preview_digest"])

    def test_pages_reconnect_without_duplicate_delivery(self):
        first = self.store.event_page("demo", limit=2)
        second = self.store.event_page("demo", after=first["next_after"], limit=100)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["events"] + second["events"], self.store.events("demo"))
        self.assertFalse(first["history_lost"])

    def test_global_sequence_gaps_are_not_retention_gaps(self):
        run_agent(Model(), "other", tools=ToolRegistry(), store=self.store, session_id="other")
        self.store.save(self.state, event=event("later"))
        page = self.store.event_page("demo")
        self.assertFalse(page["history_lost"])
        self.assertEqual(page["pruned_through"], 0)

    def test_preview_has_no_effect_apply_preserves_state_and_ledger(self):
        state, ledger = self.store.inspect_session("demo")
        events = self.store.events("demo")
        preview = self.preview()
        self.assertFalse(preview["applied"])
        self.assertEqual(self.store.events("demo"), events)
        result = self.apply(preview)
        self.assertTrue(result["applied"])
        self.assertEqual(self.store.events("demo"), events[-2:])
        after_state, after_ledger = self.store.inspect_session("demo")
        self.assertEqual(after_state.to_dict(), state.to_dict())
        self.assertEqual(after_ledger, ledger)
        page = self.store.event_page("demo")
        self.assertTrue(page["history_lost"])
        self.assertEqual(page["pruned_through"], events[-3]["sequence"])
        self.assertFalse(self.store.event_page("demo", after=page["pruned_through"])["history_lost"])

    def test_changed_preview_rejected_without_deletion(self):
        preview = self.preview()
        self.store.save(self.state, event=event("new"))
        before = self.store.events("demo")
        with self.assertRaises(ValueError):
            self.apply(preview)
        self.assertEqual(self.store.events("demo"), before)

    def test_apply_lock_protected_preview_allowed_while_locked(self):
        with self.store.exclusive("demo"):
            preview = self.preview()
            with self.assertRaises(SessionBusy):
                self.apply(preview)

    def test_prune_transaction_rolls_back_on_metadata_failure(self):
        preview = self.preview()
        before = self.store.events("demo")
        with self.store._connection() as connection:
            connection.execute("CREATE TRIGGER prevent_retention BEFORE INSERT ON event_retention BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply(preview)
        self.assertEqual(self.store.events("demo"), before)

    def test_follow_wait_sees_new_commit_and_can_cancel(self):
        cursor = self.store.event_page("demo")["next_after"]
        timer = threading.Timer(0.1, lambda: self.store.save(self.state, event=event("live_update")))
        timer.start()
        self.addCleanup(timer.join)
        pages = list(self.store.follow_events("demo", after=cursor, duration=0.35, poll_interval=0.05))
        self.assertEqual([item["type"] for page in pages for item in page["events"]], ["live_update"])
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(Cancelled):
            list(self.store.follow_events("demo", duration=1, cancellation=token))

    def test_follow_reports_lost_history_once(self):
        self.apply(self.preview())
        pages = list(self.store.follow_events("demo", duration=0.1, poll_interval=0.05))
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0]["history_lost"])

    def test_export_backup_and_delete_keep_retention_consistent(self):
        self.apply(self.preview())
        archive = self.root / "archive.json"
        self.store.export_session("demo", archive)
        exported = json.loads(archive.read_text())
        self.assertEqual(exported["event_retention"]["through_sequence"], self.store.event_page("demo")["pruned_through"])
        self.store.backup(self.root / "backup.sqlite3")
        copied = SessionStore(self.root / "backup.sqlite3")
        self.assertEqual(copied.event_page("demo"), self.store.event_page("demo"))
        self.store.delete_session("demo")
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM event_retention").fetchone()[0], 0)

    def test_cli_follow_and_prune_without_model(self):
        base = [sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo"]
        process = subprocess.Popen(base + ["--follow-events", "0.5", "--event-limit", "2"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        try:
            first = json.loads(process.stdout.readline())
            self.store.save(self.state, event=event("cli_live_update"))
            output, error = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, error)
        pages = [first] + [json.loads(line) for line in output.splitlines()]
        values = [item for page in pages for item in page["events"]]
        self.assertEqual(len(values), len({item["sequence"] for item in values}))
        self.assertEqual(values, self.store.events("demo"))
        self.assertTrue(any(item["type"] == "cli_live_update" for item in values))
        preview = subprocess.run(base + ["--prune-events", "1000000", "--keep-events", "2"], capture_output=True, timeout=10)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        value = json.loads(preview.stdout)
        applied = subprocess.run(base + ["--prune-events", "1000000", "--keep-events", "2", "--event-prune-digest", value["preview_digest"]], capture_output=True, timeout=10)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(len(self.store.events("demo")), 2)

    def test_validation_and_missing_session(self):
        for kwargs in ({"after": -1}, {"limit": 0}, {"limit": True}):
            with self.assertRaises(ValueError):
                self.store.event_page("demo", **kwargs)
        with self.assertRaises(ValueError):
            self.store.event_page("missing")
        with self.assertRaises(ValueError):
            list(self.store.follow_events("demo", duration=float("nan")))
        with self.assertRaises(ValueError):
            self.store.prune_events("demo", through=1, keep_latest=0)

    def test_v3_migration_preserves_existing_events(self):
        before = self.store.events("demo")
        with self.store._connection() as connection:
            connection.execute("DROP TABLE event_retention")
            connection.execute("PRAGMA user_version=3")
        upgraded = SessionStore(self.store.path)
        self.assertEqual(upgraded.event_page("demo")["events"], before)
        with upgraded._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)


if __name__ == "__main__":
    unittest.main()
