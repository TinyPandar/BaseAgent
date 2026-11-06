"""Offline trigger demo. No watcher, no network, no paid model.

Writes a fixture failure log, wakes the read-only qa summary, and leaves
every other project file untouched.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from baseagent.agent.triggers import TriggerBus  # noqa: E402


def main():
    root = Path(tempfile.mkdtemp(prefix="baseagent-trigger-"))
    (root / "sample.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    reports = root / "reports"
    reports.mkdir()
    before = (root / "sample.py").read_bytes()
    bus = TriggerBus({"enabled": True, "debounce_seconds": 1, "subscriptions": [
        {"name": "test-failure-summary", "kind": "fs", "glob": "reports/*.log", "worker": "qa", "mode": "readonly"},
    ]})
    (reports / "test.log").write_text("FAIL addition\n", encoding="utf-8")
    woken = bus.poll(root, now=1)
    again = bus.poll(root, now=1.1)
    untouched = (root / "sample.py").read_bytes() == before
    report = {"woke": len(woken), "debounced_followup": again, "worker": woken[0]["worker"] if woken else None,
              "summary": woken[0]["summary"] if woken else "", "sample_unchanged": untouched,
              "wrote": bus.inspect()["wrote"], "tokens": bus.inspect()["tokens"], "cost_cny": 0}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["woke"] == 1 and report["sample_unchanged"] and not report["wrote"] else 1


if __name__ == "__main__":
    sys.exit(main())
