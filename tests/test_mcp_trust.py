"""First-use trust for the project ``.mcp.json`` (README 施工段 AC 11-15).

Offline: the in-repo fake MCP server (``--pid-file`` proves whether a process started), scripted
models, and a trust store under a temporary ``BASEAGENT_CONFIG_HOME`` (tests/conftest.py).
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_helpers import Script, server_entry, write_config

from baseagent.main import main
from baseagent.mcp.trust import PROMPT, TrustStore, snapshot


def answers(*lines):
    """input() stand-in that records prompts; raises if asked more than scripted."""
    prompts, queue = [], list(lines)

    def read(prompt=""):
        prompts.append(prompt)
        if not queue:
            raise AssertionError(f"unexpected question: {prompt!r}")
        return queue.pop(0)
    return read, prompts


class TrustCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "ws"
        self.root.mkdir()
        self.db = self.base / "sessions.sqlite3"
        self.pid_file = self.base / "pid"
        self.config = write_config(self.root / ".mcp.json", {"demo": self.entry(env={"TOKEN": "s3cret-value"})})

    def entry(self, env=None):
        value = server_entry("--pid-file", str(self.pid_file))
        if env:
            value["env"] = env
        return value

    def run_cli(self, argv, lines=(), *, terminal=True, model=None):
        read, prompts = answers(*lines)
        out, err = io.StringIO(), io.StringIO()
        self.model = model or Script({"content": "hi"}, {"content": "again"})
        with patch("builtins.input", side_effect=read), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             patch("baseagent.mcp.trust.stdin_is_terminal", lambda: terminal):
            try:
                code = main(["--root", str(self.root), "--db", str(self.db), *argv], model_factory=lambda args: self.model)
            except SystemExit as exit:
                code = exit.code
        self.prompts = prompts
        return code, out.getvalue(), err.getvalue()

    def chat(self, *answers_first, terminal=True, extra=()):
        return self.run_cli(["--no-main-agent", *extra], [*answers_first, "hello", "/exit"], terminal=terminal)

    def mcp_tools(self):
        return [name for names in self.model.tools for name in names if name.startswith("mcp__")]

    def started(self):
        return self.pid_file.exists()


class InteractiveTests(TrustCase):
    def test_first_use_lists_servers_and_env_key_names_without_values(self):
        code, _, err = self.chat("y")
        self.assertEqual(code, 0)
        self.assertEqual(self.prompts[0], PROMPT)
        self.assertIn("wants to start 1 server(s):", err)
        self.assertIn('  - demo: ["', err)
        self.assertIn("--pid-file", err)
        self.assertIn("env keys: TOKEN (values not shown)", err)
        self.assertNotIn("s3cret-value", err)
        self.assertTrue(self.started())
        self.assertIn("mcp__demo__echo", self.mcp_tools())
        record = json.loads((Path(os.environ["BASEAGENT_CONFIG_HOME"]) / "mcp-trust.json").read_text(encoding="utf-8"))
        self.assertNotIn("s3cret-value", json.dumps(record))

    def test_decline_starts_no_process_and_the_session_runs_without_mcp_tools(self):
        code, stdout, err = self.chat("n")
        self.assertEqual(code, 0, stdout + err)
        self.assertFalse(self.started())
        self.assertEqual(self.mcp_tools(), [])
        self.assertIn("hi", stdout)
        self.assertIn("not trusted; no MCP server was started", err)
        self.assertIn("--mcp-trust", err)
        self.assertNotIn("MCP: starting server", err)

    def test_trusted_file_is_not_asked_again(self):
        self.chat("y")
        self.pid_file.unlink()
        code, _, err = self.chat()  # no trust answer scripted: asking again would fail the test
        self.assertEqual(code, 0)
        self.assertNotIn(PROMPT, self.prompts)
        self.assertTrue(self.started())

    def test_any_byte_change_including_an_env_value_asks_again(self):
        self.chat("y")
        self.pid_file.unlink()
        text = self.config.read_text(encoding="utf-8").replace("s3cret-value", "s3cret-valuf")
        self.config.write_text(text, encoding="utf-8")
        code, _, err = self.chat("n")
        self.assertEqual(code, 0)
        self.assertEqual(self.prompts[0], PROMPT)
        self.assertIn("changed since you trusted it", err)
        self.assertFalse(self.started())

    def test_repository_cannot_bring_its_own_trust_record(self):
        snap = snapshot(self.config)
        forged = {"version": 1, "projects": {str(self.config.resolve()): {"sha256": snap.digest, "servers": ["demo"],
                                                                          "trusted_at": "2026-01-01T00:00:00+00:00"}}}
        for place in (self.root / ".baseagent" / "mcp-trust.json", self.root / "mcp-trust.json"):
            place.parent.mkdir(exist_ok=True)
            place.write_text(json.dumps(forged), encoding="utf-8")
        code, _, _ = self.chat(terminal=False)
        self.assertEqual(code, 0)
        self.assertFalse(self.started())
        # Even a store directory pointed into the workspace is refused, so a checked-in record never counts.
        with patch.dict(os.environ, {"BASEAGENT_CONFIG_HOME": str(self.root / ".baseagent")}):
            code, _, err = self.chat(terminal=True)
        self.assertEqual(code, 0)
        self.assertFalse(self.started())
        self.assertIn("is inside the workspace", err)
        self.assertNotIn(PROMPT, self.prompts)

    def test_tui_and_web_ask_on_the_terminal_before_the_frontend_starts(self):
        for ui, runner in (("tui", "baseagent.tui.app.run_tui"), ("web", "baseagent.web.server.serve")):
            with self.subTest(ui=ui):
                seen = {}

                def capture(session, **options):
                    seen["started_before_frontend"] = self.started()
                    seen["asked_before_frontend"] = list(self.prompts_so_far)
                    return 0

                read, prompts = answers("y")
                self.prompts_so_far = prompts
                with patch(runner, capture), patch("builtins.input", side_effect=read), \
                     patch("baseagent.mcp.trust.stdin_is_terminal", lambda: True), \
                     patch("baseagent.main._missing_frontend", lambda ui: None), \
                     contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    code = main(["--root", str(self.root), "--db", str(self.db), "--ui", ui],
                                model_factory=lambda args: Script())
                self.assertEqual(code, 0)
                self.assertEqual(seen["asked_before_frontend"], [PROMPT])
                self.assertTrue(seen["started_before_frontend"])
                TrustStore().revoke(self.config)
                self.pid_file.unlink(missing_ok=True)


class NonInteractiveTests(TrustCase):
    def test_one_shot_does_not_start_and_says_why(self):
        code, _, err = self.run_cli(["task"], terminal=True)
        self.assertEqual(code, 0)
        self.assertFalse(self.started())
        self.assertEqual(self.mcp_tools(), [])
        self.assertIn("is not trusted yet; its servers were not started (non-interactive run)", err)
        self.assertIn(f"baseagent --root {self.root} --mcp-trust", err)

    def test_chat_without_a_terminal_does_not_ask_or_start(self):
        code, _, err = self.chat(terminal=False)
        self.assertEqual(code, 0)
        self.assertNotIn(PROMPT, self.prompts)
        self.assertFalse(self.started())

    def test_json_and_jsonl_show_the_skip(self):
        expected = {"skipped": "untrusted", "config": ".mcp.json", "servers": ["demo"]}
        code, stdout, _ = self.run_cli(["--output-format", "json", "task"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["mcp"], expected)
        code, stdout, _ = self.run_cli(["--output-format", "jsonl", "task"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.splitlines()[-1])["data"]["mcp"], expected)
        self.assertFalse(self.started())

    def test_strict_exits_2_before_any_model_call(self):
        code, stdout, err = self.run_cli(["--mcp-strict", "--output-format", "json", "task"])
        self.assertEqual(code, 2)
        self.assertIn("not trusted", json.loads(stdout)["error"])
        self.assertEqual(self.model.tools, [])
        self.assertFalse(self.started())

    def test_strict_interactive_decline_also_exits_2(self):
        code, _, err = self.run_cli(["--no-main-agent", "--mcp-strict"], ["n"])
        self.assertEqual(code, 2)
        self.assertIn("MCP error:", err)

    def test_trusted_one_shot_starts(self):
        code, _, _ = self.run_cli(["--mcp-trust"])
        self.assertEqual(code, 0)
        code, stdout, _ = self.run_cli(["--output-format", "json", "task"])
        self.assertEqual(code, 0)
        self.assertNotIn("mcp", json.loads(stdout))
        self.assertTrue(self.started())


class CommandTests(TrustCase):
    def test_trust_and_untrust_commands(self):
        code, stdout, _ = self.run_cli(["--mcp-trust"])
        self.assertEqual(code, 0)
        self.assertIn("env keys: TOKEN (values not shown)", stdout)
        self.assertIn("Trusted", stdout)
        self.assertNotIn("s3cret-value", stdout)
        self.assertFalse(self.started())  # trusting starts nothing
        self.assertFalse(self.db.exists())  # and needs no session database
        code, stdout, _ = self.run_cli(["--mcp-untrust"])
        self.assertEqual(code, 0)
        self.assertIn("Trust revoked", stdout)
        code, _, err = self.run_cli(["task"])
        self.assertFalse(self.started())
        self.assertIn("not trusted yet", err)

    def test_trust_command_rejects_prompt_and_session(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli(["--mcp-trust", "task"])[0], 2)


class UnchangedTests(TrustCase):
    def test_mcp_config_flag_is_never_asked(self):
        other = write_config(self.base / "mine.json", {"mine": self.entry()})
        code, _, err = self.run_cli(["--mcp-config", str(other), "task"], terminal=True)
        self.assertEqual(code, 0)
        self.assertNotIn(PROMPT, self.prompts)
        self.assertTrue(self.started())
        self.assertNotIn("trust", err.lower())

    def test_no_project_file_means_no_mcp_output(self):
        self.config.unlink()
        code, _, err = self.run_cli(["task"], terminal=True)
        self.assertEqual(code, 0)
        self.assertNotIn("MCP", err)
        self.assertEqual(self.prompts, [])


if __name__ == "__main__":
    unittest.main()
