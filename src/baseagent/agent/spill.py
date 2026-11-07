"""Deterministic, file-backed replacements for oversized tool results.

Spill is a harness-kernel context tier, not a memory manager. The complete raw
result stays in the authoritative transcript (and tool ledger); the model sees a
small stub containing a path it can open with the ordinary workspace ``read``
tool. Replacements are recorded separately from the transcript, exactly like
``history_summary``, and replayed by :func:`baseagent.tools.memory.project_history`.

Three independent budgets apply to results in the current raw transcript:

``single``
    One result exceeds ``single_chars``.
``total``
    All still-full results together exceed ``total_chars``; the largest are
    spilled until the remainder is under budget.
``age``
    A result is older than ``max_age_turns``; its preview is cut further to
    ``old_preview_chars``.

Every replacement emits ``tool_result_spilled`` with one of those stable
``reason`` values. Text is line-addressable: structured values use indented JSON
and every line is split into segments of at most 1000 characters.
"""

import json
import os
import tempfile
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from baseagent.tools.result import ErrorCode, ToolResult

DEFAULT_CONFIG = {
    "enabled": True,
    "threshold_tokens": None,
    "single_chars": 50_000,
    "total_chars": 200_000,
    "max_age_turns": 10,
    "old_preview_chars": 200,
    "preview_chars": 2_048,
    "head_lines": 20,
    "tail_lines": 10,
}

REASONS = ("single", "total", "age")


def _session_component(state):
    session_id = state.session_id
    if session_id is None:
        # Process-local runs still get an isolated temporary spill root.
        return "memory"
    if (not isinstance(session_id, str) or not session_id or session_id in {".", ".."}
            or "/" in session_id or "\\" in session_id or "\x00" in session_id):
        raise ValueError("session_id must be a safe path component")
    return session_id


def _wrapped(content):
    wrapped = []
    for line in content.splitlines(keepends=True) or [""]:
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line[:-len(ending)] if ending else line
        width = 1000 - len(ending or "\n")
        segments = [body[i:i + width] for i in range(0, len(body), width)] or [""]
        wrapped.extend(segment + (ending if index == len(segments) - 1 else "\n")
                       for index, segment in enumerate(segments))
    return "".join(wrapped)


def _budget_lines(lines, count, budget):
    """Take at most ``count`` lines, keeping the joined result within ``budget``."""
    result = []
    used = 0
    for line in lines[:count]:
        line = line[:max(1, budget)]
        if result and used + len(line) + 1 > budget:
            break
        if not result and budget <= 0:
            break
        remaining = max(0, budget - used)
        if len(line) > remaining:
            line = line[:remaining]
        if not line and result:
            break
        result.append(line)
        used += len(line) + 1
        if used >= budget:
            break
    return result


class SpillStore:
    def __init__(self, store):
        self.store = store
        self._temporary = None

    def root_path(self, state):
        """Return the session directory without creating it."""
        session_id = _session_component(state)
        if self.store is not None and getattr(self.store, "durable", False):
            return self.store.path.parent / "spill" / session_id
        if self._temporary is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="baseagent-spill-")
        return Path(self._temporary.name) / session_id

    def directory(self, state):
        root = self.root_path(state)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _display_path(self, state, path):
        root = state.workspace_root
        if root:
            try:
                return path.relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                # Outside the workspace: the ordinary read tool can still open
                # the absolute path through its registered spill root.
                return str(path)
        return path.relative_to(self.root_path(state).parent).as_posix()

    @staticmethod
    def _turn_distances(messages):
        current, turns = -1, []
        for message in messages:
            if message.get("role") == "user":
                current += 1
            turns.append(current)
        return [0 if turn < 0 else current - turn for turn in turns]

    @staticmethod
    def _tool_names(messages):
        names = {}
        for message in messages:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    if call.get("id") and function.get("name"):
                        names[call["id"]] = function["name"]
        return names

    @staticmethod
    def _entries(state):
        names = SpillStore._tool_names(state.messages)
        distances = SpillStore._turn_distances(state.messages)
        entries = []
        for index, message in enumerate(state.messages):
            if message.get("role") != "tool":
                continue
            call_id = message.get("tool_call_id")
            tool = names.get(call_id)
            if tool == "read_spill":
                continue
            try:
                value = json.loads(message.get("content") or "")
            except (ValueError, TypeError):
                continue
            if not isinstance(value, dict) or not value.get("ok"):
                continue
            data = value.get("data")
            try:
                content = data if isinstance(data, str) else json.dumps(
                    data, ensure_ascii=False, allow_nan=False, indent=1, sort_keys=False)
                content = _wrapped(content)
            except (TypeError, ValueError, RecursionError):
                continue
            entries.append({
                "index": index,
                "call_id": call_id,
                "tool": tool or "unknown",
                "content": content,
                "chars": len(content),
                "distance": distances[index],
            })
        return entries

    def _record(self, state, entry, reason, config, records, checkpoint, *, aged=False):
        content = entry["content"]
        ref = sha256(f"{state.session_id}:{entry['call_id']}:{sha256(content.encode()).hexdigest()}".encode()).hexdigest()
        root = self.directory(state)
        path = root / f"{ref}.txt"
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root, prefix=f".{ref}.", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        lines = content.splitlines() or [""]
        preview = config["preview_chars"]
        tail_budget = preview - preview // 2 if config["tail_lines"] else 0
        head = _budget_lines(lines, config["head_lines"], preview - tail_budget)
        tail = _budget_lines(list(reversed(lines)), config["tail_lines"], tail_budget)
        tail.reverse()
        tokens = (len(content) + 3) // 4
        record = {
            "ref": ref,
            "path": self._display_path(state, path),
            "chars": len(content),
            "estimated_tokens": tokens,
            "lines": len(lines),
            "head": head,
            "tail": tail,
            "reason": reason,
            "call_id": entry["call_id"],
            "tool": entry["tool"],
            "aged": aged,
            "age_applied": aged,
        }
        records[str(entry["index"])] = record
        state.metadata["spill_replacements"] = records
        checkpoint("tool_result_spilled", ref=ref, call_id=entry["call_id"], tool=entry["tool"],
                   reason=reason, chars=len(content), estimated_tokens=tokens, lines=len(lines))
        return record

    def apply(self, state, config, checkpoint):
        """Record replacements for oversized, excessive, or old results.

        Runs before history compaction and model projection. It never edits
        ``state.messages``; projection applies the recorded stubs.
        """
        if not config.get("enabled", True):
            return
        records = deepcopy(state.metadata.get("spill_replacements") or {})
        entries = self._entries(state)
        aged = {entry["index"] for entry in entries
                if entry["distance"] > config["max_age_turns"]}
        changed = False

        # 1. Per-result budget.
        for entry in entries:
            if entry["chars"] <= config["single_chars"] or str(entry["index"]) in records:
                continue
            reason = "age" if entry["index"] in aged else "single"
            self._record(state, entry, reason, config, records, checkpoint, aged=reason == "age")
            changed = True

        # 2. Aggregate budget over results that are still full.
        remaining = [entry for entry in entries if str(entry["index"]) not in records]
        total = sum(entry["chars"] for entry in remaining)
        for entry in sorted(remaining, key=lambda item: (-item["chars"], item["index"])):
            if total <= config["total_chars"]:
                break
            reason = "age" if entry["index"] in aged else "total"
            self._record(state, entry, reason, config, records, checkpoint, aged=reason == "age")
            total -= entry["chars"]
            changed = True

        # 3. Age budget for results that were not already replaced.
        for entry in entries:
            if entry["index"] not in aged:
                continue
            record = records.get(str(entry["index"]))
            if record is None:
                self._record(state, entry, "age", config, records, checkpoint, aged=True)
                changed = True
            elif not record.get("age_applied"):
                record["aged"] = record["age_applied"] = True
                state.metadata["spill_replacements"] = records
                checkpoint("tool_result_spilled", ref=record["ref"], call_id=entry["call_id"],
                           tool=entry["tool"], reason="age", chars=record["chars"],
                           estimated_tokens=record["estimated_tokens"], lines=record["lines"])
                changed = True

        if changed:
            state.metadata["spill_replacements"] = records

    def read(self, state, ref, offset, limit):
        if not isinstance(ref, str) or len(ref) != 64 or any(c not in "0123456789abcdef" for c in ref):
            return ToolResult.failure(ErrorCode.NOT_FOUND, "unknown spill reference")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            return ToolResult.failure(ErrorCode.INVALID_ARGUMENTS, "offset must be nonnegative and limit must be 1-200 lines")
        path = self.directory(state) / f"{ref}.txt"
        try:
            value = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return ToolResult.failure(ErrorCode.NOT_FOUND, "unknown spill reference")
        return ToolResult(data={"ref": ref, "offset": offset, "total_lines": len(value),
                                "lines": value[offset:offset + limit],
                                "next_offset": offset + limit if offset + limit < len(value) else None})


def register_read_spill(tools, spill_store):
    """Register the bounded legacy spill reader once the run config is known."""
    if "read_spill" in {spec["function"]["name"] for spec in tools.specs()}:
        return
    tools.register(
        "read_spill", "Read a bounded slice of a spilled tool result (line offsets). New spills are normally read with the ordinary read tool.",
        {"type": "object", "properties": {"ref": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["ref", "offset", "limit"]},
        lambda *, context, ref, offset, limit: spill_store.read(context.state, ref, offset, limit),
        retry_safe=True, contextual=True)
