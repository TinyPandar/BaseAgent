import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baseagent.main import main
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace


class Model:
    """Offline fake model; records every projected message list it receives."""

    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append([dict(message) for message in messages])
        return next(self.responses)


class InterruptOnceModel:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            raise KeyboardInterrupt
        return {"content": f"answer {self.calls}"}


def tool_call(identifier, name, arguments):
    return {"id": identifier, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "sessions.sqlite3"

    def invoke(self, argv, model, inputs, capability_factory=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=inputs), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv, model_factory=lambda args: model,
                        capability_factory=capability_factory)
        return code, stdout.getvalue(), stderr.getvalue()

    def chat_args(self, *extra):
        return ["--root", str(self.root), "--db", str(self.db), *extra]

    def test_two_turns_share_one_session_and_second_sees_first(self):
        model = Model([{"content": "first answer"}, {"content": "second answer"}])
        code, stdout, _ = self.invoke(
            self.chat_args(),
            model,
            ["first task", "second task", "/exit"],
            capability_factory=lambda workspace, args, skills, policy: ToolRegistry(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(model.seen), 2)
        conversation = [message["content"] for message in model.seen[1]
                        if message.get("role") in {"user", "assistant"}]
        self.assertEqual(conversation, ["first task", "first answer", "second task"])
        self.assertIn("first answer", stdout)
        self.assertIn("second answer", stdout)
        sessions = SessionStore(self.db).list_sessions()["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["status"], "completed")

    def test_one_shot_prompt_stays_one_shot(self):
        model = Model([{"content": "one shot answer"}])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=AssertionError("interactive prompt must not run")), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(self.chat_args("Explain"), model_factory=lambda args: model,
                        capability_factory=lambda workspace, args, skills, policy: ToolRegistry())
        self.assertEqual(code, 0)
        self.assertEqual(len(model.seen), 1)
        self.assertIn("one shot answer", stdout.getvalue())

    def test_existing_session_id_is_reused(self):
        model = Model([{"content": "first"}, {"content": "second"}])
        code, _, _ = self.invoke(
            self.chat_args("--session", "demo"),
            model,
            ["first", "second", "/exit"],
            capability_factory=lambda workspace, args, skills, policy: ToolRegistry(),
        )
        self.assertEqual(code, 0)
        sessions = SessionStore(self.db).list_sessions()["sessions"]
        self.assertEqual([session["id"] for session in sessions], ["demo"])
        self.assertEqual(sessions[0]["status"], "completed")

    def test_status_reports_session_concepts(self):
        model = Model([{"content": "answer"}])
        code, stdout, _ = self.invoke(
            self.chat_args("--session", "demo"),
            model,
            ["task", "/status", "/quit"],
            capability_factory=lambda workspace, args, skills, policy: ToolRegistry(),
        )
        self.assertEqual(code, 0)
        for value in ("Session: demo", "Status: completed", "Workspace:", "Tokens:"):
            self.assertIn(value, stdout)

    def write_request(self):
        instructions = Workspace(self.root).get_instructions("out.txt")["digest"]
        return {"path": "out.txt", "content": "hello",
                "expected_sha256": "missing", "expected_instructions": instructions}

    def test_write_approval_allow_executes_once(self):
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"write_file": "ask"}}), encoding="utf-8")
        model = Model([
            {"tool_calls": [tool_call("w1", "write_file", self.write_request())]},
            {"content": "done"},
        ])
        code, stdout, stderr = self.invoke(
            self.chat_args("--allow-write", "--tool-policy", str(policy)),
            model,
            ["write a file", "y", "/exit"],
        )
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "out.txt").read_text(), "hello")
        self.assertIn("Approval required: write_file", stdout)
        self.assertIn("Tool call allowed.", stdout)
        self.assertIn("done", stdout)
        self.assertIn("tool: write_file", stderr)

    def test_write_approval_deny_does_not_execute(self):
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"write_file": "ask"}}), encoding="utf-8")
        model = Model([
            {"tool_calls": [tool_call("w1", "write_file", self.write_request())]},
            {"content": "done"},
        ])
        code, stdout, _ = self.invoke(
            self.chat_args("--allow-write", "--tool-policy", str(policy)),
            model,
            ["write a file", "n", "/exit"],
        )
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "out.txt").exists())
        self.assertIn("Approval required: write_file", stdout)
        self.assertIn("Tool call denied.", stdout)
        self.assertIn("done", stdout)

    def test_existing_pending_approval_is_resolved_at_chat_start(self):
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"write_file": "ask"}}), encoding="utf-8")
        options = ["--session", "demo", "--allow-write", "--tool-policy", str(policy)]
        first = Model([{"tool_calls": [tool_call("w1", "write_file", self.write_request())]}])
        code, _, _ = self.invoke(self.chat_args("write a file", *options), first, [])
        self.assertEqual(code, 1)
        self.assertEqual(SessionStore(self.db).load("demo").status, "awaiting_approval")
        model = Model([{"content": "done"}])
        code, stdout, _ = self.invoke(self.chat_args(*options), model, ["y", "/exit"])
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "out.txt").read_text(), "hello")
        self.assertIn("Approval required: write_file", stdout)
        self.assertIn("done", stdout)

    def test_approval_does_not_enable_command_capability(self):
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"run_command": "ask"}}), encoding="utf-8")
        marker = self.root / "marker.txt"
        command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"]
        model = Model([
            {"tool_calls": [tool_call("c1", "run_command", {"argv": command})]},
            {"content": "done"},
        ])
        code, stdout, stderr = self.invoke(
            self.chat_args("--tool-policy", str(policy)),
            model,
            ["run it", "y", "/exit"],
        )
        self.assertEqual(code, 0)
        self.assertFalse(marker.exists())
        self.assertIn("Tool call allowed.", stdout)
        self.assertIn("permission_denied", stderr)

    def test_cancel_returns_to_prompt_for_next_line(self):
        model = InterruptOnceModel()
        code, stdout, stderr = self.invoke(
            self.chat_args(),
            model,
            ["first task", "second task", "/exit"],
            capability_factory=lambda workspace, args, skills, policy: ToolRegistry(),
        )
        self.assertEqual(code, 0)
        self.assertGreaterEqual(model.calls, 2)
        self.assertIn("Turn cancelled.", stderr)
        self.assertIn(f"answer {model.calls}", stdout)

    def test_eof_exits_cleanly(self):
        model = Model([{"content": "answer"}])
        code, stdout, _ = self.invoke(
            self.chat_args(),
            model,
            ["task", EOFError()],
            capability_factory=lambda workspace, args, skills, policy: ToolRegistry(),
        )
        self.assertEqual(code, 0)
        self.assertIn("answer", stdout)


if __name__ == "__main__":
    unittest.main()
