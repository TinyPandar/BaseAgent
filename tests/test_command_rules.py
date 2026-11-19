"""Command-level tool policy rules (argv_prefix, strictest precedence) and deny absoluteness."""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import warnings

from baseagent.agent import run_agent
from baseagent.agent.gateway import RESTRICTIONS, GatewayPolicy
from baseagent.agent.state import RunStatus, State
from baseagent.agent.subtasks import IntersectionPolicy
from baseagent.middleware.harnessknight import HarnessKnightClient, HarnessKnightToolPolicy
from baseagent.session import SessionStore
from baseagent.tools.edit_policy import EditPolicy, EditToolPolicy, bind_edit_policy
from baseagent.tools.policy import CommandRuleWarning, ToolPolicy, command_wrapper, request_digest
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace, coding_tools


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "tool-policy.commands.json"


class Model:
    def __init__(self, messages):
        self.messages = iter(messages)

    def complete(self, messages, tools):
        return next(self.messages)


def call(name, args, call_id="a"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def argv(*parts, timeout=20):
    return json.dumps({"argv": list(parts), "timeout": timeout})


def forge_approval(state, policy, name, arguments, call_id="a", decision="allow"):
    """An approval record that is valid in every field the policy checks."""
    state.metadata.setdefault("tool_approvals", {})[call_id] = {
        "call_id": call_id, "turn_id": state.turn_id, "name": name, "policy": policy.fingerprint,
        "request_digest": request_digest(name, arguments), "decision": decision}


def commands(rules, default="deny", precedence=None):
    rule = {"default": default, "rules": [{"action": action, "match": match} for action, match in rules]}
    if precedence is not None:
        rule["precedence"] = precedence
    return rule


GIT_RULES = [("allow", {"argv_prefix": ["git", "status"]}), ("allow", {"argv_prefix": ["git", "diff"]}),
             ("ask", {"argv_prefix": ["git", "push"]}), ("deny", {"argv_prefix": ["rm"]})]


class ArgvPrefixMatchingTests(unittest.TestCase):
    def setUp(self):
        self.state = State(turn_id="turn")

    def action(self, rule, arguments, name="run_command"):
        return ToolPolicy({name: rule}).action(self.state, "a", name, arguments)

    def test_prefix_matches_leading_elements_only(self):
        rule = commands(GIT_RULES, default="deny")
        self.assertEqual(self.action(rule, argv("git", "status")), "allow")
        self.assertEqual(self.action(rule, argv("git", "status", "--porcelain")), "allow")
        self.assertEqual(self.action(rule, argv("git", "diff", "--", ".")), "allow")
        self.assertEqual(self.action(rule, argv("git", "push", "origin", "main")), "ask")
        self.assertEqual(self.action(rule, argv("rm", "-rf", "build")), "deny")
        self.assertEqual(self.action(rule, argv("git")), "deny")             # shorter than prefix
        self.assertEqual(self.action(rule, argv("git", "statusx")), "deny")  # whole elements, not string prefix
        self.assertEqual(self.action(rule, argv("git", "-C", ".", "status")), "deny")
        self.assertEqual(self.action(rule, argv("Git", "status")), "deny")   # case-sensitive
        self.assertEqual(self.action(rule, argv("/bin/rm", "x")), "deny")    # by default, not by rule

    def test_prefix_ignores_non_list_or_non_string_argv(self):
        rule = commands([("allow", {"argv_prefix": ["git"]})], default="ask")
        for value in [{"argv": "git status"}, {"argv": None}, {}, {"argv": [1, "status"]}, {"argv": {"0": "git"}}]:
            with self.subTest(value=value):
                self.assertEqual(self.action(rule, json.dumps(value)), "ask")
        self.assertEqual(self.action(rule, json.dumps({"argv": ["git", 1]})), "allow")  # only the prefix is compared

    def test_applies_to_every_tool_with_argv(self):
        rule = commands(GIT_RULES)
        for name in ("run_command", "verify_command", "start_job"):
            with self.subTest(tool=name):
                self.assertEqual(self.action(rule, argv("git", "status"), name=name), "allow")
                self.assertEqual(self.action(rule, argv("rm", "x"), name=name), "deny")

    def test_combines_with_other_conditions(self):
        rule = commands([("allow", {"argv_prefix": ["git", "status"], "equals": {"timeout": 20}})])
        self.assertEqual(self.action(rule, argv("git", "status", timeout=20)), "allow")
        self.assertEqual(self.action(rule, argv("git", "status", timeout=30)), "deny")

    def test_malformed_arguments_fail_closed(self):
        rule = commands([("allow", {"argv_prefix": ["git"]})], default="allow")
        for value in ['{"argv":["git"],"argv":["rm"]}', "bad", "[]", '{"argv": NaN}']:
            with self.subTest(value=value):
                self.assertEqual(self.action(rule, value), "deny")


class PrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.state = State(turn_id="turn")

    def action(self, rule, arguments):
        return ToolPolicy({"run_command": rule}).action(self.state, "a", "run_command", arguments)

    def test_default_precedence_stays_first_match(self):
        rules = [("allow", {"argv_prefix": ["git"]}), ("deny", {"argv_prefix": ["git", "push"]})]
        self.assertEqual(self.action(commands(rules), argv("git", "push")), "allow")
        self.assertEqual(self.action(commands(rules, precedence="first_match"), argv("git", "push")), "allow")
        self.assertEqual(self.action(commands(list(reversed(rules))), argv("git", "push")), "deny")

    def test_strictest_is_order_independent(self):
        rules = [("allow", {"argv_prefix": ["git"]}), ("ask", {"argv_prefix": ["git", "push"]}),
                 ("deny", {"argv_prefix": ["git", "push", "--force"]})]
        for ordered in (rules, list(reversed(rules)), rules[1:] + rules[:1]):
            rule = commands(ordered, default="ask", precedence="strictest")
            with self.subTest(order=[action for action, _ in ordered]):
                self.assertEqual(self.action(rule, argv("git", "log")), "allow")
                self.assertEqual(self.action(rule, argv("git", "push", "origin")), "ask")
                self.assertEqual(self.action(rule, argv("git", "push", "--force", "origin")), "deny")
                self.assertEqual(self.action(rule, argv("ls")), "ask")  # nothing matched: tool default

    def test_strictest_with_other_match_kinds(self):
        rule = {"default": "deny", "precedence": "strictest", "rules": [
            {"action": "allow", "match": {"argv_prefix": ["python"]}},
            {"action": "deny", "match": {"argv_exact": ["python", "-c", "boom"]}}]}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CommandRuleWarning)
            self.assertEqual(self.action(rule, argv("python", "-c", "boom")), "deny")
            self.assertEqual(self.action(rule, argv("python", "-c", "ok")), "allow")

    def test_strictest_ask_honors_valid_approval_only(self):
        policy = ToolPolicy({"run_command": commands(GIT_RULES, precedence="strictest")})
        args = argv("git", "push")
        self.assertEqual(policy.action(self.state, "a", "run_command", args), "ask")
        forge_approval(self.state, policy, "run_command", args)
        self.assertEqual(policy.action(self.state, "a", "run_command", args), "allow")
        self.assertEqual(policy.action(self.state, "a", "run_command", argv("git", "push", "-f")), "ask")


class ValidationTests(unittest.TestCase):
    def test_invalid_prefixes_and_precedence_rejected(self):
        bad = [[], "git", ["git", ""], ["git", 1], [None], ["x" * 4001], ["a"] * 129]
        for prefix in bad:
            with self.subTest(prefix=prefix if len(str(prefix)) < 50 else "long"), self.assertRaises(ValueError):
                ToolPolicy({"run_command": commands([("deny", {"argv_prefix": prefix})])})
        ToolPolicy({"run_command": commands([("deny", {"argv_prefix": ["a"] * 128}), ("deny", {"argv_prefix": ["x" * 4000]})])})
        for precedence in ["", "STRICTEST", "last", 1, None, ["strictest"]]:
            with self.subTest(precedence=precedence), self.assertRaises(ValueError):
                ToolPolicy({"run_command": {"default": "deny", "rules": [], "precedence": precedence}})
        with self.assertRaises(ValueError):
            ToolPolicy({"run_command": {"default": "deny", "rules": [], "order": "strictest"}})
        with self.assertRaises(ValueError):
            ToolPolicy({"run_command": {"rules": [], "precedence": "strictest"}})
        with self.assertRaises(ValueError):
            ToolPolicy({"run_command": commands([("deny", {"argv_glob": ["rm*"]})])})

    def test_duplicate_keys_in_json_rejected(self):
        text = '{"default":"deny","tools":{"run_command":{"default":"deny","precedence":"strictest","precedence":"first_match","rules":[]}}}'
        with self.assertRaises(ValueError):
            ToolPolicy.from_json(text)

    def test_allow_prefix_on_wrapper_warns(self):
        for program in ["bash", "sh", "/bin/bash", "python", "python3.12", "C:\\Python\\python.exe", "env", "xargs",
                        "cmd", "CMD.EXE", "powershell", "pwsh", "node", "sudo", "find"]:
            with self.subTest(program=program), self.assertWarns(CommandRuleWarning):
                policy = ToolPolicy({"run_command": commands([("allow", {"argv_prefix": [program, "-c"]})])})
            self.assertEqual(len(policy.warnings), 1)
            self.assertIn("argv_prefix", policy.warnings[0])

    def test_no_warning_for_safe_or_restrictive_rules(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", CommandRuleWarning)
            for rule in [commands(GIT_RULES), commands([("deny", {"argv_prefix": ["bash"]}), ("ask", {"argv_prefix": ["python"]})]),
                         commands([("allow", {"argv_exact": ["python", "-c", "print(1)"]})])]:
                self.assertEqual(ToolPolicy({"run_command": rule}).warnings, [])
        self.assertIsNone(command_wrapper("git"))
        self.assertIsNone(command_wrapper("pytest"))
        self.assertEqual(command_wrapper("Python3.EXE"), "python")

    def test_example_file_loads_and_behaves(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", CommandRuleWarning)
            policy = ToolPolicy.from_json(EXAMPLE.read_text(encoding="utf-8"))
        state = State(turn_id="turn")
        for name in ("run_command", "verify_command"):
            self.assertEqual(policy.action(state, "a", name, argv("git", "status")), "allow")
            self.assertEqual(policy.action(state, "a", name, argv("git", "diff", "HEAD")), "allow")
            self.assertEqual(policy.action(state, "a", name, argv("git", "push")), "ask")
            self.assertEqual(policy.action(state, "a", name, argv("rm", "-rf", ".")), "deny")
            self.assertEqual(policy.action(state, "a", name, argv("make")), "ask")
        # Background jobs (--background-jobs) are listed explicitly: rules on run_command do not
        # carry over to start_job, so the example asks for every job and denies rm.
        start = lambda *parts: json.dumps({"argv": list(parts)})
        self.assertEqual(policy.action(state, "a", "start_job", start("rm", "-rf", ".")), "deny")
        self.assertEqual(policy.action(state, "a", "start_job", start("git", "status")), "ask")
        self.assertEqual(policy.action(state, "a", "start_job", start("python", "train.py")), "ask")


class CompatibilityTests(unittest.TestCase):
    LEGACY = {"default": "ask", "tools": {"read_file": "allow", "write_file": "deny",
              "run_command": {"default": "deny", "rules": [
                  {"action": "allow", "match": {"argv_exact": ["git", "status"]}},
                  {"action": "ask", "match": {"equals": {"timeout": 20}}}]}}}

    def test_contract_of_legacy_policies_is_byte_identical(self):
        # Values recorded with the implementation before argv_prefix/precedence existed.
        policy = ToolPolicy.from_dict(json.loads(json.dumps(self.LEGACY)))
        self.assertEqual(json.dumps(policy.contract()),
                         '{"default": "ask", "tools": {"read_file": "allow", "write_file": "deny", "run_command": '
                         '{"default": "deny", "rules": [{"action": "allow", "match": {"argv_exact": ["git", "status"]}}, '
                         '{"action": "ask", "match": {"equals": {"timeout": 20}}}]}}}')
        self.assertEqual(policy.fingerprint, "eb0c0aa8ed66cefad9afc825cd9cecaa6ab4821e170cc6a5c338fd3efdee390a")
        self.assertEqual(ToolPolicy().fingerprint, "b5aca4d68a2d556a296e6bc79fe1aeafa1ba91b0a9492a31bfdcc5f89b6437a4")

    def test_new_keys_change_the_fingerprint(self):
        legacy = ToolPolicy.from_dict(json.loads(json.dumps(self.LEGACY)))
        value = json.loads(json.dumps(self.LEGACY))
        value["tools"]["run_command"]["precedence"] = "strictest"
        self.assertNotEqual(ToolPolicy.from_dict(value).fingerprint, legacy.fingerprint)
        value = json.loads(json.dumps(self.LEGACY))
        value["tools"]["run_command"]["rules"][0]["match"] = {"argv_prefix": ["git", "status"]}
        self.assertNotEqual(ToolPolicy.from_dict(value).fingerprint, legacy.fingerprint)


class DenyIsAbsoluteTests(unittest.TestCase):
    """A policy deny survives every layer and every approval record."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = State(turn_id="turn")
        self.args = argv("rm", "-rf", "src")
        self.deny = ToolPolicy({"run_command": commands([("allow", {"argv_prefix": ["git"]}),
                                                         ("deny", {"argv_prefix": ["rm"]})], default="allow",
                                                        precedence="strictest")})

    def assert_denied_even_if_approved(self, policy, name="run_command", args=None):
        args = self.args if args is None else args
        self.assertEqual(policy.action(self.state, "a", name, args), "deny")
        forge_approval(self.state, policy, name, args)
        self.assertEqual(policy.action(self.state, "a", name, args), "deny")
        # An approval bound to the inner policy's fingerprint does not help either.
        inner = getattr(policy, "parent", None)
        if inner is not None:
            forge_approval(self.state, inner, name, args)
            self.assertEqual(policy.action(self.state, "a", name, args), "deny")

    def test_tool_policy_ignores_approval_for_denied_call(self):
        self.assert_denied_even_if_approved(self.deny)
        self.assert_denied_even_if_approved(ToolPolicy({"run_command": "deny"}))
        self.assert_denied_even_if_approved(ToolPolicy(default="deny"))

    def test_gateway_cannot_loosen_deny(self):
        self.assert_denied_even_if_approved(GatewayPolicy(self.deny, ToolRegistry()))
        mcp_tools = SimpleNamespace(_mcp_servers={"run_command": "srv"}, retry_safe=lambda name: False,
                                    capability_contract={"mcp": {"srv": "digest"}})
        gateway = GatewayPolicy(self.deny, mcp_tools)
        self.assertNotEqual(gateway.fingerprint, self.deny.fingerprint)
        self.assert_denied_even_if_approved(gateway)
        # A hook restriction can only tighten, never relax.
        self.state.metadata[RESTRICTIONS] = {"a": {"action": "allow", "request_digest": request_digest("run_command", self.args)}}
        self.assertEqual(gateway.action(self.state, "a", "run_command", self.args), "deny")

    def test_intersection_policy_parent_and_child(self):
        allow = ToolPolicy()
        for policy in (IntersectionPolicy(self.deny, allow), IntersectionPolicy(allow, self.deny),
                       IntersectionPolicy(IntersectionPolicy(self.deny, allow), ToolPolicy({"run_command": "ask"}))):
            with self.subTest(policy=policy.contract()):
                self.state.metadata.pop("tool_approvals", None)
                self.assertEqual(policy.action(self.state, "a", "run_command", self.args), "deny")
                forge_approval(self.state, policy, "run_command", self.args)
                self.assertEqual(policy.action(self.state, "a", "run_command", self.args), "deny")
                for inner in (policy.parent, policy.child):
                    forge_approval(self.state, inner, "run_command", self.args, call_id="a")
                    self.assertEqual(policy.action(self.state, "a", "run_command", self.args), "deny")

    def test_worker_child_policy_shape_keeps_parent_deny(self):
        # router.MainAgentWorkers/_child_policy and subtasks use IntersectionPolicy(bind_edit_policy(parent), child).
        workspace = Workspace(self.root, allow_write=True, allow_command=True)
        policy = IntersectionPolicy(bind_edit_policy(self.deny, coding_tools(workspace)), ToolPolicy())
        self.assertEqual(policy.action(self.state, "a", "run_command", self.args), "deny")
        forge_approval(self.state, policy, "run_command", self.args)
        self.assertEqual(policy.action(self.state, "a", "run_command", self.args), "deny")

    def test_edit_tool_policy_cannot_loosen_deny(self):
        workspace = Workspace(self.root, allow_write=True, allow_command=True, edit_policy=EditPolicy(max_changed_lines=1))
        parent = ToolPolicy({"write_file": "deny", "run_command": commands([("deny", {"argv_prefix": ["rm"]})], default="allow")})
        policy = bind_edit_policy(parent, coding_tools(workspace))
        self.assertIsInstance(policy, EditToolPolicy)
        write = json.dumps({"path": "a.txt", "content": "one\ntwo\nthree\n"})
        self.assert_denied_even_if_approved(policy, "write_file", write)
        self.state.metadata.pop("tool_approvals", None)
        self.assert_denied_even_if_approved(policy)

    def test_harnessknight_review_and_fail_open_cannot_loosen_deny(self):
        for decision in ("ALLOW", "REVIEW", None):
            for fail_mode in ("closed", "open"):
                with self.subTest(decision=decision, fail_mode=fail_mode):
                    self.state.metadata.pop("tool_approvals", None)
                    client = HarnessKnightClient(SimpleNamespace(), fail_mode=fail_mode)
                    if decision is not None:
                        client._tool_decisions[("a", request_digest("run_command", self.args))] = (decision, [])
                    self.assert_denied_even_if_approved(HarnessKnightToolPolicy(self.deny, client, "http://localhost"))

    def test_full_mode_run_agent_denies_and_never_executes(self):
        workspace = Workspace(self.root, allow_write=True, allow_command=True)  # what --mode full configures
        marker = self.root / "marker.txt"
        command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"]
        policy = ToolPolicy({"run_command": commands([("deny", {"argv_prefix": [sys.executable, "-c"]})], default="allow")})
        store = SessionStore(self.root / ".baseagent" / "sessions.sqlite3")
        state = run_agent(Model([{"tool_calls": [call("run_command", {"argv": command, "timeout": 20})]}, {"content": "done"}]),
                          "task", tools=coding_tools(workspace), tool_policy=policy, store=store, session_id="full")
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertFalse(marker.exists())
        result = json.loads(next(m for m in state.messages if m.get("role") == "tool")["content"])
        self.assertEqual(result["error"]["code"], "permission_denied")
        # No approval can be recorded for a call that was denied rather than paused.
        with self.assertRaises(ValueError):
            store.decide_tool("full", "a", "allow", request_digest("run_command", json.dumps({"argv": command, "timeout": 20})))

    def test_full_mode_ask_then_policy_tightened_to_deny_ignores_old_approval(self):
        workspace = Workspace(self.root, allow_write=True, allow_command=True)
        marker = self.root / "pushed.txt"
        command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"]
        store = SessionStore(self.root / ".baseagent" / "sessions.sqlite3")
        tools = coding_tools(workspace)
        ask = ToolPolicy({"run_command": commands([("ask", {"argv_prefix": [sys.executable]})], default="allow")})
        state = run_agent(Model([{"tool_calls": [call("run_command", {"argv": command, "timeout": 20})]}]), "task",
                          tools=tools, tool_policy=ask, store=store, session_id="demo")
        self.assertEqual(state.status, RunStatus.AWAITING_APPROVAL)
        store.decide_tool("demo", "a", "allow", state.metadata["approval_request"]["request_digest"])
        deny = ToolPolicy({"run_command": commands([("ask", {"argv_prefix": [sys.executable]}),
                                                    ("deny", {"argv_prefix": [sys.executable, "-c"]})],
                                                   default="allow", precedence="strictest")})
        state = run_agent(Model([{"content": "done"}]), tools=tools, tool_policy=deny, store=store, session_id="demo",
                          accept_config_changes=True)
        self.assertFalse(marker.exists())
        result = json.loads(next(m for m in state.messages if m.get("role") == "tool")["content"])
        self.assertEqual(result["error"]["code"], "permission_denied")


if __name__ == "__main__":
    unittest.main()
