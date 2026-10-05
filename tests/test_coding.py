from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus, State
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.middleware import AgentMiddleware
from baseagent.middleware.repository import RepositoryMiddleware
from baseagent.session import SessionStore
from baseagent.tools.context import ToolContext
from baseagent.tools.editing import file_lock
from baseagent.tools.result import ErrorCode, ToolFailure, ToolResult
from baseagent.tools.workspace import Workspace, coding_tools


class Model:
    def __init__(self, messages=()):
        self.messages = iter(messages)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(deepcopy(messages))
        return ModelResponse(next(self.messages), TokenUsage(3, 1, 4))


def call(name, args, identifier="a"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class CodingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "x.txt").write_bytes(b"first\r\nsecond\r\nlast\r\n")
        self.workspace = Workspace(self.root, allow_write=True, allow_command=True)
        self.tools = coding_tools(self.workspace)
        self.store = SessionStore(self.root / ".baseagent" / "sessions.sqlite3")

    def run_turn(self, model, prompt=None, *, accept=False, layers=(), **kwargs):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id="demo", workspace_root=self.root,
                         middleware=[RepositoryMiddleware(self.workspace, accept_changes=accept), *layers], **kwargs)

    def edit_args(self, edits):
        read = self.workspace.read_file("x.txt")
        return {"path": "x.txt", "edits": edits, "expected_sha256": read["sha256"], "expected_instructions": read["instruction_digest"]}

    def test_read_range_preserves_crlf_and_reports_whole_file_digest(self):
        value = self.workspace.read_file("x.txt", start_line=2, max_lines=1)
        self.assertEqual(value["content"], "second\r\n")
        self.assertEqual(value["sha256"], sha256((self.root / "x.txt").read_bytes()).hexdigest())
        self.assertEqual(value["total_lines"], 3)
        self.assertTrue(value["truncated"])

    def test_stale_hash_cannot_overwrite_external_changes(self):
        args = self.edit_args([{"old_text": "second", "new_text": "changed"}])
        (self.root / "x.txt").write_text("external change", encoding="utf-8")
        result = self.tools.execute("edit_file", args)
        self.assertEqual(result.error.code, ErrorCode.CONFLICT)
        self.assertEqual((self.root / "x.txt").read_text(), "external change")
        with self.assertRaises(ToolFailure):
            self.workspace.write_file("x.txt", "unconditional")

    def test_multiple_edits_are_atomic_and_preserve_other_bytes(self):
        result = self.tools.execute("edit_file", self.edit_args([{"old_text": "first", "new_text": "FIRST"}, {"old_text": "last", "new_text": "LAST"}]))
        self.assertTrue(result.ok)
        self.assertEqual((self.root / "x.txt").read_bytes(), b"FIRST\r\nsecond\r\nLAST\r\n")

    def test_failed_later_edit_or_ambiguous_anchor_applies_nothing(self):
        before = (self.root / "x.txt").read_bytes()
        result = self.tools.execute("edit_file", self.edit_args([{"old_text": "first", "new_text": "FIRST"}, {"old_text": "absent", "new_text": "oops"}]))
        self.assertEqual(result.error.code, ErrorCode.CONFLICT)
        self.assertEqual((self.root / "x.txt").read_bytes(), before)
        result = self.tools.execute("edit_file", self.edit_args([{"old_text": "\r\n", "new_text": "\n"}]))
        self.assertEqual(result.error.code, ErrorCode.CONFLICT)
        self.assertEqual((self.root / "x.txt").read_bytes(), before)

    def test_cooperating_file_lock_blocks_second_writer(self):
        read = self.workspace.read_file("x.txt")
        with file_lock(self.workspace, self.root / "x.txt"):
            with self.assertRaises(ToolFailure) as error:
                self.workspace.write_file("x.txt", "changed", read["sha256"], read["instruction_digest"])
        self.assertEqual(error.exception.code, ErrorCode.CONFLICT)

    def test_external_change_during_staging_is_detected(self):
        args = self.edit_args([{"old_text": "first", "new_text": "FIRST"}])
        fsync = os.fsync

        def change_during_flush(descriptor):
            (self.root / "x.txt").write_text("other writer", encoding="utf-8")
            return fsync(descriptor)

        with patch("baseagent.tools.editing.os.fsync", side_effect=change_during_flush):
            result = self.tools.execute("edit_file", args)
        self.assertEqual(result.error.code, ErrorCode.CONFLICT)
        self.assertEqual((self.root / "x.txt").read_text(), "other writer")
        self.assertEqual(list(self.root.glob(".baseagent-edit-*")), [])

    def test_new_file_creation_cannot_replace_racing_creator(self):
        link = os.link
        target = self.root / "new.txt"

        def creator(source, destination):
            Path(destination).write_text("racing creator", encoding="utf-8")
            return link(source, destination)

        with patch("baseagent.tools.editing.os.link", side_effect=creator):
            with self.assertRaises(ToolFailure) as error:
                self.workspace.write_file("new.txt", "ours", "missing", self.workspace.get_instructions("new.txt")["digest"])
        self.assertEqual(error.exception.code, ErrorCode.CONFLICT)
        self.assertEqual(target.read_text(), "racing creator")

    def test_instructions_are_scoped_ordered_and_drift_blocks_edit(self):
        (self.root / "AGENTS.md").write_text("root rules", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "AGENTS.md").write_text("nested rules", encoding="utf-8")
        guidance = self.workspace.get_instructions("sub/new.py")
        self.assertEqual([entry["path"] for entry in guidance["instructions"]], ["AGENTS.md", str(Path("sub/AGENTS.md"))])
        self.assertEqual(len(self.workspace.get_instructions("x.txt")["instructions"]), 1)
        args = self.edit_args([{"old_text": "first", "new_text": "FIRST"}])
        (self.root / "AGENTS.md").write_text("changed rules", encoding="utf-8")
        result = self.tools.execute("edit_file", args)
        self.assertEqual(result.error.code, ErrorCode.CONFLICT)
        self.assertTrue((self.root / "x.txt").read_bytes().startswith(b"first"))

    def test_guidance_limits_and_protected_staging(self):
        (self.root / "AGENTS.md").write_text("x" * 20001, encoding="utf-8")
        with self.assertRaises(ToolFailure):
            self.workspace.get_instructions()
        with self.assertRaises(ToolFailure):
            self.workspace.read_file(".baseagent-edit-secret")

    def test_read_limit_is_enforced_by_actual_bytes(self):
        (self.root / "large.txt").write_bytes(b"x" * 200001)
        result = self.tools.execute("read_file", {"path": "large.txt"})
        self.assertEqual(result.error.code, ErrorCode.LIMIT_EXCEEDED)

    def test_plan_revision_business_rules_and_missing_context(self):
        state = State()
        context = ToolContext(state, lambda *args, **kwargs: None)
        steps = [{"id": "read", "title": "Inspect", "status": "in_progress"}]
        self.assertEqual(self.tools.execute("update_plan", {"steps": steps, "expected_revision": 0}).error.code, ErrorCode.EXECUTION_FAILED)
        result = self.tools.execute("update_plan", {"steps": steps, "expected_revision": 0}, context=context)
        self.assertTrue(result.ok)
        self.assertEqual(state.plan_revision, 1)
        stale = self.tools.execute("update_plan", {"steps": [], "expected_revision": 0}, context=context)
        self.assertEqual(stale.error.code, ErrorCode.CONFLICT)
        invalid = self.tools.execute("update_plan", {"steps": steps * 2, "expected_revision": 1}, context=context)
        self.assertEqual(invalid.error.code, ErrorCode.INVALID_ARGUMENTS)
        self.assertEqual(state.plan_revision, 1)

    def test_context_argument_cannot_be_injected_by_model(self):
        result = self.tools.execute("update_plan", {"steps": [], "expected_revision": 0, "context": {}})
        self.assertEqual(result.error.code, ErrorCode.INVALID_ARGUMENTS)

    def test_plan_and_real_verification_persist_and_appear_in_model_context(self):
        steps = [{"id": "check", "title": "Verify x.txt", "status": "in_progress"}]
        model = Model([{"tool_calls": [call("update_plan", {"steps": steps, "expected_revision": 0}, "plan"),
                                       call("verify_command", {"argv": [sys.executable, "-c", "from pathlib import Path; assert 'second' in Path('x.txt').read_text(); print('verified')"], "paths": ["x.txt"]}, "check")]}, {"content": "done"}])
        state = self.run_turn(model, "task")
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.plan_revision, 1)
        self.assertEqual(state.verifications[-1]["status"], "passed")
        self.assertEqual(state.verifications[-1]["exit_code"], 0)
        self.assertIn("Tracked task records", model.seen[-1][1]["content"])
        self.store = SessionStore(self.store.path)
        self.assertEqual(self.store.load("demo").plan, steps)
        follow_up = self.run_turn(Model([{"content": "again"}]), "follow-up")
        self.assertEqual(follow_up.plan_revision, 1)
        self.assertEqual(follow_up.verifications[-1]["status"], "passed")
        self.assertIn("verification_completed", [event["type"] for event in self.store.events("demo")])

    def test_failed_or_file_mutating_check_cannot_be_marked_passed(self):
        for script, status in [("raise SystemExit(7)", "failed"), ("from pathlib import Path; Path('x.txt').write_text('changed')", "stale")]:
            with self.subTest(status=status):
                state = self.run_turn(Model([{"tool_calls": [call("verify_command", {"argv": [sys.executable, "-c", script], "paths": ["x.txt"]})]}, {"content": "done"}]), "check")
                self.assertEqual(state.verifications[-1]["status"], status)
                self.assertFalse(json.loads(state.messages[-2]["content"])["ok"])

    def test_later_edit_invalidates_past_verification(self):
        state = self.run_turn(Model([{"tool_calls": [call("verify_command", {"argv": [sys.executable, "-c", "print('ok')"], "paths": ["x.txt"]})]}, {"content": "done"}]), "check")
        self.assertEqual(state.verifications[-1]["status"], "passed")
        (self.root / "x.txt").write_text("changed externally", encoding="utf-8")
        state = self.run_turn(Model([{"content": "inspect"}]), "next task")
        self.assertEqual(state.verifications[-1]["status"], "stale")

    def test_interrupted_verification_is_durable_and_needs_recovery(self):
        with patch.object(self.workspace, "run_command", side_effect=KeyboardInterrupt):
            state = self.run_turn(Model([{"tool_calls": [call("verify_command", {"argv": [sys.executable, "-c", "print('ok')"], "paths": ["x.txt"]})]}]), "check")
        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        self.assertEqual(self.store.load("demo").verifications[-1]["status"], "interrupted")
        self.assertEqual(self.run_turn(Model()).status, RunStatus.NEEDS_RECOVERY)

    def test_plan_terminal_state_survives_outer_wrapper_interruption(self):
        class Interrupt(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                handler(request)
                raise KeyboardInterrupt

        state = self.run_turn(Model([{"tool_calls": [call("update_plan", {"steps": [{"id": "a", "title": "work", "status": "pending"}], "expected_revision": 0})]}]), "plan", layers=[Interrupt()])
        saved = self.store.load("demo")
        self.assertEqual(saved.plan_revision, 1)
        self.assertEqual(self.run_turn(Model()).status, RunStatus.NEEDS_RECOVERY)
        result = ToolResult.from_dict(json.loads(self.store.call(saved, "a")["terminal_result_json"]))
        self.store.resolve_call("demo", "a", result)
        finished = self.run_turn(Model([{"content": "done"}]))
        self.assertEqual(finished.plan_revision, 1)

    def test_workspace_acceptance_cannot_bypass_stale_pending_write_hash(self):
        class PauseWrite(AgentMiddleware):
            def after_model(self, state, message):
                if message.get("tool_calls", [{}])[0].get("function", {}).get("name") == "edit_file":
                    raise KeyboardInterrupt

        args = self.edit_args([{"old_text": "first", "new_text": "FIRST"}])
        state = self.run_turn(Model([{"tool_calls": [call("read_file", {"path": "x.txt"}, "read")]},
                                    {"tool_calls": [call("edit_file", args, "write")]}]), "edit", layers=[PauseWrite()])
        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        (self.root / "x.txt").write_text("external version", encoding="utf-8")
        unused = Model()
        self.assertEqual(self.run_turn(unused).status, RunStatus.WORKSPACE_CHANGED)
        self.assertEqual(unused.seen, [])
        state = self.run_turn(Model([{"content": "conflict"}]), accept=True)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(json.loads(state.messages[-2]["content"])["error"]["code"], "conflict")
        self.assertEqual((self.root / "x.txt").read_text(), "external version")

    def test_root_guidance_changes_pause_resume_without_observed_file(self):
        (self.root / "AGENTS.md").write_text("root rules", encoding="utf-8")
        class Pause(AgentMiddleware):
            def after_model(self, state, message):
                raise KeyboardInterrupt

        self.run_turn(Model([{"content": "saved answer"}]), "task", layers=[Pause()])
        (self.root / "AGENTS.md").write_text("new rules", encoding="utf-8")
        self.assertEqual(self.run_turn(Model()).status, RunStatus.WORKSPACE_CHANGED)
        state = self.run_turn(Model(), accept=True)
        self.assertEqual(state.final_answer, "saved answer")

    def test_guidance_changed_during_model_request_blocks_before_tool_starts(self):
        guidance = self.root / "AGENTS.md"
        guidance.write_text("old rules", encoding="utf-8")
        args = self.edit_args([{"old_text": "first", "new_text": "FIRST"}])

        class ChangingModel(Model):
            def complete(inner, messages, tools):
                guidance.write_text("changed during request", encoding="utf-8")
                return ModelResponse({"tool_calls": [call("edit_file", args)]}, TokenUsage(3, 1, 4))

        state = self.run_turn(ChangingModel(), "edit")
        self.assertEqual(state.status, RunStatus.WORKSPACE_CHANGED)
        self.assertEqual(self.store.call(state, "a")["status"], "pending")
        self.assertEqual(state.tool_calls, 0)
        self.assertTrue((self.root / "x.txt").read_bytes().startswith(b"first"))

    def test_two_sessions_share_registry_without_sharing_plan_state(self):
        for identifier, title in [("first", "first plan"), ("second", "second plan")]:
            model = Model([{"tool_calls": [call("update_plan", {"steps": [{"id": "a", "title": title, "status": "pending"}], "expected_revision": 0})]}, {"content": "done"}])
            run_agent(model, "task", tools=self.tools, store=self.store, session_id=identifier)
        self.assertEqual(self.store.load("first").plan[0]["title"], "first plan")
        self.assertEqual(self.store.load("second").plan[0]["title"], "second plan")


if __name__ == "__main__":
    unittest.main()
