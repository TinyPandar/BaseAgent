import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from baseagent.config import load_harness_config
from baseagent.main import main
from baseagent.session import SessionStore


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "harness.json"
        self.skill = self.root / "skills" / "check"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: check\ndescription: Check code\n---\nRead code first\n", encoding="utf-8")

    def write(self, **sections):
        self.config.write_text(json.dumps({"version": 1, **sections}), encoding="utf-8")

    def invoke(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["baseagent", *args]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main()
        return code, output.getvalue(), error.getvalue()

    def test_relative_paths_and_empty_package_selection(self):
        self.write(workspace={"root": ".", "db": "sessions.db"}, capabilities={"tool_packages": [], "skills_dirs": ["skills"]}, policies={"tool_policy": "tools.json"}, run={"tokenizer_file": "tokenizer.json"})
        values = load_harness_config(self.config)
        self.assertEqual(values["root"], self.root)
        self.assertEqual(values["skills_dir"], [self.root / "skills"])
        self.assertEqual(values["db"], self.root / "sessions.db")
        self.assertEqual(values["tool_policy"], self.root / "tools.json")
        self.assertEqual(values["tokenizer_file"], self.root / "tokenizer.json")
        self.assertEqual(values["tool_package"], [])

    def test_invalid_config_types_names_versions_and_duplicate_json(self):
        invalid = [
            {"version": True}, {"version": 2}, {"version": 1, "unknown": {}},
            {"version": 1, "run": {"max_steps": True}},
            {"version": 1, "run": {"max_duration_seconds": "5"}},
            {"version": 1, "run": {"max_duration_seconds": 10 ** 400}},
            {"version": 1, "permissions": {"allow_command": "false"}},
            {"version": 1, "capabilities": {"tool_packages": ["unknown"]}},
            {"version": 1, "capabilities": {"tool_packages": ["coding", "coding"]}},
            {"version": 1, "run": {"model_retries": 5}},
            {"version": 1, "workspace": {"root": None}},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.config.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_harness_config(self.config)
        self.config.write_text('{"version":1,"run":{"max_steps":1,"max_steps":2}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_harness_config(self.config)

    def test_cli_overrides_scalars_permissions_and_replaces_lists(self):
        self.write(workspace={"root": ".", "db": "sessions.db"}, capabilities={"tool_packages": ["coding"], "skills_dirs": ["absent"]}, permissions={"allow_write": True, "allow_command": True}, run={"max_steps": 3})
        result = SimpleNamespace(session_id="test", total_tokens=0, unknown_usage_calls=0, final_answer="done", error=None, status="completed")
        with patch("baseagent.main.run_agent", return_value=result) as run:
            code, _, _ = self.invoke("task", "--config", str(self.config), "--tool-package", "memory", "--skills-dir", str(self.root / "skills"), "--no-allow-write", "--no-allow-command", "--max-steps", "9")
        self.assertEqual(code, 0)
        options = run.call_args.kwargs
        names = {spec["function"]["name"] for spec in options["tools"].specs()}
        self.assertIn("remember", names)
        self.assertIn("load_skill", names)
        self.assertNotIn("read_file", names)
        self.assertFalse(options["tools"].capability_contract["allow_write"])
        self.assertFalse(options["tools"].capability_contract["allow_command"])
        self.assertEqual(options["max_steps"], 9)

    def test_config_runs_real_harness_and_changed_modules_refuse_resume(self):
        self.write(workspace={"root": ".", "db": "sessions.db"}, capabilities={"tool_packages": [], "skills_dirs": ["skills"]}, run={"max_steps": 1})
        responses = iter([
            {"tool_calls": [{"id": "one", "type": "function", "function": {"name": "load_skill", "arguments": '{"name":"check"}'}}]},
            {"content": "checked"},
        ])
        seen = []
        class Model:
            def complete(self, messages, tools):
                seen.append((messages, tools))
                return next(responses)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test", "DEEPSEEK_BASE_URL": "https://example.invalid"}), patch("baseagent.main.Model", return_value=Model()):
            self.assertEqual(self.invoke("check", "--config", str(self.config), "--session", "test")[0], 1)
            store = SessionStore(self.root / "sessions.db")
            original = store.load("test").to_dict()
            code, _, error = self.invoke("--config", str(self.config), "--session", "test", "--resume", "--tool-package", "coding")
            self.assertEqual(code, 1)
            self.assertIn("configuration changed", error)
            self.assertEqual(store.load("test").to_dict(), original)
            self.assertEqual(self.invoke("--config", str(self.config), "--session", "test", "--resume", "--max-steps", "3", "--max-model-calls", "3")[0], 0)
        self.assertEqual(len(seen), 2)
        state = store.load("test")
        self.assertEqual(state.final_answer, "checked")
        self.assertEqual(state.tool_calls, 1)
        self.assertEqual({spec["function"]["name"] for spec in seen[0][1]}, {"list_skills", "load_skill", "read_skill_resource"})

    def test_package_discovery_avoids_model_and_database(self):
        with patch("baseagent.main.SessionStore", side_effect=AssertionError("database access")), patch("baseagent.main.Model", side_effect=AssertionError("provider access")):
            code, output, _ = self.invoke("--list-tool-packages")
        self.assertEqual(code, 0)
        self.assertEqual(set(json.loads(output)), {"coding", "memory", "shared-memory"})

    def test_rejected_config_fails_before_database_and_provider(self):
        self.write(run={"max_steps": "1"})
        with patch("baseagent.main.SessionStore", side_effect=AssertionError("database access")), patch("baseagent.main.Model", side_effect=AssertionError("provider access")):
            with self.assertRaises(SystemExit) as raised:
                self.invoke("task", "--config", str(self.config))
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
