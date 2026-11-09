"""Offline full-duplex main-agent demo. No provider calls, cost 0.

Drives the real chat entry (``baseagent`` without a prompt) with scripted input
and a scripted fake model:

1. one message asks two read-only questions; the main agent starts w1 and w2
   and replies at once;
2. while both workers are still running, the user chats and gets an answer;
3. ``/status`` lists both workers; ``/cancel w1`` stops one;
4. w2 finishes and its result is pushed between prompts; the next message lets
   the main agent relay it.

The transcript goes to stdout; the last line is a JSON summary.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from baseagent.agent.cancellation import Cancelled  # noqa: E402
from baseagent.main import main  # noqa: E402
from baseagent.session import SessionStore  # noqa: E402

WAIT = 30


def _call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class Gate:
    """Hold one scripted worker response until released; cancellation ends the wait."""

    def __init__(self, response):
        self.response = response
        self.entered, self.opened = threading.Event(), threading.Event()

    def wait(self, cancellation):
        self.entered.set()
        deadline = time.monotonic() + WAIT
        while not self.opened.wait(0.01):
            if cancellation is not None and cancellation.cancelled():
                raise Cancelled()
            if time.monotonic() > deadline:
                raise RuntimeError("demo gate was never released")
        return self.response


class Script:
    """Main-agent calls carry delegate_task; worker calls are keyed by their task text."""

    def __init__(self, main, workers):
        self.main = iter(main)
        self.workers = {task: iter(steps) for task, steps in workers.items()}
        self.lock = threading.Lock()

    def complete_with_control(self, messages, tools, *, cancellation=None, timeout=None):
        names = [tool["function"]["name"] for tool in tools]
        with self.lock:
            if "delegate_task" in names:
                response = next(self.main)
            else:
                task = next(message["content"] for message in messages if message["role"] == "user")
                response = next(self.workers[task])
        while True:
            if isinstance(response, Gate):
                response = response.wait(cancellation)
            elif callable(response):
                response = response(messages)
            else:
                return response


def run():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "app.py").write_text("def main():\n    return 'hello'\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_app.py").write_text("from app import main\n\n\ndef test_main():\n    assert main()\n",
                                                   encoding="utf-8")
        entry, tests = "Find the CLI entry point in this repository", "Check whether this repository has tests"
        slow = Gate({"content": "never reached: w1 is cancelled"})
        finish = Gate({"content": "Found one test file, tests/test_app.py:1-5, testing app.main."})
        observed = {}

        def answer_while_running(messages):
            observed["chatted_while_running"] = (slow.entered.is_set() and finish.entered.is_set()
                                                 and not slow.opened.is_set() and not finish.opened.is_set())
            return {"content": "我是主 Agent：只负责对话和分派，自己不读文件。w1、w2 还在后台跑，结果出来会自动推送。"}

        def relay(messages):
            observed["push_reached_main_agent"] = "[worker 推送" in messages[-1]["content"]
            return {"content": "w2 的结论：仓库里有一个测试文件 tests/test_app.py:1-5，测的是 app.main。w1 已按你的要求取消。"}

        model = Script(
            main=[{"tool_calls": [_call("d1", "delegate_task", {"task": entry}),
                                  _call("d2", "delegate_task", {"task": tests})]},
                  {"content": "已派出 w1（找入口）和 w2（查测试），两个只读 worker 并行跑；你可以继续聊。"},
                  answer_while_running, relay],
            workers={entry: [{"tool_calls": [_call("g1", "grep", {"pattern": "def main"})]}, slow],
                     tests: [{"tool_calls": [_call("t1", "glob", {"pattern": "tests/*.py"})]}, finish]})
        transcript = io.StringIO()

        def typed(text, before=None):
            def respond(prompt):
                if before:
                    before()
                transcript.write(prompt + text + "\n")
                return text
            return respond

        def both_running():
            if not (slow.entered.wait(WAIT) and finish.entered.wait(WAIT)):
                raise RuntimeError("workers did not start")

        inputs = iter([
            typed("并行帮我查两件事：入口在哪？有没有测试？"),
            typed("顺便问一句，你是谁？", both_running),
            typed("/status"),
            typed("/cancel w1"),
            typed("/wait", finish.opened.set),
            typed("w2 查到什么了？"),
            typed("/exit"),
        ])
        db = root / ".baseagent" / "sessions.sqlite3"
        with patch("builtins.input", side_effect=lambda prompt="": next(inputs)(prompt)), \
             contextlib.redirect_stdout(transcript), contextlib.redirect_stderr(transcript):
            code = main(["--root", str(root), "--db", str(db), "--session", "demo"], model_factory=lambda args: model)
        workers = SessionStore(db).load("demo").metadata.get("workers", {})
        report = {"exit_code": code, "cost_cny": 0, "provider_calls": 0,
                  "workers": {worker_id: record["status"] for worker_id, record in sorted(workers.items())},
                  "chatted_while_running": observed.get("chatted_while_running", False),
                  "push_reached_main_agent": observed.get("push_reached_main_agent", False)}
        return transcript.getvalue(), report


if __name__ == "__main__":
    text, summary = run()
    print(text.replace(str(Path(tempfile.gettempdir()).resolve()), "<tmp>"))
    print(json.dumps(summary, ensure_ascii=False))
    ok = (summary["exit_code"] == 0 and summary["workers"] == {"w1": "cancelled", "w2": "completed"}
          and summary["chatted_while_running"] and summary["push_reached_main_agent"])
    sys.exit(0 if ok else 1)
