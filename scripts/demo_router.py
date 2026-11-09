"""Offline demo of the synchronous WorkerRuntime library entry. No provider calls.

One continuous session: small talk is answered without a worker, a read is
delegated to one generic worker whose tool body never reaches the main window,
and a write pauses for approval inside the worker node.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from baseagent.agent import run_agent  # noqa: E402
from baseagent.agent.router import SEQUENTIAL_MAIN_AGENT_PROMPT, WorkerRuntime, isolation_report, route  # noqa: E402
from baseagent.llm.response import ModelResponse, TokenUsage  # noqa: E402
from baseagent.session import SessionStore  # noqa: E402
from baseagent.tools.policy import ToolPolicy  # noqa: E402
from baseagent.tools.registry import ToolRegistry  # noqa: E402


def _call(name, arguments, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class Script:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.tools = []

    def complete(self, messages, tools=None):
        self.tools.append([tool["function"]["name"] for tool in tools or ()])
        return ModelResponse(next(self.responses), TokenUsage(3, 1, 4))


def main():
    greeting, question, change = "hi", "knowledge_search 怎么防越权", "Fix sample.py and run the tests"
    root = Path(tempfile.mkdtemp(prefix="baseagent-main-agent-"))
    secret = "SECRET_TOOL_BODY_" + ("x" * 2000)
    citation = "applications/project_assistant/access.py:49-62"
    tools = ToolRegistry()
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False}
    tools.register("read_file", "read", schema, lambda path="": {"body": secret, "citation": citation}, retry_safe=True)
    tools.register("edit_file", "edit", schema, lambda path="": {"edited": True})
    worker = Script([
        {"tool_calls": [_call("read_file", {"path": "applications/project_assistant/access.py"}, "w-read")]},
        {"content": f"knowledge_search checks the read policy before returning text. {citation}"},
        {"tool_calls": [_call("edit_file", {"path": "sample.py"}, "w-edit")]},
    ])
    main_agent = Script([
        {"content": "Hello! What would you like to work on?"},
        {"tool_calls": [_call("delegate_task", {"task": "Explain how knowledge_search prevents unauthorized reads; cite sources."}, "d-read")]},
        {"content": f"It checks the read policy first ({citation})."},
        {"tool_calls": [_call("delegate_task", {"task": change, "allow_writes": True}, "d-edit")]},
    ])
    runtime = WorkerRuntime(tools, system_prompt="Entry prompt for the demo workspace.", model=worker)
    store = SessionStore(root / "sessions.sqlite3")
    policy = ToolPolicy({"edit_file": "ask"})
    turns = []
    for prompt in (greeting, question, change):
        state = run_agent(main_agent, prompt, tools=tools, store=store, session_id="main-agent-demo",
                          workspace_root=root, subtasks=runtime, system_prompt=SEQUENTIAL_MAIN_AGENT_PROMPT,
                          tool_policy=policy, max_steps=16)
        nodes = store.task_tree("main-agent-demo")["nodes"]
        turns.append({"prompt": prompt, "debug_route": route(prompt)["action"], "status": state.status,
                      "workers": [node["name"] for node in nodes],
                      "worker_status": [node["state"]["status"] for node in nodes]})
    reports = [json.loads(message["content"])["data"] for message in state.messages
               if message.get("role") == "tool"]
    main_window = json.dumps(state.messages, ensure_ascii=False)
    report = isolation_report([secret], {"summary": reports[0]["summary"], "citations": reports[0]["citations"]})
    print(json.dumps({
        "turns": turns, "main_agent_tools": main_agent.tools[0], "worker_tools": worker.tools,
        "worker_report": reports[0], "main_saw_tool_body": secret in main_window,
        "isolation": report, "cost_cny": 0,
    }, ensure_ascii=False, indent=2))
    ok = (turns[0]["workers"] == [] and turns[1]["workers"] == ["worker"] and turns[2]["status"] == "awaiting_approval"
          and turns[2]["worker_status"] == ["awaiting_approval"] and main_agent.tools[0] == ["delegate_task"]
          and "edit_file" not in worker.tools[0] and "edit_file" in worker.tools[2] and secret not in main_window)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
