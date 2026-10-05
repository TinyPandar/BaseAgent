import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from baseagent.agent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.middleware import AgentMiddleware
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.result import ErrorCode, ToolFailure, ToolResult
from baseagent.tools.workspace import Workspace, coding_tools


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append((list(messages), tools))
        return next(self.responses)


def call(name, arguments, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class AgentTests(unittest.TestCase):
    def test_library_call_limits_reject_nonintegers_before_model_dispatch(self):
        for name in ("max_steps", "max_model_calls", "max_tool_calls"):
            for value in (True, False, 1.5, float("nan"), float("inf"), float("-inf"), "2", -1):
                model = FakeModel([{"content": "never"}])
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    run_agent(model, "task", tools=ToolRegistry(), **{name: value})
                self.assertEqual(model.seen, [])

    def test_invalid_resume_limit_preserves_checkpoint_and_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")
            state = run_agent(FakeModel([{"tool_calls": [call("missing", {})]}]), "task",
                              tools=ToolRegistry(), store=store, session_id="limits", max_steps=1)
            before, events = state.to_dict(), store.events("limits")
            for name in ("max_steps", "max_model_calls", "max_tool_calls"):
                for value in (True, 1.5, float("nan"), float("inf")):
                    model = FakeModel([{"content": "never"}])
                    with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                        run_agent(model, tools=ToolRegistry(), store=store, session_id="limits", **{name: value})
                    self.assertEqual(model.seen, [])
                    self.assertEqual(store.load("limits").to_dict(), before)
                    self.assertEqual(store.events("limits"), events)

    def test_completion(self):
        state = run_agent(FakeModel([{"content": "done"}]), "task", tools=ToolRegistry())
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.final_answer, "done")
        self.assertEqual(state.step, 1)

    def test_tool_result_is_returned_to_model(self):
        tools = ToolRegistry()
        tools.register("add", "Add two numbers", {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}, lambda a, b: a + b)
        model = FakeModel([
            {"tool_calls": [call("add", {"a": 2, "b": 3})]},
            {"content": "5"},
        ])
        state = run_agent(model, "add", tools=tools)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(json.loads(model.seen[1][0][-1]["content"])["data"], 5)
        self.assertEqual(state.step, 2)

    def test_limits_and_model_error(self):
        looping = FakeModel([{"tool_calls": [call("missing", {})]}])
        state = run_agent(looping, "task", tools=ToolRegistry(), max_steps=1)
        self.assertEqual(state.status, RunStatus.MAX_STEPS_EXCEEDED)
        limited = FakeModel([{"tool_calls": [call("missing", {})]}])
        self.assertEqual(run_agent(limited, "task", tools=ToolRegistry(), max_tool_calls=0).status, RunStatus.MAX_TOOL_CALLS_EXCEEDED)
        failed = run_agent(FakeModel([]), "task", tools=ToolRegistry())
        self.assertEqual(failed.status, RunStatus.FAILED)
        self.assertIn("StopIteration", failed.error)

    def test_wrap_order_and_short_circuit(self):
        events = []

        class Layer(AgentMiddleware):
            def __init__(self, name):
                self.name = name

            def wrap_model_call(self, request, handler):
                events.append(f"{self.name}:model:before")
                result = handler(request)
                events.append(f"{self.name}:model:after")
                return result

            def wrap_tool_call(self, request, handler):
                events.append(f"{self.name}:tool:before")
                result = handler(request)
                events.append(f"{self.name}:tool:after")
                return result

        tools = ToolRegistry()
        tools.register("ping", "Ping", {"type": "object"}, lambda: events.append("tool") or "pong")
        model = FakeModel([{"tool_calls": [call("ping", {})]}, {"content": "done"}])
        state = run_agent(model, "task", tools=tools, middleware=[Layer("A"), Layer("B")])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(events[4:9], ["A:tool:before", "B:tool:before", "tool", "B:tool:after", "A:tool:after"])
        self.assertEqual(events[:4], ["A:model:before", "B:model:before", "B:model:after", "A:model:after"])

        class Deny(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                return ToolResult.failure(ErrorCode.PERMISSION_DENIED, "denied")

        denied = FakeModel([{"tool_calls": [call("ping", {})]}, {"content": "blocked"}])
        events.clear()
        state = run_agent(denied, "task", tools=tools, middleware=[Deny()])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(events, [])
        self.assertEqual(json.loads(denied.seen[1][0][-1]["content"])["error"]["code"], "permission_denied")
        self.assertEqual(state.tool_calls, 0)

    def test_wrap_can_replace_tool_request(self):
        class Redirect(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                return handler(replace(request, name="safe", arguments='{"value": 7}'))

        tools = ToolRegistry()
        tools.register("safe", "Safe", {"type": "object", "properties": {"value": {"type": "integer"}}}, lambda value: value)
        model = FakeModel([{"tool_calls": [call("other", {"value": 1})]}, {"content": "done"}])
        state = run_agent(model, "task", tools=tools, middleware=[Redirect()])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(json.loads(model.seen[1][0][-1]["content"])["data"], 7)

    def test_model_wrap_can_short_circuit_and_after_agent_runs(self):
        class Cached(AgentMiddleware):
            def __init__(self):
                self.finished = None

            def wrap_model_call(self, request, handler):
                return {"content": "cached"}

            def after_agent(self, state):
                self.finished = state.status

        layer = Cached()
        model = FakeModel([])
        state = run_agent(model, "task", tools=ToolRegistry(), middleware=[layer])
        self.assertEqual(state.final_answer, "cached")
        self.assertEqual(model.seen, [])
        self.assertEqual(layer.finished, RunStatus.COMPLETED)

    def test_node_hook_order_matches_nested_layers(self):
        events = []

        class Layer(AgentMiddleware):
            def __init__(self, name):
                self.name = name

            def before_agent(self, state):
                events.append(f"{self.name}:before_agent")

            def before_model(self, state):
                events.append(f"{self.name}:before_model")

            def after_model(self, state, message):
                events.append(f"{self.name}:after_model")

            def after_agent(self, state):
                events.append(f"{self.name}:after_agent")

        state = run_agent(FakeModel([{"content": "done"}]), "task", tools=ToolRegistry(), middleware=[Layer("A"), Layer("B")])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(events, [
            "A:before_agent", "B:before_agent",
            "A:before_model", "B:before_model",
            "B:after_model", "A:after_model",
            "B:after_agent", "A:after_agent",
        ])


class WorkspaceTests(unittest.TestCase):
    def test_read_write_diff_and_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            tools = coding_tools(workspace)
            self.assertEqual(tools.execute("write_file", {"path": "x.txt", "content": "hello", "expected_sha256": "missing", "expected_instructions": workspace.get_instructions("x.txt")["digest"]}).error.code, ErrorCode.PERMISSION_DENIED)
            self.assertEqual(tools.execute("read_file", {"path": "../outside.txt"}).error.code, ErrorCode.PERMISSION_DENIED)
            self.assertEqual(tools.execute("read_file", {"path": ".env"}).error.code, ErrorCode.PERMISSION_DENIED)
            self.assertEqual(tools.execute("run_command", {"argv": ["python", "--version"]}).error.code, ErrorCode.PERMISSION_DENIED)
            writable = Workspace(root, allow_write=True)
            self.assertEqual(writable.write_file("sub/x.txt", "hello")["bytes"], 5)
            self.assertEqual(writable.read_file("sub/x.txt")["content"], "hello")
            self.assertEqual(writable.search_files("hello")["matches"][0]["line"], 1)
            with self.assertRaises(ToolFailure):
                writable.search_files("hello", "../**/*")
            runnable = Workspace(root, allow_command=True)
            command = runnable.run_command([sys.executable, "-c", "print('verified')"])
            self.assertTrue(command.ok)
            self.assertEqual(command.data["exit_code"], 0)
            self.assertEqual(command.data["stdout"].strip(), "verified")


if __name__ == "__main__":
    unittest.main()
