from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from baseagent.agent import run_agent
from baseagent.agent.state import State, RunStatus
from baseagent.middleware import AgentMiddleware
from baseagent.session import SessionStore
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace, coding_tools


class Model:
    def __init__(self, messages):
        self.messages = iter(messages)

    def complete(self, messages, tools):
        return next(self.messages)


def call(name, args):
    return {"id": "a", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def conditional(match, action="allow", default="deny"):
    return {"default": default, "rules": [{"action": action, "match": match}]}


class ScopedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "file.py").write_text("answer = 5\n")
        self.workspace = Workspace(self.root, allow_write=True, allow_command=True)
        self.state = State(turn_id="turn")

    def action(self, rule, arguments, name="effect"):
        policy = ToolPolicy({name: rule}, workspace=self.workspace)
        return policy.action(self.state, "a", name, arguments if isinstance(arguments, str) else json.dumps(arguments))

    def test_directory_boundaries_and_path_aliases(self):
        rule = conditional({"path_within": {"path": ["src"]}})
        for value in ["src/file.py", "src/new.py", "src/nested/new.py"]:
            self.assertEqual(self.action(rule, {"path": value}), "allow")
        for value in ["src-other/file.py", "src/../outside.py", "../escape", str(self.root / "src/file.py"), "src/file.py:stream", "C:src/file.py", "", 1, None]:
            self.assertEqual(self.action(rule, {"path": value}), "deny")

    def test_missing_parameters_and_protected_paths_denied(self):
        rule = conditional({"path_within": {"path": ["."]}})
        for args in [{}, {"path": ".env"}, {"path": ".git/config"}, {"path": ".baseagent/sessions.sqlite3"}]:
            self.assertEqual(self.action(rule, args), "deny")

    def test_actual_directory_link_is_rejected(self):
        target = self.root / "other"
        target.mkdir()
        (target / "file.py").write_text("outside scope")
        link = self.root / "src" / "alias"
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            link.symlink_to(target, target_is_directory=True)
        rule = conditional({"path_within": {"path": ["src"]}})
        self.assertEqual(self.action(rule, {"path": "src/alias/file.py"}), "deny")
        with self.assertRaises(ValueError):
            ToolPolicy({"effect": conditional({"path_within": {"path": ["src/alias"]}})}, workspace=self.workspace)

    def test_exact_command_rejects_extra_arguments(self):
        command = [sys.executable, "-B", "-m", "unittest"]
        rule = conditional({"argv_exact": command})
        self.assertEqual(self.action(rule, {"argv": command}), "allow")
        self.assertEqual(self.action(rule, {"argv": command + ["extra"]}), "deny")
        self.assertEqual(self.action(rule, {"argv": [sys.executable, "-c", "print('not the test')"]}), "deny")

    def test_all_match_conditions_and_json_types(self):
        rule = conditional({"equals": {"timeout": 20}, "path_within": {"path": ["src"]}})
        self.assertEqual(self.action(rule, {"timeout": 20, "path": "src/file.py"}), "allow")
        self.assertEqual(self.action(rule, {"timeout": 20, "path": "other/file.py"}), "deny")
        self.assertEqual(self.action(conditional({"equals": {"value": 1}}), {"value": True}), "deny")
        self.assertEqual(self.action(conditional({"equals": {"value": {"a": 1, "b": 2}}}), {"value": {"b": 2, "a": 1}}), "allow")

    def test_first_match_has_priority(self):
        rule = {"default": "deny", "rules": [{"action": "deny", "match": {"equals": {"path": "src/locked.py"}}},
                                            {"action": "allow", "match": {"path_within": {"path": ["src"]}}}]}
        self.assertEqual(self.action(rule, {"path": "src/locked.py"}), "deny")
        self.assertEqual(self.action(rule, {"path": "src/other.py"}), "allow")

    def test_malformed_arguments_fail_closed_even_with_allow_fallback(self):
        rule = conditional({"equals": {"value": 1}}, default="allow")
        for value in ['{"value":1,"value":2}', '{"value": NaN}', '{"value":1e999}', '[]', 'bad', '{"value":"' + 'a' * 256000 + '"}']:
            self.assertEqual(self.action(rule, value), "deny")

    def test_configuration_validation(self):
        for match in [{}, {"unknown": 1}, {"argv_exact": []}, {"equals": {}}, {"equals": {"v": float("nan")}},
                      {"path_within": {"path": ["../outside"]}}, {"path_within": {"path": [".baseagent"]}}]:
            with self.subTest(match=match), self.assertRaises(ValueError):
                ToolPolicy({"effect": conditional(match)}, workspace=self.workspace)
        with self.assertRaises(ValueError):
            ToolPolicy({"effect": conditional({"path_within": {"path": ["src"]}})})
        with self.assertRaises(ValueError):
            ToolPolicy.from_json('{"default":"deny","default":"allow","tools":{}}')

    def test_input_configuration_is_copied(self):
        rule = conditional({"equals": {"value": 1}})
        policy = ToolPolicy({"effect": rule})
        rule["rules"][0]["action"] = "allow"
        rule["default"] = "allow"
        self.assertEqual(policy.action(self.state, "a", "effect", '{"value": 2}'), "deny")

    def test_actual_redirect_cannot_escape_argument_scope(self):
        effects = []
        tools = ToolRegistry()
        tools.register("effect", "effect", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, lambda path: effects.append(path))
        policy = ToolPolicy({"effect": conditional({"path_within": {"path": ["src"]}})}, workspace=self.workspace)
        class Redirect(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                return handler(replace(request, arguments='{"path":"outside.py"}'))
        state = run_agent(Model([{"tool_calls": [call("effect", {"path": "src/file.py"})]}, {"content": "done"}]), "task", tools=tools, tool_policy=policy, middleware=[Redirect()])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(effects, [])
        self.assertEqual(state.tool_calls, 0)

    def test_scoped_approval_and_rule_change_invalidate_old_decision(self):
        store = SessionStore(self.root / ".baseagent" / "sessions.sqlite3")
        tools = coding_tools(self.workspace)
        policy = ToolPolicy({"read_file": conditional({"path_within": {"path": ["src"]}}, action="ask")}, workspace=self.workspace)
        state = run_agent(Model([{"tool_calls": [call("read_file", {"path": "src/file.py"})]}]), "task", tools=tools, tool_policy=policy, store=store, session_id="demo")
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        store.decide_tool("demo", "a", "allow", state.metadata["approval_request"]["request_digest"])
        changed = ToolPolicy({"read_file": conditional({"path_within": {"path": ["src"]}, "equals": {"path": "src/file.py"}}, action="ask")}, workspace=self.workspace)
        state = run_agent(Model([]), tools=tools, tool_policy=changed, store=store, session_id="demo", accept_config_changes=True)
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        self.assertEqual(state.tool_calls, 0)

    def test_cli_scoped_policy_denies_pending_read_without_model(self):
        store = SessionStore(self.root / ".baseagent" / "sessions.sqlite3")
        tools = coding_tools(self.workspace)
        state = run_agent(Model([{"tool_calls": [call("read_file", {"path": "src/file.py"})]}]), "task", tools=tools, store=store, session_id="demo", workspace_root=self.root, max_tool_calls=0)
        path = self.root / ".baseagent" / "policy.json"
        path.write_text(json.dumps({"default": "deny", "tools": {"read_file": conditional({"path_within": {"path": ["other"]}})}}))
        result = subprocess.run([sys.executable, "-m", "baseagent", "--db", str(store.path), "--session", "demo", "--resume", "--tool-policy", str(path),
                                 "--accept-config-changes", "--max-tool-calls", "1", "--max-steps", "1"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        state = store.load("demo")
        self.assertEqual(state.status, RunStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(state.tool_calls, 0)
        self.assertEqual(json.loads(state.messages[-1]["content"])["error"]["code"], "permission_denied")


if __name__ == "__main__":
    unittest.main()
