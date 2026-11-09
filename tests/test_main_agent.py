"""Full-duplex main agent + background workers in continuous chat. Fake models, no provider calls.

Concurrency is controlled with events, gates and barriers, never with sleeps:
a ``Gate`` blocks one scripted model response until the test releases it (and
honours cancellation), so every interleaving below is forced, not hoped for.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from baseagent.agent.cancellation import Cancelled
from baseagent.agent.errors import BudgetExceeded
from baseagent.agent.router import MAIN_AGENT_PROMPT, SEQUENTIAL_MAIN_AGENT_PROMPT, is_main_agent_session
from baseagent.agent.state import RunStatus, State
from baseagent.agent.workers import PUSH_HEADER, SharedBudgetMiddleware, TurnBudget, WorkerManager
from baseagent.cli_chat import PromptLine, stop_explanation
from baseagent.kernel.errors import AuthError
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.main import main
from baseagent.session import SessionStore
from baseagent.tools.workspace import Workspace

MAIN_TOOLS = ["delegate_task", "message_worker"]
WORKSPACE_TOOLS = {"read_file", "grep", "glob", "edit_file", "write_file", "run_command"}
SECRET = "SECRET_README_BODY_" + "x" * 3000
WAIT = 20


def tool_call(identifier, name, arguments):
    return {"id": identifier, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def delegate(identifier, task, allow_writes=None):
    return {"tool_calls": [delegation(identifier, task, allow_writes)]}


def delegation(identifier, task, allow_writes=None):
    arguments = {"task": task, **({"allow_writes": allow_writes} if allow_writes is not None else {})}
    return tool_call(identifier, "delegate_task", arguments)


class Gate:
    """Block one scripted response until released; a cancelled worker stops waiting."""

    def __init__(self, response):
        self.response = response
        self.entered = threading.Event()
        self.opened = threading.Event()

    def release(self):
        self.opened.set()

    def wait(self, cancellation):
        self.entered.set()
        deadline = time.monotonic() + WAIT
        while not self.opened.wait(0.01):
            if cancellation is not None and cancellation.cancelled():
                raise Cancelled()
            if time.monotonic() > deadline:
                raise AssertionError("gate was never released")
        return self.response


class Duplex:
    """One thread-safe fake model. Main-agent calls (they carry delegate_task) and worker calls use
    separate scripts; worker scripts can be keyed by the delegated task text."""

    def __init__(self, main=(), worker=(), by_task=None):
        self.main = iter(main)
        self.worker = iter(worker)
        self.by_task = {task: iter(script) for task, script in (by_task or {}).items()}
        self.lock = threading.Lock()
        self.main_seen, self.main_tools, self.worker_seen, self.worker_tools = [], [], [], []

    def complete_with_control(self, messages, tools, *, cancellation=None, timeout=None):
        names = [tool["function"]["name"] for tool in tools]
        copy = [dict(message) for message in messages]
        with self.lock:
            if "delegate_task" in names:
                self.main_seen.append(copy)
                self.main_tools.append(names)
                response = next(self.main)
            else:
                task = next(message["content"] for message in messages if message["role"] == "user")
                self.worker_seen.append(copy)
                self.worker_tools.append(names)
                response = next(self.by_task.get(task, self.worker))
        while True:
            if isinstance(response, Gate):
                response = response.wait(cancellation)
            elif isinstance(response, BaseException):
                raise response
            elif callable(response):
                response = response(messages)
            else:
                return response


def looping(prefix):
    """Worker that never finishes: a new distinct tool call every step."""
    counter = iter(range(1, 1000))

    def respond(messages):
        index = next(counter)
        return {"tool_calls": [tool_call(f"{prefix}{index}", "glob", {"pattern": f"*{prefix}{index}*"})]}

    return [respond] * 100


@contextlib.contextmanager
def pushed(marker):
    """Event set once the manager posts a push containing marker (posting happens after the worker settles)."""
    seen = threading.Event()
    original = WorkerManager.post

    def spy(manager, stream, text):
        original(manager, stream, text)
        if marker in text:
            seen.set()

    with patch.object(WorkerManager, "post", spy):
        yield seen


def feed(items, prompts=None, approvals=None):
    """input() replacement: strings are typed lines, exceptions are raised, callables run first.

    With ``approvals``, approval prompts ("Allow ...") take their answers from that separate list, so a
    worker that parks earlier or later than expected cannot swallow a chat line.
    """
    items = iter(items)
    answers = iter(approvals) if approvals is not None else None

    def read(prompt=""):
        if prompts is not None:
            prompts.append(prompt)
        item = next(answers if answers is not None and prompt.startswith("Allow ") else items)
        if isinstance(item, BaseException):
            raise item
        return item(prompt) if callable(item) else item

    return read


class MainAgentTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / ".state" / "sessions.sqlite3"
        (self.root / "README.md").write_text(SECRET + "\n", encoding="utf-8")

    def invoke(self, argv, model, inputs, prompts=None, approvals=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        reader = inputs if callable(inputs) else feed(inputs, prompts, approvals)
        with patch("builtins.input", side_effect=reader), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--root", str(self.root), "--db", str(self.db), *argv], model_factory=lambda args: model)
        return code, stdout.getvalue(), stderr.getvalue()

    def store(self):
        return SessionStore(self.db)

    def worker(self, worker_id="w1", session="demo"):
        return self.store().load(f"{session}-{worker_id}")

    def registry(self, session="demo"):
        return self.store().load(session).metadata.get("workers", {})

    def tool_results(self, state):
        return [json.loads(message["content"]) for message in state.messages if message.get("role") == "tool"]

    def root_calls(self, state):
        return [item["function"]["name"] for message in state.messages if message.get("role") == "assistant"
                for item in message.get("tool_calls") or []]

    def write_request(self):
        instructions = Workspace(self.root).get_instructions("out.txt")["digest"]
        return {"path": "out.txt", "content": "hello", "expected_sha256": "missing",
                "expected_instructions": instructions}

    def policy(self, tools):
        path = self.root / "policy.json"
        path.write_text(json.dumps({"default": "allow", "tools": {name: "ask" for name in tools}}), encoding="utf-8")
        return str(path)


class MainAgentChatTests(MainAgentTestCase):
    def test_small_talk_is_answered_without_a_worker(self):
        model = Duplex([{"content": "hello!"}])
        code, stdout, _ = self.invoke(["--session", "demo"], model, ["hi", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("hello!", stdout)
        self.assertEqual(model.main_tools, [MAIN_TOOLS])
        self.assertEqual(model.worker_seen, [])
        self.assertEqual(model.main_seen[0][0]["content"], MAIN_AGENT_PROMPT)
        state = self.store().load("demo")
        self.assertEqual(self.root_calls(state), [])
        self.assertEqual(state.metadata.get("workers", {}), {})
        self.assertIsNone(self.worker())

    def test_delegate_returns_immediately_and_result_is_pushed(self):
        # AC1/AC3/AC4: the worker is held until the main agent has already replied.
        gate = Gate({"content": "README.md holds a long marker line. README.md:1-1"})

        def dispatched(messages):
            result = json.loads(messages[-1]["content"])
            self.assertEqual(result["data"]["status"], "running")
            self.assertFalse(gate.opened.is_set())
            gate.release()
            return {"content": "已派出 w1，在读 README。"}

        model = Duplex(
            main=[delegate("d1", "Read README.md and summarize it"), dispatched,
                  {"content": "Summary: the README is one long marker line (README.md:1-1)."}],
            worker=[{"tool_calls": [tool_call("r1", "read_file", {"path": "README.md"})]}, gate])
        code, stdout, _ = self.invoke(["--session", "demo"], model, ["读一下 README 并总结", "/wait", "结果呢？", "/exit"])
        self.assertEqual(code, 0)
        self.assertLess(stdout.index("已派出 w1"), stdout.index("[w1 已完成]"))
        self.assertIn("[w1 已完成] README.md holds a long marker line.", stdout)
        self.assertIn("引用: README.md:1-1", stdout)
        self.assertIn("Summary: the README is one long marker line", stdout)
        state = self.store().load("demo")
        self.assertEqual(self.root_calls(state), ["delegate_task"])
        result = self.tool_results(state)[0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["task_id"], "w1")
        self.assertEqual(result["data"]["status"], "running")
        # The push reaches the main agent's next turn as reference data, without the tool body.
        pushed = model.main_seen[2][-1]["content"]
        self.assertTrue(pushed.startswith(PUSH_HEADER))
        self.assertTrue(pushed.endswith("[用户消息]\n结果呢？"))
        payload = json.loads(pushed.splitlines()[1])
        self.assertEqual(set(payload), {"task_id", "status", "summary", "summary_truncated", "citations", "paths",
                                        "call_ids", "spill_refs"})
        self.assertEqual((payload["task_id"], payload["status"]), ("w1", "completed"))
        self.assertEqual(payload["citations"], ["README.md:1-1"])
        self.assertEqual(payload["paths"], ["README.md"])
        self.assertEqual(payload["call_ids"], ["r1"])
        for messages in model.main_seen:
            self.assertNotIn(SECRET, json.dumps(messages, ensure_ascii=False))
        self.assertNotIn(SECRET, json.dumps(state.messages, ensure_ascii=False))
        # The full tool output stays in the worker's own session ledger.
        worker = self.worker()
        self.assertEqual(worker.status, "completed")
        self.assertIn(SECRET[:40], json.dumps(self.store().calls("demo-w1", worker.turn_id)))
        self.assertEqual(worker.metadata["session_mode"], "worker")
        self.assertEqual(worker.metadata["main_session"], "demo")
        worker_tools = set(model.worker_tools[0])
        self.assertIn("read_file", worker_tools)
        self.assertTrue(worker_tools.isdisjoint({"write_file", "edit_file", "run_command", "delegate_task",
                                                 "message_worker", "run_subtask"}))

    def test_chat_answers_while_worker_is_blocked(self):
        # AC2: the worker sits at a controlled blocking point while a full main-agent turn completes.
        gate = Gate({"content": "done reading"})
        model = Duplex(
            main=[delegate("d1", "Read README.md"), {"content": "已派出 w1。"}, {"content": "你好！w1 还在跑。"},
                  {"content": "w1 读完了。"}],
            worker=[gate])

        def check_then_release(prompt):
            self.assertTrue(gate.entered.wait(WAIT))
            self.assertFalse(gate.opened.is_set())
            gate.release()
            return "/wait"

        code, stdout, _ = self.invoke(["--session", "demo"], model,
                                      ["读一下 README", "hi", check_then_release, "怎么样了", "/exit"])
        self.assertEqual(code, 0)
        self.assertLess(stdout.index("你好！w1 还在跑。"), stdout.index("[w1 已完成] done reading"))
        self.assertEqual(len(model.main_seen), 4)
        self.assertNotIn(PUSH_HEADER, model.main_seen[2][-1]["content"])
        self.assertIn(PUSH_HEADER, model.main_seen[3][-1]["content"])

    def test_worker_has_entry_tools_and_nothing_more(self):
        one_shot = Duplex(main=[], worker=[{"content": "done"}])
        self.invoke(["--allow-write", "--allow-command", "--session", "single", "task"], one_shot, [])
        entry_tools = one_shot.worker_tools[0]
        model = Duplex(main=[delegate("d1", "Fix sample.py and run the tests", allow_writes=True), {"content": "ok"}],
                       worker=[{"content": "nothing to fix"}])
        self.invoke(["--allow-write", "--allow-command", "--session", "demo"], model,
                    ["修改 sample.py 并跑测试", "/wait", "/exit"])
        self.assertEqual(model.main_tools[0], MAIN_TOOLS)
        self.assertEqual(sorted(model.worker_tools[0]), sorted(entry_tools))
        self.assertNotIn("delegate_task", entry_tools)
        # Read-only delegation is a tightened subset of the same entry tools.
        readonly = Duplex(main=[delegate("d2", "look around"), {"content": "ok"}], worker=[{"content": "looked"}])
        self.invoke(["--allow-write", "--allow-command", "--session", "demo2"], readonly,
                    ["look around the repo", "/wait", "/exit"])
        self.assertTrue(set(readonly.worker_tools[0]) < set(entry_tools))

    def test_read_only_worker_never_gets_run_command(self):
        # AC21: --allow-command authorizes the entry, not a read-only worker.
        model = Duplex(main=[delegate("d1", "run the tests"), {"content": "ok"}],
                       worker=[{"tool_calls": [tool_call("c1", "run_command", {"argv": ["true"]})]},
                               {"content": "run_command is not available to me"}])
        code, _, _ = self.invoke(["--allow-command", "--session", "demo"], model, ["跑一下测试", "/wait", "/exit"])
        self.assertEqual(code, 0)
        self.assertNotIn("run_command", model.worker_tools[0])
        worker = self.worker()
        result = json.loads(self.store().calls("demo-w1", worker.turn_id)[0]["result_json"])
        self.assertEqual(result["error"]["code"], "unknown_tool")

    def test_worker_write_waits_for_chat_approval(self):
        def answer(prompt):
            self.assertFalse((self.root / "out.txt").exists(), "file must not change before approval")
            self.assertIn("for worker w1", prompt)
            return "y"

        model = Duplex(
            main=[delegate("d1", "Create out.txt with hello", allow_writes=True), {"content": "已派出 w1。"}],
            worker=[{"tool_calls": [tool_call("w1c", "write_file", self.write_request())]}, {"content": "Created out.txt."}])
        code, stdout, _ = self.invoke(["--session", "demo", "--allow-write", "--tool-policy", self.policy(["write_file"])],
                                      model, ["create out.txt containing hello", "/wait", "/wait", "/exit"],
                                      approvals=[answer])
        self.assertEqual(code, 0)
        self.assertIn("Approval required: write_file", stdout)
        self.assertIn("requested by: worker w1 (session demo-w1)", stdout)
        self.assertIn('arguments: {"path": "out.txt", "content": "hello"', stdout)
        self.assertIn("Tool call allowed.", stdout)
        self.assertIn("[w1 已完成] Created out.txt.", stdout)
        self.assertEqual((self.root / "out.txt").read_text(), "hello")
        self.assertEqual(self.worker().status, "completed")
        state = self.store().load("demo")
        self.assertEqual([record["call_id"] for record in self.store().calls("demo", state.turn_id)], ["d1"])
        self.assertEqual(self.registry()["w1"]["status"], "completed")

    def test_worker_write_denial_leaves_file_untouched(self):
        model = Duplex(main=[delegate("d1", "Create out.txt", allow_writes=True), {"content": "ok"}],
                       worker=[{"tool_calls": [tool_call("w1c", "write_file", self.write_request())]},
                               {"content": "Write was denied."}])
        code, stdout, _ = self.invoke(["--session", "demo", "--allow-write", "--tool-policy", self.policy(["write_file"])],
                                      model, ["create out.txt", "/wait", "/wait", "/exit"], approvals=["n"])
        self.assertEqual(code, 0)
        self.assertIn("Tool call denied.", stdout)
        self.assertFalse((self.root / "out.txt").exists())

    def test_pending_worker_approval_survives_restart(self):
        model = Duplex(main=[delegate("d1", "Create out.txt", allow_writes=True), {"content": "ok"}],
                       worker=[{"tool_calls": [tool_call("w1c", "write_file", self.write_request())]}])
        options = ["--session", "demo", "--allow-write", "--tool-policy", self.policy(["write_file"])]
        code, stdout, stderr = self.invoke(options, model, ["create out.txt", "/wait", "/status", "/exit"],
                                           approvals=[EOFError()])
        self.assertEqual(code, 0)
        self.assertIn("w1 awaiting_approval 等待审批", stdout)
        self.assertIn("这条审批保持待定", stderr)
        self.assertFalse((self.root / "out.txt").exists())
        resumed = Duplex(worker=[{"content": "Created out.txt."}])
        code, stdout, _ = self.invoke(options, resumed, ["/wait", "/status", "/exit"], approvals=["y"])
        self.assertEqual(code, 0)
        self.assertIn("Approval required: write_file", stdout)
        self.assertEqual((self.root / "out.txt").read_text(), "hello")
        self.assertEqual(self.worker().status, "completed")
        self.assertIn("w1 completed 已完成", stdout)

    def test_unclear_request_is_clarified_and_never_writes_by_default(self):
        model = Duplex([{"content": "你是想让我解释这个文件，还是修改它？"}])
        code, stdout, _ = self.invoke(["--session", "demo", "--allow-write"], model, ["能不能帮我处理一下这个文件", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("还是修改它", stdout)
        self.assertIsNone(self.worker())
        self.assertIn("ask one short clarifying question", MAIN_AGENT_PROMPT)
        # Even if the main agent delegates, writes are off unless explicitly requested.
        model = Duplex(main=[delegate("d1", "handle the file"), {"content": "ok"}],
                       worker=[{"tool_calls": [tool_call("w1c", "write_file", self.write_request())]},
                               {"content": "write_file is not available"}])
        self.invoke(["--session", "demo2", "--allow-write"], model, ["能不能帮我处理一下这个文件", "/wait", "/exit"])
        self.assertNotIn("write_file", model.worker_tools[0])
        self.assertFalse((self.root / "out.txt").exists())

    def test_worker_limit_is_reported_in_chinese_and_chat_stays_usable(self):
        model = Duplex(main=[delegate("d1", "list every file"), {"content": "已派出 w1。"},
                             {"content": "worker stopped; please narrow the task"}],
                       worker=looping("g"))
        code, stdout, stderr = self.invoke(["--session", "demo", "--max-steps", "6"], model,
                                           ["list every file", "/wait", "/status", "怎么样", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("执行子 Agent（worker w1）", stderr)
        self.assertIn("用完了自己的步数上限", stderr)
        self.assertIn("可以在对话里重试", stderr)
        self.assertNotIn("no error detail was recorded", stderr)
        self.assertIn("worker stopped; please narrow the task", stdout)
        self.assertIn("w1 failed 失败（max_steps_exceeded）", stdout)
        worker = self.worker()
        # Six model calls in the turn: one dispatch, the reserved reply, and four for the worker.
        self.assertEqual(worker.max_model_calls, 4)
        self.assertEqual(worker.model_calls, 4)
        self.assertEqual(self.store().load("demo").status, "completed")
        payload = json.loads(model.main_seen[2][-1]["content"].splitlines()[1])
        self.assertEqual((payload["status"], payload["detail"]), ("failed", "max_steps_exceeded"))

    def test_no_worker_without_room_for_the_main_reply(self):
        model = Duplex([delegate("d1", "Read README.md"), {"content": "Not enough budget left for a worker."}])
        code, stdout, _ = self.invoke(["--session", "demo", "--max-steps", "2"], model, ["读一下 README", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("Not enough budget left for a worker.", stdout)
        self.assertIsNone(self.worker())
        state = self.store().load("demo")
        self.assertEqual(self.tool_results(state)[0]["error"]["code"], "limit_exceeded")
        self.assertEqual((state.status, state.model_calls), ("completed", 2))

    def test_worker_failure_explains_and_retries_in_chat(self):
        model = Duplex(
            main=[delegate("d1", "Read README.md"), {"content": "已派出 w1。"},
                  delegate("d2", "Read README.md again"), {"content": "已重新派出 w2。"}],
            by_task={"Read README.md": [AuthError("invalid key", status_code=401)],
                     "Read README.md again": [{"content": "README.md read. README.md:1-1"}]})
        code, stdout, stderr = self.invoke(["--session", "demo"], model,
                                           ["读一下 README", "/wait", "/status", "again please", "/wait", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("执行子 Agent（worker w1）", stderr)
        self.assertIn("模型或工具出错", stderr)
        self.assertIn("可以在对话里重试", stderr)
        self.assertNotIn("no error detail was recorded", stderr)
        self.assertIn("w1 failed 失败（failed）", stdout)
        self.assertIn("[w2 已完成] README.md read.", stdout)
        self.assertIn('"status": "failed"', model.main_seen[2][-1]["content"])

    def test_status_lists_every_worker_with_state_time_and_calls(self):
        # AC9
        gate = Gate({"content": "second done"})
        model = Duplex(main=[{"tool_calls": [delegation("d1", "first"), delegation("d2", "second")]}, {"content": "ok"}],
                       by_task={"first": [{"content": "first done"}], "second": [gate]})

        def wait_first(prompt):
            self.assertTrue(gate.entered.wait(WAIT))
            self.assertTrue(first_pushed.wait(WAIT))
            return "/status"

        def release(prompt):
            gate.release()
            return "/wait"

        with pushed("[w1 已完成]") as first_pushed:
            code, stdout, _ = self.invoke(["--session", "demo"], model,
                                          ["两个问题", wait_first, release, "/status", "/exit"])
        self.assertEqual(code, 0)
        first_status = stdout.split("Session: demo")[1]
        self.assertRegex(first_status, r"w2 running 运行中; \d+\.\ds; model 1/12")
        self.assertIn("Turn budget (latest main-agent turn + workers it started): model 4/16", first_status)
        last_status = stdout.split("Session: demo")[2]
        self.assertRegex(last_status, r"w1 completed 已完成; \d+\.\ds; model 1/12, steps 1/12, tools 0/\d+; read-only; first")
        self.assertRegex(last_status, r"w2 completed 已完成; \d+\.\ds; model 1/12")

    def test_worker_trace_preview_is_printed_between_prompts(self):
        # AC21: the glob preview also shows for a worker, tagged with its id, in main-agent chat.
        for index in range(15):
            (self.root / f"file{index:02}.txt").write_text("x", encoding="utf-8")
        model = Duplex(main=[delegate("d1", "list txt files"), {"content": "ok"}],
                       worker=[{"tool_calls": [tool_call("g1", "glob", {"pattern": "*.txt", "max_results": 12})]},
                               {"content": "listed"}])
        code, _, stderr = self.invoke(["--session", "demo"], model, ["list files", "/wait", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("[w1] tool: glob", stderr)
        self.assertIn("[w1] tool result: 12 items (tool stopped at max_results=12; more may exist); "
                      "preview shows first 10, 2 more not shown", stderr)


class ParallelWorkerTests(MainAgentTestCase):
    def test_read_only_workers_run_in_parallel(self):
        barrier = threading.Barrier(2, timeout=WAIT)

        def meet(name):
            def respond(messages):
                barrier.wait()  # Only passes when both workers are inside a model call at the same time.
                return {"content": f"{name} done"}
            return respond

        model = Duplex(main=[{"tool_calls": [delegation("d1", "A"), delegation("d2", "B")]}, {"content": "两个都派出了"}],
                       by_task={"A": [meet("A")], "B": [meet("B")]})
        code, stdout, _ = self.invoke(["--session", "demo"], model, ["问两个问题", "/wait", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("[w1 已完成]", stdout)
        self.assertIn("[w2 已完成]", stdout)

    def test_max_workers_rejects_extra_delegation(self):
        # AC6: rejection, not queueing.
        gate = Gate({"content": "A done"})
        model = Duplex(main=[{"tool_calls": [delegation("d1", "A"), delegation("d2", "B")]},
                             {"content": "w1 已派出；第二个被拒绝，要等 w1。"}],
                       by_task={"A": [gate]})

        def release(prompt):
            gate.release()
            return "/wait"

        code, stdout, _ = self.invoke(["--session", "demo", "--max-workers", "1"], model, ["两个问题", release, "/exit"])
        self.assertEqual(code, 0)
        results = self.tool_results(self.store().load("demo"))
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        self.assertTrue(results[1]["error"]["message"].startswith("rejected: max_workers (1/1 active: w1)"))
        self.assertEqual(results[1]["data"], {"status": "rejected", "reason": "max_workers", "active": ["w1"]})
        self.assertEqual(sorted(self.registry()), ["w1"])

    def test_only_one_writer_at_a_time(self):
        # AC7: a second write worker is rejected with the reason; a read-only one still starts.
        gate = Gate({"content": "writer done"})
        model = Duplex(main=[{"tool_calls": [delegation("d1", "write A", True), delegation("d2", "write B", True),
                                             delegation("d3", "read C")]}, {"content": "w1 在写，第二个写入被拒，w2 在读。"}],
                       by_task={"write A": [gate], "read C": [{"content": "read done"}]})

        def release(prompt):
            gate.release()
            return "/wait"

        code, stdout, _ = self.invoke(["--session", "demo", "--allow-write", "--max-workers", "3"], model,
                                      ["改 A 和 B，读 C", release, "/exit"])
        self.assertEqual(code, 0)
        results = self.tool_results(self.store().load("demo"))
        self.assertEqual([result["ok"] for result in results], [True, False, True])
        self.assertTrue(results[1]["error"]["message"].startswith("rejected: single_writer (w1 already holds write tools)"))
        self.assertEqual(results[1]["data"]["reason"], "single_writer")
        self.assertEqual(results[2]["data"]["task_id"], "w2")
        self.assertIn("w1 在写，第二个写入被拒", stdout)

    def test_shared_budget_is_never_exceeded_by_concurrent_workers(self):
        # AC8 end to end: two never-ending workers and the main agent share --max-steps 8.
        model = Duplex(main=[{"tool_calls": [delegation("d1", "A"), delegation("d2", "B")]}, {"content": "派出了两个"}],
                       by_task={"A": looping("a"), "B": looping("b")})
        code, stdout, stderr = self.invoke(["--session", "demo", "--max-steps", "8"], model, ["两个大任务", "/wait", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("派出了两个", stdout)
        main_state = self.store().load("demo")
        total = main_state.model_calls + self.worker("w1").model_calls + self.worker("w2").model_calls
        self.assertEqual(total, 8)
        self.assertEqual(main_state.status, "completed")
        self.assertEqual(self.worker("w1").max_model_calls, 6)
        self.assertIn("执行子 Agent（worker w1）", stderr)

    def test_turn_budget_admission_is_atomic(self):
        # AC8: N workers race for the last calls; dispatched calls equal the limit exactly.
        budget = TurnBudget(10, 100, reserve=0)
        layer = SharedBudgetMiddleware(budget, worker=True)
        barrier = threading.Barrier(6, timeout=WAIT)
        lock = threading.Lock()
        dispatched, refused = [], []

        def worker():
            state = SimpleNamespace(model_calls=0, prompt_tokens=0, completion_tokens=0, unknown_usage_calls=0, metadata={})

            def handler(request):
                state.model_calls += 1
                with lock:
                    dispatched.append(1)
                return {}

            barrier.wait()
            for _ in range(5):
                try:
                    layer.wrap_model_call(SimpleNamespace(state=state), handler)
                except BudgetExceeded as exc:
                    self.assertEqual(exc.status, RunStatus.MAX_MODEL_CALLS_EXCEEDED)
                    with lock:
                        refused.append(1)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT)
        self.assertEqual((len(dispatched), len(refused), budget.model_calls), (10, 20, 10))

    def test_reply_hold_keeps_one_call_for_the_main_agent(self):
        budget = TurnBudget(3, 3)
        self.assertIsNone(budget.admit_model(worker=False))
        self.assertEqual(budget.room(), 1)
        budget.hold_reply()
        self.assertIsNone(budget.admit_model(worker=True))
        self.assertEqual(budget.admit_model(worker=True), "max_model_calls")
        self.assertIsNone(budget.admit_model(worker=False))
        self.assertEqual(budget.model_calls, 3)

    def test_shared_budget_flag_does_not_outlive_its_run(self):
        state = State(messages=[], metadata={"shared_budget_exhausted": {"reason": "max_steps"}})
        SharedBudgetMiddleware(TurnBudget(4, 4), worker=False).before_agent(state)
        self.assertNotIn("shared_budget_exhausted", state.metadata)

    def test_budget_cny_is_shared_and_checked_before_dispatch(self):
        budget = TurnBudget(100, 100, budget_cny="0.001", reserve=0)
        main_layer = SharedBudgetMiddleware(budget, worker=False)
        worker_layer = SharedBudgetMiddleware(budget, worker=True)
        state = State(messages=[{"role": "system", "content": "x"}])

        def handler(request):
            state.model_calls += 1
            state.prompt_tokens += 1000  # 1000 * 0.8 / 1e6 = 0.0008 CNY
            return {}

        main_layer.wrap_model_call(SimpleNamespace(state=state), handler)
        worker_layer.wrap_model_call(SimpleNamespace(state=state), handler)  # 0.0016 >= 0.001 afterwards
        with self.assertRaises(BudgetExceeded):
            main_layer.wrap_model_call(SimpleNamespace(state=state), handler)
        self.assertEqual(state.metadata["shared_budget_exhausted"]["reason"], "budget_cny")
        self.assertEqual(budget.snapshot()["cost_cny"], "0.001600")
        self.assertEqual(budget.model_calls, 2)

    def test_budget_cny_counts_reported_usage_in_chat(self):
        usage = TokenUsage(1000, 0, 1000)
        model = Duplex(main=[ModelResponse(delegate("d1", "A"), usage), ModelResponse({"content": "ok"}, usage)],
                       by_task={"A": [ModelResponse({"content": "A done"}, usage)]})
        code, stdout, _ = self.invoke(["--session", "demo", "--budget-cny", "0.01"], model,
                                      ["一个问题", "/wait", "/status", "/exit"])
        self.assertEqual(code, 0)
        # Main dispatch + main reply + worker answer, 1000 input tokens each at 0.8 CNY per million.
        self.assertIn("CNY 0.002400/0.01", stdout)

    def test_cancel_stops_worker_and_kills_its_subprocess(self):
        # AC10: /cancel <id> stops the worker and terminates run_command's child process.
        pid_file = self.root / "sleeper.pid"
        script = f"import os, pathlib, time; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
        model = Duplex(main=[delegate("d1", "run the sleeper", allow_writes=True), {"content": "已派出 w1。"}],
                       worker=[{"tool_calls": [tool_call("c1", "run_command", {"argv": [sys.executable, "-c", script],
                                                                                "timeout": 60})]}])

        def cancel_when_running(prompt):
            deadline = time.monotonic() + WAIT
            while not pid_file.exists() or not pid_file.read_text():
                self.assertLess(time.monotonic(), deadline, "sleeper never started")
                time.sleep(0.01)
            return "/cancel w1"

        code, stdout, stderr = self.invoke(["--session", "demo", "--allow-command"], model,
                                           ["跑一个很慢的命令", cancel_when_running, "/status", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("w1 cancelled", stdout)
        self.assertIn("w1 cancelled 已取消", stdout)
        self.assertIn("被取消", stderr)
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(self.worker().status, "cancelled")
        self.assertEqual(self.registry()["w1"]["status"], "cancelled")

    def test_cancel_all_stops_every_active_worker(self):
        gates = {"A": Gate({"content": "never"}), "B": Gate({"content": "never"})}
        model = Duplex(main=[{"tool_calls": [delegation("d1", "A"), delegation("d2", "B")]}, {"content": "ok"}],
                       by_task={name: [gate] for name, gate in gates.items()})

        def cancel_all(prompt):
            for gate in gates.values():
                self.assertTrue(gate.entered.wait(WAIT))
            return "/cancel all"

        code, stdout, _ = self.invoke(["--session", "demo"], model, ["两个", cancel_all, "/status", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("w1 cancelled 已取消", stdout)
        self.assertIn("w2 cancelled 已取消", stdout)
        self.assertNotIn("还有", stdout)  # nothing left running, so /exit does not ask

    def test_tell_and_message_worker_inject_at_the_next_step(self):
        # AC11: /tell and message_worker reach a running worker; finished workers return an error.
        gate = Gate({"tool_calls": [tool_call("g1", "glob", {"pattern": "*.md"})]})
        seen = []

        def finish(messages):
            seen.append([message["content"] for message in messages if message["role"] == "user"])
            return {"content": "checked README and LICENSE"}

        def tell(prompt):
            self.assertTrue(gate.entered.wait(WAIT))
            return "/tell w1 also check LICENSE"

        def release(prompt):
            gate.release()
            return "/wait"

        model = Duplex(
            main=[delegate("d1", "check README"), {"content": "ok"},
                  {"tool_calls": [tool_call("m1", "message_worker", {"task_id": "w1", "text": "too late"})]},
                  {"content": "w1 已经结束了"}],
            worker=[gate, finish])
        code, stdout, stderr = self.invoke(["--session", "demo"], model,
                                           ["检查 README", "/tell w9 hi", tell, release, "再补充一点", "/tell w1 again",
                                            "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("unknown worker: w9", stderr)
        self.assertIn("queued for w1", stdout)
        self.assertEqual(seen[0][-1], "[主 Agent 转达的补充要求]\nalso check LICENSE")
        self.assertIn("worker w1 already finished (completed)", stderr)
        result = self.tool_results(self.store().load("demo"))[-1]
        self.assertFalse(result["ok"])
        self.assertIn("already finished", result["error"]["message"])

    def test_parallel_approvals_are_asked_in_order_and_answer_only_the_shown_call(self):
        # AC12/AC13: both workers park before the chat asks; y/N apply to the displayed call only.
        (self.root / "B.md").write_text("bee\n", encoding="utf-8")
        gates = {"A": Gate({"tool_calls": [tool_call("ra", "read_file", {"path": "README.md"})]}),
                 "B": Gate({"tool_calls": [tool_call("rb", "read_file", {"path": "B.md"})]})}
        model = Duplex(main=[{"tool_calls": [delegation("d1", "A"), delegation("d2", "B")]}, {"content": "ok"}],
                       by_task={"A": [gates["A"], {"content": "A finished"}], "B": [gates["B"], {"content": "B finished"}]})
        shown = []

        def answer(reply):
            def respond(prompt):
                shown.append(prompt.split("for worker ")[1].split("?")[0])
                return reply
            return respond

        def release_both(prompt):
            for gate in gates.values():
                gate.release()
            return "/wait"

        code, stdout, _ = self.invoke(["--session", "demo", "--tool-policy", self.policy(["read_file"])], model,
                                      ["读两个文件", release_both, "/wait", "/exit"],
                                      approvals=[answer("y"), answer("n")])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(shown), ["w1", "w2"])
        self.assertIn("(1 more pending; they are asked one at a time after this one)", stdout)
        first = stdout.index("Approval required: read_file")
        self.assertIn(f"requested by: worker {shown[0]}", stdout[first:first + 120])
        allowed, denied = shown
        for worker_id, expected in ((allowed, True), (denied, False)):
            worker = self.worker(worker_id)
            record = self.store().calls(f"demo-{worker_id}", worker.turn_id)[0]
            result = json.loads(record["result_json"])
            self.assertEqual(result["ok"], expected, worker_id)
            if not expected:
                self.assertEqual(result["error"]["code"], "permission_denied")
            self.assertEqual(worker.status, "completed")

    def test_approval_wait_blocks_neither_other_workers_nor_chat(self):
        # AC12: w1 is parked at an approval; the main agent still answers and w2 keeps working
        # while the approval prompt is on screen.
        gate = Gate({"tool_calls": [tool_call("gb", "glob", {"pattern": "*.md"})]})
        # w1 parks only after the first prompt, so its approval is asked after the "还在吗" turn.
        hold = Gate({"tool_calls": [tool_call("ra", "read_file", {"path": "README.md"})]})
        w2_done = threading.Event()

        def w2_finish(messages):
            w2_done.set()
            return {"content": "B finished"}

        model = Duplex(main=[{"tool_calls": [delegation("d1", "A"), delegation("d2", "B")]}, {"content": "ok"},
                             {"content": "我在，w1 等你审批。"}],
                       by_task={"A": [hold, {"content": "A finished"}], "B": [gate, w2_finish]})
        chat = iter(["两个", "还在吗", "/wait", "/exit"])
        approvals = []

        def read(prompt=""):
            if prompt.startswith("Allow "):
                approvals.append(prompt)
                self.assertIn("for worker w1", prompt)
                self.assertFalse(w2_done.is_set())
                gate.release()  # w2 runs to completion while this prompt is shown
                self.assertTrue(w2_done.wait(WAIT))
                return "y"
            item = next(chat)
            if item == "还在吗":
                hold.release()
                self.assertTrue(parked.wait(WAIT))
            return item

        with pushed("[w1 等待审批]") as parked:
            code, stdout, _ = self.invoke(["--session", "demo", "--tool-policy", self.policy(["read_file"])], model, read)
        self.assertEqual(code, 0)
        self.assertEqual(len(approvals), 1)
        self.assertLess(stdout.index("我在，w1 等你审批。"), stdout.index("Approval required: read_file"))
        self.assertEqual(self.store().load("demo-w1").status, "completed")
        self.assertIn("[w1 已完成] A finished", stdout)
        self.assertIn("[w2 已完成] B finished", stdout)


class LifecycleTests(MainAgentTestCase):
    def test_worker_status_survives_exit_and_resume(self):
        # AC14
        model = Duplex(main=[delegate("d1", "Read README.md"), {"content": "ok"}],
                       worker=[{"content": "read it. README.md:1-1"}])
        self.invoke(["--session", "demo"], model, ["读 README", "/wait", "/exit"])
        code, stdout, _ = self.invoke(["--session", "demo"], Duplex(), ["/status", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("w1 completed 已完成", stdout)
        self.assertNotIn("[w1 已完成]", stdout)  # already delivered; not pushed again
        record = self.registry()["w1"]
        self.assertEqual(record["report"]["citations"], ["README.md:1-1"])
        self.assertEqual(record["session_id"], "demo-w1")

    def test_exit_with_running_worker_asks_then_cancels_and_records(self):
        # AC15: "c" cancels and exits; nothing is lost silently.
        gate = Gate({"content": "never"})
        model = Duplex(main=[delegate("d1", "slow"), {"content": "ok"}], worker=[gate])

        def leave(prompt):
            self.assertTrue(gate.entered.wait(WAIT))
            return "/exit"

        code, stdout, _ = self.invoke(["--session", "demo"], model, ["慢任务", leave, "c"])
        self.assertEqual(code, 0)
        self.assertIn("还有 1 个 worker 在运行（w1）", stdout)
        self.assertEqual(self.registry()["w1"]["status"], "cancelled")
        self.assertEqual(self.worker().status, "cancelled")
        _, stdout, _ = self.invoke(["--session", "demo"], Duplex(), ["/status", "/exit"])
        self.assertIn("w1 cancelled 已取消", stdout)

    def test_exit_can_keep_waiting(self):
        gate = Gate({"content": "slow done"})
        model = Duplex(main=[delegate("d1", "slow"), {"content": "ok"}], worker=[gate])

        def leave(prompt):
            self.assertTrue(gate.entered.wait(WAIT))
            return "/exit"

        def keep_waiting(prompt):
            gate.release()
            return "w"

        code, stdout, _ = self.invoke(["--session", "demo"], model, ["慢任务", leave, keep_waiting, "/wait", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("继续等待", stdout)
        self.assertIn("[w1 已完成] slow done", stdout)
        self.assertEqual(self.registry()["w1"]["status"], "completed")

    def test_ctrl_c_at_the_prompt_records_running_workers_as_cancelled(self):
        gate = Gate({"content": "never"})
        model = Duplex(main=[delegate("d1", "slow"), {"content": "ok"}], worker=[gate])

        def interrupt(prompt):
            self.assertTrue(gate.entered.wait(WAIT))
            raise KeyboardInterrupt

        code, stdout, _ = self.invoke(["--session", "demo"], model, ["慢任务", interrupt])
        self.assertEqual(code, 0)
        self.assertIn("w1 cancelled", stdout)
        self.assertEqual(self.registry()["w1"]["status"], "cancelled")

    def test_eof_waits_for_running_workers_and_prints_their_pushes(self):
        gate = Gate({"content": "late answer"})
        model = Duplex(main=[delegate("d1", "slow"), {"content": "ok"}], worker=[gate])

        def end(prompt):
            gate.release()
            raise EOFError

        code, stdout, stderr = self.invoke(["--session", "demo"], model, ["慢任务", end])
        self.assertEqual(code, 0)
        self.assertIn("输入已结束：等待运行中的 worker 完成后退出", stderr)
        self.assertIn("[w1 已完成] late answer", stdout)
        self.assertEqual(self.registry()["w1"]["status"], "completed")

    def test_pushes_never_split_a_line_being_typed(self):
        # AC5: a worker finishes while the user is typing; its push waits for the next prompt.
        gate = Gate({"content": "pushed result"})
        model = Duplex(main=[delegate("d1", "slow"), {"content": "ok"}, {"content": "hello back"}], worker=[gate])
        def type_slowly(prompt):
            sys.stdout.write(prompt + "h")
            gate.release()
            self.assertTrue(posted.wait(WAIT))  # the push is posted mid-line ...
            sys.stdout.write("i\n")
            return "hi"

        def echo(text):
            def respond(prompt):
                sys.stdout.write(prompt + text + "\n")
                return text
            return respond

        with pushed("[w1 已完成]") as posted:
            code, stdout, _ = self.invoke(["--session", "demo"], model, [echo("慢任务"), type_slowly, echo("/exit")])
        self.assertEqual(code, 0)
        lines = stdout.splitlines()
        self.assertIn("> hi", lines)  # ... but the typed line stays whole
        push = next(index for index, line in enumerate(lines) if "[w1 已完成]" in line)
        self.assertGreater(push, lines.index("> hi"))
        self.assertGreater(push, lines.index("hello back"))
        self.assertTrue(lines[push].startswith("[w1 已完成] pushed result"))

    def test_live_terminal_prints_only_above_an_empty_prompt(self):
        out = io.StringIO()
        buffer = {"text": ""}
        line = PromptLine(out, live=True, line_buffer=lambda: buffer["text"])
        self.assertFalse(line.try_print("out", "[w1 已完成] x"))  # no prompt shown yet
        line._prompt = "> "
        self.assertTrue(line.try_print("out", "[w1 已完成] x"))
        self.assertEqual(out.getvalue(), "\r\x1b[K[w1 已完成] x\n> ")
        buffer["text"] = "half typed"
        self.assertFalse(line.try_print("out", "[w2 已完成] y"))
        self.assertFalse(PromptLine(io.StringIO()).live)


class ModeAndCompatibilityTests(MainAgentTestCase):
    def test_session_mode_is_recorded_in_metadata(self):
        # AC20
        self.invoke(["--session", "main"], Duplex([{"content": "hi"}]), ["hi", "/exit"])
        self.invoke(["--session", "single", "--no-main-agent"], Duplex(worker=[{"content": "hi"}]), ["hi", "/exit"])
        self.assertEqual(self.store().load("main").metadata["session_mode"], "main-agent")
        self.assertEqual(self.store().load("single").metadata["session_mode"], "single-agent")

    def test_mode_metadata_wins_and_old_sessions_fall_back_to_the_prompt(self):
        recorded = State(messages=[{"role": "system", "content": MAIN_AGENT_PROMPT}], metadata={"session_mode": "single-agent"})
        self.assertFalse(is_main_agent_session(recorded))
        recorded = State(messages=[{"role": "system", "content": "anything"}], metadata={"session_mode": "main-agent"})
        self.assertTrue(is_main_agent_session(recorded))
        for prompt, expected in ((MAIN_AGENT_PROMPT, True), (SEQUENTIAL_MAIN_AGENT_PROMPT, True), ("other", False)):
            self.assertEqual(is_main_agent_session(State(messages=[{"role": "system", "content": prompt}])), expected)
        self.assertFalse(is_main_agent_session(None))

    def test_old_main_agent_session_without_metadata_keeps_its_mode(self):
        store = self.store()
        state = State(messages=[{"role": "system", "content": SEQUENTIAL_MAIN_AGENT_PROMPT},
                                {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
                      session_id="old", workspace_root=str(self.root.resolve()), status=RunStatus.COMPLETED)
        store.save(state)
        model = Duplex([{"content": "still main"}])
        code, _, stderr = self.invoke(["--session", "old", "--no-main-agent"], model, ["hi again", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("keeps that mode", stderr)
        self.assertEqual(model.main_tools, [MAIN_TOOLS])

    def test_one_shot_prompt_is_refused_for_a_main_agent_session(self):
        # AC22
        self.invoke(["--session", "demo"], Duplex([{"content": "hi"}]), ["hi", "/exit"])
        model = Duplex()
        code, stdout, stderr = self.invoke(["--session", "demo", "summarize README"], model, [])
        self.assertEqual(code, 1)
        self.assertIn("is a main-agent chat session", stderr)
        self.assertIn("baseagent --session demo", stderr)
        self.assertEqual((model.main_seen, model.worker_seen), ([], []))

    def test_stop_without_error_text_explains_usage_and_how_to_continue(self):
        # AC23
        state = State(messages=[], session_id="demo", status=RunStatus.FAILED, step=3, max_steps=16,
                      model_calls=3, max_model_calls=16, tool_calls=2, max_tool_calls=32)
        text = stop_explanation(state, interactive=True)
        self.assertNotIn("no error detail was recorded", text)
        self.assertIn("used steps 3/16, model calls 3/16, tool calls 2/32", text)
        self.assertIn("baseagent --session demo --resume", text)

    def test_shared_budget_stop_is_named_in_the_limit_explanation(self):
        state = State(messages=[], session_id="demo", status=RunStatus.MAX_MODEL_CALLS_EXCEEDED, step=3, max_steps=16,
                      model_calls=3, max_model_calls=16, metadata={"shared_budget_exhausted": {
                          "reason": "max_model_calls", "model_calls": 16, "max_model_calls": 16, "steps": 16, "max_steps": 16}})
        self.assertIn("The shared turn budget ran out first (main agent + workers): model calls 16/16",
                      stop_explanation(state, interactive=True))

    def test_worker_and_budget_options_are_chat_only(self):
        for option in (["--max-workers", "3"], ["--budget-cny", "1"]):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                main(["--db", str(self.db), *option, "task"])
            self.assertIn("apply to main-agent chat", stderr.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--db", str(self.db), "--max-workers", "0"])


class OneShotStaysSingleAgentTests(MainAgentTestCase):
    def test_one_shot_prompt_uses_the_original_single_agent_path(self):
        model = Duplex(worker=[{"tool_calls": [tool_call("r1", "read_file", {"path": "README.md"})]}, {"content": "read it"}])
        code, stdout, _ = self.invoke(["--session", "demo", "summarize README"], model, [])
        self.assertEqual(code, 0)
        self.assertIn("read it", stdout)
        self.assertNotIn("delegate_task", model.worker_tools[0])
        self.assertTrue(WORKSPACE_TOOLS - {"edit_file", "write_file", "run_command"} <= set(model.worker_tools[0]))
        self.assertNotEqual(model.worker_seen[0][0]["content"], MAIN_AGENT_PROMPT)
        self.assertNotIn("session_mode", self.store().load("demo").metadata)
        self.assertEqual(self.store().task_tree("demo")["nodes"], [])

    def test_main_agent_flag_is_chat_only_and_excludes_named_subtasks(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(["--db", str(self.db), "--main-agent", "task"])
        self.assertIn("--main-agent applies to interactive chat", stderr.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--db", str(self.db), "--main-agent", "--subtasks-config", "x.json"])

    def test_no_main_agent_keeps_single_agent_chat(self):
        model = Duplex(worker=[{"content": "hello"}])
        self.invoke(["--session", "demo", "--no-main-agent"], model, ["hi", "/exit"])
        self.assertNotIn("delegate_task", model.worker_tools[0])
        self.assertNotEqual(model.worker_seen[0][0]["content"], MAIN_AGENT_PROMPT)


class DemoTests(unittest.TestCase):
    def run_script(self, name):
        import subprocess
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, "-B", str(root / "scripts" / name)],
                                   capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def test_offline_demo_runs(self):
        report = json.loads(self.run_script("demo_router.py").stdout)
        self.assertFalse(report["main_saw_tool_body"])
        self.assertEqual(report["main_agent_tools"], ["delegate_task"])
        self.assertEqual(report["cost_cny"], 0)

    def test_offline_duplex_demo_runs(self):
        # AC27
        completed = self.run_script("demo_duplex.py")
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(report["cost_cny"], 0)
        self.assertEqual(report["workers"], {"w1": "cancelled", "w2": "completed"})
        self.assertTrue(report["chatted_while_running"])


if __name__ == "__main__":
    unittest.main()
