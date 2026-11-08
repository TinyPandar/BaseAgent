"""Main conversation agent + one generic worker in continuous chat. Fake models, no provider calls."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baseagent.agent.router import MAIN_AGENT_PROMPT
from baseagent.kernel.errors import AuthError
from baseagent.main import main
from baseagent.session import SessionStore
from baseagent.tools.workspace import Workspace

WORKSPACE_TOOLS = {"read_file", "grep", "glob", "edit_file", "write_file", "run_command"}
SECRET = "SECRET_README_BODY_" + "x" * 3000


def tool_call(identifier, name, arguments):
    return {"id": identifier, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def delegate(identifier, task, allow_writes=None):
    arguments = {"task": task, **({"allow_writes": allow_writes} if allow_writes is not None else {})}
    return {"tool_calls": [tool_call(identifier, "delegate_task", arguments)]}


class Model:
    """One scripted model serves the main agent and the worker in call order."""

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
        if callable(response):
            return response(messages)
        return response


class LoopingWorker:
    """Main agent delegates once; the worker never finishes; then the main agent replies."""

    def __init__(self):
        self.seen, self.tools = [], []

    def complete(self, messages, tools):
        self.seen.append([dict(message) for message in messages])
        names = [tool["function"]["name"] for tool in tools]
        self.tools.append(names)
        if names == ["delegate_task"]:
            if messages[-1].get("role") == "tool":
                return {"content": "worker stopped; please narrow the task"}
            return delegate("d1", "list every file")
        index = len(self.seen)
        return {"tool_calls": [tool_call(f"g{index}", "glob", {"pattern": f"*{index}*"})]}


class MainAgentTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / ".state" / "sessions.sqlite3"
        (self.root / "README.md").write_text(SECRET + "\n", encoding="utf-8")

    def invoke(self, argv, model, inputs):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=inputs), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--root", str(self.root), "--db", str(self.db), *argv], model_factory=lambda args: model)
        return code, stdout.getvalue(), stderr.getvalue()

    def store(self):
        return SessionStore(self.db)

    def nodes(self, session="demo"):
        return self.store().task_tree(session)["nodes"]

    def root_calls(self, state):
        return [item["function"]["name"] for message in state.messages if message.get("role") == "assistant"
                for item in message.get("tool_calls") or []]

    def write_request(self):
        instructions = Workspace(self.root).get_instructions("out.txt")["digest"]
        return {"path": "out.txt", "content": "hello", "expected_sha256": "missing",
                "expected_instructions": instructions}

    def policy(self):
        path = self.root / "policy.json"
        path.write_text(json.dumps({"default": "allow", "tools": {"write_file": "ask"}}), encoding="utf-8")
        return str(path)


class MainAgentChatTests(MainAgentTestCase):
    def test_small_talk_is_answered_without_a_worker(self):
        model = Model([{"content": "hello!"}])
        code, stdout, _ = self.invoke(["--session", "demo"], model, ["hi", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("hello!", stdout)
        self.assertEqual(model.tools, [["delegate_task"]])
        self.assertEqual(model.seen[0][0]["content"], MAIN_AGENT_PROMPT)
        self.assertEqual(self.nodes(), [])
        self.assertEqual(self.root_calls(self.store().load("demo")), [])

    def test_read_request_delegates_one_generic_read_only_worker(self):
        model = Model([
            delegate("d1", "Read README.md and summarize it"),
            {"tool_calls": [tool_call("r1", "read_file", {"path": "README.md"})]},
            {"content": "README.md holds a long marker line. README.md:1-1"},
            {"content": "Summary: the README is one long marker line."},
        ])
        code, stdout, _ = self.invoke(["--session", "demo"], model, ["读一下 README 并总结", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("Summary: the README is one long marker line.", stdout)
        state = self.store().load("demo")
        self.assertEqual(state.status, "completed")
        # The main agent only delegated; the worker did the read.
        self.assertEqual(self.root_calls(state), ["delegate_task"])
        self.assertEqual(model.tools[0], ["delegate_task"])
        worker_tools = set(model.tools[1])
        self.assertIn("read_file", worker_tools)
        self.assertTrue(worker_tools.isdisjoint({"write_file", "edit_file", "run_command", "delegate_task", "run_subtask"}))
        nodes = self.nodes()
        self.assertEqual([node["name"] for node in nodes], ["worker"])
        self.assertNotIn(nodes[0]["name"], {"qa", "coding", "reader"})

    def test_worker_has_entry_tools_and_nothing_more(self):
        one_shot = Model([{"content": "done"}])
        self.invoke(["--allow-write", "--allow-command", "--session", "single", "task"], one_shot, AssertionError("no chat"))
        entry_tools = one_shot.tools[0]
        model = Model([
            delegate("d1", "Fix sample.py and run the tests", allow_writes=True),
            {"content": "nothing to fix"},
            {"content": "The worker found nothing to fix."},
        ])
        self.invoke(["--allow-write", "--allow-command", "--session", "demo"], model, ["修改 sample.py 并跑测试", "/exit"])
        self.assertEqual(model.tools[0], ["delegate_task"])
        self.assertEqual(sorted(model.tools[1]), sorted(entry_tools))
        self.assertNotIn("delegate_task", entry_tools)
        # Read-only delegation is a tightened subset of the same entry tools.
        readonly = Model([delegate("d2", "look around"), {"content": "looked"}, {"content": "ok"}])
        self.invoke(["--allow-write", "--allow-command", "--session", "demo2"], readonly, ["look around the repo", "/exit"])
        self.assertTrue(set(readonly.tools[1]) < set(entry_tools))

    def test_worker_context_is_isolated_and_parent_only_gets_references(self):
        model = Model([
            delegate("d1", "Read README.md and summarize it"),
            {"tool_calls": [tool_call("r1", "read_file", {"path": "README.md"})]},
            {"content": "One marker line. README.md:1-1"},
            {"content": "It is one marker line."},
        ])
        self.invoke(["--session", "demo"], model, ["读一下 README 并总结", "/exit"])
        state = self.store().load("demo")
        root_text = json.dumps(state.messages, ensure_ascii=False)
        self.assertNotIn(SECRET, root_text)
        for messages in (model.seen[0], model.seen[3]):
            self.assertNotIn(SECRET, json.dumps(messages, ensure_ascii=False))
        self.assertIn(SECRET[:40], json.dumps(model.seen[2], ensure_ascii=False))
        result = json.loads(next(message["content"] for message in state.messages if message.get("role") == "tool"))
        self.assertEqual(set(result["data"]), {"task_id", "status", "summary", "summary_truncated", "citations",
                                               "paths", "call_ids", "spill_refs"})
        self.assertEqual(result["data"]["paths"], ["README.md"])
        self.assertEqual(result["data"]["call_ids"], ["r1"])
        self.assertEqual(result["data"]["citations"], ["README.md:1-1"])
        self.assertEqual(result["data"]["summary"], "One marker line. README.md:1-1")
        # The full tool output stays in the child node's own ledger.
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["--db", str(self.db), "--session", "demo", "--task-tree"]), 0)
        tree = json.loads(stdout.getvalue())
        node = tree["nodes"][0]
        self.assertNotEqual(node["node_turn_id"], tree["root_turn_id"])
        self.assertIn(SECRET[:40], json.dumps(node["tool_calls"]))
        root_ledger = self.store().calls("demo", state.turn_id)
        self.assertNotIn(SECRET[:40], json.dumps(root_ledger))

    def test_worker_write_waits_for_chat_approval_and_delegate_is_charged_once(self):
        def answer(prompt):
            self.assertFalse((self.root / "out.txt").exists(), "file must not change before approval")
            return "y"

        model = Model([
            delegate("d1", "Create out.txt with hello", allow_writes=True),
            {"tool_calls": [tool_call("w1", "write_file", self.write_request())]},
            {"content": "Created out.txt."},
            {"content": "Done: out.txt was created."},
        ])
        inputs = iter(["create out.txt containing hello", answer, "/exit"])

        def feed(prompt=""):
            item = next(inputs)
            return item(prompt) if callable(item) else item

        code, stdout, _ = self.invoke(["--session", "demo", "--allow-write", "--tool-policy", self.policy()], model, feed)
        self.assertEqual(code, 0)
        self.assertIn("Approval required: write_file", stdout)
        self.assertIn("requested by: worker", stdout)
        self.assertIn("Tool call allowed.", stdout)
        self.assertIn("Done: out.txt was created.", stdout)
        self.assertEqual((self.root / "out.txt").read_text(), "hello")
        state = self.store().load("demo")
        self.assertEqual(state.status, "completed")
        self.assertEqual(state.metadata["direct_usage"]["tool_calls"], 1)
        delegate_records = [record for record in self.store().calls("demo", state.turn_id)]
        self.assertEqual([record["call_id"] for record in delegate_records], ["d1"])

    def test_worker_write_denial_leaves_file_untouched(self):
        model = Model([
            delegate("d1", "Create out.txt", allow_writes=True),
            {"tool_calls": [tool_call("w1", "write_file", self.write_request())]},
            {"content": "Write was denied."},
            {"content": "The write was denied; nothing changed."},
        ])
        code, stdout, _ = self.invoke(["--session", "demo", "--allow-write", "--tool-policy", self.policy()], model,
                                      ["create out.txt", "n", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("Tool call denied.", stdout)
        self.assertFalse((self.root / "out.txt").exists())

    def test_pending_worker_approval_survives_restart_without_task_id(self):
        model = Model([
            delegate("d1", "Create out.txt", allow_writes=True),
            {"tool_calls": [tool_call("w1", "write_file", self.write_request())]},
        ])
        options = ["--session", "demo", "--allow-write", "--tool-policy", self.policy()]
        code, stdout, _ = self.invoke(options, model, ["create out.txt", EOFError(), "/status", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("等待审批", stdout)
        self.assertFalse((self.root / "out.txt").exists())
        resumed = Model([{"content": "Created out.txt."}, {"content": "Done."}])
        code, stdout, _ = self.invoke(options, resumed, ["y", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("Approval required: write_file", stdout)
        self.assertEqual((self.root / "out.txt").read_text(), "hello")
        self.assertEqual(self.store().load("demo").status, "completed")

    def test_unclear_request_is_clarified_and_never_writes_by_default(self):
        model = Model([{"content": "你是想让我解释这个文件，还是修改它？"}])
        code, stdout, _ = self.invoke(["--session", "demo", "--allow-write"], model,
                                      ["能不能帮我处理一下这个文件", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("还是修改它", stdout)
        self.assertEqual(self.nodes(), [])
        self.assertIn("ask one short clarifying question", MAIN_AGENT_PROMPT)
        # Even if the main agent delegates, writes are off unless explicitly requested.
        model = Model([
            delegate("d1", "handle the file"),
            {"tool_calls": [tool_call("w1", "write_file", self.write_request())]},
            {"content": "write_file is not available"},
            {"content": "I only looked at it."},
        ])
        self.invoke(["--session", "demo2", "--allow-write"], model, ["能不能帮我处理一下这个文件", "/exit"])
        self.assertNotIn("write_file", model.tools[1])
        self.assertFalse((self.root / "out.txt").exists())

    def test_worker_limit_is_reported_in_chinese_and_chat_stays_usable(self):
        model = LoopingWorker()
        code, stdout, stderr = self.invoke(["--session", "demo", "--max-steps", "6"], model,
                                           ["list every file", "/status", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("执行子 Agent（worker", stderr)
        self.assertIn("用完了自己的", stderr)
        self.assertIn("可以在对话里", stderr)
        self.assertNotIn("no error detail was recorded", stderr)
        self.assertIn("worker stopped; please narrow the task", stdout)
        self.assertIn("已结束（失败）", stdout)
        state = self.store().load("demo")
        self.assertEqual(state.status, "completed")
        node = self.nodes()[0]
        # Six model calls: one dispatch, the reserved reply, and four for the worker.
        self.assertEqual(node["state"]["max_model_calls"], 4)
        self.assertEqual(state.model_calls, 6)
        result = json.loads(next(message["content"] for message in state.messages if message.get("role") == "tool"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["status"], node["state"]["status"])

    def test_no_worker_without_room_for_the_main_reply(self):
        model = Model([delegate("d1", "Read README.md"), {"content": "Not enough budget left for a worker."}])
        code, stdout, _ = self.invoke(["--session", "demo", "--max-steps", "2"], model, ["读一下 README", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("Not enough budget left for a worker.", stdout)
        self.assertEqual(self.nodes(), [])
        state = self.store().load("demo")
        result = json.loads(next(message["content"] for message in state.messages if message.get("role") == "tool"))
        self.assertEqual(result["error"]["code"], "limit_exceeded")
        self.assertEqual((state.status, state.model_calls), ("completed", 2))

    def test_worker_failure_explains_and_retries_in_chat(self):
        model = Model([
            delegate("d1", "Read README.md"),
            AuthError("invalid key", status_code=401),
            {"content": "README.md read. README.md:1-1"},
            {"content": "Retried: README read."},
            {"content": "second answer"},
        ])
        code, stdout, stderr = self.invoke(["--session", "demo"], model,
                                           ["读一下 README", "/status", "again please", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("执行子 Agent（worker", stderr)
        self.assertIn("模型或工具出错", stderr)
        self.assertIn("可以在对话里重试", stderr)
        self.assertNotIn("no error detail was recorded", stderr)
        self.assertIn("失败（failed）", stdout)
        self.assertIn("Retried: README read.", stdout)
        self.assertEqual(len(self.nodes()), 0)  # the new turn has no worker yet
        self.assertEqual(self.store().load("demo").status, "completed")

    def test_status_shows_completed_worker(self):
        model = Model([delegate("d1", "Read README.md"), {"content": "read"}, {"content": "done"}])
        _, stdout, _ = self.invoke(["--session", "demo"], model, ["读一下 README", "/status", "/exit"])
        self.assertIn("Worker: ", stdout)
        self.assertIn("已完成", stdout)
        model = Model([{"content": "hello"}])
        _, stdout, _ = self.invoke(["--session", "chat2"], model, ["hi", "/status", "/exit"])
        self.assertIn("Worker: none in this turn", stdout)


class OneShotStaysSingleAgentTests(MainAgentTestCase):
    def test_one_shot_prompt_uses_the_original_single_agent_path(self):
        model = Model([{"tool_calls": [tool_call("r1", "read_file", {"path": "README.md"})]}, {"content": "read it"}])
        code, stdout, _ = self.invoke(["--session", "demo", "summarize README"], model, AssertionError("no chat"))
        self.assertEqual(code, 0)
        self.assertIn("read it", stdout)
        self.assertNotIn("delegate_task", model.tools[0])
        self.assertTrue(WORKSPACE_TOOLS - {"edit_file", "write_file", "run_command"} <= set(model.tools[0]))
        self.assertNotEqual(model.seen[0][0]["content"], MAIN_AGENT_PROMPT)
        self.assertEqual(self.nodes(), [])

    def test_main_agent_flag_is_chat_only_and_excludes_named_subtasks(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(["--db", str(self.db), "--main-agent", "task"])
        self.assertIn("--main-agent applies to interactive chat", stderr.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--db", str(self.db), "--main-agent", "--subtasks-config", "x.json"])

    def test_no_main_agent_keeps_single_agent_chat(self):
        model = Model([{"content": "hello"}])
        self.invoke(["--session", "demo", "--no-main-agent"], model, ["hi", "/exit"])
        self.assertNotIn("delegate_task", model.tools[0])
        self.assertNotEqual(model.seen[0][0]["content"], MAIN_AGENT_PROMPT)


class DemoTests(unittest.TestCase):
    def test_offline_demo_runs(self):
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, "-B", str(root / "scripts" / "demo_router.py")],
                                   capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["main_saw_tool_body"])
        self.assertEqual(report["main_agent_tools"], ["delegate_task"])
        self.assertEqual(report["cost_cny"], 0)


if __name__ == "__main__":
    unittest.main()
