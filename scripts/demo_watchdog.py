"""Offline Watchdog demo: a repeated read is stopped before the step limit.

No provider calls. Prints stall/repeat counts and budget remaining.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from baseagent.agent import run_agent  # noqa: E402
from baseagent.agent.watchdog import summary  # noqa: E402
from baseagent.llm.response import ModelResponse, TokenUsage  # noqa: E402
from baseagent.session import SessionStore  # noqa: E402
from baseagent.tools.registry import ToolRegistry  # noqa: E402


def _call(name, args, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class LoopModel:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        if self.calls < 8:
            return ModelResponse({"tool_calls": [_call("read_file", {"path": "README.md"}, f"r{self.calls}")]},
                                 TokenUsage(2, 1, 3))
        return ModelResponse({"content": "still reading"}, TokenUsage(2, 1, 3))


def main():
    root = Path(tempfile.mkdtemp(prefix="baseagent-watchdog-"))
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}
    tools = ToolRegistry()
    tools.register("read_file", "read", schema, lambda path: {"path": path, "text": "demo"})
    tools.register("write_file", "write", schema, lambda path: (_ for _ in ()).throw(AssertionError("write after stop")))
    store = SessionStore(root / "sessions.sqlite3")
    state = run_agent(LoopModel(), "Keep reading README.md until you are sure.", tools=tools, store=store,
                      session_id="watchdog-demo", workspace_root=root, max_steps=8,
                      runtime_config={"watchdog": {"enabled": True, "stall_steps": 2, "stall_abort_steps": 3,
                                                   "repeat_abort": 3, "max_steps": 6}})
    report = summary(state)
    decisions = [event for event in store.events("watchdog-demo") if event.get("type") == "watchdog_decision"]
    print(json.dumps({"status": state.status, "error": state.error, "tool_calls": state.tool_calls,
                      "stall_count": report["stall_count"], "repeat_count": report["repeat_count"],
                      "budget_remaining": report["budget_remaining"], "decisions": len(decisions),
                      "cost_cny": 0}, ensure_ascii=False, indent=2))
    return 0 if state.status == "aborted_by_watchdog" and state.tool_calls < 8 else 1


if __name__ == "__main__":
    sys.exit(main())
