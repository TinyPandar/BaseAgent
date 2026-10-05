import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baseagent.agent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.middleware import AgentMiddleware
from baseagent.tools.process import run_process
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.result import ErrorCode, ToolFailure


INTEGER_SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}


class RegistryTests(unittest.TestCase):
    def test_direct_library_nonfinite_or_non_json_arguments_never_execute(self):
        executed = []
        tools = ToolRegistry()
        tools.register("record", "Record", {"type": "object", "properties": {"value": {}}, "required": ["value"]}, lambda value: executed.append(value))
        recursive = {}
        recursive["cycle"] = recursive
        for value in [float("nan"), float("inf"), float("-inf"), object(), {1, 2}, recursive, {"nested": [float("nan")]}]:
            result = tools.execute("record", {"value": value})
            self.assertEqual(result.error.code, ErrorCode.INVALID_ARGUMENTS)
        self.assertEqual(executed, [])
        self.assertTrue(tools.execute("record", {"value": 1.25}).ok)
        self.assertEqual(executed, [1.25])

    def test_bad_arguments_cannot_reach_handler(self):
        executed = []
        tools = ToolRegistry()
        tools.register("record", "Record", INTEGER_SCHEMA, lambda value: executed.append(value))
        for arguments in [{}, {"value": "7"}, {"value": True}, {"value": 7, "extra": 1}, [7]]:
            with self.subTest(arguments=arguments):
                self.assertEqual(tools.execute("record", arguments).error.code, ErrorCode.INVALID_ARGUMENTS)
        self.assertEqual(executed, [])
        self.assertTrue(tools.execute("record", {"value": 7}).ok)
        self.assertEqual(executed, [7])

    def test_nested_schema_and_schema_copy(self):
        schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "integer"}}}, "required": ["items"]}
        tools = ToolRegistry()
        tools.register("sum", "Sum", schema, lambda items: sum(items))
        schema["properties"].clear()
        tools.specs()[0]["function"]["parameters"]["properties"].clear()
        self.assertEqual(tools.execute("sum", {"items": [1, "2"]}).error.code, ErrorCode.INVALID_ARGUMENTS)
        self.assertEqual(tools.execute("sum", {"items": [1, 2]}).data, 3)

    def test_errors_remain_distinct_and_nonretryable_by_default(self):
        tools = ToolRegistry()

        def denied():
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "disabled")

        def broken():
            raise RuntimeError("implementation error")

        tools.register("denied", "Denied", {"type": "object"}, denied)
        tools.register("broken", "Broken", {"type": "object"}, broken)
        tools.register("invalid", "Invalid", {"type": "object"}, lambda: object())
        self.assertEqual(tools.execute("missing", {}).error.code, ErrorCode.UNKNOWN_TOOL)
        self.assertEqual(tools.execute("denied", {}).error.code, ErrorCode.PERMISSION_DENIED)
        failure = tools.execute("broken", {})
        self.assertEqual(failure.error.code, ErrorCode.EXECUTION_FAILED)
        self.assertFalse(failure.error.retryable)
        self.assertEqual(tools.execute("invalid", {}).error.code, ErrorCode.INVALID_RESULT)

    def test_large_results_do_not_enter_transcript(self):
        tools = ToolRegistry(max_result_bytes=100)
        tools.register("large", "Large", {"type": "object"}, lambda: "x" * 1000)
        self.assertEqual(tools.execute("large", {}).error.code, ErrorCode.LIMIT_EXCEEDED)

    def test_excessively_nested_results_return_error_without_crashing_run(self):
        value = []
        # Python 3.12's C JSON encoder can exceed Python's recursion limit.
        for _ in range(10_000):
            value = [value]
        with self.assertRaises(RecursionError):
            json.dumps(value)
        tools = ToolRegistry()
        tools.register("nested", "Nested", {"type": "object"}, lambda: value)
        self.assertEqual(tools.execute("nested", {}).error.code, ErrorCode.INVALID_RESULT)


class BudgetTests(unittest.TestCase):
    def test_retry_consumes_model_budget_even_when_first_call_fails(self):
        class BrokenModel:
            calls = 0

            def complete(self, messages, tools):
                self.calls += 1
                raise ConnectionError("temporary failure")

        class Retry(AgentMiddleware):
            def wrap_model_call(self, request, handler):
                for _ in range(3):
                    try:
                        return handler(request)
                    except ConnectionError:
                        continue

        model = BrokenModel()
        state = run_agent(model, "task", tools=ToolRegistry(), middleware=[Retry()], max_model_calls=1)
        self.assertEqual(state.status, RunStatus.MAX_MODEL_CALLS_EXCEEDED)
        self.assertEqual(state.model_calls, 1)
        self.assertEqual(model.calls, 1)

    def test_tool_budget_blocks_duplicate_side_effect_from_wrapper(self):
        executed = []
        tools = ToolRegistry()
        tools.register("write", "Write", {"type": "object"}, lambda: executed.append("written"))

        class Model:
            def complete(self, messages, tools):
                return {"tool_calls": [{"id": "c1", "function": {"name": "write", "arguments": "{}"}}]}

        class Twice(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                handler(request)
                return handler(request)

        state = run_agent(Model(), "task", tools=tools, middleware=[Twice()], max_tool_calls=1)
        self.assertEqual(state.status, RunStatus.MAX_TOOL_CALLS_EXCEEDED)
        self.assertEqual(state.tool_calls, 1)
        self.assertEqual(executed, ["written"])

    def test_invalid_json_yields_structured_error_without_execution(self):
        class Model:
            def complete(self, messages, tools):
                return {"tool_calls": [{"id": "c1", "function": {"name": "record", "arguments": self.arguments}}]}

        executed = []
        tools = ToolRegistry()
        tools.register("record", "Record", INTEGER_SCHEMA, lambda value: executed.append(value))
        deeply_nested = '{"value":' + '[' * 10_000 + '0' + ']' * 10_000 + '}'
        for arguments in ['{"value": NaN}', '{"value": 1e999}', '{"value": 1, "value": 2}', '[1]', '{', deeply_nested]:
            model = Model()
            model.arguments = arguments
            state = run_agent(model, "task", tools=tools, max_steps=1)
            self.assertEqual(json.loads(state.messages[-1]["content"])["error"]["code"], "invalid_arguments")
        self.assertEqual(executed, [])


class ProcessTests(unittest.TestCase):
    def test_invalid_runtime_limits_refuse_before_process_or_job_creation(self):
        with patch("baseagent.tools.process.subprocess.Popen") as launch, patch("baseagent.tools._windows_job.WindowsJob") as job:
            for timeout in [True, 0, -1, float("nan"), float("inf"), float("-inf"), "1", None]:
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    run_process([sys.executable, "-c", "raise SystemExit(1)"], Path.cwd(), timeout=timeout)
            for output_limit in [True, 0, -1, 1.5, float("nan"), float("inf"), "100", None]:
                with self.subTest(output_limit=output_limit), self.assertRaises(ValueError):
                    run_process([sys.executable, "-c", "raise SystemExit(1)"], Path.cwd(), timeout=1, output_limit=output_limit)
            launch.assert_not_called()
            job.assert_not_called()

    def test_output_is_bounded_and_both_pipes_are_drained(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_process([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*500000); sys.stderr.buffer.write(b'y'*500000)"], Path(directory), timeout=10, output_limit=1024)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["stdout"]), 1024)
        self.assertEqual(len(result.data["stderr"]), 1024)
        self.assertTrue(result.data["stdout_truncated"])
        self.assertTrue(result.data["stderr_truncated"])

    def test_nonzero_exit_and_timeout_are_failures_with_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failed = run_process([sys.executable, "-c", "print('failure evidence'); raise SystemExit(3)"], root, timeout=10)
            self.assertEqual(failed.error.code, ErrorCode.EXECUTION_FAILED)
            self.assertEqual(failed.data["exit_code"], 3)
            timed_out = run_process([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(20)"], root, timeout=0.2)
            self.assertEqual(timed_out.error.code, ErrorCode.TIMEOUT)
            self.assertIn("started", timed_out.data["stdout"])

    def test_timeout_stops_spawned_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # The child would write evidence after the parent's timeout.
            child = "import time; from pathlib import Path; time.sleep(1.5); Path('leaked.txt').write_text('leaked')"
            parent = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', " + repr(child) + "]); time.sleep(20)"
            result = run_process([sys.executable, "-c", parent], root, timeout=0.3)
            self.assertEqual(result.error.code, ErrorCode.TIMEOUT)
            # A subsequent process waits long enough to expose a surviving child.
            run_process([sys.executable, "-c", "import time; time.sleep(1.7)"], root, timeout=5)
            self.assertFalse((root / "leaked.txt").exists())

    def test_normal_completion_also_cleans_background_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = "import time; from pathlib import Path; time.sleep(1); Path('leaked.txt').write_text('leaked')"
            parent = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', " + repr(child) + "])"
            result = run_process([sys.executable, "-c", parent], root, timeout=5)
            self.assertTrue(result.ok)
            run_process([sys.executable, "-c", "import time; time.sleep(1.2)"], root, timeout=5)
            self.assertFalse((root / "leaked.txt").exists())

    def test_interrupt_cleans_process_before_propagating(self):
        import subprocess
        original_wait = subprocess.Popen.wait
        interrupted = []

        def interrupt_once(process, *args, **kwargs):
            if not interrupted:
                interrupted.append(process)
                raise KeyboardInterrupt()
            return original_wait(process, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch.object(subprocess.Popen, "wait", interrupt_once):
            with self.assertRaises(KeyboardInterrupt):
                run_process([sys.executable, "-c", "import time; time.sleep(20)"], Path(directory), timeout=5)
        self.assertIsNotNone(interrupted[0].poll())


if __name__ == "__main__":
    unittest.main()
