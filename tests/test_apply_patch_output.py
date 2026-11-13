"""apply_patch display: CLI text, --output-format json/jsonl, chat events and approval prompts.

README 施工段 AC 11, 16, 17, 23, 24 (display part). Real ``baseagent.main`` with fake models only.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from chat_helpers import ChatTestCase
from checkpoint_helpers import Script
from patch_helpers import patch_step, tree

from baseagent.main import main

FILES = {"a.txt": "one\ntwo\n", "b.txt": "bee\n"}


def three(root):
    return patch_step(root, ("delete", "b.txt"), ("add", "n.txt", "NEW-CONTENT-MARKER\n"),
                      ("modify", "a.txt", [{"old_text": "two", "new_text": "TWO"}]))


class OneShotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "work"
        self.root.mkdir()
        for name, text in FILES.items():
            (self.root / name).write_text(text)
        self.db = Path(self.temp.name).resolve() / "s.sqlite3"

    def run_cli(self, argv, steps):
        out, err = io.StringIO(), io.StringIO()
        model = Script(steps)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--root", str(self.root), "--db", str(self.db), "--session", "p", *argv],
                        model_factory=lambda args: model)
        return code, out.getvalue(), err.getvalue()

    def test_text_mode_prints_the_batch_diff_under_the_result(self):
        code, stdout, stderr = self.run_cli(["--mode", "edit", "patch it"], [three(self.root), {"content": "done"}])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "done\n")
        self.assertIn("apply_patch 已应用：3 个文件（新建 1，修改 1，删除 1），+2 -2 行", stderr)
        for line in ("--- a/b.txt\n+++ /dev/null", "+++ b/n.txt", "-two\n+TWO"):
            self.assertIn(line, stderr)

    def test_text_mode_truncates_a_long_diff_with_a_marker(self):
        content = "".join(f"line {index:05d}\n" for index in range(3000))
        code, _, stderr = self.run_cli(["--mode", "edit", "patch it"],
                                       [patch_step(self.root, ("add", "long.txt", content)), {"content": "done"}])
        self.assertEqual(code, 0, stderr)
        self.assertIn("[diff truncated at 20000 characters", stderr)

    def test_json_result_has_patches_and_jsonl_has_metadata_only(self):
        code, stdout, stderr = self.run_cli(["--mode", "edit", "--output-format", "json", "patch it"],
                                            [three(self.root), {"content": "done"}])
        self.assertEqual(code, 0, stderr)
        value = json.loads(stdout)
        self.assertEqual(value["schema_version"], 1)
        (patch,) = value["patches"]
        self.assertEqual(patch["call_id"], "p1")
        self.assertEqual([item["path"] for item in patch["files"]], ["b.txt", "n.txt", "a.txt"])
        self.assertIn("+NEW-CONTENT-MARKER", patch["diff"])
        self.assertFalse(patch["diff_truncated"])
        self.assertNotIn("apply_patch 已应用", stderr)       # machine formats keep stderr unchanged

        for name, text in FILES.items():
            (self.root / name).write_text(text)
        (self.root / "n.txt").unlink()
        code, stdout, stderr = self.run_cli(["--mode", "edit", "--output-format", "jsonl", "--session", "q", "patch"],
                                            [three(self.root), {"content": "done"}])
        self.assertEqual(code, 0, stderr)
        lines = [json.loads(line) for line in stdout.splitlines()]
        applied = [line["data"] for line in lines if line["type"] == "patch_applied"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["files"][0], {"path": "b.txt", "op": "delete", "added": 0, "removed": 1})
        self.assertNotIn("patches", lines[-1]["data"])
        self.assertNotIn("NEW-CONTENT-MARKER", stdout)
        self.assertNotIn("+++", stdout)

    def test_json_without_apply_patch_has_no_new_field(self):
        code, stdout, _ = self.run_cli(["--mode", "edit", "--output-format", "json", "hi"], [{"content": "hello"}])
        self.assertEqual(code, 0)
        self.assertEqual(set(json.loads(stdout)), {"schema_version", "status", "final_answer", "error", "session_id",
                                                   "usage", "exit_code", "pending_approval"})

    def test_ask_policy_stops_and_shows_the_pending_diff(self):
        policy = Path(self.temp.name) / "ask.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"apply_patch": "ask"}}))
        before = tree(self.root)
        code, _, stderr = self.run_cli(["--mode", "edit", "--tool-policy", str(policy), "patch it"], [three(self.root)])
        self.assertEqual(code, 1)
        self.assertEqual(tree(self.root), before)
        self.assertIn("apply_patch 待审批：3 个文件", stderr)
        self.assertIn("-two\n+TWO", stderr)
        self.assertIn("--approve-tool p1 --approval-digest", stderr)


class ChatTests(ChatTestCase):
    def setUp(self):
        super().setUp()
        for name, text in FILES.items():
            (self.root / name).write_text(text)

    def test_cli_chat_ask_prompt_shows_file_count_and_diff_then_applies(self):
        policy = self.root.parent / "ask-policy.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"apply_patch": "ask"}}))
        model = Script([three(self.root), {"content": "改好了"}])
        code, out, err = self.invoke(["--session", "demo", "--mode", "edit", "--no-main-agent", "--tool-policy",
                                      str(policy)], model, ["改三个文件", "y", "/exit"])
        self.assertEqual(code, 0, err)
        self.assertIn("Approval required: apply_patch", out)
        self.assertIn("apply_patch 待审批：3 个文件（新建 1，修改 1，删除 1）", out)
        self.assertLess(out.index("-two\n+TWO"), out.index("改好了"))
        self.assertIn("apply_patch 已应用", err)
        self.assertLess(err.index("tool result:"), err.index("apply_patch 已应用"))   # under the tool result
        self.assertFalse((self.root / "b.txt").exists())

    def test_shared_events_carry_the_diff_for_tui_and_web(self):
        policy = self.root.parent / "ask-policy.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {"apply_patch": "ask"}}))
        session = self.session(["--session", "demo", "--mode", "edit", "--no-main-agent", "--tool-policy", str(policy)],
                               Script([three(self.root), {"content": "改好了"}]))
        session.start()
        session.turn("改三个文件", lambda view: ("allow", False))
        items = session.events_after(0)
        kinds = [item["kind"] for item in items]
        notices = [item["data"]["text"] for item in items if item["kind"] == "notice"]
        pending = next(index for index, item in enumerate(items) if item["kind"] == "notice"
                       and item["data"]["text"].startswith("apply_patch 待审批"))
        self.assertEqual(kinds[pending + 1], "approval_requested")
        self.assertIn("patch", items[pending + 1]["data"])
        self.assertTrue(any(text.startswith("apply_patch 已应用") and "+TWO" in text for text in notices))
        self.assertFalse((self.root / "b.txt").exists())


if __name__ == "__main__":
    unittest.main()
