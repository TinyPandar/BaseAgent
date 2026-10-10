import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import io
from types import SimpleNamespace
import os
import subprocess

from baseagent.agent import run_agent
from baseagent.capabilities import assemble_capabilities, ToolPackage, SkillCatalog, SkillMiddleware
from baseagent.session import SessionStore
from baseagent.tools.workspace import Workspace, coding_tools


def call(name, arguments, identifier="one"):
    return {"content": None, "tool_calls": [{"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class CapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root)
        self.skill = self.root / "skills" / "check"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: check\ndescription: Check code\n---\nFollow checklist.md\n", encoding="utf-8")
        (self.skill / "checklist.md").write_text("Verify the diff", encoding="utf-8")

    def catalog(self):
        return SkillCatalog([self.root / "skills"])

    def test_default_and_selected_packages_preserve_permissions(self):
        default = assemble_capabilities(self.workspace)
        self.assertEqual(default.specs(), coding_tools(self.workspace).specs())
        memory = assemble_capabilities(self.workspace, packages=["memory"])
        self.assertFalse(memory.execute("read_file", {"path": "x"}).ok)
        result = default.execute("run_command", {"argv": ["echo", "x"]})
        self.assertEqual(result.error.code, "permission_denied")
        with self.assertRaises(ValueError):
            assemble_capabilities(self.workspace, packages=["missing"])

    def test_custom_package_dispatch_and_duplicate_rejection(self):
        package = ToolPackage("business", "1", lambda registry: registry.register("answer", "Answer", {"type": "object"}, lambda: 42))
        registry = assemble_capabilities(self.workspace, packages=[], extensions=[package])
        self.assertEqual(registry.execute("answer", {}).data, 42)
        with self.assertRaises(ValueError):
            assemble_capabilities(self.workspace, extensions=[package, package])
        duplicate = ToolPackage("duplicate", "1", lambda registry: registry.register("read_file", "x", {"type": "object"}, lambda: None))
        with self.assertRaises(ValueError):
            assemble_capabilities(self.workspace, extensions=[duplicate])

    def test_skill_load_resources_snapshot_and_paths(self):
        catalog = self.catalog()
        registry = assemble_capabilities(self.workspace, skills=catalog)
        self.assertEqual(registry.execute("load_skill", {"name": "check"}).data["resources"], ["checklist.md"])
        for path in ("../SKILL.md", str(self.skill / "checklist.md"), "absent"):
            self.assertFalse(registry.execute("read_skill_resource", {"name": "check", "path": path}).ok)
        (self.skill / "checklist.md").write_text("new", encoding="utf-8")
        self.assertEqual(catalog.read_skill_resource("check", "checklist.md")["content"], "Verify the diff")
        self.assertNotEqual(catalog.contract(), self.catalog().contract())

    def test_invalid_metadata_duplicate_and_resource_limit(self):
        (self.skill / "SKILL.md").write_text("missing metadata", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.catalog()
        (self.skill / "SKILL.md").write_text("---\nname: check\ndescription: x\n---\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            SkillCatalog([self.root / "skills", self.root / "skills"])
        (self.skill / "big.txt").write_bytes(b"x" * 64001)
        with self.assertRaises(ValueError):
            self.catalog()

    def test_cli_composes_selected_package_and_skill_without_provider(self):
        from baseagent.main import main
        result = SimpleNamespace(session_id="cli", total_tokens=0, unknown_usage_calls=0,
                                 final_answer="done", error=None, status="completed")
        with patch("sys.argv", ["baseagent", "check", "--root", str(self.root), "--db", str(self.root / "cli.db"), "--tool-package", "coding", "--skills-dir", str(self.root / "skills")]), patch("baseagent.main.run_agent", return_value=result) as run, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 0)
        names = {spec["function"]["name"] for spec in run.call_args.kwargs["tools"].specs()}
        self.assertIn("load_skill", names)
        self.assertIn("read_file", names)
        self.assertNotIn("remember", names)
        self.assertTrue(any(isinstance(layer, SkillMiddleware) for layer in run.call_args.kwargs["middleware"]))

    def test_skill_resource_link_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("outside", encoding="utf-8")
        link = self.skill / "linked"
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.catalog()

    def test_harness_skill_loop_and_changed_content_refuses_resume(self):
        catalog = self.catalog()
        responses = iter([call("load_skill", {"name": "check"}), call("read_skill_resource", {"name": "check", "path": "checklist.md"}, "two"), {"content": "done"}])
        seen = []
        class Model:
            def complete(self, messages, tools):
                seen.append(messages)
                return next(responses)
        registry = assemble_capabilities(self.workspace, skills=catalog)
        store = SessionStore(self.root / "sessions.db")
        state = run_agent(Model(), "check code", tools=registry, middleware=[SkillMiddleware(catalog)], store=store, session_id="skills", workspace_root=self.root, max_steps=1)
        self.assertNotEqual(state.status, "completed")
        (self.skill / "checklist.md").write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError):
            run_agent(Model(), None, tools=assemble_capabilities(self.workspace, skills=self.catalog()), store=store, session_id="skills", workspace_root=self.root)
        state = run_agent(Model(), None, tools=registry, middleware=[SkillMiddleware(catalog)], store=store, session_id="skills", workspace_root=self.root, max_steps=4, max_model_calls=4)
        self.assertEqual(state.final_answer, "done")
        self.assertEqual(state.tool_calls, 2)
        self.assertTrue(any("Available skills" in (message.get("content") or "") for message in seen[0]))
        self.assertTrue(any("Verify the diff" in (message.get("content") or "") for message in seen[-1]))


if __name__ == "__main__":
    unittest.main()
