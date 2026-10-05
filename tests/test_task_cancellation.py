import json
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_scope as fixtures
import test_subtasks as delegation
from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus, State
from baseagent.agent.subtasks import SubtaskDefinition, SubtaskRuntime
from baseagent.session import SessionBusy, SessionStore
from baseagent.tools.workspace import Workspace, coding_tools


class TaskCancellationTests(unittest.TestCase):
    setUp = fixtures.ScopeTests.setUp
    node = fixtures.ScopeTests.node
    run_node = fixtures.ScopeTests.run_node

    def create(self):
        with self.store.exclusive("demo"):
            return self.node()

    def test_current_node_request_is_idempotent_targeted_and_state_preserving(self):
        view = self.create()
        before = self.store.task_tree("demo")
        identifier = self.store.request_cancellation("demo", task_id=view.task_id)
        self.assertEqual(identifier, self.store.request_cancellation("demo", task_id=view.task_id))
        self.assertEqual(self.store.task_tree("demo"), before)
        self.assertIsNone(self.store.cancellation("demo", self.state.turn_id))
        self.assertEqual(view.cancellation("demo", view.state.turn_id), identifier)
        value = self.store.events("demo")[-1]
        self.assertEqual((value["task_id"], value["parent_task_id"]), (view.task_id, "root"))
        self.assertNotIn("task", value["data"])

    def test_node_request_reaches_descendants_but_not_siblings(self):
        with self.store.exclusive("demo"):
            view = self.node()
            self.store.save(self.state, [fixtures.call("sibling", "delegate")])
            self.store.start_call(self.state, "sibling")
            sibling = State(session_id="demo", turn_id=uuid4().hex, workspace_root=str(self.root), messages=view.state.messages)
            record = self.store.create_task(self.state, call_id="sibling", name="sibling", state=sibling)
            other = self.store.task_node(self.state, record["task_id"])
            view.save(view.state, [fixtures.call("nested", "delegate")])
            view.start_call(view.state, "nested")
            leaf = State(session_id="demo", turn_id=uuid4().hex, workspace_root=str(self.root), messages=view.state.messages)
            record = self.store.create_task(self.state, parent_task_id=view.task_id, call_id="nested", name="leaf", state=leaf)
            descendant = self.store.task_node(self.state, record["task_id"])
            identifier = self.store.request_cancellation("demo", task_id=view.task_id)
            self.assertEqual(descendant.cancellation("demo", descendant.state.turn_id), identifier)
            self.assertIsNone(other.cancellation("demo", other.state.turn_id))
            model = fixtures.Model()
            self.assertEqual(self.run_node(descendant, model).status, RunStatus.CANCELLED)
            self.assertEqual(model.calls, 0)

    def test_exact_clear_preserves_root_request_and_refuses_stale_ack(self):
        view = self.create()
        node_request = self.store.request_cancellation("demo", task_id=view.task_id)
        root_request = self.store.request_cancellation("demo")
        with self.assertRaises(ValueError):
            self.store.clear_cancellation("demo", "wrong", task_id=view.task_id)
        self.store.clear_cancellation("demo", node_request, task_id=view.task_id)
        self.assertEqual(view.cancellation("demo", view.state.turn_id), root_request)
        newer = self.store.request_cancellation("demo", task_id=view.task_id)
        with self.assertRaises(ValueError):
            self.store.clear_cancellation("demo", node_request, task_id=view.task_id)
        self.assertEqual(self.store.cancellation("demo", view.state.turn_id), newer)

    def test_request_runs_under_root_lock_but_clear_requires_it(self):
        view = self.create()
        with self.store.exclusive("demo"):
            identifier = self.store.request_cancellation("demo", task_id=view.task_id)
            with self.assertRaises(SessionBusy):
                self.store.clear_cancellation("demo", identifier, task_id=view.task_id)

    def test_event_failures_roll_back_request_and_clear(self):
        view = self.create()
        with patch.object(self.store, "_event", side_effect=RuntimeError("event failure")):
            with self.assertRaises(RuntimeError):
                self.store.request_cancellation("demo", task_id=view.task_id)
        self.assertIsNone(self.store.cancellation("demo", view.state.turn_id))
        identifier = self.store.request_cancellation("demo", task_id=view.task_id)
        with patch.object(self.store, "_event", side_effect=RuntimeError("event failure")):
            with self.assertRaises(RuntimeError):
                self.store.clear_cancellation("demo", identifier, task_id=view.task_id)
        self.assertEqual(self.store.cancellation("demo", view.state.turn_id), identifier)

    def test_completed_and_historical_nodes_refuse_and_backup_export_keep_request(self):
        view = self.create()
        identifier = self.store.request_cancellation("demo", task_id=view.task_id)
        self.store.export_session("demo", self.root / "export.json")
        self.store.backup(self.root / "backup.sqlite3")
        data = json.loads((self.root / "export.json").read_text(encoding="utf-8"))
        self.assertEqual(data["cancellation_requests"], [{"turn_id": view.state.turn_id, "request_id": identifier}])
        self.assertEqual(SessionStore(self.root / "backup.sqlite3").cancellation("demo", view.state.turn_id), identifier)
        self.store.clear_cancellation("demo", identifier, task_id=view.task_id)
        with self.store.exclusive("demo"):
            view.state.status = RunStatus.COMPLETED
            view.save(view.state)
        with self.assertRaises(ValueError):
            self.store.request_cancellation("demo", task_id=view.task_id)
        self.state.turn_id = "new-root-turn"
        self.store.save(self.state)
        for action in [lambda: self.store.request_cancellation("demo", task_id=view.task_id), lambda: self.store.clear_cancellation("demo", identifier, task_id=view.task_id)]:
            with self.assertRaises(ValueError):
                action()

    def test_cli_node_cancellation_and_clear_without_model_access(self):
        view = self.create()
        base = [sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo", "--task-id", view.task_id]
        result = subprocess.run([*base, "--cancel-session"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        identifier = self.store.cancellation("demo", view.state.turn_id)
        result = subprocess.run([*base, "--clear-cancel", identifier], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.store.cancellation("demo", view.state.turn_id))

    def test_cli_node_request_cancels_actual_delegated_command_then_resume_does_not_replay(self):
        self._cancel_actual_delegated_command(task_target=True)

    def test_cli_root_request_cancels_actual_delegated_command_then_resume_does_not_replay(self):
        self._cancel_actual_delegated_command(task_target=False)

    def _cancel_actual_delegated_command(self, *, task_target):
        tools = coding_tools(Workspace(self.root, allow_command=True))
        code = "from pathlib import Path; import time; Path('started').write_text('yes'); time.sleep(20)"
        child = fixtures.Model([{"tool_calls": [{"id": "command", "type": "function", "function": {"name": "verify_command", "arguments": json.dumps({"argv": [sys.executable, "-c", code], "paths": []})}}]}, {"content": "child done"}])
        runtime = SubtaskRuntime([SubtaskDefinition("reader", ("verify_command",), model=child)])
        result, errors = [], []
        def execute():
            try:
                result.append(run_agent(fixtures.Model([{"tool_calls": [delegation.delegate()]}]), "task", tools=tools, store=self.store,
                                        session_id="live", workspace_root=self.root, subtasks=runtime))
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=execute)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not (self.root / "started").exists() and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.root / "started").exists(), errors)
            node = self.store.task_tree("live")["nodes"][0]
            operation = subprocess.run([sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "live", *(["--task-id", node["task_id"]] if task_target else []), "--cancel-session"], capture_output=True, timeout=10)
            self.assertEqual(operation.returncode, 0, operation.stderr)
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result[0].status, RunStatus.CANCELLED)
            node = self.store.task_tree("live")["nodes"][0]
            self.assertEqual(node["state"]["verifications"][-1]["status"], "cancelled")
            record = node["tool_calls"][0]
            self.assertEqual(record["status"], "completed")
            self.assertEqual(json.loads(record["result_json"])["error"]["code"], "cancelled")
            identifier = self.store.cancellation("live", node["state"]["turn_id"] if task_target else result[0].turn_id)
            self.assertIsNone(self.store.cancellation("live", result[0].turn_id if task_target else node["state"]["turn_id"]))
            self.store.clear_cancellation("live", identifier, **({"task_id": node["task_id"]} if task_target else {}))
            resumed = run_agent(fixtures.Model([{"content": "done"}]), tools=tools, store=self.store, session_id="live", workspace_root=self.root, subtasks=runtime)
            self.assertEqual((resumed.status, resumed.tool_calls), (RunStatus.COMPLETED, 2))
            self.assertEqual(child.calls, 2)
        finally:
            if worker.is_alive() and self.store.load("live"):
                self.store.request_cancellation("live")
            worker.join(timeout=25)


if __name__ == "__main__":
    unittest.main()
