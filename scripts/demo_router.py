"""Offline router demo. No provider calls.

Shows a qa citation, a coding approval pause, separate worker ledgers,
and the token gap versus inlining both tool bodies.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from baseagent.agent import run_agent  # noqa: E402
from baseagent.agent.router import handoff, isolation_report, route  # noqa: E402
from baseagent.agent.subtasks import SubtaskDefinition, SubtaskRuntime  # noqa: E402
from baseagent.llm.response import ModelResponse, TokenUsage  # noqa: E402
from baseagent.llm.streaming import StreamDelta  # noqa: E402
from baseagent.session import SessionStore  # noqa: E402
from baseagent.tools.policy import ToolPolicy  # noqa: E402
from baseagent.tools.registry import ToolRegistry  # noqa: E402


def _call(name, arguments, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class StreamAnswer:
    def __init__(self, responses):
        self.responses = iter(responses)

    def complete_stream(self, messages, tools=None):
        text = "knowledge_search checks the read policy. applications/project_assistant/access.py:49-62"
        for piece in (text[:24], text[24:]):
            yield StreamDelta(kind="content", index=0, delta=piece)
        return ModelResponse({"content": text}, TokenUsage(3, 2, 5))

    def complete(self, messages, tools=None):
        return ModelResponse(next(self.responses), TokenUsage(3, 2, 5))


class Script:
    def __init__(self, responses):
        self.responses = iter(responses)

    def complete(self, messages, tools=None):
        return ModelResponse(next(self.responses), TokenUsage(3, 1, 4))


def main():
    question = "knowledge_search 怎么防越权"
    change = "Fix sample.py and run the tests"
    qa_route, coding_route = route(question), route(change)
    root = Path(tempfile.mkdtemp(prefix="baseagent-router-"))
    secret = "SECRET_TOOL_BODY_" + ("x" * 2000)
    tools = ToolRegistry()
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    tools.register("read_file", "read", schema, lambda: {"body": secret, "citation": "applications/project_assistant/access.py:49-62"})
    tools.register("edit_file", "edit", schema, lambda: {"edited": True})
    qa = StreamAnswer([{"tool_calls": [_call("read_file", {}, "qa-read")]},
                       {"content": "knowledge_search checks the read policy. applications/project_assistant/access.py:49-62"}])
    coding = Script([{"tool_calls": [_call("edit_file", {}, "code-edit")]}, {"content": "edited"}])
    runtime = SubtaskRuntime([
        SubtaskDefinition("qa", ("read_file",), model=qa),
        SubtaskDefinition("coding", ("edit_file",), model=coding, policy=ToolPolicy({"edit_file": "ask"})),
    ])
    parent = Script([
        {"tool_calls": [_call("run_subtask", {"name": qa_route["worker"], "prompt": question}, "to-qa")]},
        {"tool_calls": [_call("run_subtask", {"name": coding_route["worker"], "prompt": change}, "to-coding")]},
        {"content": "routed"},
    ])
    store = SessionStore(root / "sessions.sqlite3")
    state = run_agent(parent, question, tools=tools, store=store, session_id="router-demo",
                      workspace_root=root, subtasks=runtime, max_steps=6,
                      runtime_config={"stream": {"enabled": True}})
    nodes = store.task_tree("router-demo")["nodes"]
    payload = handoff("qa", "knowledge_search checks the read policy. applications/project_assistant/access.py:49-62",
                      [{"role": "tool", "content": secret}])
    report = isolation_report([secret, secret], payload)
    print(json.dumps({
        "qa_route": qa_route, "coding_route": coding_route, "status": state.status,
        "workers": [node["name"] for node in nodes],
        "router_saw_secret": secret in json.dumps(state.messages, ensure_ascii=False),
        "isolation": report, "cost_cny": 0,
    }, ensure_ascii=False, indent=2))
    return 0 if qa_route["worker"] == "qa" and coding_route["worker"] == "coding" and state.status == "awaiting_approval" and not (secret in json.dumps(state.messages)) else 1


if __name__ == "__main__":
    sys.exit(main())
