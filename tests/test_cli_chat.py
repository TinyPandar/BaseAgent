import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baseagent.kernel.errors import AuthError, ModelTimeoutError
from baseagent.main import _list_preview, main
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace


class Model:
    """Offline fake model; records every projected message list it receives."""

    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.seen = []
        self.tools = []

    def complete(self, messages, tools):
        self.seen.append([dict(message) for message in messages])
        self.tools.append([tool["function"]["name"] for tool in tools])
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class ToolLoopModel:
    """Never finishes: every response asks for one more distinct tool call."""

    def __init__(self):
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append([dict(message) for message in messages])
        index = len(self.seen)
        return {"tool_calls": [tool_call(f"p{index}", "ping", {"n": index})]}


def ping_registry(workspace=None, args=None, skills=None, policy=None):
    tools = ToolRegistry()
    tools.register("ping", "ping", {"type": "object", "properties": {"n": {"type": "integer"}}},
                   lambda n=0: "ok", retry_safe=True)
    return tools


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


class ChatTestCase(unittest.TestCase):
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


class ChatTests(ChatTestCase):
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
            self.chat_args("--no-main-agent", "--allow-write", "--tool-policy", str(policy)),
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
            self.chat_args("--no-main-agent", "--allow-write", "--tool-policy", str(policy)),
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
            self.chat_args("--no-main-agent", "--tool-policy", str(policy)),
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


class ChatUxTests(ChatTestCase):
    """Continuous-chat UX defaults, previews and stop explanations."""

    def one_shot(self, argv, model, capability_factory=None):
        return self.invoke(argv, model, AssertionError("interactive prompt must not run"),
                           capability_factory=capability_factory)

    def test_projected_prompt_answers_greetings_without_state_tools(self):
        model = Model([{"content": "hello"}])
        code, _, _ = self.invoke(self.chat_args("--no-main-agent"), model, ["hi", "/exit"])
        self.assertEqual(code, 0)
        system = model.seen[0][0]["content"].replace("\n", " ")
        self.assertIn("Answer greetings, small talk and general questions that do not depend on earlier "
                      "work directly, without calling get_task_state, recall_memory or read_history.", system)
        self.assertIn("Use those tools only when the user refers to earlier tasks, plans, memory or "
                      "conversation", system)
        for name in ("get_task_state", "recall_memory", "read_history"):
            self.assertIn(name, model.tools[0])

    def test_glob_preview_reports_counts_and_tool_truncation(self):
        for index in range(15):
            (self.root / f"file{index:02}.txt").write_text("x", encoding="utf-8")
        model = Model([
            {"tool_calls": [tool_call("g1", "glob", {"pattern": "*.txt", "max_results": 12})]},
            {"tool_calls": [tool_call("g2", "glob", {"pattern": "file0*.txt"})]},
            {"content": "listed"},
        ])
        code, _, stderr = self.invoke(self.chat_args("--no-main-agent"), model, ["list files", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("tool result: 12 items (tool stopped at max_results=12; more may exist); "
                      "preview shows first 10, 2 more not shown", stderr)
        self.assertIn("tool result: 10 items (tool returned the complete result); preview shows all", stderr)
        self.assertIn("  file09.txt", stderr)
        self.assertNotIn("  file10.txt", stderr)

    def test_grep_preview_distinguishes_clipped_lines_from_match_limit(self):
        clipped = {"matches": [{"path": "a.py", "line": 3, "text": "x" * 300}], "truncated": True}
        self.assertIn("tool returned all matches; some long lines were clipped",
                      _list_preview("grep", "{}", clipped, 80))
        limited = {"matches": [{"path": "a.py", "line": n, "text": "x"} for n in range(5)], "truncated": True}
        preview = _list_preview("grep", json.dumps({"pattern": "x", "max_matches": 5}), limited, 80)
        self.assertIn("5 items (tool stopped at max_matches=5; more may exist); preview shows all", preview)
        self.assertIn("  a.py:4: x", preview)
        self.assertIsNone(_list_preview("read_file", "{}", {"content": "text", "sha256": "abc"}, 80))

    def test_model_timeouts_are_retried_by_default(self):
        model = Model([ModelTimeoutError("model transport timed out"),
                       ModelTimeoutError("model transport timed out"), {"content": "recovered"}])
        code, stdout, _ = self.one_shot(self.chat_args("task", "--session", "demo"), model,
                                        capability_factory=ping_registry)
        self.assertEqual(code, 0)
        self.assertIn("recovered", stdout)
        self.assertEqual(SessionStore(self.db).load("demo").model_calls, 3)

    def test_model_retries_zero_disables_retry(self):
        model = Model([ModelTimeoutError("model transport timed out"), {"content": "unused"}])
        code, stdout, _ = self.one_shot(self.chat_args("task", "--model-retries", "0"), model,
                                        capability_factory=ping_registry)
        self.assertEqual(code, 1)
        self.assertEqual(len(model.seen), 1)
        self.assertIn("ModelTimeoutError", stdout)

    def test_authentication_errors_are_not_retried(self):
        model = Model([AuthError("denied", status_code=401), {"content": "unused"}])
        code, _, _ = self.one_shot(self.chat_args("task"), model, capability_factory=ping_registry)
        self.assertEqual(code, 1)
        self.assertEqual(len(model.seen), 1)

    def test_unknown_usage_under_token_budget_explains_why_retry_stopped(self):
        model = Model([ModelTimeoutError("model transport timed out"), {"content": "unused"}])
        code, _, stderr = self.one_shot(self.chat_args("task", "--max-total-tokens", "1000"), model,
                                        capability_factory=ping_registry)
        self.assertEqual(code, 1)
        self.assertEqual(len(model.seen), 1)
        self.assertIn("Automatic model retries (--model-retries) stop here", stderr)
        self.assertIn("--resolve-usage", stderr)

    def test_chat_defaults_to_sixteen_steps_and_explains_limit(self):
        model = ToolLoopModel()
        code, stdout, stderr = self.invoke(self.chat_args("--session", "demo"), model,
                                           ["work", "/status", "/exit"], capability_factory=ping_registry)
        self.assertEqual(code, 0)
        state = SessionStore(self.db).load("demo")
        self.assertEqual((state.max_steps, state.max_model_calls), (16, 16))
        self.assertEqual(state.status, "max_steps_exceeded")
        self.assertIn("Step limit reached: max-steps 16/16 used (steps 16/16, model calls 16/16", stderr)
        self.assertIn("--resume continues this same turn and keeps the used counts", stderr)
        self.assertIn("baseagent --session demo --resume --max-steps 32 --max-model-calls 32", stderr)
        self.assertIn("/status shows the session ID", stderr)
        self.assertNotIn("no error detail was recorded", stderr)
        self.assertIn("Attempts: steps 16/16, model 16/16, tools 16/32", stdout)

    def test_one_shot_keeps_eight_step_default_and_explains_limit(self):
        model = ToolLoopModel()
        code, _, stderr = self.one_shot(self.chat_args("work", "--session", "demo"), model,
                                        capability_factory=ping_registry)
        self.assertEqual(code, 1)
        state = SessionStore(self.db).load("demo")
        self.assertEqual((state.max_steps, state.max_model_calls), (8, 8))
        self.assertIn("Step limit reached: max-steps 8/8 used", stderr)
        self.assertIn("baseagent --session demo --resume --max-steps 16 --max-model-calls 16", stderr)
        self.assertNotIn("/status", stderr)

    def test_model_call_limit_names_the_limit(self):
        model = ToolLoopModel()
        code, _, stderr = self.one_shot(self.chat_args("work", "--max-steps", "5", "--max-model-calls", "2"),
                                        model, capability_factory=ping_registry)
        self.assertEqual(code, 1)
        self.assertIn("Model-call limit reached: max-model-calls 2/2 used", stderr)

    def test_explicit_chat_limits_override_defaults(self):
        model = ToolLoopModel()
        code, _, stderr = self.invoke(self.chat_args("--session", "demo", "--max-steps", "3"), model,
                                      ["work", "/exit"], capability_factory=ping_registry)
        self.assertEqual(code, 0)
        state = SessionStore(self.db).load("demo")
        self.assertEqual((state.max_steps, state.max_model_calls), (3, 3))
        self.assertIn("max-steps 3/3 used", stderr)

    def test_chat_injects_wrapup_hint_by_default(self):
        model = ToolLoopModel()
        self.invoke(self.chat_args("--max-steps", "3"), model, ["work", "/exit"],
                    capability_factory=ping_registry)
        hints = [index for index, messages in enumerate(model.seen)
                 if any("logical steps remain" in str(message.get("content")) for message in messages)]
        self.assertEqual(hints[0], 1)

    def test_one_shot_has_no_wrapup_hint_unless_requested(self):
        model = ToolLoopModel()
        self.one_shot(self.chat_args("work", "--max-steps", "3"), model, capability_factory=ping_registry)
        self.assertFalse(any("logical steps remain" in str(message.get("content"))
                             for messages in model.seen for message in messages))
        model = ToolLoopModel()
        self.one_shot(self.chat_args("work", "--max-steps", "3", "--wrapup-hint-steps", "2"), model,
                      capability_factory=ping_registry)
        self.assertTrue(any("logical steps remain" in str(message.get("content"))
                            for messages in model.seen for message in messages))

    def test_chat_explicit_zero_disables_wrapup_hint(self):
        model = ToolLoopModel()
        self.invoke(self.chat_args("--max-steps", "3", "--wrapup-hint-steps", "0"), model, ["work", "/exit"],
                    capability_factory=ping_registry)
        self.assertFalse(any("logical steps remain" in str(message.get("content"))
                             for messages in model.seen for message in messages))

    def test_list_sessions_timestamps_are_utc_iso8601(self):
        self.one_shot(self.chat_args("task", "--session", "demo"), Model([{"content": "done"}]),
                      capability_factory=ping_registry)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["--db", str(self.db), "--list-sessions"]), 0)
        updated = json.loads(stdout.getvalue())["sessions"][0]["updated_at"]
        self.assertRegex(updated, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


if __name__ == "__main__":
    unittest.main()
