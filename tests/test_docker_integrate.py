"""--backend docker with the features merged into master (README plan "Docker 后端集成进新 master", AC 21-27).

Offline: the fake Docker runner from ``baseagent.testing.docker`` and fake models. AC 1-20 stay in
test_docker_cli.py; real-Docker smoke tests in test_docker_smoke.py.
"""

import json
import os
from hashlib import sha256
from unittest.mock import patch

import config_isolation  # noqa: F401 -- also under plain unittest
from checkpoint_helpers import Script, current_hash, tool_call
from mcp_helpers import write_config
from test_docker_cli import DOCKER, DockerCliCase

from baseagent.backends import DockerBackend
from baseagent.backends.docker import ADDED_CAPABILITIES
from baseagent.main import FULL_MODE_WARNING
from baseagent.tools.instructions import FILE_LIMIT, describe_rules, rules_summary
from baseagent.tools.result import ErrorCode, ToolFailure
from baseagent.tools.workspace import Workspace

RULES_TEXT = "Always answer in haiku. "


def big_rules(head="R", size=FILE_LIMIT + 5_000):
    """AGENTS.md text over the 20 KB limit, with a 3-byte character straddling the cut."""
    filler = head * (FILE_LIMIT - 1)                              # "界" starts at byte FILE_LIMIT - 1
    return RULES_TEXT + filler[len(RULES_TEXT):] + "界" + "z" * (size - FILE_LIMIT - 2)


class RulesCase(DockerCliCase):
    def backend(self):
        backend = DockerBackend(self.root, runner=self.fake, owner=False)
        self.addCleanup(backend.close)
        return backend


# ----- AC 21: the rules switch -----------------------------------------------------------------
class RulesSwitchTests(RulesCase):
    def test_no_project_rules_hides_the_text_under_docker_like_local(self):
        (self.root / "AGENTS.md").write_text(RULES_TEXT + "keep the README short\n")
        config = self.root.parent / f"{self.root.name}-config.json"
        config.write_text(json.dumps({"version": 1, "run": {"project_rules": False}}))
        self.addCleanup(config.unlink)
        seen = {}
        for label, argv in (("local-flag", ["--no-project-rules"]), ("docker-flag", [*DOCKER, "--no-project-rules"]),
                            ("docker-config", [*DOCKER, "--config", str(config)]), ("docker-on", DOCKER)):
            with self.subTest(label=label):
                model = Script([lambda messages: {"tool_calls": [tool_call("g", "get_instructions", {})]},
                                {"content": "ok"}])
                code, out, err = self.run_cli([*argv, "--output-format", "json", "--session", label, "hi"], model)
                self.assertEqual(code, 0, err)
                text = json.dumps(model.seen)
                result = json.loads(out)
                instructions = json.loads(model.seen[-1][-1]["content"])["data"]
                seen[label] = instructions["digest"]
                if label == "docker-on":
                    self.assertIn(RULES_TEXT, text)
                    self.assertTrue(result["project_rules"]["loaded"])
                    continue
                self.assertNotIn(RULES_TEXT, text)                 # neither the reference nor get_instructions
                self.assertNotIn("content", instructions["instructions"][0])
                self.assertEqual(result["project_rules"]["enabled"], False)
                self.assertEqual(result["project_rules"]["shown_bytes"], 0)
        self.assertEqual(len(set(seen.values())), 1)             # the write digest is the same in every case


# ----- AC 22: truncation, whole-file hashes, drift and CAS ----------------------------------------
class RulesTruncationTests(RulesCase):
    def setUp(self):
        super().setUp()
        (self.root / "AGENTS.md").write_text(big_rules())
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "AGENTS.md").write_text(big_rules("p", FILE_LIMIT + 30_000))
        (self.root / "pkg" / "mod.py").write_text("x = 1\n")

    def test_docker_cuts_and_hashes_exactly_like_local(self):
        docker, local = Workspace(backend=self.backend()), Workspace(self.root)
        for path in (".", "pkg", "pkg/mod.py", "pkg/new/file.py", "AGENTS.md"):
            with self.subTest(path=path):
                self.assertEqual(docker.get_instructions(path), local.get_instructions(path))
        value = docker.get_instructions("pkg/mod.py")
        root, nested = value["instructions"]
        self.assertTrue(root["truncated"])
        self.assertEqual(root["size_bytes"], (self.root / "AGENTS.md").stat().st_size)
        self.assertEqual(root["shown_bytes"], FILE_LIMIT - 1)    # stepped back to a whole character
        self.assertEqual(root["sha256"], sha256((self.root / "AGENTS.md").read_bytes()).hexdigest())
        self.assertEqual(nested["shown_bytes"] + root["shown_bytes"], len(
            (nested["content"] + root["content"]).encode("utf-8")))
        self.assertLessEqual(root["shown_bytes"] + nested["shown_bytes"], 40_000)
        self.assertEqual(nested["sha256"], sha256((self.root / "pkg" / "AGENTS.md").read_bytes()).hexdigest())
        self.assertIn("truncated", value["note"])
        self.assertEqual(docker.observe_many(["pkg/mod.py", "AGENTS.md"]), local.observe_many(["pkg/mod.py", "AGENTS.md"]))
        self.assertNotIn("unavailable", json.dumps(docker.observe_many(["pkg/mod.py"])))

    def test_a_change_after_the_cut_is_drift_and_a_write_conflict(self):
        backend = self.backend()
        docker = Workspace(backend=backend, allow_write=True)
        before = docker.get_instructions("pkg/mod.py")
        observed = docker.observe_many(["pkg/mod.py"])
        with (self.root / "AGENTS.md").open("a") as handle:
            handle.write("tail rule changed\n")                  # far beyond the shown 20 KB
        after = docker.get_instructions("pkg/mod.py")
        self.assertEqual(after["instructions"][0]["content"], before["instructions"][0]["content"])
        self.assertNotEqual(after["digest"], before["digest"])
        self.assertNotEqual(docker.observe_many(["pkg/mod.py"]), observed)
        with self.assertRaises(ToolFailure) as caught:
            docker.write_file("pkg/mod.py", "x = 2\n", current_hash(self.root, "pkg/mod.py"), before["digest"])
        self.assertEqual(caught.exception.code, ErrorCode.CONFLICT)
        self.assertEqual((self.root / "pkg" / "mod.py").read_text(), "x = 1\n")
        docker.write_file("pkg/mod.py", "x = 2\n", current_hash(self.root, "pkg/mod.py"), after["digest"])
        self.assertEqual((self.root / "pkg" / "mod.py").read_text(), "x = 2\n")

    def test_rules_reads_are_one_exec_and_never_transfer_whole_files(self):
        backend = self.backend()
        calls = len(self.fake.calls)
        backend.get_instructions("pkg/mod.py")
        backend.observe_many(["pkg/mod.py", "AGENTS.md"])
        self.assertEqual(len(self.fake.calls) - calls, 2)

        original = self.fake.run
        sizes = []

        def measure(argv, **options):
            code, stdout, stderr = original(argv, **options)
            sizes.append(len(stdout))
            return code, stdout, stderr
        self.fake.run = measure
        backend.get_instructions("pkg/mod.py")
        backend.observe_many(["pkg/mod.py"])
        self.assertLessEqual(sizes[0], 2 * (FILE_LIMIT + 4))     # heads only, not the 75 KB of rules
        self.assertLess(sizes[1], 1_000)                         # drift check: hashes only

    def test_a_chat_with_long_rules_runs_and_the_model_sees_the_cut(self):
        model = Script([{"content": "ok"}])
        code, _, err = self.run_cli([*DOCKER, "--session", "long", "hi"], model)
        self.assertEqual(code, 0, err)
        self.assertIn("AGENTS.md loaded, truncated", err)
        reference = next(message["content"] for message in model.seen[0] if "Repository guidance" in str(message["content"]))
        self.assertIn(RULES_TEXT, reference)
        self.assertNotIn("z" * 100, reference)


# ----- AC 23: rule metadata does not follow container-made links ---------------------------------
class RulesMetadataTests(RulesCase):
    def test_a_rules_link_to_a_host_file_is_not_described(self):
        host = self.root.parent / f"{self.root.name}-host-secret.txt"
        host.write_text("host secret " * 50)
        self.addCleanup(host.unlink)
        os.symlink(host, self.root / "AGENTS.md")              # what a container command could create
        workspace = Workspace(backend=self.backend())
        summary = rules_summary(workspace)
        self.assertEqual(summary["error"], "symlink")
        self.assertFalse(summary["loaded"])
        text = json.dumps(summary)
        self.assertNotIn(sha256(host.read_bytes()).hexdigest(), text)
        self.assertNotIn(str(host.stat().st_size), text)
        self.assertIn("could not be read (symlink)", describe_rules(summary))
        # The local backend runs commands as the host user anyway and keeps its host read (unchanged output).
        self.assertEqual(rules_summary(Workspace(self.root))["sha256"], sha256(host.read_bytes()).hexdigest())

    def test_the_summary_is_read_through_the_backend_without_host_file_access(self):
        (self.root / "AGENTS.md").write_text(big_rules())
        (self.root / "CLAUDE.md").write_text("ignored")
        workspace = Workspace(backend=self.backend())
        expected = rules_summary(Workspace(self.root))
        with patch("pathlib.Path.open", side_effect=AssertionError("host read")), \
                patch("pathlib.Path.is_file", side_effect=AssertionError("host stat")):
            self.assertEqual(rules_summary(workspace), expected)
        self.assertTrue(expected["truncated"] and expected["claude_md_ignored"])

    def test_status_and_json_report_rules_while_the_container_is_still_there(self):
        (self.root / "AGENTS.md").write_text(RULES_TEXT)
        code, out, err = self.run_cli([*DOCKER, "--output-format", "json", "--session", "j", "hi"],
                                      Script([{"content": "ok"}]))
        self.assertEqual(code, 0, err)
        value = json.loads(out)
        self.assertEqual(value["project_rules"]["sha256"], sha256(RULES_TEXT.encode()).hexdigest())
        self.assertEqual(self.fake.containers, {})               # removed after the summary was read
        code, out, _ = self.run_cli([*DOCKER, "--no-main-agent", "--session", "st"], Script([{"content": "ok"}]),
                                    ["hi", "/status", "/exit"])
        self.assertIn(f"Project rules: AGENTS.md loaded ({len(RULES_TEXT)} bytes)", out)
        self.assertIn("Backend: docker", out)

    def test_status_survives_a_dead_container(self):
        (self.root / "AGENTS.md").write_text(RULES_TEXT)
        backend = self.backend()
        self.fake.kill(backend.name)
        summary = rules_summary(Workspace(backend=backend))
        self.assertEqual((summary["loaded"], summary["error"]), (False, "DockerError"))
        self.assertIn("could not be read (DockerError)", describe_rules(summary))


# ----- AC 25: several optional output fields at once ---------------------------------------------
class OutputFieldTests(RulesCase):
    def test_project_rules_mcp_backend_and_patches_together(self):
        (self.root / "AGENTS.md").write_text(RULES_TEXT)
        write_config(self.root / ".mcp.json")
        digest = Workspace(self.root).get_instructions()["digest"]

        def patching(messages):
            return {"tool_calls": [tool_call("p1", "apply_patch", {"operations": [
                {"op": "modify", "path": "a.txt", "expected_sha256": current_hash(self.root, "a.txt"),
                 "expected_instructions": digest, "edits": [{"old_text": "A0", "new_text": "PATCHED-LINE"}]}]})]}

        results = {}
        for fmt in ("json", "jsonl"):
            (self.root / "a.txt").write_text("A0\n")
            code, out, err = self.run_cli([*DOCKER, "--mode", "edit", "--output-format", fmt, "--session", fmt, "go"],
                                          Script([patching, {"content": "done"}]))
            self.assertEqual(code, 0, err)
            results[fmt] = out
        value = json.loads(results["json"])
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["backend"]["kind"], "docker")
        self.assertEqual(value["mcp"], {"skipped": "untrusted", "config": ".mcp.json", "servers": ["demo"]})
        self.assertTrue(value["project_rules"]["loaded"])
        self.assertIn("+A0", "".join(value["patches"][0]["diff"]).replace("-A0", "+A0"))
        self.assertIn("PATCHED-LINE", value["patches"][0]["diff"])
        lines = [json.loads(line) for line in results["jsonl"].splitlines()]
        result = lines[-1]["data"]
        self.assertEqual(set(result) - set(value), set())
        self.assertNotIn("patches", result)
        self.assertEqual({key: result[key] for key in ("project_rules", "mcp")},
                         {key: value[key] for key in ("project_rules", "mcp")})
        self.assertEqual(result["backend"]["image"], value["backend"]["image"])
        self.assertIn("backend_ready", [line["type"] for line in lines])
        self.assertIn("patch_applied", [line["type"] for line in lines])
        self.assertNotIn("PATCHED-LINE", results["jsonl"])         # no diff text in jsonl
        self.assertNotIn(RULES_TEXT, results["jsonl"])             # no rules text in jsonl
        self.assertNotIn(RULES_TEXT, results["json"])


# ----- AC 26 and 27 -------------------------------------------------------------------------------
class HardeningTests(RulesCase):
    def test_local_full_mode_warning_points_to_the_docker_backend(self):
        self.assertIn("For isolation use --backend docker", FULL_MODE_WARNING)
        self.assertNotIn("DockerBackend", FULL_MODE_WARNING)
        code, _, err = self.run_cli(["--mode", "full", "--session", "w", "hi"], Script([{"content": "ok"}]))
        self.assertEqual(code, 0, err)
        self.assertIn(FULL_MODE_WARNING, err)

    def test_containers_drop_capabilities_and_privilege_gain(self):
        backend = DockerBackend(self.root, runner=self.fake, owner=(1234, 5678))
        run = next(argv for argv in self.fake.calls if argv[1] == "run")
        self.assertEqual(run[run.index("--cap-drop") + 1], "ALL")
        added = [run[index + 1] for index, value in enumerate(run) if value == "--cap-add"]
        self.assertEqual(added, ["CHOWN", "DAC_OVERRIDE", "FOWNER"])
        self.assertEqual(tuple(added), ADDED_CAPABILITIES)
        self.assertEqual(run[run.index("--security-opt") + 1], "no-new-privileges")
        self.assertLess(run.index("--cap-drop"), run.index("--cap-add"))
        self.assertLess(run.index("--security-opt"), run.index(backend.image))      # options, not command args
        self.assertNotIn("--privileged", run)
        self.assertEqual(backend.contract()["capabilities"], list(ADDED_CAPABILITIES))
        # The ownership hand-back (AC 8) and the removal (AC 15) still run.
        Workspace(backend=backend, allow_command=True).run_command(["echo", "hi"])
        backend.close()
        after = [argv for argv in self.fake.calls if "after" in argv]
        self.assertTrue(after and after[-1][-2:] == ["1234", "5678"])
        self.assertIn(backend.name, self.fake.removed())

    def test_hardening_is_part_of_the_resume_contract(self):
        # A turn started before hardening (contract without capabilities) asks to accept the change on resume.
        old = DockerBackend(self.root, runner=self.fake, owner=False, start=False).contract()
        old.pop("capabilities")
        old.pop("no_new_privileges")
        self.assertNotEqual(old, DockerBackend(self.root, runner=self.fake, owner=False, start=False).contract())

