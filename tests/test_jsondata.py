import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.jsondata import object_from_json, read_json_object
from baseagent.main import main
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry


class JsonDataTests(unittest.TestCase):
    def test_strict_object_rejects_duplicates_overflow_and_nesting(self):
        deep = '{"value":' + '[' * 10_000 + '0' + ']' * 10_000 + '}'
        for text in ('[]', '{', '{"x":1,"x":2}', '{"x":{"y":1,"y":2}}',
                     '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', deep,
                     '{"x":"\\ud800"}'):
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                object_from_json(text, max_bytes=32000)
        self.assertEqual(object_from_json('{"x":"中文🙂","n":1.25}', max_bytes=100), {"x": "中文🙂", "n": 1.25})

    def test_size_is_actual_utf8_and_read_is_bounded_without_stat(self):
        text = '{"x":"中"}'
        size = len(text.encode("utf-8"))
        self.assertEqual(object_from_json(text, max_bytes=size), {"x": "中"})
        with self.assertRaises(ValueError):
            object_from_json(text, max_bytes=size - 1)

        class GrowingFile(io.BytesIO):
            def read(self, count=-1):
                self.requested = count
                return super().read(count)

        stream = GrowingFile(b'x' * 100000)
        with patch.object(Path, "open", return_value=stream), patch.object(Path, "stat", side_effect=AssertionError("stat cannot bound a later read")):
            with self.assertRaises(ValueError):
                read_json_object("growing.json", max_bytes=4096)
        self.assertEqual(stream.requested, 4097)

        with patch.object(Path, "open") as opened:
            for invalid in (True, 0, -1, 1.5, float("nan"), float("inf"), None):
                with self.subTest(limit=invalid), self.assertRaises(ValueError):
                    read_json_object("input.json", max_bytes=invalid)
                with self.assertRaises(ValueError):
                    object_from_json("{}", max_bytes=invalid)
            opened.assert_not_called()

    def test_file_bom_supported_invalid_utf8_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_bytes(b'\xef\xbb\xbf{"ok":true}')
            self.assertEqual(read_json_object(path, max_bytes=100), {"ok": True})
            path.write_bytes(b'{"x":"\xff"}')
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                read_json_object(path, max_bytes=100)


class JsonCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.db")
        self.input = self.root / "input.json"

    def invoke(self, options):
        output, error = io.StringIO(), io.StringIO()
        argv = ["baseagent", "--db", str(self.store.path), *options]
        with patch.object(sys, "argv", argv), patch("baseagent.main.Model", side_effect=AssertionError("provider initialized")), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main()
        return code, output.getvalue(), error.getvalue()

    def test_config_errors_refuse_before_session_or_provider(self):
        inputs = {
            "--completion-policy": '{"require_plan":true,"require_plan":false,"checks":[],"artifacts":[]}',
            "--tool-policy": '{"default":"deny","default":"allow","tools":{}}',
            "--subtasks-config": '{"tasks":[],"tasks":[]}',
        }
        deep = '{"x":' + '[' * 10_000 + '0' + ']' * 10_000 + '}'
        for option, duplicate in inputs.items():
            for text in (duplicate, '{"x":1e999}', deep):
                self.input.write_text(text, encoding="utf-8")
                with self.subTest(option=option, text=text[:30]):
                    code, _, error = self.invoke(["task", "--root", str(self.root), "--session", "new", option, str(self.input)])
                    self.assertEqual(code, 1, error)
                    self.assertIn("Session error:", error)
                    self.assertNotIn("Traceback", error)
                    self.assertIsNone(self.store.load("new"))

    def test_invalid_tool_receipts_leave_uncertain_call_and_events_unchanged(self):
        effects = []
        tools = ToolRegistry()

        def effect():
            effects.append("written")
            raise KeyboardInterrupt()

        tools.register("write", "Write", {"type": "object"}, effect)

        class Model:
            def complete(self, messages, tools):
                return {"tool_calls": [{"id": "a", "function": {"name": "write", "arguments": "{}"}}]}

        run_agent(Model(), "task", tools=tools, store=self.store, session_id="demo", workspace_root=self.root)
        before = self.store.inspect_session("demo")
        events = self.store.events("demo")
        bad = [b'{"ok":true,"data":"first","data":"second","error":null}',
               b'{"ok":true,"data":NaN,"error":null}',
               b'{"ok":true,"data":"\xff","error":null}',
               ('{"ok":true,"error":null,"data":' + '[' * 10_000 + '0' + ']' * 10_000 + '}').encode(),
               b' ' * 256001]
        options = ["--session", "demo", "--resolve-tool", "a", "--result-file", str(self.input)]
        for content in bad:
            self.input.write_bytes(content)
            code, _, error = self.invoke(options)
            self.assertEqual(code, 1, error)
            after = self.store.inspect_session("demo")
            self.assertEqual(after[0].to_dict(), before[0].to_dict())
            self.assertEqual(after[1], before[1])
            self.assertEqual(self.store.events("demo"), events)
        self.input.write_text(json.dumps({"ok": True, "data": "verified", "error": None}), encoding="utf-8-sig")
        code, _, error = self.invoke(options)
        self.assertEqual(code, 0, error)
        self.assertEqual(self.store.inspect_session("demo")[1][0]["status"], "completed")
        self.assertEqual(effects, ["written"])

    def test_usage_duplicates_or_overflow_do_not_clear_unknown_attempt(self):
        class Model:
            def complete(self, messages, tools):
                raise ConnectionError("failed")

        run_agent(Model(), "task", tools=ToolRegistry(), store=self.store, session_id="demo", max_total_tokens=100)
        before = self.store.load("demo").to_dict()
        events = self.store.events("demo")
        options = ["--session", "demo", "--resolve-usage", str(self.input)]
        for text in ('{"unknown_calls":0,"unknown_calls":1,"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}',
                     '{"unknown_calls":1,"prompt_tokens":1e999,"completion_tokens":0,"total_tokens":1e999}'):
            self.input.write_text(text, encoding="utf-8")
            code, _, error = self.invoke(options)
            self.assertEqual(code, 1, error)
            self.assertEqual(self.store.load("demo").to_dict(), before)
            self.assertEqual(self.store.events("demo"), events)
        self.input.write_text('{"unknown_calls":1,"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}', encoding="utf-8-sig")
        code, _, error = self.invoke(options)
        self.assertEqual(code, 0, error)
        state = self.store.load("demo")
        self.assertEqual((state.unknown_usage_calls, state.total_tokens, state.model_calls), (0, 2, 1))


if __name__ == "__main__":
    unittest.main()
