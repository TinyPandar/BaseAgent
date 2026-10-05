from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.middleware import AgentMiddleware
from baseagent.session import SessionStore, SessionBusy
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace, coding_tools


def call(name="effect", args=None, identifier="a"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}


class Model:
    def __init__(self, messages=()):
        self.messages = iter(messages)
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return next(self.messages)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.sqlite3")
        self.effects = []
        self.tools = ToolRegistry()
        self.tools.register("effect", "effect", {"type": "object", "properties": {"value": {"type": "integer"}}},
                            lambda **args: self.effects.append(args) or "done")
        self.policy = ToolPolicy({"effect": "ask"})

    def run_turn(self, model, prompt=None, **kwargs):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id="demo",
                         tool_policy=kwargs.pop("tool_policy", self.policy), **kwargs)

    def pause(self, calls=None):
        state = self.run_turn(Model([{"tool_calls": calls or [call()]}]), "task")
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.assertEqual(self.store.call(state, "a")["status"], "pending")
        self.assertEqual(state.tool_calls, 0)
        self.assertEqual(self.effects, [])
        return state

    def decide(self, state, decision="allow"):
        return self.store.decide_tool("demo", state.metadata["approval_request"]["call_id"], decision,
                                      state.metadata["approval_request"]["request_digest"])

    def test_approve_and_resume_without_replaying_model(self):
        state = self.pause()
        self.decide(state)
        self.assertEqual(self.effects, [])
        result = self.run_turn(Model([{"content": "done"}]))
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, [{}])
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(result.tool_calls, 1)
        self.run_turn(Model())
        self.assertEqual(self.effects, [{}])

    def test_repeated_resume_without_decision_stays_pending(self):
        self.pause()
        model = Model()
        state = self.run_turn(model)
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.assertEqual(model.calls, 0)
        self.assertEqual(state.model_calls, 1)
        self.assertEqual(self.effects, [])

    def test_denial_becomes_paired_result_without_dispatch(self):
        state = self.pause()
        self.decide(state, "deny")
        result = self.run_turn(Model([{"content": "denied"}]))
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(self.effects, [])
        self.assertEqual(json.loads(result.messages[-2]["content"])["error"]["code"], "permission_denied")

    def test_multiple_calls_each_need_decision(self):
        state = self.pause([call(identifier="a"), call(identifier="b")])
        self.decide(state)
        state = self.run_turn(Model())
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.assertEqual(state.metadata["approval_request"]["call_id"], "b")
        self.assertEqual(len(self.effects), 1)
        self.decide(state)
        self.assertEqual(self.run_turn(Model([{"content": "done"}])).status, RunStatus.COMPLETED)
        self.assertEqual(len(self.effects), 2)

    def test_decision_digest_and_session_lock(self):
        state = self.pause()
        with self.assertRaises(ValueError):
            self.store.decide_tool("demo", "a", "allow", "wrong")
        with self.assertRaises(ValueError):
            self.store.decide_tool("demo", "wrong", "allow", state.metadata["approval_request"]["request_digest"])
        with self.store.exclusive("demo"), self.assertRaises(SessionBusy):
            self.decide(state)

    def test_policy_change_requires_acceptance_and_invalidates_approval(self):
        state = self.pause()
        self.decide(state)
        changed = ToolPolicy({"effect": "ask", "other": "deny"})
        with self.assertRaises(ValueError):
            self.run_turn(Model(), tool_policy=changed)
        result = self.run_turn(Model(), tool_policy=changed, accept_config_changes=True)
        self.assertEqual(result.status, RunStatus.AWAITING_APPROVAL)
        self.assertEqual(self.effects, [])

    def test_redirected_arguments_do_not_inherit_approval(self):
        class Redirect(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                return handler(replace(request, arguments='{"value": 2}'))
        state = self.pause()
        self.decide(state)
        result = self.run_turn(Model([{"content": "done"}]), middleware=[Redirect()])
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(self.effects, [])

    def test_redirected_tool_is_rechecked(self):
        class Redirect(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                return handler(replace(request, name="effect"))
        self.tools.register("read", "read", {"type": "object"}, lambda: "read")
        result = self.run_turn(Model([{"tool_calls": [call("read")]}, {"content": "done"}]), "task", middleware=[Redirect()])
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, [])
        self.assertEqual(result.tool_calls, 0)

    def test_deny_does_not_enter_wrapper(self):
        class Forbidden(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                raise AssertionError("denied tool must not enter wrapper")
        result = self.run_turn(Model([{"tool_calls": [call()]}, {"content": "done"}]), "task",
                               tool_policy=ToolPolicy(default="deny"), middleware=[Forbidden()])
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, [])

    def test_approval_does_not_enable_capability(self):
        self.tools = coding_tools(Workspace(self.root))
        self.policy = ToolPolicy({"run_command": "ask"})
        state = self.pause([call("run_command", {"argv": [sys.executable, "-c", "raise AssertionError()"]})])
        self.decide(state)
        result = self.run_turn(Model([{"content": "done"}]))
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(json.loads(result.messages[-2]["content"])["error"]["code"], "permission_denied")

    def test_approval_is_not_reused_on_new_turn(self):
        state = self.pause()
        self.decide(state)
        self.run_turn(Model([{"content": "done"}]))
        state = self.run_turn(Model([{"tool_calls": [call()]}]), "next task")
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.assertEqual(len(self.effects), 1)
        self.assertNotIn("a", state.metadata.get("tool_approvals", {}))

    def test_cli_decision_without_provider_credentials(self):
        state = self.pause()
        result = subprocess.run([sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo",
                                 "--deny-tool", "a", "--approval-digest", state.metadata["approval_request"]["request_digest"]],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.load("demo").metadata["tool_approvals"]["a"]["decision"], "deny")

    def test_events_do_not_include_arguments(self):
        state = self.pause([call(args={"value": 456789})])
        self.decide(state)
        events = self.store.events("demo")
        approvals = [item for item in events if item["type"].startswith("approval_")]
        self.assertEqual(len(approvals), 2)
        self.assertNotIn("456789", json.dumps(approvals))

    def test_capability_upgrade_requires_explicit_config_acceptance(self):
        self.tools = coding_tools(Workspace(self.root))
        self.policy = ToolPolicy({"run_command": "ask"})
        state = self.pause([call("run_command", {"argv": [sys.executable, "-c", "print('ok')"]})])
        self.decide(state)
        self.tools = coding_tools(Workspace(self.root, allow_command=True))
        with self.assertRaises(ValueError):
            self.run_turn(Model())
        self.assertEqual(self.store.call(state, "a")["status"], "pending")
        state = self.run_turn(Model([{"content": "done"}]), accept_config_changes=True)
        self.assertEqual(state.status, RunStatus.COMPLETED)

    def test_invalid_policy_rejected(self):
        for value in ({}, {"default": "allow", "tools": []}, {"default": [], "tools": {}},
                      {"default": "allow", "tools": {"effect": []}}, {"default": "allow", "tools": {}, "extra": 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ToolPolicy.from_dict(value)

    def test_approval_and_dispatch_across_processes(self):
        self.pause()
        state = self.store.load("demo")
        digest = state.metadata["approval_request"]["request_digest"]
        decision = subprocess.run([sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo",
                                   "--approve-tool", "a", "--approval-digest", digest], capture_output=True)
        self.assertEqual(decision.returncode, 0, decision.stderr)
        script = '''
from pathlib import Path
import sys
from baseagent.agent import run_agent
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.policy import ToolPolicy
root = Path(sys.argv[1])
tools = ToolRegistry()
def effect(**args):
    with (root / "marker").open("a", encoding="utf-8") as handle:
        handle.write("executed\\n")
    return "done"
tools.register("effect", "effect", {"type":"object", "properties":{"value":{"type":"integer"}}}, effect)
class NoModel:
    def complete(self, messages, tools):
        raise AssertionError("model must not be called")
state = run_agent(NoModel(), tools=tools, store=SessionStore(root / "sessions.sqlite3"), session_id="demo",
                  tool_policy=ToolPolicy({"effect":"ask"}), max_steps=1)
assert state.status == "max_steps_exceeded", state.error
assert state.tool_calls == 1
'''
        for _ in range(2):
            result = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "marker").read_text(), "executed\n")


if __name__ == "__main__":
    unittest.main()
