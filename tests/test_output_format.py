"""--output-format text|json|jsonl for one-shot prompts (README plan B, AC 11-14 and 23). Fake models only.

``text`` must match master byte for byte: ``fixtures/one_shot_text_master.json`` was captured
by running the scenarios below against origin/master (2ed4a52) with the same scripted model.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from checkpoint_helpers import Script, tool_call

from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.main import main
from baseagent.output import SCHEMA_VERSION
from baseagent.session import SessionStore

FIXTURE = Path(__file__).parent / "fixtures" / "one_shot_text_master.json"
RESULT_FIELDS = {"schema_version", "status", "final_answer", "error", "session_id", "usage", "exit_code", "pending_approval"}
USAGE_FIELDS = {"prompt_tokens", "completion_tokens", "total_tokens", "unknown_usage_calls", "model_calls", "tool_calls", "steps"}


def scenarios():
    """The master capture scenarios: name -> (argv, model steps, files)."""
    write = {"path": "x.txt", "content": "x", "expected_sha256": "missing", "expected_instructions": "0" * 64}
    return {
        "completed": (["hello"], [ModelResponse({"content": "hi there"}, TokenUsage(3, 2, 5))], {}),
        "unknown_usage": (["hello"], [{"content": "plain"}], {}),
        "tool_then_answer": (["read it"], [ModelResponse({"tool_calls": [tool_call("c1", "read_file", {"path": "a.txt"})]}, TokenUsage(4, 1, 5)),
                                           ModelResponse({"content": "done"}, TokenUsage(6, 2, 8))], {"a.txt": "A\n"}),
        "max_steps": (["loop", "--max-steps", "1"], [ModelResponse({"tool_calls": [tool_call("c1", "glob", {"pattern": "*"})]}, TokenUsage(1, 1, 2))], {}),
        "denied_write": (["write", "--max-steps", "2"], [ModelResponse({"tool_calls": [tool_call("c1", "write_file", write)]}, TokenUsage(1, 1, 2)),
                                                         ModelResponse({"content": "could not write"}, TokenUsage(1, 1, 2))], {}),
        "failed_model": (["boom"], [RuntimeError("model exploded")], {}),
    }


class OutputFormatTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / "s.sqlite3"

    def run_cli(self, argv, steps=(), files=None, session="golden"):
        for path, text in (files or {}).items():
            (self.root / path).write_text(text)
        out, err = io.StringIO(), io.StringIO()
        model = Script(list(steps))
        prefix = ["--root", str(self.root), "--db", str(self.db)] + (["--session", session] if session else [])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main([*prefix, *argv], model_factory=lambda args: model)
            except SystemExit as exit:
                code = exit.code
        normalize = lambda text: text.replace(str(self.root), "<ROOT>")
        return code, normalize(out.getvalue()), normalize(err.getvalue())


class TextFormatTests(OutputFormatTestCase):
    def test_text_output_matches_master(self):
        expected = {case["name"]: case for case in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]}
        cases = scenarios()
        self.assertEqual(set(cases), set(expected))
        for name, (argv, steps, files) in cases.items():
            for explicit in ([], ["--output-format", "text"]):
                with self.subTest(name=name, explicit=bool(explicit)):
                    self.setUp()
                    code, stdout, stderr = self.run_cli([*argv, *explicit], steps, files)
                    self.assertEqual((code, stdout, stderr), (expected[name]["code"], expected[name]["stdout"], expected[name]["stderr"]))


class JsonFormatTests(OutputFormatTestCase):
    def test_json_prints_exactly_one_object_with_the_documented_fields(self):
        argv, steps, files = scenarios()["tool_then_answer"]
        code, stdout, stderr = self.run_cli([*argv, "--output-format", "json"], steps, files)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.count("\n"), 1)
        result = json.loads(stdout)
        self.assertEqual(set(result), RESULT_FIELDS)
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual((result["status"], result["final_answer"], result["error"], result["session_id"], result["exit_code"]),
                         ("completed", "done", None, "golden", 0))
        self.assertEqual(result["usage"], {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
                                           "unknown_usage_calls": 0, "model_calls": 2, "tool_calls": 1, "steps": 2})
        self.assertIsNone(result["pending_approval"])
        self.assertIn("Session: golden", stderr)
        self.assertNotIn("Session:", stdout)

    def test_json_stops_and_errors_keep_exit_codes(self):
        for name, status, code in (("max_steps", "max_steps_exceeded", 1), ("failed_model", "failed", 1)):
            with self.subTest(name=name):
                self.setUp()
                argv, steps, files = scenarios()[name]
                exit_code, stdout, stderr = self.run_cli([*argv, "--output-format", "json"], steps, files)
                result = json.loads(stdout)
                self.assertEqual((exit_code, result["exit_code"], result["status"]), (code, code, status))
                self.assertIn("Agent stopped:", stderr)
        self.setUp()
        exit_code, stdout, _ = self.run_cli(["stop", "--output-format", "json"], [KeyboardInterrupt()])
        result = json.loads(stdout)
        self.assertEqual((exit_code, result["exit_code"], result["status"]), (130, 130, "interrupted"))

    def test_session_errors_still_print_one_json_object(self):
        self.run_cli(["first"], [{"content": "ok"}])
        other = self.root / "other"
        other.mkdir()
        code, stdout, stderr = self.run_cli(["--root", str(other), "again", "--output-format", "json"], [])
        self.assertEqual(code, 1)
        result = json.loads(stdout)
        self.assertEqual((result["status"], result["exit_code"], result["session_id"]), ("error", 1, "golden"))
        self.assertIn("different workspace", result["error"])
        self.assertIn("Session error:", stderr)

    def test_json_awaiting_approval_names_the_pending_call_and_digest(self):
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"glob": "ask"}}))
        steps = [{"tool_calls": [tool_call("c1", "glob", {"pattern": "*"})]}]
        code, stdout, _ = self.run_cli(["list", "--tool-policy", str(policy), "--output-format", "json"], steps)
        result = json.loads(stdout)
        self.assertEqual((code, result["status"], result["exit_code"]), (1, "awaiting_approval", 1))
        request = SessionStore(self.db).load("golden").metadata["approval_request"]
        self.assertEqual(result["pending_approval"], {"call_id": "c1", "digest": request["request_digest"], "name": "glob"})
        decided = self.run_cli(["--approve-tool", "c1", "--approval-digest", request["request_digest"]])
        self.assertEqual(decided[0], 0)
        code, stdout, _ = self.run_cli(["--resume", "--tool-policy", str(policy), "--output-format", "json"],
                                       [{"content": "listed"}])
        self.assertEqual((code, json.loads(stdout)["status"], json.loads(stdout)["final_answer"]), (0, "completed", "listed"))

    def test_cny_appears_only_for_a_known_price_and_complete_usage(self):
        code, stdout, _ = self.run_cli(["hi", "--model", "qwen-plus", "--output-format", "json"],
                                       [ModelResponse({"content": "ok"}, TokenUsage(1_000_000, 500_000, 1_500_000))])
        self.assertEqual(json.loads(stdout)["usage"]["cny"], 1.8)
        self.setUp()
        code, stdout, _ = self.run_cli(["hi", "--model", "qwen-plus", "--output-format", "json"], [{"content": "ok"}])
        self.assertNotIn("cny", json.loads(stdout)["usage"])           # usage unknown
        self.setUp()
        code, stdout, _ = self.run_cli(["hi", "--output-format", "json"], [ModelResponse({"content": "ok"}, TokenUsage(1, 1, 2))])
        self.assertNotIn("cny", json.loads(stdout)["usage"])           # no public price for the default model

    def test_new_session_without_id_reports_the_generated_id(self):
        code, stdout, stderr = self.run_cli(["hi", "--output-format", "json"], [{"content": "ok"}], session=None)
        result = json.loads(stdout)
        self.assertRegex(result["session_id"], "^[0-9a-f]{32}$")
        self.assertIn(f"Session: {result['session_id']}", stderr)


class JsonLinesFormatTests(OutputFormatTestCase):
    def test_every_line_parses_events_are_metadata_and_the_last_line_is_the_result(self):
        argv, steps, files = scenarios()["tool_then_answer"]
        code, stdout, stderr = self.run_cli([*argv, "--output-format", "jsonl"], steps, files)
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in stdout.splitlines()]
        self.assertTrue(all(set(line) == {"type", "timestamp", "data"} for line in lines))
        self.assertEqual(lines[0]["type"], "output_started")
        self.assertEqual(lines[0]["data"], {"schema_version": SCHEMA_VERSION, "format": "jsonl", "session_id": "golden"})
        self.assertEqual(lines[-1]["type"], "result")
        self.assertEqual(set(lines[-1]["data"]), RESULT_FIELDS)
        self.assertEqual((lines[-1]["data"]["status"], lines[-1]["data"]["final_answer"]), ("completed", "done"))
        kinds = [line["type"] for line in lines[1:-1]]
        for kind in ("run_started", "model_started", "model_returned", "tool_attempt", "tool_completed", "turn_finished"):
            self.assertIn(kind, kinds)
        stored = [item["type"] for item in SessionStore(self.db).events("golden", limit=1000)]
        self.assertEqual(kinds, stored)
        body = json.dumps(lines[1:-1])
        for secret in ("read it", '"a.txt"', "A\\n", '"done"'):
            self.assertNotIn(secret, body)                              # no prompt, arguments or bodies
        self.assertIn("Session: golden", stderr)

    def test_streamed_deltas_keep_only_their_size(self):
        class Streaming(Script):
            def complete_stream(self, messages, tools):
                yield {"kind": "content", "index": 0, "delta": "secret streamed text"}
                return {"content": "secret streamed text"}

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--root", str(self.root), "--db", str(self.db), "--session", "s", "--stream", "--output-format",
                         "jsonl", "hi"], model_factory=lambda args: Streaming([]))
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        deltas = [line["data"] for line in lines if line["type"] == "model_delta"]
        self.assertEqual(deltas, [{"attempt": 1, "delta_kind": "content", "index": 0, "chars": 20}])
        self.assertNotIn("secret streamed text", json.dumps(lines[:-1]))
        self.assertEqual(lines[-1]["data"]["final_answer"], "secret streamed text")
        self.assertIn("secret streamed text", err.getvalue())         # the live stream still goes to stderr

    def test_early_errors_end_with_a_result_line(self):
        code, stdout, _ = self.run_cli(["--root", str(self.root / "missing"), "hi", "--output-format", "jsonl"], [])
        lines = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual([line["type"] for line in lines], ["output_started", "result"])
        self.assertEqual(lines[-1]["data"]["status"], "error")


class FormatScopeTests(OutputFormatTestCase):
    def test_chat_and_other_operations_refuse_machine_formats(self):
        self.run_cli(["first"], [{"content": "ok"}])
        for argv, reason in ((["--output-format", "json"], "interactive chat"),
                             (["--output-format", "jsonl", "--ui", "tui"], "interactive chat"),
                             (["--events", "--output-format", "json"], "session and database operations"),
                             (["--checkpoints", "--output-format", "json"], "session and database operations"),
                             (["--inspect-session", "--output-format", "jsonl"], "session and database operations")):
            with self.subTest(argv=argv):
                code, stdout, stderr = self.run_cli(argv)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn(reason, stderr)
        session_less = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(session_less):
            with self.assertRaises(SystemExit) as exit:
                main(["--db", str(self.db), "--list-sessions", "--output-format", "json"])
        self.assertEqual(exit.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
