import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from baseagent.testing import CassetteModel, Redactor
from baseagent.llm import ModelResponse, TokenUsage
from baseagent.kernel.errors import BadRequestError
import json


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("synthetic_recording_script", ROOT / "scripts" / "record_cassettes.py")
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


class RealCassetteTests(unittest.TestCase):
    def scenario(self, name):
        path = ROOT / "tests" / "cassettes" / f"deepseek_{name}.jsonl"
        if not path.exists():
            self.skipTest("real synthetic cassette pending; run scripts/record_cassettes.py with DeepSeek credentials")
        with patch("socket.socket.connect", side_effect=AssertionError("real fixture replay must be offline")):
            with CassetteModel(path, mode="replay") as model:
                script.run_scenario(model, name)

    def test_direct(self):
        self.scenario("direct")

    def test_tool(self):
        self.scenario("tool")

    def test_correction(self):
        self.scenario("correction")

    def test_repository_cassette_privacy_scan(self):
        for path in (ROOT / "tests" / "cassettes").glob("*.jsonl"):
            with self.subTest(path=path.name):
                Redactor().assert_safe(path.read_text(encoding="utf-8"))
                CassetteModel(path, mode="replay")  # Validate version/hash/envelopes.

    def test_script_missing_credentials_makes_no_calls_and_writes_no_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixtures"
            with patch.dict(os.environ, {}, clear=True), patch.object(script, "find_dotenv", return_value=""), patch.object(script, "BoundedModel", side_effect=AssertionError("no credential must not construct adapter")):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(script.main(["--output-dir", str(path)]), 2)
            self.assertIn("pending", output.getvalue())
            self.assertFalse(path.exists())

    def test_script_fixed_scenarios_publish_only_after_checked_record_and_replay(self):
        class SyntheticModel:
            model_name = "script-test-only"
            generation_params = {"temperature": 0}
            max_completion_tokens = 512
            calls = 0

            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def complete(self, messages, tools):
                self.calls += 1
                prompt = messages[1]["content"]
                if "SYNTHETIC_OK" in prompt:
                    value = {"content": "SYNTHETIC_OK"}
                else:
                    results = [m for m in messages if m["role"] == "tool"]
                    correction = "synthetic error recovery" in prompt
                    if len(results) == (2 if correction else 1):
                        value = {"content": "42"}
                    else:
                        args = {"a": 84, "b": 0 if not results else 2} if correction else {"a": 23, "b": 19}
                        value = {"tool_calls": [{"id": f"call-{len(results)}", "type": "function", "function": {
                            "name": "divide" if correction else "add", "arguments": json.dumps(args)}}]}
                return ModelResponse(value, TokenUsage(2, 1, 3))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixtures"
            # Synthetic fake is limited to temporary files, never repository fixtures.
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-only", "DEEPSEEK_BASE_URL": "https://example.invalid"}), patch.object(script, "find_dotenv", return_value=""), patch.object(script, "BoundedModel", SyntheticModel), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(script.main(["--output-dir", str(path)]), 0)
                self.assertEqual(len(list(path.glob("*.jsonl"))), 3)
                self.assertEqual(script.main(["--output-dir", str(path)]), 1)
            for name in script.SCENARIOS:
                with CassetteModel(path / f"deepseek_{name}.jsonl", mode="replay") as model:
                    script.run_scenario(model, name)

    def test_script_network_or_scenario_failure_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixtures"
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-only", "DEEPSEEK_BASE_URL": "https://example.invalid"}), patch.object(script, "find_dotenv", return_value=""), patch.object(script, "BoundedModel", side_effect=BadRequestError("synthetic failure")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(script.main(["--output-dir", str(path)]), 1)
            self.assertFalse(path.exists())

    def test_script_total_http_attempts_are_bounded(self):
        # This checks the live dispatch guard without creating an SDK/client.
        model = object.__new__(script.BoundedModel)
        model.calls = 0
        with patch("baseagent.llm.adapters.OpenAICompatModel.complete_with_timeout", return_value=ModelResponse({"content": "synthetic"})) as dispatch:
            for _ in range(9):
                model.complete_with_timeout([], [])
            with self.assertRaises(BadRequestError):
                model.complete_with_timeout([], [])
        self.assertEqual(dispatch.call_count, 9)


if __name__ == "__main__":
    unittest.main()
