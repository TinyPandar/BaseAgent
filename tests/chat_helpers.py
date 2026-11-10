"""Shared fixtures for chat-layer, TUI and Web tests. Fake models only; no provider calls.

``ChatTestCase.session(argv, model, ui)`` runs the real ``baseagent.main`` composition
and captures the ``ChatSession`` it hands to the selected frontend, so tests drive
exactly what ``baseagent --ui <ui>`` would run.
"""

import contextlib
import importlib.util
import io
import json
import threading
import time
from unittest.mock import patch

from baseagent.main import main
from test_main_agent import WAIT, MainAgentTestCase

HAS_TEXTUAL = importlib.util.find_spec("textual") is not None
HAS_WEB = importlib.util.find_spec("fastapi") is not None and importlib.util.find_spec("uvicorn") is not None
NO_TEXTUAL = "textual is not installed (optional 'tui' extra): uv sync --extra tui"
NO_WEB = "fastapi/uvicorn are not installed (optional 'web' extra): uv sync --extra web"
RUNNERS = {"cli": "baseagent.cli_chat.run_chat", "tui": "baseagent.tui.app.run_tui", "web": "baseagent.web.server.serve"}


def wait_until(predicate, timeout=WAIT, message="condition was never met"):
    """Poll an observable condition (worker threads and turn threads report through it)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(0.01)


def join_background():
    """WorkerManager marks a worker finished before it announces the result; wait for that thread too."""
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.name.startswith(("worker-", "chat-turn")):
            thread.join(WAIT)


class ChatTestCase(MainAgentTestCase):
    def setUp(self):
        super().setUp()
        # CLI-mode sessions print trace lines from worker threads; keep test output readable.
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def session(self, argv, model, ui="cli"):
        captured = {}

        def capture(session, **options):
            captured.update(options, session=session)
            return 0

        extra = [] if ui == "cli" else ["--ui", ui]
        with patch(RUNNERS[ui], capture):
            code = main(["--root", str(self.root), "--db", str(self.db), *argv, *extra],
                        model_factory=lambda args: model)
        self.assertEqual(code, 0)
        session = captured["session"]
        self.options = {key: value for key, value in captured.items() if key != "session"}
        self.addCleanup(self.shutdown, session)
        return session

    def shutdown(self, session):
        for gate in getattr(self, "gates", ()):
            gate.release()
        wait_until(lambda: not session.busy, message="turn thread did not finish")
        if not session.closed:
            session.exit(force=True, why="test end")
        session.wait(WAIT)
        join_background()

    def idle(self, session):
        """No turn thread and no running worker thread (including its final push and status event)."""
        wait_until(lambda: not session.busy and not session.running(), message="session never became idle")
        join_background()

    def kinds(self, session, after=0):
        return [item["kind"] for item in session.events_after(after)]

    def events(self, session, kind, after=0):
        return [item["data"] for item in session.events_after(after) if item["kind"] == kind]

    def tool_result(self, worker_id, session_id="demo"):
        state = self.store().load(f"{session_id}-{worker_id}")
        record = self.store().calls(f"{session_id}-{worker_id}", state.turn_id)[0]
        return json.loads(record["result_json"])
