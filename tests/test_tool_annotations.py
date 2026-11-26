"""Tool annotations (host-side metadata), annotation-level policy and the critical-path guard."""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from baseagent.agent import run_agent
from baseagent.agent.gateway import GatewayPolicy
from baseagent.agent.state import RunStatus, State
from baseagent.agent.subtasks import IntersectionPolicy
from baseagent.kernel.tools import RETRY_SAFE_ANNOTATIONS, ToolAnnotations, mcp_annotation_hints
from baseagent.mcp import McpManager, ServerConfig
from baseagent.middleware.harnessknight import infer_impacts
from baseagent.tools.annotations import COMMAND, DELEGATION, OVERWRITE, READ, SESSION_STATE
from baseagent.tools.critical_paths import critical_path_violation
from baseagent.tools.policy import ToolPolicy, call_annotations, request_digest
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace, coding_tools


def schema():
    return {"type": "object", "properties": {}}


def forge_approval(state, policy, name, arguments, call_id="a", decision="allow"):
    state.metadata.setdefault("tool_approvals", {})[call_id] = {
        "call_id": call_id, "turn_id": state.turn_id, "name": name, "policy": policy.fingerprint,
        "request_digest": request_digest(name, arguments), "decision": decision}


class Model:
    def __init__(self, messages):
        self.messages = iter(messages)

    def complete(self, messages, tools):
        return next(self.messages)


def call(name, args, call_id="a"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def r(read_only, destructive, idempotent, open_world):
    return {"read_only": read_only, "destructive": destructive, "idempotent": idempotent, "open_world": open_world}


RO = r(True, False, True, False)
EXPECTED = {
    "get_instructions": RO, "read_file": RO, "search_files": RO, "grep": RO, "glob": RO,
    "get_task_state": RO, "git_diff": RO, "git_status": RO, "recall_memory": RO, "read_history": RO,
    "search_shared_memory": RO, "job_list": RO, "job_output": RO,
    "write_file": r(False, True, True, False), "edit_file": r(False, True, True, False),
    "apply_patch": r(False, True, True, False), "publish_memory": r(False, True, True, False),
    "withdraw_memory": r(False, True, True, False),
    "run_command": r(False, True, False, True), "verify_command": r(False, True, False, True),
    "start_job": r(False, True, False, True), "job_kill": r(False, True, True, False),
    # They write the job's link (confirmation, settlement), so they are not read-only and not retried.
    "job_wait": r(False, False, False, False), "job_settle": r(False, False, False, False),
    "update_plan": r(False, False, True, False), "remember": r(False, False, True, False),
    "forget_memory": r(False, False, True, False), "set_history_summary": r(False, False, True, False),
}


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()


class RecordTests(unittest.TestCase):
    def test_defaults_are_the_conservative_mcp_defaults(self):
        self.assertEqual(ToolAnnotations().to_dict(), r(False, True, False, True))
        registry = ToolRegistry()
        registry.register("plain", "d", schema(), lambda: 1)
        registry.register("safe", "d", schema(), lambda: 1, retry_safe=True)
        self.assertEqual(registry.annotations("plain"), ToolAnnotations())
        self.assertEqual(registry.annotations("safe"), RETRY_SAFE_ANNOTATIONS)
        self.assertEqual(RETRY_SAFE_ANNOTATIONS.to_dict(), r(True, False, True, True))
        self.assertEqual(registry.annotations("unknown"), ToolAnnotations())

    def test_record_is_immutable_and_validated(self):
        with self.assertRaises(Exception):
            READ.read_only = False
        with self.assertRaises(ValueError):
            ToolAnnotations(read_only=True)  # read-only but (default) destructive
        with self.assertRaises(ValueError):
            ToolAnnotations(destructive=1)

    def test_retry_safe_implies_read_only_and_idempotent(self):
        registry = ToolRegistry()
        for annotations in (COMMAND, SESSION_STATE, OVERWRITE):
            with self.assertRaises(ValueError):
                registry.register("x", "d", schema(), lambda: 1, retry_safe=True, annotations=annotations)
        self.assertNotIn("x", registry._tools)
        with self.assertRaises(ValueError):
            registry.register("x", "d", schema(), lambda: 1, annotations={"read_only": True})
        with self.assertRaises(ValueError):
            registry.register("x", "d", schema(), lambda: 1, annotation_hints={"readOnlyHint": True})
        registry.register("x", "d", schema(), lambda: 1, retry_safe=True, annotations=READ)
        self.assertTrue(registry.retry_safe("x"))

    def test_subset_keeps_annotations_and_hints(self):
        registry = ToolRegistry()
        registry.register("a", "d", schema(), lambda: 1, annotations=COMMAND, annotation_hints={"read_only": True})
        registry.register("b", "d", schema(), lambda: 1)
        child = registry.subset(["a"])
        self.assertEqual(child.annotations("a"), COMMAND)
        self.assertEqual(child.annotation_hints("a"), {"read_only": True})
        self.assertEqual(child.annotation_table(), {"a": COMMAND.to_dict()})


class BuiltinTableTests(Temp):
    def tools(self):
        workspace = Workspace(self.root, allow_write=True, allow_command=True, background_jobs=True)
        return coding_tools(workspace)

    def test_every_builtin_tool_is_annotated_as_documented(self):
        self.assertEqual(self.tools().annotation_table(), EXPECTED)

    def test_retry_safe_tools_are_exactly_the_read_only_ones(self):
        tools = self.tools()
        for name in tools._tools:
            self.assertEqual(tools.retry_safe(name), tools.annotations(name).read_only, name)

    def test_other_builtin_packages(self):
        from baseagent.capabilities.skills import SkillCatalog
        from baseagent.agent.spill import register_read_spill
        tools = ToolRegistry()
        register_read_spill(tools, SimpleNamespace(read=None))
        SkillCatalog().install(tools)
        for name in ("read_spill", "list_skills", "load_skill", "read_skill_resource"):
            self.assertEqual(tools.annotations(name), READ, name)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        self.addCleanup(sys.path.remove, str(Path(__file__).resolve().parents[1]))
        from applications.project_assistant.git_history import git_history_package
        package = git_history_package(self.root)
        package.install(tools)
        for name in ("git_log", "git_show", "git_pickaxe"):
            self.assertEqual(tools.annotations(name), READ, name)
        self.assertEqual(DELEGATION.to_dict(), r(False, False, False, False))

    def test_annotations_are_not_sent_to_the_model_nor_in_the_contract(self):
        tools = self.tools()
        plain = ToolRegistry()
        plain.capability_contract = tools.capability_contract
        for name, (spec, handler, _) in tools._tools.items():
            function = spec["function"]
            plain.register(name, function["description"], function["parameters"], handler,
                           retry_safe=tools.retry_safe(name), contextual=name in tools._contextual)
        self.assertEqual(tools.specs(), plain.specs())
        self.assertEqual(json.dumps(tools.contract(), sort_keys=True), json.dumps(plain.contract(), sort_keys=True))
        self.assertNotIn("annotations", json.dumps(tools.specs()))
        self.assertNotIn("read_only", json.dumps(tools.contract()))

    def test_harnessknight_name_groups_agree_with_annotations(self):
        # infer_impacts keeps its name table (impacts depend on argument shapes); this pins it to the annotations.
        tools = self.tools()
        for name in ("read_file", "grep", "glob", "git_status", "git_diff"):
            self.assertTrue(tools.annotations(name).read_only, name)
            self.assertTrue(all(i["operation"] == "read" for i in infer_impacts(name, {"path": "x", "pattern": "y"})))
        for name in ("write_file", "edit_file", "apply_patch"):
            self.assertTrue(tools.annotations(name).destructive, name)
        for name in ("run_command", "verify_command", "start_job"):
            flags = tools.annotations(name)
            self.assertTrue(flags.destructive and flags.open_world, name)
            self.assertEqual(infer_impacts(name, {"argv": ["ls"]})[0]["operation"], "execute")


class FakeClient:
    def call_tool(self, *args, **kwargs):
        raise AssertionError("not called")


class McpTrustTests(unittest.TestCase):
    def register(self, listed, read_only=(), allow_write=True):
        lines = []
        manager = McpManager([ServerConfig("srv", sys.executable, (), read_only_tools=frozenset(read_only))],
                             warn=lines.append)
        manager.clients = {"srv": FakeClient()}
        manager.tools = {"srv": listed}
        registry = ToolRegistry()
        manager.register(registry, allow_write=allow_write)
        return registry, lines

    def test_hints_are_parsed_and_only_booleans_are_kept(self):
        self.assertEqual(mcp_annotation_hints({"readOnlyHint": True, "openWorldHint": "no", "title": "t"}),
                         {"read_only": True})
        self.assertEqual(mcp_annotation_hints(None), {})

    def test_server_claiming_read_only_gains_nothing(self):
        tool = {"name": "wipe", "inputSchema": schema(),
                "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}}
        registry, _ = self.register([tool])
        name = "mcp__srv__wipe"
        self.assertEqual(registry.annotations(name), ToolAnnotations())
        self.assertFalse(registry.retry_safe(name))
        self.assertEqual(registry.annotation_hints(name), {"read_only": True, "destructive": False, "open_world": False})
        # Still hidden in read-only mode and still asks by default at the gateway.
        hidden, _ = self.register([tool], allow_write=False)
        self.assertNotIn(name, hidden._tools)
        gateway = GatewayPolicy(ToolPolicy(), registry)
        self.assertEqual(gateway.action(State(turn_id="t"), "a", name, "{}"), "ask")
        # An annotation floor still applies to it.
        policy = GatewayPolicy(ToolPolicy(annotations={"destructive": "deny"}), registry)
        self.assertEqual(policy.action(State(turn_id="t"), "a", name, "{}"), "deny")

    def test_configured_read_only_comes_from_configuration(self):
        tool = {"name": "echo", "inputSchema": schema(), "annotations": {"destructiveHint": True}}
        registry, lines = self.register([tool], read_only=("echo",))
        self.assertEqual(registry.annotations("mcp__srv__echo"), RETRY_SAFE_ANNOTATIONS)
        self.assertTrue(any("configured readOnly" in line for line in lines))

    def test_annotations_do_not_change_the_registry_contract(self):
        bare = {"name": "echo", "inputSchema": schema()}
        hinted = {**bare, "annotations": {"readOnlyHint": True, "idempotentHint": True}}
        self.assertEqual(self.register([bare])[0].contract(), self.register([hinted])[0].contract())


def args(**values):
    return json.dumps(values)


class AnnotationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.state = State(turn_id="t")

    def test_contract_is_unchanged_without_annotations(self):
        for value in ({"default": "ask", "tools": {"read_file": "allow"}}, {"default": "allow", "tools": {}}):
            policy = ToolPolicy.from_dict(value)
            self.assertEqual(policy.contract(), value)
            self.assertEqual(policy.fingerprint, ToolPolicy(value["tools"], default=value["default"]).fingerprint)
        # Fixed digest of the default policy, as before this change.
        import hashlib
        self.assertEqual(ToolPolicy().fingerprint, hashlib.sha256(
            json.dumps({"default": "allow", "tools": {}}, sort_keys=True).encode()).hexdigest())

    def test_annotations_section_is_validated_and_part_of_the_contract(self):
        for bad in ({}, {"destructive": "allow"}, {"writes": "ask"}, {"destructive": True}, []):
            with self.assertRaises(ValueError):
                ToolPolicy.from_dict({"default": "allow", "tools": {}, "annotations": bad})
        policy = ToolPolicy.from_dict({"default": "allow", "tools": {}, "annotations": {"open_world": "ask", "destructive": "deny"}})
        self.assertEqual(policy.contract()["annotations"], {"destructive": "deny", "open_world": "ask"})
        self.assertNotEqual(policy.fingerprint, ToolPolicy().fingerprint)
        # Round trip, as subtask definitions do.
        self.assertEqual(ToolPolicy.from_json(json.dumps(policy.contract())).contract(), policy.contract())

    def test_unbound_call_uses_conservative_defaults(self):
        policy = ToolPolicy(annotations={"destructive": "ask"})
        self.assertEqual(policy.action(self.state, "a", "anything", "{}"), "ask")
        with call_annotations("anything", READ):
            self.assertEqual(policy.action(self.state, "a", "anything", "{}"), "allow")
        # The record must belong to the call's tool.
        with call_annotations("other", READ):
            self.assertEqual(policy.action(self.state, "a", "anything", "{}"), "ask")

    def test_precedence(self):
        run = args(argv=["git", "status"])
        cases = [
            # (tools, annotations, expected for run_command git status)
            ({}, {"destructive": "ask"}, "ask"),
            ({"run_command": "allow"}, {"destructive": "ask"}, "allow"),
            ({"run_command": "allow"}, {"destructive": "deny"}, "deny"),
            ({"run_command": {"default": "allow", "rules": [{"action": "deny", "match": {"argv_prefix": ["rm"]}}]}},
             {"destructive": "ask"}, "ask"),  # a conditional default is not explicit
            ({"run_command": {"default": "deny", "rules": [{"action": "allow", "match": {"argv_prefix": ["git", "status"]}}]}},
             {"open_world": "ask"}, "allow"),  # a matched rule is explicit
            ({"run_command": {"default": "deny", "rules": [{"action": "allow", "match": {"argv_prefix": ["git", "status"]}}]}},
             {"open_world": "deny"}, "deny"),
            ({"run_command": "deny"}, {"destructive": "ask"}, "deny"),
            ({}, {"read_only": "ask"}, "allow"),
        ]
        for tools, annotations, expected in cases:
            with self.subTest(tools=tools, annotations=annotations), call_annotations("run_command", COMMAND):
                policy = ToolPolicy(tools, annotations=annotations)
                self.assertEqual(policy.action(self.state, "a", "run_command", run), expected)

    def test_annotation_ask_is_approvable_and_deny_is_not(self):
        policy = ToolPolicy(annotations={"open_world": "ask", "destructive": "deny"})
        with call_annotations("x", ToolAnnotations(destructive=False)):
            self.assertEqual(policy.action(self.state, "a", "x", "{}"), "ask")
            forge_approval(self.state, policy, "x", "{}")
            self.assertEqual(policy.action(self.state, "a", "x", "{}"), "allow")
        with call_annotations("x", ToolAnnotations()):
            self.assertEqual(policy.action(self.state, "a", "x", "{}"), "deny")

    def test_gateway_supplies_annotations_through_wrappers(self):
        with tempfile.TemporaryDirectory() as temp:
            tools = coding_tools(Workspace(Path(temp), allow_write=True, allow_command=True))
            policy = ToolPolicy(annotations={"destructive": "ask"})
            for wrapped in (policy, IntersectionPolicy(ToolPolicy(), policy)):
                gateway = GatewayPolicy(wrapped, tools)
                self.assertEqual(gateway.action(self.state, "a", "read_file", args(path="x")), "allow")
                self.assertEqual(gateway.action(self.state, "a", "update_plan", "{}"), "allow")
                self.assertEqual(gateway.action(self.state, "a", "write_file", "{}"), "ask")
                self.assertEqual(gateway.action(self.state, "a", "run_command", args(argv=["ls"])), "ask")
            self.assertEqual(GatewayPolicy(policy, tools).fingerprint, policy.fingerprint)

    def test_subtask_child_policy_sees_annotations(self):
        # Subtask children run under IntersectionPolicy(parent gateway, child policy) without their own gateway.
        with tempfile.TemporaryDirectory() as temp:
            tools = coding_tools(Workspace(Path(temp), allow_write=True, allow_command=True))
            child = IntersectionPolicy(GatewayPolicy(ToolPolicy(), tools), ToolPolicy(annotations={"destructive": "ask"}))
            self.assertEqual(child.action(self.state, "a", "read_file", args(path="x")), "allow")
            self.assertEqual(child.action(self.state, "a", "write_file", "{}"), "ask")
            dangerous = args(argv=["rm", "-rf", "."])
            self.assertEqual(child.action(self.state, "a", "run_command", dangerous), "deny")
            self.assertIn("critical-path guard", child.denial_message("run_command", dangerous))


class CriticalPathGuardTests(Temp):
    def check(self, argv, cwd=None):
        return critical_path_violation(argv, root=str(self.root), cwd=cwd, home=str(self.root.parent / "home"))

    def test_caught(self):
        home = str(self.root.parent / "home")
        anchor = os.path.abspath(os.sep)
        for argv in (["rm", "-rf", "."], ["rm", "-r", "./"], ["rm", "-fr", ".git"], ["rm", "-R", "sub/.baseagent"],
                     ["rm", "--recursive", "--force", ".."], ["rm", "-rf", anchor], ["rm", "-rf", "~"],
                     ["rm", "-rf", home], ["rm", "-rf", "*"], ["rm", "-rf", "--", "."], ["/bin/rm", "-rf", "."],
                     ["sudo", "rm", "-rf", "."], ["env", "A=1", "rm", "-rf", ".git"],
                     ["bash", "-c", "make && rm -rf ."], ["sh", "-c", "rm -rf \"$HOME\""],
                     ["cmd", "/c", "rmdir", "/s", "/q", "."], ["cmd", "/c", "rd /S /Q .git"], ["del", "/s", "/q", "."],
                     ["powershell", "-Command", "Remove-Item -Recurse -Force ."],
                     ["pwsh", "-c", "Remove-Item -Path .git -Recurse"], ["Remove-Item", "-LiteralPath", "a,.", "-Recurse"],
                     ["git", "clean", "-fdx"], ["git", "clean", "-f", "-d", "-X", "."], ["git", "-C", ".", "clean", "-ffdx"],
                     ["find", ".", "-delete"], [str(self.root / "rm.exe"), "-rf", str(self.root)]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(self.check(argv))
        self.assertIsNotNone(self.check(["rm", "-rf", ".."], cwd="sub"))

    def test_not_caught(self):
        for argv in (["rm", "-rf", "build"], ["rm", ".git"], ["rm", "-f", "."], ["rmdir", ".git"],
                     ["rm", "-rf", "sub/.github"], ["rmdir", "/s", "/q", "build"], ["del", "/q", "."],
                     ["powershell", "-Command", "Remove-Item -Force ."], ["git", "clean", "-fd"],
                     ["git", "clean", "-fdxn"], ["git", "clean", "-fdx", "build"], ["git", "status"],
                     ["find", ".", "-name", "x"], ["git", "commit", "-m", "remove the build dir: rm -rf build"],
                     ["rm", "-rf", "$TARGET"], ["rm", "-rf", "~/project/tmp"], ["ls", "-la"]):
            with self.subTest(argv=argv):
                self.assertIsNone(self.check(argv))
        self.assertIsNone(critical_path_violation("rm -rf /", root=str(self.root)))
        self.assertIsNone(critical_path_violation(["rm", 1], root=str(self.root)))

    def test_gateway_denies_regardless_of_policy_and_approval(self):
        tools = coding_tools(Workspace(self.root, allow_write=True, allow_command=True, background_jobs=True))
        state = State(turn_id="t")
        dangerous = args(argv=["rm", "-rf", ".git"])
        for parent in (ToolPolicy(), ToolPolicy({"run_command": "allow"}), ToolPolicy(default="ask"),
                       ToolPolicy({"run_command": {"default": "allow", "rules": [
                           {"action": "allow", "match": {"argv_prefix": ["rm"]}}]}})):
            gateway = GatewayPolicy(parent, tools)
            for name in ("run_command", "verify_command"):
                state.metadata.pop("tool_approvals", None)
                self.assertEqual(gateway.action(state, "a", name, dangerous), "deny")
                forge_approval(state, gateway, name, dangerous)
                self.assertEqual(gateway.action(state, "a", name, dangerous), "deny")
            self.assertEqual(gateway.action(state, "a", "start_job", args(argv=["rm", "-rf", ".."], cwd="sub")), "deny")
            self.assertIn("critical-path guard", gateway.denial_message("run_command", dangerous))
            self.assertEqual(gateway.denial_message("run_command", args(argv=["ls"])), "tool request denied by policy")
        # Generic by argument name: any tool with an argv list.
        other = GatewayPolicy(ToolPolicy(), SimpleNamespace(_mcp_servers={}, retry_safe=lambda name: False),
                              root=str(self.root))
        self.assertEqual(other.action(state, "a", "mcp__x__exec", dangerous), "deny")
        self.assertEqual(other.action(state, "a", "mcp__x__exec", args(argv=["ls"])), "allow")
        # The guard adds nothing to contracts.
        self.assertEqual(GatewayPolicy(ToolPolicy(), tools).contract(), ToolPolicy().contract())

    def test_full_mode_run_is_refused_before_the_handler(self):
        (self.root / ".git").mkdir()
        (self.root / ".git" / "keep").write_text("x", encoding="utf-8")
        marker = self.root / "ran"
        command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')  # then rm -rf ."]
        tools = coding_tools(Workspace(self.root, allow_write=True, allow_command=True))
        state = run_agent(Model([{"tool_calls": [call("run_command", {"argv": ["rm", "-rf", ".git"]}, "a"),
                                                 call("run_command", {"argv": command}, "b")]},
                                 {"content": "done"}]), "task", tools=tools)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertTrue((self.root / ".git" / "keep").exists())
        self.assertFalse(marker.exists())
        results = [json.loads(m["content"]) for m in state.messages if m.get("role") == "tool"]
        self.assertEqual([item["error"]["code"] for item in results], ["permission_denied"] * 2)
        self.assertIn("critical-path guard", results[0]["error"]["message"])


if __name__ == "__main__":
    unittest.main()
