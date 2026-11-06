"""Project triggers stay read-only and do not start a watcher."""

import json
import tempfile
import unittest
from pathlib import Path

from baseagent.agent.triggers import TriggerBus, glob_match, load_config


def config(*subscriptions, enabled=True, debounce=1.0):
    return {"enabled": enabled, "debounce_seconds": debounce, "subscriptions": list(subscriptions)}


def fs(name="test-failure-summary", pattern="reports/*.log", worker="qa", enabled=True):
    return {"name": name, "kind": "fs", "glob": pattern, "worker": worker, "mode": "readonly", "enabled": enabled}


class TriggerTests(unittest.TestCase):
    def test_file_change_wakes_the_named_worker_once_across_a_burst(self):
        bus = TriggerBus(config(fs()))
        first = bus.deliver({"kind": "fs", "path": "reports/test.log", "now": 10}, text="FAIL addition")
        second = bus.deliver({"kind": "fs", "path": "reports/test.log", "now": 10.2}, text="FAIL addition")
        self.assertEqual(first[0]["worker"], "qa")
        self.assertEqual(first[0]["action"], "woke")
        self.assertEqual(second, [])
        self.assertEqual(bus.inspect()["debounced"], 1)
        self.assertFalse(bus.inspect()["wrote"])

    def test_timer_wakes_once_and_a_disabled_bus_stays_quiet(self):
        timer = {"name": "interval", "kind": "timer", "worker": "qa", "mode": "readonly", "enabled": True}
        bus = TriggerBus(config(timer, debounce=0))
        woken = bus.deliver({"kind": "timer", "subscription": "interval", "now": 1})
        self.assertEqual(woken[0]["worker"], "qa")
        quiet = TriggerBus(config(timer, enabled=False))
        self.assertEqual(quiet.deliver({"kind": "timer", "subscription": "interval", "now": 1}), [])
        disabled_sub = TriggerBus(config(fs(enabled=False)))
        self.assertEqual(disabled_sub.deliver({"kind": "fs", "path": "reports/test.log", "now": 1}), [])

    def test_poll_coalesces_and_does_not_change_project_files(self):
        root = Path(tempfile.mkdtemp())
        reports = root / "reports"
        reports.mkdir()
        log = reports / "test.log"
        log.write_text("FAIL sample\n", encoding="utf-8")
        before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        bus = TriggerBus(config(fs(), debounce=5))
        clock = {"now": 100}

        def fake_clock():
            return clock["now"]

        bus.clock = fake_clock
        first = bus.poll(root)
        log.write_text("FAIL sample\nFAIL again\n", encoding="utf-8")
        clock["now"] = 101
        second = bus.poll(root)
        after = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(before.keys(), after.keys())
        self.assertIn("Read-only qa summary", first[0]["summary"])
        self.assertFalse(bus.inspect()["wrote"])

    def test_coding_subscription_still_does_not_write(self):
        bus = TriggerBus(config(fs(worker="coding")))
        woken = bus.deliver({"kind": "fs", "path": "reports/test.log", "now": 1}, text="FAIL")
        self.assertEqual(woken[0]["worker"], "coding")
        self.assertTrue(woken[0]["approval_required"])
        self.assertFalse(woken[0]["wrote"])

    def test_glob_and_config_are_explicit(self):
        self.assertTrue(glob_match("reports/*.log", "reports/test.log"))
        self.assertFalse(glob_match("reports/*.log", "src/app.py"))
        path = Path(tempfile.mkdtemp()) / "triggers.json"
        path.write_text(json.dumps(config(fs())), encoding="utf-8")
        loaded = load_config(path)
        self.assertTrue(loaded["enabled"])
        self.assertEqual(loaded["subscriptions"][0]["worker"], "qa")


if __name__ == "__main__":
    unittest.main()
