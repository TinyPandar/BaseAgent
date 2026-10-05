import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from baseagent.agent import run_agent
from baseagent.agent.cancellation import CancellationToken, Cancelled
from baseagent.agent.state import RunStatus
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.middleware.retry import RetryMiddleware
from baseagent.session import SessionStore, SessionBusy
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.process import run_process
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace, coding_tools


def call(name, args=None):
    return {"id": "a", "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}


class Model:
    def __init__(self, messages=()):
        self.messages = iter(messages)
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return ModelResponse(next(self.messages), TokenUsage(2, 1, 3))


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.sqlite3")
        self.tools = ToolRegistry()

    def run_turn(self, model, prompt=None, **kwargs):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id="demo", **kwargs)

    def pause(self):
        self.tools.register("effect", "effect", {"type": "object"}, lambda: "ok")
        return self.run_turn(Model([{"tool_calls": [call("effect")]}]), "task", max_tool_calls=0)

    def test_pre_cancelled_token_does_not_dispatch(self):
        token = CancellationToken()
        token.cancel()
        model = Model()
        state = self.run_turn(model, "task", cancellation=token)
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(model.calls, 0)
        self.assertEqual(state.unknown_usage_calls, 0)

    def test_durable_cancel_clear_and_resume_pending_call(self):
        self.pause()
        request = self.store.request_cancellation("demo")
        self.assertEqual(request, self.store.request_cancellation("demo"))
        state = self.run_turn(Model(), max_tool_calls=1)
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(self.store.call(state, "a")["status"], "pending")
        with self.assertRaises(ValueError):
            self.store.clear_cancellation("demo", "wrong")
        self.store.clear_cancellation("demo", request)
        state = self.run_turn(Model([{"content": "done"}]))
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.tool_calls, 1)
        with self.assertRaises(ValueError):
            self.store.request_cancellation("demo")

    def test_cancel_request_works_under_executor_lock_clear_does_not(self):
        state = self.pause()
        with self.store.exclusive("demo"):
            request = self.store.request_cancellation("demo")
            self.assertEqual(self.store.cancellation("demo", state.turn_id), request)
            with self.assertRaises(SessionBusy):
                self.store.clear_cancellation("demo", request)

    def test_late_model_response_is_saved_with_usage_before_stop(self):
        token = CancellationToken()
        class CancellingModel(Model):
            def complete(inner, messages, tools):
                token.cancel()
                return ModelResponse({"tool_calls": [call("not_executed")]}, TokenUsage(2, 1, 3))
        state = self.run_turn(CancellingModel(), "task", cancellation=token)
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(state.total_tokens, 3)
        self.assertEqual(state.unknown_usage_calls, 0)
        self.assertEqual(self.store.call(state, "a")["status"], "pending")
        self.assertEqual(state.tool_calls, 0)

    def test_custom_model_receives_control(self):
        class Controlled:
            def complete_with_control(inner, messages, tools, *, cancellation, timeout):
                cancellation.cancel()
                cancellation.check()
        state = self.run_turn(Controlled(), "task")
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(state.unknown_usage_calls, 1)

    def test_custom_tool_cancel_is_not_normalized_and_requires_recovery(self):
        def handler(*, context):
            context.cancellation.cancel()
            context.cancellation.check()
        self.tools.register("effect", "effect", {"type": "object"}, handler, contextual=True)
        state = self.run_turn(Model([{"tool_calls": [call("effect")]}]), "task")
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(self.store.call(state, "a")["status"], "running")
        self.assertEqual(self.run_turn(Model()).status, RunStatus.NEEDS_RECOVERY)

    def test_retry_backoff_cancelled_without_second_dispatch(self):
        token = CancellationToken()
        class FailingModel:
            calls = 0
            def complete(inner, messages, tools):
                inner.calls += 1
                token.cancel()
                raise ConnectionError("retryable")
        model = FailingModel()
        state = self.run_turn(model, "task", cancellation=token, middleware=[RetryMiddleware(model_retries=4, delay=8)])
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(model.calls, 1)

    def test_pre_cancelled_process_never_launches(self):
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(Cancelled):
            run_process(["definitely-not-a-real-command"], self.root, timeout=1, cancellation=token)

    def test_command_cancel_cleans_descendants(self):
        token = CancellationToken()
        child = "import time; from pathlib import Path; time.sleep(1.5); Path('escaped').write_text('bad')"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); print('ready',flush=True); time.sleep(20)"
        timer = threading.Timer(0.4, token.cancel)
        timer.start()
        self.addCleanup(timer.join)
        result = run_process([sys.executable, "-c", parent], self.root, timeout=10, cancellation=token)
        self.assertEqual(result.error.code, "cancelled")
        self.assertIn("ready", result.data["stdout"])
        time.sleep(1.6)
        self.assertFalse((self.root / "escaped").exists())

    def test_verify_cancel_is_completed_ledger_with_cancelled_record(self):
        token = CancellationToken()
        self.tools = coding_tools(Workspace(self.root, allow_command=True))
        timer = threading.Timer(0.5, token.cancel)
        timer.start()
        self.addCleanup(timer.join)
        state = self.run_turn(Model([{"tool_calls": [call("verify_command", {"argv": [sys.executable, "-c", "import time; time.sleep(20)"], "paths": []})]}]), "task", cancellation=token)
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(state.verifications[-1]["status"], "cancelled")
        self.assertEqual(self.store.call(state, "a")["status"], "completed")
        resumed = self.run_turn(Model([{"content": "cancelled check recorded"}]))
        self.assertEqual(resumed.status, RunStatus.COMPLETED)
        self.assertEqual(resumed.tool_calls, 1)

    def test_export_backup_delete_preserve_or_remove_cancel_request(self):
        state = self.pause()
        request = self.store.request_cancellation("demo")
        archive = self.root / "session.json"
        self.store.export_session("demo", archive)
        self.assertEqual(json.loads(archive.read_text())["cancellation_requests"], [{"turn_id": state.turn_id, "request_id": request}])
        self.store.backup(self.root / "backup.sqlite3")
        self.assertEqual(SessionStore(self.root / "backup.sqlite3").cancellation("demo", state.turn_id), request)
        self.store.delete_session("demo", discard_unfinished=True)
        self.assertIsNone(self.store.cancellation("demo", state.turn_id))

    def test_cli_cancel_clear_without_model_credentials(self):
        state = self.pause()
        base = [sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo"]
        result = subprocess.run(base + ["--cancel-session"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        request = self.store.cancellation("demo", state.turn_id)
        result = subprocess.run(base + ["--clear-cancel", request], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.store.cancellation("demo", state.turn_id))

    def test_old_request_cannot_clear_new_request(self):
        self.pause()
        first = self.store.request_cancellation("demo")
        self.store.clear_cancellation("demo", first)
        second = self.store.request_cancellation("demo")
        with self.assertRaises(ValueError):
            self.store.clear_cancellation("demo", first)
        self.assertNotEqual(first, second)
        self.assertIsNotNone(self.store.cancellation("demo", self.store.load("demo").turn_id))

    def test_cli_cancels_live_command_under_session_lock(self):
        self.tools = coding_tools(Workspace(self.root, allow_command=True))
        result = []
        errors = []
        command = "from pathlib import Path; import time; Path('started').write_text('ready'); time.sleep(20)"
        def execute():
            try:
                result.append(self.run_turn(Model([{"tool_calls": [call("run_command", {"argv": [sys.executable, "-c", command]})]}]), "task"))
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=execute)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not (self.root / "started").exists() and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.root / "started").exists(), errors)
            operation = subprocess.run([sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo", "--cancel-session"],
                                       capture_output=True, timeout=10)
            self.assertEqual(operation.returncode, 0, operation.stderr)
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result[0].status, RunStatus.CANCELLED)
            ledger = self.store.call(result[0], "a")
            self.assertEqual(ledger["status"], "completed")
            self.assertEqual(json.loads(ledger["result_json"])["error"]["code"], "cancelled")
            self.assertIsNotNone(self.store.cancellation("demo", result[0].turn_id))
        finally:
            if worker.is_alive() and self.store.load("demo"):
                self.store.request_cancellation("demo")
            worker.join(timeout=25)

    def test_v2_migration_retains_state_and_adds_cancellation(self):
        state = self.pause()
        with self.store._connection() as connection:
            connection.execute("DROP TABLE cancellation_requests")
            connection.execute("PRAGMA user_version=2")
        upgraded = SessionStore(self.store.path)
        self.assertEqual(upgraded.load("demo").to_dict(), state.to_dict())
        request = upgraded.request_cancellation("demo")
        self.assertEqual(upgraded.cancellation("demo", state.turn_id), request)
        with upgraded._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)


if __name__ == "__main__":
    unittest.main()
