"""One gateway for local and MCP tools: hooks, policy, approvals, HarnessKnight, Watchdog (README plan A/C/D).

Agent-level tests drive ``run_agent`` with scripted models and the in-repo fake MCP server.
"""

import ast
import json
import tempfile
import unittest
from pathlib import Path

from mcp_helpers import ECHO, NOTE, Recorder, Script, manager, server_config, tool_call

from baseagent.agent import run_agent
from baseagent.agent.gateway import GatewayPolicy, restrict
from baseagent.agent.state import RunStatus
from baseagent.kernel import AgentMiddleware, KernelState, ToolRegistry, iter_agent
from baseagent.middleware.harnessknight import install_harnessknight
from baseagent.session import SessionStore
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.workspace import Workspace, coding_tools

SOURCE = Path(__file__).resolve().parents[1] / "src" / "baseagent"


class GatewayCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.notes = self.root / "notes.txt"
        self.workspace = Workspace(self.root / "ws", allow_write=True) if (self.root / "ws").mkdir() is None else None
        self.mcp = manager(server_config("--notes", str(self.notes))).start()
        self.addCleanup(self.mcp.close)
        self.tools = coding_tools(self.workspace)
        self.mcp.register(self.tools, allow_write=True)
        self.store = SessionStore(self.root / "sessions.sqlite3")

    def write_args(self, path="made.txt"):
        return {"path": path, "content": "created", "expected_sha256": "missing",
                "expected_instructions": self.workspace.get_instructions(path)["digest"]}

    def run_turn(self, model, prompt="go", *, session="s", **options):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id=session,
                         workspace_root=self.workspace.root, **options)

    def approve(self, state, decision="allow", session="s", digest=None):
        request = state.metadata["approval_request"]
        self.store.decide_tool(session, request["call_id"], decision, digest or request["request_digest"])
        return request

    def results(self, state):
        return [json.loads(message["content"]) for message in state.messages if message.get("role") == "tool"]

    def events(self, session="s"):
        return self.store.events(session, limit=1000)


class HookTests(GatewayCase):
    def test_one_recorder_sees_local_and_mcp_calls_through_the_same_hooks(self):
        recorder = Recorder()
        policy = ToolPolicy({"write_file": "ask"})
        model = Script({"tool_calls": [tool_call("local", "write_file", self.write_args())]})
        state = self.run_turn(model, middleware=[recorder], tool_policy=policy)
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.approve(state)
        model = Script({"tool_calls": [tool_call("remote", NOTE, {"text": "TOP-SECRET-ARGUMENT"})]})
        state = self.run_turn(model, None, middleware=[recorder], tool_policy=policy)
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.assertFalse(self.notes.exists())
        self.assertEqual(state.metadata["approval_request"]["mcp_server_id"], "demo")
        self.approve(state)
        model = Script({"tool_calls": [tool_call("broken", "mcp__demo__fail", {})]})
        state = self.run_turn(model, None, middleware=[recorder], tool_policy=policy)
        self.approve(state)
        state = self.run_turn(Script({"content": "done"}), None, middleware=[recorder], tool_policy=policy)
        self.assertEqual(state.status, RunStatus.COMPLETED, state.error)
        self.assertEqual((self.workspace.root / "made.txt").read_text(), "created")
        self.assertEqual(self.notes.read_text(), "TOP-SECRET-ARGUMENT\n")
        self.assertEqual(recorder.events, [
            ("before_tool", "write_file", "local", "local", None),
            ("approval_stop", "write_file", "local", None),
            ("before_tool", "write_file", "local", "local", None),
            ("after_tool", "write_file", "local", "local", None, True),
            ("before_tool", NOTE, "remote", "mcp", "demo"),
            ("approval_stop", NOTE, "remote", "demo"),
            ("before_tool", NOTE, "remote", "mcp", "demo"),
            ("after_tool", NOTE, "remote", "mcp", "demo", True),
            ("before_tool", "mcp__demo__fail", "broken", "mcp", "demo"),
            ("approval_stop", "mcp__demo__fail", "broken", "demo"),
            ("before_tool", "mcp__demo__fail", "broken", "mcp", "demo"),
            ("after_tool", "mcp__demo__fail", "broken", "mcp", "demo", False),
            ("on_tool_failure", "mcp__demo__fail", "broken", "mcp", "demo", "execution_failed"),
        ])
        # Both backends share the ledger; metadata events name the server and never carry arguments.
        ledger = {record["call_id"]: record["status"] for record in self.store.calls("s", state.turn_id)}
        self.assertEqual(ledger, {"local": "completed", "remote": "completed", "broken": "completed"})
        events = self.events()
        self.assertNotIn("TOP-SECRET-ARGUMENT", json.dumps(events))
        by_type = lambda kind, call: [e["data"] for e in events if e["type"] == kind and e["data"].get("call_id") == call]
        for kind in ("approval_requested", "tool_call_started", "tool_call_finished"):
            self.assertEqual({item.get("mcp_server_id") for item in by_type(kind, "remote")}, {"demo"}, kind)
            self.assertEqual({item.get("mcp_server_id") for item in by_type(kind, "local")}, {None}, kind)

    def test_a_denied_call_reaches_on_tool_failure_without_running(self):
        recorder = Recorder()
        model = Script({"tool_calls": [tool_call("c1", NOTE, {"text": "no"})]}, {"content": "ok"})
        state = self.run_turn(model, middleware=[recorder], tool_policy=ToolPolicy({NOTE: "deny"}))
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertFalse(self.notes.exists())
        self.assertEqual(recorder.kinds(NOTE), ["before_tool", "after_tool", "on_tool_failure"])
        self.assertEqual(self.results(state)[0]["error"]["code"], "permission_denied")

    def test_hooks_can_only_tighten_a_call(self):
        class Guard(AgentMiddleware):
            def __init__(self, action):
                self.action = action

            def before_tool(self, request):
                restrict(request, self.action, "test guard")

        # deny: neither a local read nor a read-only MCP tool runs.
        model = Script({"tool_calls": [tool_call("c1", ECHO, {"text": "x"}), tool_call("c2", "glob", {"pattern": "*"})]},
                       {"content": "ok"})
        state = self.run_turn(model, middleware=[Guard("deny")], session="deny")
        self.assertEqual([result["error"]["code"] for result in self.results(state)], ["permission_denied"] * 2)
        # ask: the call waits for the existing approval, then runs.
        model = Script({"tool_calls": [tool_call("c1", ECHO, {"text": "x"})]})
        state = self.run_turn(model, middleware=[Guard("ask")], session="ask")
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.approve(state, session="ask")
        state = self.run_turn(Script({"content": "ok"}), None, middleware=[Guard("ask")], session="ask")
        self.assertEqual(self.results(state)[0]["data"]["text"], "x")
        # A hook cannot loosen: "allow" is refused, and a policy denial stays a denial.
        with self.assertRaises(ValueError):
            Guard("allow").before_tool(type("R", (), {"name": "t", "arguments": "{}", "call_id": "c", "state": state})())
        model = Script({"tool_calls": [tool_call("c1", ECHO, {"text": "x"})]}, {"content": "ok"})
        state = self.run_turn(model, middleware=[Guard("ask")], tool_policy=ToolPolicy({ECHO: "deny"}), session="both")
        self.assertEqual(self.results(state)[0]["error"]["code"], "permission_denied")

    def test_kernel_loop_runs_the_same_closing_hooks_for_local_tools(self):
        recorder = Recorder()
        registry = ToolRegistry()
        registry.register("ping", "ping", {"type": "object"}, lambda: "pong")
        registry.register("boom", "boom", {"type": "object"}, lambda: 1 / 0)
        state = KernelState(messages=[{"role": "user", "content": "go"}])
        model = Script({"tool_calls": [tool_call("a", "ping", {}), tool_call("b", "boom", {})]}, {"content": "ok"})
        list(iter_agent(model, state, tools=registry, hooks=[recorder]))
        self.assertEqual([event[:2] for event in recorder.events],
                         [("before_tool", "ping"), ("after_tool", "ping"), ("before_tool", "boom"),
                          ("after_tool", "boom"), ("on_tool_failure", "boom")])


class BypassTests(GatewayCase):
    def test_mcp_tools_do_not_run_outside_the_harness_gateway(self):
        # The bare kernel loop has no policy, approval or ledger: an MCP-backed handler refuses it.
        state = KernelState(messages=[{"role": "user", "content": "go"}])
        model = Script({"tool_calls": [tool_call("c1", NOTE, {"text": "sneaky"})]}, {"content": "ok"})
        list(iter_agent(model, state, tools=self.tools))
        self.assertIn("MCP tools run only through the tool gateway", state.messages[-2]["content"])
        self.assertFalse(self.notes.exists())

    def test_only_the_registered_handler_reaches_the_transport(self):
        """Static guard: tools/call is sent from one place, and model adapters never import the MCP package."""
        callers, importers = [], []
        for path in SOURCE.rglob("*.py"):
            relative = path.relative_to(SOURCE).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "call_tool":
                    callers.append(relative)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    if any(name.startswith("baseagent.mcp") for name in names):
                        importers.append(relative)
        self.assertEqual(callers, ["mcp/manager.py"])
        self.assertEqual(sorted(set(importers)), ["main.py"])
        text = (SOURCE / "mcp" / "client.py").read_text(encoding="utf-8")
        self.assertEqual(text.count('"tools/call"'), 1)


class PolicyTests(GatewayCase):
    def test_side_effect_mcp_tools_ask_by_default_and_a_policy_cannot_switch_that_off(self):
        for policy in (None, ToolPolicy({NOTE: "allow"}), ToolPolicy(default="allow")):
            session = f"p{id(policy)}"
            model = Script({"tool_calls": [tool_call("c1", NOTE, {"text": "hi"})]})
            state = self.run_turn(model, session=session, tool_policy=policy)
            self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
            self.assertFalse(self.notes.exists())

    def test_read_only_mcp_tools_follow_the_policy_as_written(self):
        model = Script({"tool_calls": [tool_call("c1", ECHO, {"text": "hi"})]}, {"content": "ok"})
        state = self.run_turn(model, session="allow")
        self.assertEqual(self.results(state)[0]["data"]["text"], "hi")
        model = Script({"tool_calls": [tool_call("c1", ECHO, {"text": "hi"})]})
        self.assertEqual(self.run_turn(model, session="ask", tool_policy=ToolPolicy({ECHO: "ask"})).status,
                         RunStatus.AWAITING_APPROVAL)

    def test_wrong_digest_is_refused_and_an_approval_covers_one_call_only(self):
        model = Script({"tool_calls": [tool_call("c1", NOTE, {"text": "first"})]})
        state = self.run_turn(model)
        with self.assertRaises(ValueError):
            self.approve(state, digest="0" * 64)
        self.assertFalse(self.notes.exists())
        self.approve(state)
        again = Script({"tool_calls": [tool_call("c2", NOTE, {"text": "first"})]})
        state = self.run_turn(again, None)
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)  # same arguments, new call id: asks again
        self.assertEqual(self.notes.read_text(), "first\n")
        self.approve(state, "deny")
        state = self.run_turn(Script({"content": "ok"}), None)
        self.assertEqual(self.results(state)[-1]["error"]["code"], "permission_denied")
        self.assertEqual(self.notes.read_text(), "first\n")

    def test_gateway_policy_is_transparent_without_mcp_tools(self):
        plain = coding_tools(self.workspace)
        parent = ToolPolicy({"write_file": "ask"})
        wrapped = GatewayPolicy(parent, plain)
        self.assertEqual((wrapped.contract(), wrapped.fingerprint), (parent.contract(), parent.fingerprint))
        with_mcp = GatewayPolicy(parent, self.tools)
        self.assertNotEqual(with_mcp.fingerprint, parent.fingerprint)
        self.assertEqual(with_mcp.contract()["mcp_default_ask"][NOTE], "demo")
        self.assertNotIn(ECHO, with_mcp.contract()["mcp_default_ask"])


class FakeHarness:
    def __init__(self, decision):
        self.decision, self.calls = decision, []

    def evaluate(self, event, idempotency_key, timeout):
        self.calls.append(json.loads(json.dumps(event)))
        return {"decision": self.decision if event["event_type"] == "tool.call.before" else "ALLOW"}


class HarnessKnightAndWatchdogTests(GatewayCase):
    def harness(self, decision):
        transport = FakeHarness(decision)
        policy, layers = install_harnessknight(ToolPolicy(), [], url="http://fake", transport=transport)
        return transport, {"tool_policy": policy, "middleware": layers}

    def test_existing_harnessknight_sees_the_server_id_and_review_uses_the_existing_approval(self):
        transport, options = self.harness("REVIEW")
        model = Script({"tool_calls": [tool_call("c1", ECHO, {"text": "hi"}), tool_call("c2", "glob", {"pattern": "*"})]})
        state = self.run_turn(model, **options)
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.assertEqual(state.metadata["approval_request"]["name"], ECHO)
        self.approve(state)
        state = self.run_turn(Script({"content": "ok"}), None, **options)
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)  # the local call is reviewed the same way
        self.approve(state)
        state = self.run_turn(Script({"content": "ok"}), None, **options)
        self.assertEqual(state.status, RunStatus.COMPLETED, state.error)
        self.assertEqual(self.results(state)[0]["data"]["text"], "hi")
        trace = {(event["event_type"], event["payload"].get("tool_name")): event["trace"]["mcp_server_id"]
                 for event in transport.calls if event["event_type"].startswith("tool.call")}
        self.assertEqual(trace, {("tool.call.before", ECHO): "demo", ("tool.call.after", ECHO): "demo",
                                 ("tool.call.before", "glob"): None, ("tool.call.after", "glob"): None})

    def test_harnessknight_block_does_not_run_the_mcp_tool(self):
        transport, options = self.harness("BLOCK")
        model = Script({"tool_calls": [tool_call("c1", NOTE, {"text": "blocked"})]}, {"content": "ok"})
        state = self.run_turn(model, **options)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(self.results(state)[0]["error"]["code"], "permission_denied")
        self.assertFalse(self.notes.exists())

    def test_harnessknight_allow_still_leaves_the_default_ask_on_side_effect_tools(self):
        transport, options = self.harness("ALLOW")
        model = Script({"tool_calls": [tool_call("c1", NOTE, {"text": "x"})]})
        self.assertEqual(self.run_turn(model, **options).status, RunStatus.AWAITING_APPROVAL)

    def test_watchdog_counts_repeated_mcp_calls_in_its_existing_phases(self):
        repeat = lambda index: {"tool_calls": [tool_call(f"c{index}", ECHO, {"text": "same"})]}
        model = Script(repeat(1), repeat(2), repeat(3), {"content": "never"})
        state = self.run_turn(model, runtime_config={"watchdog": {"enabled": True, "repeat_abort": 2}})
        self.assertEqual(state.status, RunStatus.ABORTED_BY_WATCHDOG)
        watchdog = state.metadata["watchdog"]
        self.assertEqual([call["name"] for call in watchdog["calls"]], [ECHO, ECHO])
        self.assertEqual(watchdog["open_tools"], {})  # before_tool opened and after_tool closed both calls
        self.assertEqual(watchdog["stop"]["rule"], "repeat_call")
        self.assertEqual(watchdog["stop"]["evidence"]["tool"], ECHO)


if __name__ == "__main__":
    unittest.main()
