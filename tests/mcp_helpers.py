"""Shared pieces for MCP tests: the in-repo fake stdio server, scripted models, a gateway recorder.

Everything is offline: the only "server" is ``fixtures/fake_mcp_server.py`` run with this interpreter.
"""

import json
import os
import sys
import time
from pathlib import Path

from baseagent.mcp import McpManager, ServerConfig
from baseagent.middleware import AgentMiddleware

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")
SERVER = "demo"
ECHO, NOTE = "mcp__demo__echo", "mcp__demo__add_note"
WAIT = 20


def server_entry(*flags, read_only=("echo",), timeout=None, script=FAKE_SERVER):
    entry = {"command": sys.executable, "args": [script, *flags],
             "tools": {name: {"readOnly": True} for name in read_only}}
    if timeout is not None:
        entry["timeout"] = timeout
    return entry


def write_config(path, servers=None):
    path = Path(path)
    path.write_text(json.dumps({"mcpServers": servers if servers is not None else {SERVER: server_entry()}}),
                    encoding="utf-8")
    return path


def server_config(*flags, name=SERVER, read_only=("echo",), timeout=60.0):
    return ServerConfig(name, sys.executable, (FAKE_SERVER, *flags), read_only_tools=frozenset(read_only), timeout=timeout)


def manager(*configs, **options):
    options.setdefault("warn", lambda line: None)
    return McpManager(list(configs) or [server_config()], **options)


def tool_call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class Script:
    """Sequential fake model; records the tool names offered at every step."""

    def __init__(self, *steps):
        self.steps, self.tools, self.seen = iter(steps), [], []

    def complete(self, messages, tools):
        self.tools.append([tool["function"]["name"] for tool in tools])
        self.seen.append(json.loads(json.dumps(messages)))
        step = next(self.steps)
        return step(messages) if callable(step) else step


class Recorder(AgentMiddleware):
    """One gateway observer for every hook. Stores names and ids only, like a real audit hook would."""

    def __init__(self):
        self.events = []

    def before_tool(self, request):
        self.events.append(("before_tool", request.name, request.call_id, request.source, request.mcp_server_id))

    def after_tool(self, request, result):
        self.events.append(("after_tool", request.name, request.call_id, request.source, request.mcp_server_id, result.ok))

    def on_tool_failure(self, request, result):
        self.events.append(("on_tool_failure", request.name, request.call_id, request.source, request.mcp_server_id,
                            str(result.error.code)))

    def after_agent(self, state):
        approval = state.metadata.get("approval_request") or {}
        if str(state.status) == "awaiting_approval":
            self.events.append(("approval_stop", approval.get("name"), approval.get("call_id"),
                                approval.get("mcp_server_id")))

    def kinds(self, name):
        return [event[0] for event in self.events if event[1] == name]


def process_gone(pid, timeout=WAIT):
    """True once the pid no longer exists (a killed child may need a moment to be reaped)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.02)
    return False


def read_pid(path):
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        text = Path(path).read_text(encoding="utf-8") if Path(path).exists() else ""
        if text.strip():
            return int(text)
        time.sleep(0.02)
    raise AssertionError(f"pid file was never written: {path}")
