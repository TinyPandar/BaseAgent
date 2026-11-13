"""--mode read-only|edit|full (README plan C, AC 15-18 and 24). Fake models only.

The capability matrix runs every mode (and no mode) with every explicit permission flag,
both as a one-shot prompt and in chat, and checks each cell: the exit code and error for
conflicts, the tool list the model receives, and whether a write and a command actually ran.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_helpers import HAS_TEXTUAL, HAS_WEB, RUNNERS
from checkpoint_helpers import Script, tool_call, write_call

from baseagent.main import FULL_MODE_WARNING, MODES, main
from baseagent.session import SessionStore

FLAGS = ([], ["--allow-write"], ["--no-allow-write"], ["--allow-command"], ["--no-allow-command"],
         ["--allow-write", "--allow-command"])
TOOLS = ["get_instructions", "read_file", "search_files", "grep", "glob", "write_file", "edit_file", "apply_patch", "run_command",
         "update_plan", "get_task_state", "verify_command", "git_diff", "git_status", "remember", "forget_memory",
         "recall_memory", "read_history", "set_history_summary", "publish_memory", "search_shared_memory",
         "withdraw_memory"]
WRITE_TOOLS = {"write_file", "edit_file", "apply_patch"}
COMMAND_TOOLS = {"run_command", "verify_command"}
PUBLISH_TOOLS = {"publish_memory", "withdraw_memory"}        # need --allow-memory-publish, outside --mode


def visible(write, command):
    """Tools the model is offered: disabled capabilities are hidden (ExecutionGuidanceMiddleware)."""
    hidden = PUBLISH_TOOLS | (set() if write else WRITE_TOOLS) | (set() if command else COMMAND_TOOLS)
    return [name for name in TOOLS if name not in hidden]


def expected(mode, flags):
    """(conflict message or None, allow_write, allow_command) for one matrix cell."""
    write = "--allow-write" in flags
    command = "--allow-command" in flags
    if mode is None:
        return None, write, command
    wanted_write, wanted_command = MODES[mode]
    for flag in flags:
        name = flag.removeprefix("--no-").removeprefix("--")
        wanted = wanted_write if name == "allow-write" else wanted_command
        if flag.startswith("--no-") == wanted:
            return f"--mode {mode} conflicts with {flag}", wanted_write, wanted_command
    return None, wanted_write, wanted_command


class ModeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "work"
        self.root.mkdir()
        self.db = Path(self.temp.name).resolve() / "state" / "s.sqlite3"
        self.count = 0

    def model(self):
        """Write a new file and run a command in one step, then answer."""
        self.count += 1
        name = f"out{self.count}.txt"
        return Script([lambda messages: {"tool_calls": [write_call(self.root, "c1", name, "x"),
                                                        tool_call("c2", "run_command", {"argv": [sys.executable, "-c", "print(42)"]})]},
                       {"content": "done"}])

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

    def outcome(self, session):
        state = SessionStore(self.db).load(session)
        results = [json.loads(message["content"]) for message in state.messages[state.turn_start:] if message["role"] == "tool"]
        return results[0]["ok"], results[1]["ok"], results


class CapabilityMatrixTests(ModeTestCase):
    def test_every_mode_and_flag_as_one_shot_and_chat(self):
        for entry in ("one-shot", "chat"):
            for mode in (None, *MODES):
                for flags in FLAGS:
                    with self.subTest(entry=entry, mode=mode, flags=flags):
                        conflict, write, command = expected(mode, flags)
                        session = f"s{self.count + 1}"
                        argv = [*(["--mode", mode] if mode else []), *flags, "--session", session]
                        model = self.model()
                        if entry == "one-shot":
                            code, _, stderr = self.run_cli([*argv, "make it"], model)
                        else:
                            code, _, stderr = self.run_cli([*argv, "--no-main-agent"], model, ["make it", "/exit"])
                        if conflict:
                            self.assertEqual(code, 2)
                            self.assertIn(conflict, stderr)
                            self.assertIsNone(SessionStore(self.db).load(session))
                            continue
                        self.assertEqual(code, 0, stderr)
                        self.assertEqual(model.tools[0], visible(write, command))
                        wrote, ran, results = self.outcome(session)
                        self.assertEqual((wrote, ran), (write, command), results)
                        # A hidden tool called anyway still fails at the workspace boundary.
                        if not write:
                            self.assertEqual(results[0]["error"]["message"], "file writes disabled; start with --allow-write")
                        if not command:
                            self.assertEqual(results[1]["error"]["message"], "commands disabled; start with --allow-command")
                        self.assertEqual(FULL_MODE_WARNING in stderr, mode == "full")

    def test_main_agent_chat_workers_get_the_mode_permissions(self):
        for mode, writes in (("read-only", False), ("edit", True)):
            with self.subTest(mode=mode):
                from test_main_agent import Duplex, delegate
                name = f"w-{mode}.txt"
                model = Duplex(main=[delegate("d1", "write it", allow_writes=True), {"content": "已派出 w1。"}],
                               worker=[lambda messages: {"tool_calls": [write_call(self.root, "c1", name, "x")]},
                                       {"content": "tried"}])
                code, _, stderr = self.run_cli(["--mode", mode, "--session", f"main-{mode}"], model, ["写文件", "/wait", "/exit"])
                self.assertEqual(code, 0, stderr)
                self.assertEqual((self.root / name).exists(), writes)
                self.assertEqual(model.main_tools[0], ["delegate_task", "message_worker"])


class ModeRuleTests(ModeTestCase):
    def config(self, **permissions):
        path = Path(self.temp.name) / "harness.json"
        path.write_text(json.dumps({"version": 1, "permissions": permissions}))
        return str(path)

    def test_mode_replaces_permissions_from_config(self):
        config = self.config(allow_write=True, allow_command=True)
        code, _, _ = self.run_cli(["--config", config, "--session", "cfg", "make"], self.model())
        self.assertEqual(self.outcome("cfg")[:2], (True, True))
        code, _, stderr = self.run_cli(["--config", config, "--mode", "read-only", "--session", "ro", "make"], self.model())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.outcome("ro")[:2], (False, False))
        code, _, stderr = self.run_cli(["--config", config, "--mode", "edit", "--session", "ed", "make"], self.model())
        self.assertEqual(code, 0, stderr)                            # config values are not explicit flags
        self.assertEqual(self.outcome("ed")[:2], (True, False))

    def test_tool_policy_only_restricts_the_mode(self):
        deny = Path(self.temp.name) / "deny.json"
        deny.write_text(json.dumps({"default": "allow", "tools": {"run_command": "deny"}}))
        code, _, stderr = self.run_cli(["--mode", "full", "--tool-policy", str(deny), "--session", "p1", "make"], self.model())
        self.assertEqual(code, 0, stderr)
        wrote, ran, results = self.outcome("p1")
        self.assertEqual((wrote, ran), (True, False))
        self.assertEqual(results[1]["error"]["message"], "tool request denied by policy")
        allow = Path(self.temp.name) / "allow.json"
        allow.write_text(json.dumps({"default": "allow", "tools": {"write_file": "allow", "run_command": "allow"}}))
        code, _, stderr = self.run_cli(["--mode", "read-only", "--tool-policy", str(allow), "--session", "p2", "make"], self.model())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.outcome("p2")[:2], (False, False))   # a policy cannot grant what the mode withholds

    def test_full_mode_warning_is_one_stderr_line(self):
        code, stdout, stderr = self.run_cli(["--mode", "full", "--session", "f", "make"], self.model())
        self.assertEqual(code, 0)
        self.assertEqual(stderr.count(FULL_MODE_WARNING), 1)
        self.assertNotIn("Warning", stdout)
        self.assertIn("LocalBackend is not a sandbox", FULL_MODE_WARNING)
        self.assertIn("/undo", FULL_MODE_WARNING)
        code, _, stderr = self.run_cli(["--allow-write", "--allow-command", "--session", "g", "make"], self.model())
        self.assertNotIn(FULL_MODE_WARNING, stderr)                 # explicit flags keep today's output

    def test_unfinished_session_needs_accept_config_changes_when_the_mode_changes(self):
        ask = Path(self.temp.name) / "ask.json"
        ask.write_text(json.dumps({"default": "allow", "tools": {"write_file": "ask"}}))
        model = Script([lambda messages: {"tool_calls": [write_call(self.root, "c1", "late.txt", "x")]}])
        code, _, _ = self.run_cli(["--mode", "edit", "--tool-policy", str(ask), "--session", "u", "write"], model)
        self.assertEqual(code, 1)
        self.assertEqual(SessionStore(self.db).load("u").status, "awaiting_approval")
        code, _, stderr = self.run_cli(["--mode", "full", "--tool-policy", str(ask), "--session", "u", "--resume"])
        self.assertEqual(code, 1)
        self.assertIn("session runtime configuration changed: tools", stderr)
        code, _, stderr = self.run_cli(["--mode", "edit", "--tool-policy", str(ask), "--session", "u", "--resume"])
        self.assertEqual(code, 1)                                    # same mode: resumes, still waiting for approval
        self.assertNotIn("configuration changed", stderr)
        code, _, stderr = self.run_cli(["--mode", "full", "--tool-policy", str(ask), "--session", "u", "--resume",
                                        "--accept-config-changes"])
        self.assertNotIn("configuration changed", stderr)

    def test_mode_is_refused_for_session_and_database_operations(self):
        self.run_cli(["--session", "s", "hi"], Script([{"content": "ok"}]))
        for argv in (["--session", "s", "--events"], ["--session", "s", "--checkpoints"], ["--list-sessions"]):
            with self.subTest(argv=argv):
                code, _, stderr = self.run_cli(["--mode", "edit", *argv])
                self.assertEqual(code, 2)
                self.assertIn("--mode applies to one-shot prompts, --resume and interactive chat", stderr)
        code, _, stderr = self.run_cli(["--mode", "fast", "--session", "s", "hi"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", stderr)

    def test_tui_and_web_frontends_take_the_mode(self):
        uis = (["tui"] if HAS_TEXTUAL else []) + (["web"] if HAS_WEB else [])
        for ui in uis:
            for mode, (write, command) in MODES.items():
                with self.subTest(ui=ui, mode=mode):
                    captured = {}
                    with patch(RUNNERS[ui], lambda session, **options: captured.update(session=session) or 0):
                        code, _, stderr = self.run_cli(["--ui", ui, "--mode", mode, "--session", f"{ui}-{mode}"])
                    self.assertEqual(code, 0, stderr)
                    workspace = captured["session"].workspace
                    self.assertEqual((workspace.allow_write, workspace.allow_command), (write, command))
                    self.assertEqual(FULL_MODE_WARNING in stderr, mode == "full")
                    with patch(RUNNERS[ui], lambda session, **options: 0):
                        code, _, stderr = self.run_cli(["--ui", ui, "--mode", mode, "--no-allow-write", "--session", "x"])
                    self.assertEqual(code, 0 if not write else 2)


if __name__ == "__main__":
    unittest.main()
