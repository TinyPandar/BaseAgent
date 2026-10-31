"""Deterministic, file-backed tool-result spills.

Spilling runs before context projection, so ``--max-tool-context-bytes`` may
clip the preview sent to the model while the complete result remains retrievable.
Stored text is line-addressable: structured values use indented JSON and every
line is split into segments of at most 1000 characters.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from baseagent.tools.result import ErrorCode, ToolResult


class SpillStore:
    def __init__(self, store):
        self.store = store
        self._temporary = None

    def directory(self, state):
        session_id = state.session_id
        if (not isinstance(session_id, str) or not session_id or session_id in {".", ".."}
                or "/" in session_id or "\\" in session_id or "\x00" in session_id):
            raise ValueError("session_id must be a safe path component")
        if self.store is not None and getattr(self.store, "durable", False):
            root = self.store.path.parent / "spill" / session_id
        else:
            if self._temporary is None:
                self._temporary = tempfile.TemporaryDirectory(prefix="baseagent-spill-")
            root = Path(self._temporary.name) / session_id
        root.mkdir(parents=True, exist_ok=True)
        return root

    def spill(self, state, call_id, tool, result, threshold, head_lines, tail_lines, checkpoint):
        if not result.ok or tool == "read_spill":
            return result
        rendered = result.data if isinstance(result.data, str) else json.dumps(result.data, ensure_ascii=False, allow_nan=False, indent=1, sort_keys=False)
        wrapped = []
        for line in rendered.splitlines(keepends=True) or [""]:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            body = line[:-len(ending)] if ending else line
            width = 1000 - len(ending or "\n")
            segments = [body[i:i + width] for i in range(0, len(body), width)] or [""]
            wrapped.extend(segment + (ending if index == len(segments) - 1 else "\n")
                           for index, segment in enumerate(segments))
        content = "".join(wrapped)
        tokens = (len(content) + 3) // 4
        if tokens <= threshold:
            return result
        ref = sha256(f"{state.session_id}:{call_id}:{sha256(content.encode()).hexdigest()}".encode()).hexdigest()
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
        head = [line[:300] for line in lines[:head_lines]]
        tail = [line[:300] for line in lines[-tail_lines:]] if tail_lines else []
        note = "Call read_spill(ref, offset, limit) to fetch more lines."
        stub = {"spill_ref": ref, "path": f"spill/{state.session_id}/{ref}.txt",
                "chars": len(content), "estimated_tokens": tokens, "lines": len(lines),
                "head": head, "tail": tail, "note": note}
        checkpoint("tool_result_spilled", ref=ref, call_id=call_id, tool=tool,
                   chars=len(content), estimated_tokens=tokens, lines=len(lines))
        return ToolResult(data=stub)

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
    """Register the bounded spill reader once the effective run config is known."""
    if "read_spill" in {spec["function"]["name"] for spec in tools.specs()}:
        return
    tools.register(
        "read_spill", "Read a bounded slice of a spilled tool result (line offsets).",
        {"type": "object", "properties": {"ref": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["ref", "offset", "limit"]},
        lambda *, context, ref, offset, limit: spill_store.read(context.state, ref, offset, limit),
        retry_safe=True, contextual=True)
