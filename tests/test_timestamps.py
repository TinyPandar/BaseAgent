"""Every timestamp people or programs read carries its time zone (README 施工段 AC 5).

Convention (src/baseagent/timestamps.py): UTC everywhere; JSON strings are ISO 8601 with ``+00:00``,
text says ``UTC``, numbers are Unix seconds. Storage is unchanged, so old values still display.
One test per entry point. Offline: scripted models, no provider.
"""

import contextlib
import io
import json
import re
import sqlite3
import time
import unittest

from checkpoint_helpers import Script, writes
from chat_helpers import HAS_WEB, NO_WEB, ChatTestCase, wait_until
from test_cli_chat import ChatTestCase as CliCase
from test_cli_chat import Model, ping_registry
from test_main_agent import Duplex, delegate

from baseagent.main import main
from baseagent.session import SessionStore
from baseagent.session.maintenance import utc_timestamp
from baseagent.timestamps import iso_utc, text_utc

ISO = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00"
TEXT = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC"


class ConventionTests(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(iso_utc(0), "1970-01-01T00:00:00+00:00")
        self.assertEqual(text_utc(1794528000.7), "2026-11-13 00:00:00 UTC")
        for value in (None, "x", True, float("inf")):
            self.assertIsNone(iso_utc(value))

    def test_stored_sqlite_text_from_older_versions_still_reads(self):
        self.assertEqual(utc_timestamp("2026-11-08 09:30:00"), "2026-11-08T09:30:00+00:00")
        self.assertEqual(utc_timestamp("not a time"), "not a time")


class CliTimestampTests(CliCase):
    def setUp(self):
        super().setUp()
        self.ops = []

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--db", str(self.db), *argv])
        self.assertEqual(code, 0)
        return out.getvalue()

    def one_shot(self, model=None, *extra):
        code, _, _ = self.invoke(self.chat_args("task", "--session", "demo", *extra), model or Model([{"content": "done"}]),
                                 AssertionError("no input"), capability_factory=ping_registry)
        self.assertEqual(code, 0)
        return SessionStore(self.db).load("demo")

    def test_list_sessions_including_a_session_saved_before_this_change(self):
        self.one_shot()
        with sqlite3.connect(self.db) as connection:  # an old row: SQLite CURRENT_TIMESTAMP text, no marker
            connection.execute("UPDATE sessions SET updated_at='2026-01-02 03:04:05' WHERE id='demo'")
        listed = json.loads(self.run_main("--list-sessions"))["sessions"][0]["updated_at"]
        self.assertEqual(listed, "2026-01-02T03:04:05+00:00")

    def test_inspect_session(self):
        state = self.one_shot()
        value = json.loads(self.run_main("--session", "demo", "--inspect-session"))
        self.assertEqual(value["turn_started_at"], state.turn_started_at)  # stored number unchanged
        self.assertRegex(value["turn_started_at_utc"], ISO)
        self.assertEqual(value["turn_started_at_utc"], iso_utc(state.turn_started_at))
        self.assertEqual(value["time_zone"], "UTC")

    def test_export_session(self):
        self.one_shot()
        target = self.root / "export.json"
        self.run_main("--session", "demo", "--export-session", str(target))
        value = json.loads(target.read_text(encoding="utf-8"))
        self.assertRegex(value["updated_at"], ISO)
        self.assertEqual(value["time_zone"], "UTC")
        self.assertIsInstance(value["session"]["turn_started_at"], float)  # archive keeps exact stored values

    def test_task_tree(self):
        state = self.one_shot()
        value = json.loads(self.run_main("--session", "demo", "--task-tree"))
        self.assertEqual(value["time_zone"], "UTC")
        self.assertEqual(value["current_root_turn_started_at_utc"], iso_utc(state.turn_started_at))

    def test_checkpoints_json_and_rewind_listing(self):
        model = Script([writes(self.root, ("a.txt", "one\n")), {"content": "wrote"}])
        code, _, _ = self.invoke(self.chat_args("write a", "--session", "demo", "--mode", "edit"), model,
                                 AssertionError("no input"))
        self.assertEqual(code, 0)
        listing = json.loads(self.run_main("--session", "demo", "--checkpoints"))
        self.assertRegex(listing["checkpoints"][0]["created_at"], ISO)

    def test_chat_status_and_checkpoints_text(self):
        model = Script([writes(self.root, ("a.txt", "one\n")), {"content": "wrote"}])
        code, stdout, _ = self.invoke(self.chat_args("--session", "demo", "--mode", "edit", "--no-main-agent"), model,
                                      ["write a", "/status", "/checkpoints", "/exit"])
        self.assertEqual(code, 0)
        self.assertRegex(stdout, r"Turn started: " + TEXT)
        self.assertRegex(stdout, r"第 1 轮  " + TEXT)

    def test_status_of_a_session_without_a_start_time_shows_no_time_line(self):
        state = self.one_shot()
        state.turn_started_at = None  # what a very old session row may hold
        SessionStore(self.db).save(state)
        code, stdout, _ = self.invoke(self.chat_args("--session", "demo", "--no-main-agent"), Model([]),
                                      ["/status", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("Turn: ", stdout)
        self.assertNotIn("Turn started:", stdout)

    def test_jsonl_timestamp_is_unix_seconds(self):
        before = time.time()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--root", str(self.root), "--db", str(self.db), "--session", "demo", "--output-format", "jsonl",
                         "task"], model_factory=lambda args: Model([{"content": "done"}]),
                        capability_factory=ping_registry)
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        for line in lines:
            self.assertEqual(set(line), {"type", "timestamp", "data"})
            self.assertIsInstance(line["timestamp"], float)
            self.assertLessEqual(before - 1, line["timestamp"])
            self.assertLessEqual(line["timestamp"], time.time() + 1)
            self.assertRegex(iso_utc(line["timestamp"]), ISO)


class FrontendTimestampTests(ChatTestCase):
    def test_status_event_marks_turn_and_worker_times(self):
        model = Duplex(main=[delegate("d1", "read README.md"), {"content": "已派出 w1。"}],
                       worker=[{"content": "README.md:1-1"}])
        session = self.session(["--session", "demo"], model, ui="tui")
        session.open()
        session.submit("读 README")
        self.idle(session)
        status = session.status()
        self.assertRegex(status["turn_started_at_utc"], ISO)
        (worker,) = status["workers"]
        self.assertRegex(worker["started_at_utc"], ISO)
        self.assertRegex(worker["finished_at_utc"], ISO)
        self.assertEqual(worker["started_at_utc"], iso_utc(worker["started_at"]))

    @unittest.skipUnless(HAS_WEB, NO_WEB)
    def test_web_status_and_checkpoints_text(self):
        from fastapi.testclient import TestClient
        from baseagent.web.server import create_app
        model = Script([writes(self.root, ("a.txt", "one\n")), {"content": "wrote"}])
        session = self.session(["--session", "demo", "--mode", "edit", "--no-main-agent"], model, ui="web")
        session.open()
        client = TestClient(create_app(session, token=None, host="127.0.0.1"), base_url="http://127.0.0.1:8765")
        client.post("/messages", json={"text": "write a"})
        wait_until(lambda: not session.busy)
        for command in ("/status", "/checkpoints"):
            self.assertEqual(client.post("/messages", json={"text": command}).json(), {"result": "command"})
        notices = "\n".join(item["text"] for item in self.events(session, "notice"))
        self.assertRegex(notices, r"Turn started: " + TEXT)
        self.assertRegex(notices, r"第 1 轮  " + TEXT)


if __name__ == "__main__":
    unittest.main()
