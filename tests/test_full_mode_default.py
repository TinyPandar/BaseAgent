"""--mode full asks before destructive open-world tools by default; --auto-approve restores the old behaviour.

The floor is the annotation-level policy entry ``"destructive+open_world": "ask"`` (tools/policy.py
COMMAND_FLOOR), added only in --mode full without --auto-approve. Fake models only, no provider calls.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checkpoint_helpers import Script, tool_call, write_call

from baseagent.agent import run_agent
from baseagent.agent.gateway import GatewayPolicy
from baseagent.agent.state import State
from baseagent.main import FLOOR_RESUME_HINT, FULL_MODE_ASK_NOTE, main
from baseagent.mcp import McpManager, ServerConfig
from baseagent.session import SessionStore
from baseagent.tools.policy import COMMAND_FLOOR, ToolPolicy, call_annotations, request_digest, with_command_floor
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace, coding_tools

ROOT = Path(__file__).resolve().parents[1]
FLOORED = {"run_command", "verify_command", "start_job"}
PRINT = [sys.executable, "-c", "print(42)"]


def args(**values):
    return json.dumps(values)


class FakeClient:
    def call_tool(self, *args, **kwargs):
        raise AssertionError("not called")


def registry(root):
    """Built-in coding tools with background jobs, one MCP tool with side effects, one configured read-only
    MCP tool and a knowledge_search-style retry-safe tool (read-only, open world)."""
    tools = coding_tools(Workspace(root, allow_write=True, allow_command=True, background_jobs=True))
    manager = McpManager([ServerConfig("srv", sys.executable, (), read_only_tools=frozenset({"lookup"}))],
                         warn=lambda line: None)
    manager.clients = {"srv": FakeClient()}
    manager.tools = {"srv": [{"name": "wipe", "inputSchema": {"type": "object"}},
                             {"name": "lookup", "inputSchema": {"type": "object"}}]}
    manager.register(tools, allow_write=True)
    tools.register("knowledge_search", "d", {"type": "object", "properties": {}}, lambda: 1, retry_safe=True)
    return tools


def approve(state, policy, name, arguments, call_id="a"):
    state.metadata.setdefault("tool_approvals", {})[call_id] = {
        "call_id": call_id, "turn_id": state.turn_id, "name": name, "policy": policy.fingerprint,
        "request_digest": request_digest(name, arguments), "decision": "allow"}


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "work"
        self.root.mkdir()
        self.db = Path(self.temp.name).resolve() / "state" / "s.sqlite3"


class FloorPolicyTests(Temp):
    def test_floor_asks_exactly_for_destructive_open_world_tools(self):
        tools = registry(self.root)
        floored = GatewayPolicy(with_command_floor(ToolPolicy()), tools)
        plain = GatewayPolicy(ToolPolicy(), tools)
        state = State(turn_id="t")
        asked = {name for name in tools._tools if floored.action(state, "a", name, "{}") == "ask"}
        self.assertEqual(asked, FLOORED | {"mcp__srv__wipe"})
        # Without the floor only the gateway's MCP default asks: built-in tools run as before.
        self.assertEqual({name for name in tools._tools if plain.action(state, "a", name, "{}") == "ask"},
                         {"mcp__srv__wipe"})
        for name in ("write_file", "edit_file", "apply_patch", "job_kill", "update_plan", "remember",
                     "knowledge_search", "mcp__srv__lookup", "read_file"):
            self.assertEqual(floored.action(state, "a", name, "{}"), "allow", name)
        # The policy itself (not only the gateway's MCP default) asks for the side-effect MCP tool.
        with call_annotations("mcp__srv__wipe", tools.annotations("mcp__srv__wipe")):
            self.assertEqual(with_command_floor(ToolPolicy()).action(state, "a", "mcp__srv__wipe", "{}"), "ask")

    def test_floor_ask_is_approvable(self):
        tools = coding_tools(Workspace(self.root, allow_write=True, allow_command=True))
        gateway = GatewayPolicy(with_command_floor(ToolPolicy()), tools)
        state = State(turn_id="t")
        run = args(argv=["ls"])
        self.assertEqual(gateway.action(state, "a", "run_command", run), "ask")
        approve(state, gateway, "run_command", run)
        self.assertEqual(gateway.action(state, "a", "run_command", run), "allow")
        self.assertEqual(gateway.action(state, "a", "run_command", args(argv=["rm", "x"])), "ask")  # other arguments

    def test_combination_with_user_policies(self):
        tools = coding_tools(Workspace(self.root, allow_write=True, allow_command=True))
        state = State(turn_id="t")
        status, other = args(argv=["git", "status"]), args(argv=["ls"])
        prefix = {"default": "allow", "rules": [{"action": "allow", "match": {"argv_prefix": ["git", "status"]}}]}
        strict = {"default": "deny", "rules": [{"action": "allow", "match": {"argv_prefix": ["git", "status"]}}]}
        all_ask = {name: "ask" for name in ("write_file", "edit_file", "apply_patch", "run_command", "verify_command")}
        cases = [
            # (tools, annotations, run_command git status, run_command ls)
            ({"run_command": "allow"}, None, "allow", "allow"),       # explicit per-tool allow lifts the floor
            ({"run_command": prefix}, None, "allow", "ask"),          # a matching allow rule lifts it; the default does not
            ({"run_command": strict}, None, "allow", "deny"),
            ({"run_command": "deny"}, None, "deny", "deny"),          # deny stays absolute
            ({"run_command": "allow"}, {COMMAND_FLOOR: "deny"}, "deny", "deny"),  # a user deny floor is kept
            ({"run_command": "allow"}, {"open_world": "deny"}, "deny", "deny"),
            (all_ask, None, "ask", "ask"),                            # an all-ask policy (Chainlit UI) is unaffected
        ]
        for rules, annotations, expected_status, expected_other in cases:
            with self.subTest(rules=rules, annotations=annotations):
                for floor in (False, True):
                    policy = ToolPolicy(rules, annotations=annotations)
                    if floor:
                        with_command_floor(policy)
                    gateway = GatewayPolicy(policy, tools)
                    self.assertEqual(gateway.action(state, "a", "run_command", status), expected_status)
                    expected = expected_other if floor or rules.get("run_command") != prefix else "allow"
                    self.assertEqual(gateway.action(state, "a", "run_command", other), expected)
                    if rules is all_ask:
                        self.assertEqual(gateway.action(state, "a", "write_file", "{}"), "ask")
                    else:
                        self.assertEqual(gateway.action(state, "a", "write_file", "{}"), "allow")

    def test_contract_changes_only_while_the_floor_is_active(self):
        self.assertEqual(ToolPolicy().contract(), {"default": "allow", "tools": {}})
        self.assertEqual(with_command_floor(ToolPolicy()).contract(),
                         {"default": "allow", "tools": {}, "annotations": {"destructive+open_world": "ask"}})
        merged = with_command_floor(ToolPolicy(annotations={"read_only": "ask", COMMAND_FLOOR: "deny"}))
        self.assertEqual(merged.contract()["annotations"], {"destructive+open_world": "deny", "read_only": "ask"})
        # The joined key is also accepted from --tool-policy JSON and round-trips.
        policy = ToolPolicy.from_dict({"default": "allow", "tools": {}, "annotations": {"destructive+open_world": "ask"}})
        self.assertEqual(policy.fingerprint, with_command_floor(ToolPolicy()).fingerprint)
        for bad in ("destructive+destructive", "destructive+writes", "+", "", "destructive+"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ToolPolicy(annotations={bad: "ask"})

    def test_library_callers_keep_running_commands(self):
        # tbench/runner.py, evaluation.py and project_assistant/evaluation/coding.py call run_agent without
        # --mode: the default ToolPolicy has no floor.
        tools = coding_tools(Workspace(self.root, allow_write=True, allow_command=True))
        model = Script([{"tool_calls": [tool_call("c1", "run_command", {"argv": PRINT})]}, {"content": "done"}])
        state = run_agent(model, "run it", tools=tools)
        self.assertEqual(state.status, "completed")
        result = json.loads(next(message["content"] for message in state.messages if message["role"] == "tool"))
        self.assertTrue(result["ok"], result)


class CliTestCase(Temp):
    def run_cli(self, argv, model=None, lines=None):
        out, err = io.StringIO(), io.StringIO()
        model = model or Script([])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch("builtins.input", side_effect=list(lines or []) + [EOFError()]):
            try:
                code = main(["--root", str(self.root), "--db", str(self.db), *argv], model_factory=lambda args: model)
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def command_model(self, call_id="c1"):
        return Script([{"tool_calls": [tool_call(call_id, "run_command", {"argv": PRINT})]}, {"content": "done"}])

    def write_and_command(self):
        return Script([lambda messages: {"tool_calls": [write_call(self.root, "w1", "out.txt", "x")]},
                       {"tool_calls": [tool_call("c1", "run_command", {"argv": PRINT})]}, {"content": "done"}])

    def state(self, session):
        return SessionStore(self.db).load(session)

    def contract(self, session):
        return self.state(session).metadata["runtime_contract"]

    def config(self, **permissions):
        path = Path(self.temp.name) / "harness.json"
        path.write_text(json.dumps({"version": 1, "permissions": permissions}))
        return str(path)


class OneShotTests(CliTestCase):
    def test_text_run_stops_awaiting_approval_with_an_actionable_message(self):
        code, stdout, stderr = self.run_cli(["--mode", "full", "--session", "s", "go"], self.write_and_command())
        self.assertEqual(code, 1)
        self.assertEqual(stderr.count(FULL_MODE_ASK_NOTE), 1)
        self.assertTrue((self.root / "out.txt").exists())            # file tools stay automatic
        state = self.state("s")
        self.assertEqual(state.status, "awaiting_approval")
        request = state.metadata["approval_request"]
        self.assertEqual(request["name"], "run_command")
        self.assertIn("Agent stopped: awaiting_approval", stdout)
        self.assertIn("run_command needs approval", stderr)
        self.assertIn(f"--approve-tool c1 --approval-digest {request['request_digest']}", stderr)
        self.assertIn("--auto-approve", stderr)
        self.assertIn("--mode full --resume", stderr)
        # Following the message: approve the exact call, then resume with the same options.
        code, _, err = self.run_cli(["--session", "s", "--approve-tool", "c1", "--approval-digest", request["request_digest"]])
        self.assertEqual(code, 0, err)
        code, stdout, err = self.run_cli(["--mode", "full", "--session", "s", "--resume"], Script([{"content": "ran"}]))
        self.assertEqual((code, stdout.strip()), (0, "ran"), err)
        result = json.loads([message for message in self.state("s").messages if message["role"] == "tool"][-1]["content"])
        self.assertTrue(result["ok"], result)
        self.assertIn("42", result["data"]["stdout"])

    def test_json_output_names_the_pending_call_and_the_hint(self):
        code, stdout, _ = self.run_cli(["--mode", "full", "--output-format", "json", "--session", "j", "go"],
                                       self.command_model())
        self.assertEqual(code, 1)
        value = json.loads(stdout)
        self.assertEqual(value["status"], "awaiting_approval")
        self.assertEqual(value["exit_code"], 1)
        self.assertEqual(value["pending_approval"]["name"], "run_command")
        self.assertIn("--auto-approve", value["approval_hint"])
        # A stop that is not the full-mode command approval has no hint (the field is optional).
        code, stdout, _ = self.run_cli(["--mode", "full", "--auto-approve", "--output-format", "json", "--session", "k",
                                        "go"], self.command_model())
        self.assertEqual(code, 0)
        self.assertNotIn("approval_hint", json.loads(stdout))
        policy = Path(self.temp.name) / "ask.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"write_file": "ask"}}))
        code, stdout, _ = self.run_cli(["--mode", "full", "--tool-policy", str(policy), "--output-format", "json",
                                        "--session", "w", "go"], self.write_and_command())
        self.assertEqual(json.loads(stdout)["pending_approval"]["name"], "write_file")
        self.assertNotIn("approval_hint", json.loads(stdout))

    def test_auto_approve_restores_the_old_behaviour_byte_identically(self):
        code, _, stderr = self.run_cli(["--mode", "full", "--auto-approve", "--session", "a", "go"], self.write_and_command())
        self.assertEqual(code, 0, stderr)
        self.assertNotIn(FULL_MODE_ASK_NOTE, stderr)
        self.assertEqual(self.state("a").status, "completed")
        # The same contract as the explicit flags, which this change does not touch (= --mode full before it).
        (self.root / "out.txt").unlink()
        code, _, stderr = self.run_cli(["--allow-write", "--allow-command", "--session", "b", "go"], self.write_and_command())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.contract("a"), self.contract("b"))
        (self.root / "out.txt").unlink()
        code, _, stderr = self.run_cli(["--config", self.config(auto_approve=True), "--mode", "full", "--session", "c",
                                        "go"], self.write_and_command())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.contract("c"), self.contract("a"))
        # With the floor the policy is part of the (runtime) contract; tools and prompt are the same.
        (self.root / "out.txt").unlink()
        self.run_cli(["--mode", "full", "--session", "d", "go"], self.write_and_command())
        self.assertNotEqual(self.contract("d")["runtime"], self.contract("a")["runtime"])
        self.assertEqual({key: value for key, value in self.contract("d").items() if key != "runtime"},
                         {key: value for key, value in self.contract("a").items() if key != "runtime"})

    def test_read_only_and_edit_contracts_are_unchanged(self):
        for mode, flags in (("read-only", []), ("edit", ["--allow-write"])):
            with self.subTest(mode=mode):
                model = Script([{"content": "hi"}])
                self.assertEqual(self.run_cli(["--mode", mode, "--session", mode, "hi"], model)[0], 0)
                self.assertEqual(self.run_cli([*flags, "--session", mode + "-flags", "hi"], Script([{"content": "hi"}]))[0], 0)
                self.assertEqual(self.contract(mode), self.contract(mode + "-flags"))
                # A configured auto_approve is unused there and changes nothing.
                code, _, err = self.run_cli(["--config", self.config(auto_approve=True), "--mode", mode,
                                             "--session", mode + "-cfg", "hi"], Script([{"content": "hi"}]))
                self.assertEqual(code, 0, err)
                self.assertEqual(self.contract(mode + "-cfg"), self.contract(mode))

    def test_auto_approve_only_applies_to_full_mode(self):
        for argv in (["--mode", "edit"], ["--mode", "read-only"], [], ["--allow-write", "--allow-command"]):
            with self.subTest(argv=argv):
                code, _, stderr = self.run_cli([*argv, "--auto-approve", "--session", "x", "hi"])
                self.assertEqual(code, 2)
                self.assertIn("--auto-approve applies to --mode full", stderr)
        code, _, stderr = self.run_cli(["--mode", "edit", "--no-auto-approve", "--session", "y", "hi"],
                                       Script([{"content": "hi"}]))
        self.assertEqual(code, 0, stderr)

    def test_explicit_policy_allow_lifts_the_floor_end_to_end(self):
        policy = Path(self.temp.name) / "allow.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"run_command": {"default": "ask", "rules": [
            {"action": "allow", "match": {"argv_prefix": [sys.executable, "-c"]}}]}}}))
        code, _, stderr = self.run_cli(["--mode", "full", "--tool-policy", str(policy), "--session", "p", "go"],
                                       self.command_model())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.state("p").status, "completed")


class ResumeTests(CliTestCase):
    def test_unfinished_turn_from_before_the_default_reports_the_change(self):
        # A turn started like --mode full before this change (= --auto-approve), stopped at a write approval.
        ask = Path(self.temp.name) / "ask.json"
        ask.write_text(json.dumps({"default": "allow", "tools": {"write_file": "ask"}}))
        old = ["--mode", "full", "--tool-policy", str(ask), "--session", "u"]
        model = Script([lambda messages: {"tool_calls": [write_call(self.root, "w1", "late.txt", "x")]}])
        self.assertEqual(self.run_cli([*old, "--auto-approve", "write"], model)[0], 1)
        self.assertEqual(self.state("u").status, "awaiting_approval")
        code, _, stderr = self.run_cli([*old, "--resume"])
        self.assertEqual(code, 1)
        self.assertIn("session runtime configuration changed", stderr)
        self.assertIn(FLOOR_RESUME_HINT, stderr)
        self.assertIn("--auto-approve", FLOOR_RESUME_HINT)
        self.assertIn("--accept-config-changes", FLOOR_RESUME_HINT)
        # Either way forward works: as it started, or accepting the change.
        code, _, stderr = self.run_cli([*old, "--auto-approve", "--resume"])
        self.assertNotIn("configuration changed", stderr)
        self.assertEqual(self.state("u").status, "awaiting_approval")   # still waiting for the write approval
        code, _, stderr = self.run_cli([*old, "--resume", "--accept-config-changes"])
        self.assertNotIn("configuration changed", stderr)

    def test_other_modes_keep_the_plain_contract_error(self):
        ask = Path(self.temp.name) / "ask.json"
        ask.write_text(json.dumps({"default": "allow", "tools": {"write_file": "ask"}}))
        model = Script([lambda messages: {"tool_calls": [write_call(self.root, "w1", "late.txt", "x")]}])
        self.assertEqual(self.run_cli(["--mode", "edit", "--tool-policy", str(ask), "--session", "e", "write"], model)[0], 1)
        code, _, stderr = self.run_cli(["--mode", "read-only", "--tool-policy", str(ask), "--session", "e", "--resume"])
        self.assertEqual(code, 1)
        self.assertIn("session runtime configuration changed", stderr)
        self.assertNotIn(FLOOR_RESUME_HINT, stderr)


class WorkerTests(CliTestCase):
    def test_worker_command_waits_for_chat_approval_and_writes_do_not(self):
        from test_main_agent import Duplex, delegate, feed
        for answer, ran in (("y", True), ("n", False)):
            with self.subTest(answer=answer):
                session = "main-" + answer
                name = f"w-{answer}.txt"
                prompts = []

                def respond(prompt, answer=answer):
                    self.assertTrue((self.root / name).exists(), "the write ran without approval")
                    prompts.append(prompt)
                    return answer

                model = Duplex(main=[delegate("d1", "write then run", allow_writes=True), {"content": "已派出 w1。"}],
                               worker=[lambda messages: {"tool_calls": [write_call(self.root, "c1", name, "x")]},
                                       {"tool_calls": [tool_call("c2", "run_command", {"argv": PRINT})]},
                                       {"content": "finished"}])
                out, err = io.StringIO(), io.StringIO()
                reader = feed(["写文件再跑命令", "/wait", "/wait", "/exit"], approvals=[respond])
                with patch("builtins.input", side_effect=reader), contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    code = main(["--root", str(self.root), "--db", str(self.db), "--mode", "full", "--session", session],
                                model_factory=lambda args: model)
                self.assertEqual(code, 0, err.getvalue())
                self.assertEqual(len(prompts), 1)                         # only the command asked
                self.assertIn("Approval required: run_command", out.getvalue())
                self.assertIn("requested by: worker w1", out.getvalue())
                worker = SessionStore(self.db).load(f"{session}-w1")
                results = [json.loads(message["content"]) for message in worker.messages if message["role"] == "tool"]
                self.assertTrue(results[0]["ok"], results)
                self.assertEqual(results[1]["ok"], ran, results)
                if ran:
                    self.assertIn("42", results[1]["data"]["stdout"])
                else:
                    self.assertEqual(results[1]["error"]["code"], "permission_denied")


class AutomationCallerTests(unittest.TestCase):
    def test_local_docker_fixture_passes_auto_approve(self):
        sys.path.insert(0, str(ROOT))
        self.addCleanup(sys.path.remove, str(ROOT))
        from applications.project_assistant.evaluation import local_docker_fixture as fixture
        seen = []

        def fake_main(argv, **kwargs):
            seen.append(argv)
            return 3                                    # infrastructure: the executor stops before parsing output

        with patch("baseagent.main.main", fake_main):
            record = fixture.docker_executor()(["sh", "tests/test.sh"])
        self.assertTrue(record["infrastructure"])
        self.assertEqual(seen[0][seen[0].index("--mode") + 1:seen[0].index("--mode") + 3], ["full", "--auto-approve"])
        self.assertIn("--mode full --auto-approve", fixture.planned_local_docker()["agent_command"])

    def test_docker_demo_passes_auto_approve(self):
        source = (ROOT / "scripts" / "demo_docker.py").read_text(encoding="utf-8")
        self.assertIn('"--mode", "full", "--auto-approve"', source)

    def test_chainlit_ui_uses_explicit_flags_and_an_all_ask_policy(self):
        # ui/launch.py passes --allow-write/--allow-command (never --mode full) with an all-ask policy.
        source = (ROOT / "src" / "baseagent" / "ui" / "launch.py").read_text(encoding="utf-8")
        self.assertNotIn("'--mode'", source)
        self.assertIn("'run_command', 'verify_command'", source)


if __name__ == "__main__":
    unittest.main()
