"""Project triggers. They wake a worker; they do not silently write.

File and timer events stay inside one project. A long-running watcher is not
started here. Callers deliver events or poll a directory once.
"""

from __future__ import annotations

import fnmatch
import json
import time
from pathlib import Path


def load_config(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("subscriptions"), list):
        raise ValueError("trigger config requires a subscriptions list")
    data.setdefault("enabled", True)
    data.setdefault("debounce_seconds", 0.5)
    if data["debounce_seconds"] < 0:
        raise ValueError("debounce_seconds must be nonnegative")
    for item in data["subscriptions"]:
        if not isinstance(item, dict) or not item.get("name") or item.get("kind") not in {"fs", "timer"}:
            raise ValueError("each subscription needs a name and kind fs or timer")
        item.setdefault("enabled", True)
        item.setdefault("worker", "qa")
        item.setdefault("mode", "readonly")
        if item["worker"] not in {"qa", "coding"} or item["mode"] not in {"readonly", "approval"}:
            raise ValueError("subscription worker must be qa or coding and mode readonly or approval")
        if item["kind"] == "fs" and not item.get("glob"):
            raise ValueError("fs subscription requires a glob")
    return data


def glob_match(pattern, relative):
    """Match a repo-relative posix path. `**` crosses directories."""
    pattern_parts = pattern.split("/")
    parts = relative.split("/")

    def rec(i, j):
        if i == len(pattern_parts):
            return j == len(parts)
        token = pattern_parts[i]
        if token == "**":
            return rec(i + 1, j) or (j < len(parts) and rec(i, j + 1))
        if j >= len(parts) or not fnmatch.fnmatchcase(parts[j], token):
            return False
        return rec(i + 1, j + 1)

    return rec(0, 0)


def default_summary(subscription, event, text):
    name = event.get("path") or subscription["name"]
    failed = "FAIL" in text or "failed" in text.lower()
    status = "failure" if failed else "change"
    return f"Read-only {subscription['worker']} summary: {name} reports {status}. No files were modified."


class TriggerBus:
    def __init__(self, config, *, clock=None, summarizer=None):
        self.config = config
        self.clock = clock or time.monotonic
        self.summarizer = summarizer or default_summary
        self.seen = {}
        self.snapshots = {}
        self.ledger = []

    def deliver(self, event, *, text=""):
        if not self.config.get("enabled", True):
            return []
        now = event.get("now", self.clock())
        woken = []
        for subscription in self._matches(event):
            key = (subscription["name"], event.get("path") or subscription["name"])
            last = self.seen.get(key)
            window = self.config.get("debounce_seconds", 0.5)
            if last is not None and now - last < window:
                self.ledger.append(self._record(subscription, event, action="debounced", summary="", tokens=0))
                continue
            self.seen[key] = now
            record = self._wake(subscription, event, text=text)
            woken.append(record)
        return woken

    def poll(self, root, *, now=None):
        root = Path(root)
        woken = []
        now = self.clock() if now is None else now
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            signature = (path.stat().st_mtime_ns, path.stat().st_size)
            previous = self.snapshots.get(relative)
            self.snapshots[relative] = signature
            if previous == signature:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            woken.extend(self.deliver({"kind": "fs", "path": relative, "now": now}, text=text))
        return woken

    def _matches(self, event):
        kind = event.get("kind")
        for subscription in self.config["subscriptions"]:
            if not subscription.get("enabled", True) or subscription["kind"] != kind:
                continue
            named = event.get("subscription")
            if named and named != subscription["name"]:
                continue
            if kind == "fs" and not glob_match(subscription["glob"], event.get("path", "")):
                continue
            yield subscription

    def _wake(self, subscription, event, *, text):
        wrote = False
        approval_required = subscription["mode"] != "readonly" or subscription["worker"] == "coding"
        summary = self.summarizer(subscription, event, text)
        if approval_required:
            summary += " A later coding change still needs the existing approval ledger."
        record = self._record(subscription, event, action="woke", summary=summary, tokens=0,
                              approval_required=approval_required, wrote=wrote)
        self.ledger.append(record)
        return record

    def _record(self, subscription, event, *, action, summary, tokens, approval_required=False, wrote=False):
        return {"subscription": subscription["name"], "kind": event.get("kind"), "path": event.get("path"),
                "worker": subscription["worker"], "action": action, "summary": summary, "tokens": tokens,
                "approval_required": approval_required, "wrote": wrote}

    def inspect(self):
        return {"enabled": bool(self.config.get("enabled", True)), "events": list(self.ledger),
                "woke": sum(item["action"] == "woke" for item in self.ledger),
                "debounced": sum(item["action"] == "debounced" for item in self.ledger),
                "tokens": sum(item["tokens"] for item in self.ledger), "wrote": any(item["wrote"] for item in self.ledger)}
