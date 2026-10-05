import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.main import main
from baseagent.middleware import AgentMiddleware
from baseagent.session import SessionBusy, SessionStore
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.result import ToolFailure, ToolResult
from baseagent.tools.workspace import Workspace


class Model:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(list(messages))
        return next(self.responses)


def call(identifier):
    return {"id": identifier, "type": "function", "function": {"name": "write", "arguments": "{}"}}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.sqlite3")
        self.effects = []
        self.tools = ToolRegistry()
        self.tools.register("write", "Side effect", {"type": "object"}, lambda: self.effects.append("write") or "written")

    def run_turn(self, model, prompt=None, **kwargs):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id="demo", workspace_root=self.root, **kwargs)

    def test_new_process_store_retains_conversation_and_completed_resume_is_cached(self):
        first = self.run_turn(Model([{"content": "first answer"}]), "first")
        self.store = SessionStore(self.store.path)
        unused = Model()
        self.assertEqual(self.run_turn(unused).final_answer, "first answer")
        self.assertEqual(unused.seen, [])
        second_model = Model([{"content": "second answer"}])
        second = self.run_turn(second_model, "second")
        self.assertNotEqual(first.turn_id, second.turn_id)
        self.assertEqual([message["content"] for message in second_model.seen[0][1:]], ["first", "first answer", "second"])
        self.assertEqual(second.model_calls, 1)
        with self.assertRaisesRegex(ValueError, "different workspace"):
            run_agent(Model(), tools=self.tools, store=self.store, session_id="demo", workspace_root=self.root / "other")

    def test_resume_pending_batch_does_not_repeat_completed_tool(self):
        original = self.store.start_call

        def interrupt_second(state, identifier):
            if identifier == "b":
                raise KeyboardInterrupt
            original(state, identifier)

        with patch.object(self.store, "start_call", side_effect=interrupt_second):
            state = self.run_turn(Model([{"tool_calls": [call("a"), call("b")]}]), "write twice")
        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        self.assertEqual([row["status"] for row in self.store.calls("demo", state.turn_id)], ["completed", "pending"])
        resumed = self.run_turn(Model([{"content": "done"}]))
        self.assertEqual(resumed.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, ["write", "write"])
        self.assertEqual(resumed.tool_calls, 2)
        self.assertEqual([message["tool_call_id"] for message in resumed.messages if message["role"] == "tool"], ["a", "b"])

    def test_uncertain_wrapper_outcome_blocks_until_manual_reconciliation(self):
        class Interrupt(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                handler(request)
                raise KeyboardInterrupt

        state = self.run_turn(Model([{"tool_calls": [call("a")]}]), "write", middleware=[Interrupt()])
        row = self.store.call(state, "a")
        self.assertEqual(row["status"], "running")
        self.assertTrue(json.loads(row["terminal_result_json"])["ok"])
        unused = Model()
        resumed = self.run_turn(unused)
        self.assertEqual(resumed.status, RunStatus.NEEDS_RECOVERY)
        self.assertEqual(unused.seen, [])
        self.assertEqual(self.effects, ["write"])
        with self.assertRaisesRegex(ValueError, "current turn"):
            self.run_turn(Model(), "another task")
        self.store.resolve_call("demo", "a", ToolResult(data="verified written"))
        finished = self.run_turn(Model([{"content": "done"}]))
        self.assertEqual(finished.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, ["write"])
        self.assertEqual(json.loads(finished.messages[-2]["content"])["data"], "verified written")
        with self.assertRaisesRegex(ValueError, "only unresolved"):
            self.store.resolve_call("demo", "a", ToolResult())

    def test_budget_checkpoint_survives_failure_and_explicit_extension(self):
        failed = self.run_turn(Model(), "task", max_model_calls=1)
        self.assertEqual(failed.status, RunStatus.FAILED)
        unused = Model([{"content": "done"}])
        stopped = self.run_turn(unused)
        self.assertEqual(stopped.status, RunStatus.MAX_MODEL_CALLS_EXCEEDED)
        self.assertEqual(unused.seen, [])
        completed = self.run_turn(unused, max_model_calls=2)
        self.assertEqual(completed.status, RunStatus.COMPLETED)
        self.assertEqual(completed.model_calls, 2)

    def test_pending_tool_resumes_at_step_limit_without_resetting_counts(self):
        class Interrupt(AgentMiddleware):
            def after_model(self, state, message):
                raise KeyboardInterrupt

        self.run_turn(Model([{"tool_calls": [call("a")]}]), "write", max_steps=1, middleware=[Interrupt()])
        stopped = self.run_turn(Model())
        self.assertEqual(stopped.status, RunStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(self.effects, ["write"])
        completed = self.run_turn(Model([{"content": "done"}]), max_steps=2, max_model_calls=2)
        self.assertEqual(completed.status, RunStatus.COMPLETED)
        self.assertEqual(completed.tool_calls, 1)
        self.assertEqual(self.effects, ["write"])

    def test_result_and_transcript_commit_roll_back_together(self):
        class Interrupt(AgentMiddleware):
            def after_model(self, state, message):
                raise KeyboardInterrupt

        state = self.run_turn(Model([{"tool_calls": [call("a")]}]), "write", middleware=[Interrupt()])
        self.store.start_call(state, "a")
        state.messages.append({"role": "tool", "tool_call_id": "a", "content": "{}"})
        with patch.object(self.store, "_save", side_effect=RuntimeError("disk failure")):
            with self.assertRaisesRegex(RuntimeError, "disk failure"):
                self.store.complete_call(state, "a", ToolResult(data="written"))
        self.assertEqual(self.store.call(state, "a")["status"], "running")
        self.assertIsNone(self.store.call(state, "a")["result_json"])
        self.assertEqual(self.store.load("demo").messages[-1]["role"], "assistant")

    def test_session_excludes_concurrent_writers_but_allows_other_sessions(self):
        other = SessionStore(self.store.path)
        with self.store.exclusive("demo"):
            with self.assertRaises(SessionBusy):
                with other.exclusive("demo"):
                    self.fail("same session lock was acquired twice")
            with other.exclusive("other"):
                pass
        with other.exclusive("demo"):
            pass

    def test_hard_process_exit_preserves_running_ledger_and_releases_lock(self):
        script = '''
import os, sys
from pathlib import Path
from baseagent.agent import run_agent
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
class Model:
    def complete(self, messages, tools):
        return {"tool_calls": [{"id": "crash", "type": "function", "function": {"name": "write", "arguments": "{}"}}]}
def write():
    Path(sys.argv[2]).write_text("written", encoding="utf-8")
    os._exit(23)
tools = ToolRegistry()
tools.register("write", "Side effect", {"type": "object"}, write)
run_agent(Model(), "task", tools=tools, store=SessionStore(sys.argv[1]), session_id="demo", workspace_root=sys.argv[3])
'''
        marker = self.root / "effect.txt"
        process = subprocess.run([sys.executable, "-c", script, str(self.store.path), str(marker), str(self.root)], capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 23, process.stderr.decode())
        self.assertEqual(marker.read_text(), "written")
        saved = self.store.load("demo")
        self.assertEqual(saved.tool_calls, 1)
        self.assertEqual(self.store.call(saved, "crash")["attempts"], 1)
        resumed = self.run_turn(Model())
        self.assertEqual(resumed.status, RunStatus.NEEDS_RECOVERY)
        self.assertEqual(resumed.blocked_tool_calls, ["crash"])
        self.assertEqual(self.effects, [])

    def test_session_storage_is_excluded_from_file_tools(self):
        (self.root / ".baseagent").mkdir()
        (self.root / ".baseagent" / "secret.txt").write_text("secret")
        custom = self.root / "custom.db"
        custom.write_text("secret")
        workspace = Workspace(self.root, allow_write=True, protected_paths=[custom])
        for name in [".baseagent/secret.txt", "custom.db"]:
            with self.assertRaises(ToolFailure):
                workspace.read_file(name)
            with self.assertRaises(ToolFailure):
                workspace.write_file(name, "overwrite")
        self.assertEqual(workspace.search_files("secret")["matches"], [])

    def test_cli_inspect_and_cached_resume_need_no_provider(self):
        self.run_turn(Model([{"content": "answer"}]), "task")
        base = ["baseagent", "--session", "demo", "--db", str(self.store.path)]
        for operation in ["--inspect-session", "--resume"]:
            with patch.object(sys, "argv", base + [operation]), patch("baseagent.main.Model", side_effect=AssertionError("provider must not initialize")):
                with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(), 0)
                self.assertIn("demo" if operation == "--inspect-session" else "answer", output.getvalue())

    def test_redirected_request_and_final_wrapper_result_are_distinct(self):
        class Redirect(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                terminal = handler(replace(request, name="safe"))
                return ToolResult(data={"wrapped": terminal.data})

        self.tools.register("safe", "safe", {"type": "object"}, lambda: "safe result")
        state = self.run_turn(Model([{"tool_calls": [call("a")]}, {"content": "done"}]), "task", middleware=[Redirect()])
        row = self.store.call(state, "a")
        self.assertEqual(json.loads(row["request_json"])["function"]["name"], "write")
        self.assertEqual(json.loads(row["terminal_request_json"])["name"], "safe")
        self.assertEqual(json.loads(row["terminal_result_json"])["data"], "safe result")
        self.assertEqual(json.loads(row["result_json"])["data"], {"wrapped": "safe result"})
        self.assertEqual(self.effects, [])

    def test_cli_resolves_uncertain_call_without_executing_tool_or_model(self):
        class Interrupt(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                handler(request)
                raise KeyboardInterrupt

        self.run_turn(Model([{"tool_calls": [call("a")]}]), "write", middleware=[Interrupt()])
        result_file = self.root / "verified.json"
        result_file.write_text(json.dumps(ToolResult(data="verified").to_dict()), encoding="utf-8")
        argv = ["baseagent", "--session", "demo", "--db", str(self.store.path), "--resolve-tool", "a", "--result-file", str(result_file)]
        with patch.object(sys, "argv", argv), patch("baseagent.main.Model", side_effect=AssertionError("must not initialize")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
        self.assertEqual(self.effects, ["write"])
        state = self.store.load("demo")
        self.assertEqual(self.store.call(state, "a")["status"], "completed")
        self.assertEqual(json.loads(state.messages[-1]["content"])["data"], "verified")


if __name__ == "__main__":
    unittest.main()
