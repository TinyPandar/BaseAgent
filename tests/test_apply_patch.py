"""apply_patch: batch validation, all-or-nothing writes and rollback (README 施工段 AC 1-8, 18-21, 26).

Offline and deterministic: real Workspace and LocalBackend in a temporary directory, fault
injection by patching the backend, no model provider.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from patch_helpers import current_hash, operation, operations, tree

from baseagent.agent.state import State
from baseagent.backends import LocalBackend
from baseagent.kernel.cancellation import CancellationToken
from baseagent.tools import patch as patch_module
from baseagent.tools.context import ToolContext
from baseagent.tools.edit_policy import EditPolicy
from baseagent.tools.result import ErrorCode, ToolFailure
from baseagent.tools.workspace import Workspace, coding_tools


class PatchTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "work"
        self.root.mkdir()
        for name, text in {"a.txt": "one\ntwo\nthree\n", "b.txt": "bee\r\n", "c.txt": "sea\n"}.items():
            (self.root / name).write_bytes(text.encode())

    def workspace(self, **kwargs):
        return Workspace(self.root, allow_write=True, **kwargs)

    def execute(self, ops, workspace=None):
        return coding_tools(workspace or self.workspace()).execute("apply_patch", {"operations": ops})


class SchemaAndRegistrationTests(PatchTestCase):
    def test_registered_with_strict_schema_and_not_retry_safe(self):
        tools = coding_tools(self.workspace())
        spec = next(item["function"] for item in tools.specs() if item["function"]["name"] == "apply_patch")
        parameters = spec["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["properties"]["operations"]["maxItems"], patch_module.MAX_PATCH_FILES)
        self.assertEqual(patch_module.MAX_PATCH_FILES, 50)
        self.assertEqual(patch_module.MAX_PATCH_DIFF_CHARS, 20_000)
        self.assertFalse(parameters["properties"]["operations"]["items"]["additionalProperties"])
        self.assertFalse(tools.retry_safe("apply_patch"))
        # write_file / edit_file keep their schema and description.
        names = [item["function"]["name"] for item in tools.specs()]
        self.assertLess(names.index("edit_file"), names.index("apply_patch"))

    def test_per_operation_fields_are_enforced(self):
        bad = [
            {**operation(self.root, ("add", "n.txt", "x")), "edits": [{"old_text": "a", "new_text": "b"}]},
            {**operation(self.root, ("modify", "a.txt", [{"old_text": "one", "new_text": "1"}])), "content": "x"},
            {**operation(self.root, ("delete", "a.txt")), "content": "x"},
            {key: value for key, value in operation(self.root, ("modify", "a.txt", [])).items() if key != "edits"},
            {**operation(self.root, ("add", "n.txt", "x")), "expected_sha256": current_hash(self.root, "a.txt")},
            {key: value for key, value in operation(self.root, ("delete", "a.txt")).items()
             if key != "expected_instructions"},
        ]
        before = tree(self.root)
        for item in bad:
            with self.subTest(item=item):
                result = self.execute([item])
                self.assertEqual(result.error.code, ErrorCode.INVALID_ARGUMENTS)
        self.assertEqual(tree(self.root), before)


class MixedBatchTests(PatchTestCase):
    def test_delete_add_modify_all_apply_with_hashes_counts_and_diff(self):
        # AC 21 (and AC 3): one call, three kinds of operation.
        before_a, before_b = current_hash(self.root, "a.txt"), current_hash(self.root, "b.txt")
        result = self.execute(operations(self.root, ("delete", "b.txt"), ("add", "new/n.txt", "n1\nn2"),
                                         ("modify", "a.txt", [{"old_text": "two", "new_text": "TWO"},
                                                              {"old_text": "three\n", "new_text": ""}])))
        self.assertTrue(result.ok, result.error)
        self.assertFalse((self.root / "b.txt").exists())
        self.assertEqual((self.root / "new" / "n.txt").read_bytes(), b"n1\nn2")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"one\nTWO\n")
        files = {item["path"]: item for item in result.data["files"]}
        self.assertEqual(files["b.txt"], {"path": "b.txt", "op": "delete", "before_sha256": before_b,
                                          "after_sha256": "missing", "added": 0, "removed": 1})
        self.assertEqual(files["new/n.txt"]["before_sha256"], "missing")
        self.assertEqual(files["new/n.txt"]["after_sha256"], current_hash(self.root, "new/n.txt"))
        self.assertEqual((files["new/n.txt"]["added"], files["new/n.txt"]["removed"]), (2, 0))
        self.assertEqual(files["a.txt"]["before_sha256"], before_a)
        self.assertEqual(files["a.txt"]["after_sha256"], current_hash(self.root, "a.txt"))
        self.assertEqual((files["a.txt"]["added"], files["a.txt"]["removed"]), (1, 2))
        self.assertEqual((result.data["added"], result.data["removed"]), (3, 3))
        diff = result.data["diff"]
        self.assertFalse(result.data["diff_truncated"])
        self.assertIn("--- a/b.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bee\r\n", diff)
        self.assertIn("--- /dev/null\n+++ b/new/n.txt\n@@ -0,0 +1,2 @@\n+n1\n+n2\n\\ No newline at end of file\n", diff)
        self.assertIn("--- a/a.txt\n+++ b/a.txt\n", diff)
        self.assertIn("-two\n-three\n+TWO\n", diff)

    def test_modify_uses_apply_edits_exactly_like_edit_file(self):
        (self.root / "same.txt").write_bytes(b"x = 1\r\ny = 2\r\n")
        edits = [{"old_text": "x = 1", "new_text": "x = 10"}, {"old_text": "y", "new_text": "why"}]
        workspace = self.workspace()
        read = workspace.read_file("same.txt")
        (self.root / "twin.txt").write_bytes(b"x = 1\r\ny = 2\r\n")
        workspace.edit_file("twin.txt", edits, read["sha256"], read["instruction_digest"])
        self.assertTrue(self.execute(operations(self.root, ("modify", "same.txt", edits))).ok)
        self.assertEqual((self.root / "same.txt").read_bytes(), (self.root / "twin.txt").read_bytes())


class AllOrNothingTests(PatchTestCase):
    def assert_rejected(self, ops, code, name):
        before = tree(self.root)
        result = self.execute(ops)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, code, result.error.message)
        self.assertTrue(result.error.message.startswith(name + ":"), result.error.message)
        self.assertIn("no file was written", result.error.message)
        self.assertEqual(tree(self.root), before)
        return result

    def test_one_wrong_hash_rejects_all_three_files(self):
        ops = operations(self.root, ("modify", "a.txt", [{"old_text": "one", "new_text": "1"}]),
                         ("modify", "b.txt", [{"old_text": "bee", "new_text": "B"}]), ("delete", "c.txt"))
        ops[1]["expected_sha256"] = "0" * 64
        self.assert_rejected(ops, ErrorCode.CONFLICT, "b.txt")

    def test_add_existing_and_modify_missing_reject_the_batch(self):
        self.assert_rejected(operations(self.root, ("delete", "c.txt"), ("add", "a.txt", "x")),
                             ErrorCode.CONFLICT, "a.txt")
        ops = operations(self.root, ("delete", "c.txt"), ("modify", "a.txt", [{"old_text": "one", "new_text": "1"}]))
        ops[1]["path"] = "ghost.txt"
        self.assert_rejected(ops, ErrorCode.CONFLICT, "ghost.txt")
        ops = operations(self.root, ("delete", "c.txt"), ("delete", "a.txt"))
        ops[1]["path"] = "ghost.txt"
        self.assert_rejected(ops, ErrorCode.CONFLICT, "ghost.txt")

    def test_old_text_matching_zero_or_many_times_rejects_the_batch(self):
        (self.root / "dup.txt").write_text("same\nsame\n")
        for edits in ([{"old_text": "absent", "new_text": "x"}], [{"old_text": "same", "new_text": "x"}]):
            with self.subTest(edits=edits):
                self.assert_rejected(operations(self.root, ("add", "n.txt", "n"), ("delete", "c.txt"),
                                                ("modify", "dup.txt", edits)), ErrorCode.CONFLICT, "dup.txt")

    def test_stale_instruction_digest_rejects_the_batch(self):
        ops = operations(self.root, ("delete", "c.txt"), ("add", "n.txt", "n"))
        ops[1]["expected_instructions"] = "f" * 64
        self.assert_rejected(ops, ErrorCode.CONFLICT, "n.txt")

    def test_paths_are_rejected_before_any_write(self):
        # AC 20: .., absolute, link alias, protected paths, EditPolicy denylist, duplicates.
        outside = Path(self.temp.name).resolve() / "outside.txt"
        outside.write_text("outside\n")
        (self.root / "alias.txt").symlink_to(self.root / "a.txt")
        cases = [("../outside.txt", ErrorCode.PERMISSION_DENIED), (str(outside), ErrorCode.PERMISSION_DENIED),
                 ("alias.txt", ErrorCode.PERMISSION_DENIED), (".git/config", ErrorCode.PERMISSION_DENIED),
                 (".env", ErrorCode.PERMISSION_DENIED), ("server.pem", ErrorCode.PERMISSION_DENIED),
                 ("secrets/token.txt", ErrorCode.PERMISSION_DENIED)]
        for path, code in cases:
            with self.subTest(path=path):
                ops = operations(self.root, ("delete", "c.txt"), ("add", "n.txt", "x"))
                ops[1]["path"] = path
                self.assert_rejected(ops, code, path)
        self.assertEqual(outside.read_text(), "outside\n")
        for second in ("a.txt", "A.TXT", "./a.txt", "sub/../a.txt"):
            with self.subTest(duplicate=second):
                ops = operations(self.root, ("modify", "a.txt", [{"old_text": "one", "new_text": "1"}]),
                                 ("delete", "a.txt"))
                ops[1]["path"] = second
                self.assert_rejected(ops, ErrorCode.INVALID_ARGUMENTS, second)

    def test_edit_policy_allowlist_and_denylist_reject_the_batch(self):
        workspace = self.workspace(edit_policy=EditPolicy(allowlist=["*.txt"], denylist=["c.txt"]))
        before = tree(self.root)
        for ops, name in ((operations(self.root, ("add", "n.txt", "x"), ("delete", "c.txt")), "c.txt"),
                          (operations(self.root, ("add", "n.txt", "x"), ("add", "n.md", "x")), "n.md")):
            result = self.execute(ops, workspace)
            self.assertEqual(result.error.code, ErrorCode.PERMISSION_DENIED)
            self.assertTrue(result.error.message.startswith(name + ":"))
        self.assertEqual(tree(self.root), before)

    def test_read_only_workspace_refuses_without_touching_anything(self):
        before = tree(self.root)
        result = coding_tools(Workspace(self.root)).execute(
            "apply_patch", {"operations": operations(self.root, ("add", "n.txt", "x"))})
        self.assertEqual(result.error.code, ErrorCode.PERMISSION_DENIED)
        self.assertEqual(tree(self.root), before)


class LimitTests(PatchTestCase):
    def test_too_many_files_rejected_by_schema_and_by_validation(self):
        ops = [operation(self.root, ("add", f"f{index}.txt", "x")) for index in range(51)]
        result = self.execute(ops)
        self.assertEqual(result.error.code, ErrorCode.INVALID_ARGUMENTS)
        with self.assertRaises(ToolFailure) as raised:
            self.workspace().apply_patch(ops)
        self.assertEqual(raised.exception.code, ErrorCode.LIMIT_EXCEEDED)
        self.assertFalse(any(self.root.glob("f*.txt")))
        self.assertTrue(self.execute(ops[:50]).ok)

    def test_new_content_over_200_kb_rejects_the_batch(self):
        before = tree(self.root)
        big = "x" * 200_001
        result = self.execute(operations(self.root, ("delete", "c.txt"), ("add", "big.txt", big)))
        self.assertEqual(result.error.code, ErrorCode.LIMIT_EXCEEDED)
        self.assertTrue(result.error.message.startswith("big.txt:"))
        (self.root / "grow.txt").write_text("seed\n")
        before = tree(self.root)
        result = self.execute(operations(self.root, ("delete", "c.txt"),
                                         ("modify", "grow.txt", [{"old_text": "seed", "new_text": "y" * 200_000}])))
        self.assertEqual(result.error.code, ErrorCode.LIMIT_EXCEEDED)
        self.assertTrue(result.error.message.startswith("grow.txt:"))
        self.assertEqual(tree(self.root), before)

    def test_diff_truncation_follows_the_constant(self):
        content = "".join(f"line {index:05d}\n" for index in range(3000))   # ~33k characters of diff
        result = self.execute(operations(self.root, ("add", "long.txt", content), ("delete", "c.txt")))
        self.assertTrue(result.ok)
        diff = result.data["diff"]
        self.assertTrue(result.data["diff_truncated"])
        body, marker = diff[:diff.rindex("[diff truncated")], diff[diff.rindex("[diff truncated"):]
        self.assertLessEqual(len(body), patch_module.MAX_PATCH_DIFF_CHARS)
        self.assertIn("2 file(s) not shown", marker)
        self.assertIn("--inspect-session", marker)
        self.assertNotIn("c.txt", body)
        with patch.object(patch_module, "MAX_PATCH_DIFF_CHARS", 30):
            small = self.execute(operations(self.root, ("modify", "a.txt", [{"old_text": "one", "new_text": "1"}])))
        self.assertTrue(small.data["diff_truncated"])
        self.assertLessEqual(len(small.data["diff"].split("[diff truncated")[0]), 30)

    def test_observed_file_capacity_is_checked_for_the_whole_batch(self):
        state = State(turn_id="t")
        state.metadata["observed_files"] = {f"seen{index}.txt": {"sha256": "missing", "instruction_digest": ""}
                                            for index in range(499)}
        context = ToolContext(state, lambda *args, **kwargs: None)
        before = tree(self.root)
        with self.assertRaises(ToolFailure) as raised:
            self.workspace().apply_patch(operations(self.root, ("add", "n1.txt", "x"), ("add", "n2.txt", "y")),
                                         context=context)
        self.assertEqual(raised.exception.code, ErrorCode.LIMIT_EXCEEDED)
        self.assertEqual(tree(self.root), before)


class RollbackTests(PatchTestCase):
    """AC 5 and 19: a failure after some files were written restores the exact pre-call bytes."""

    def batch(self):
        (self.root / "d.txt").write_text("dee\n")
        os.chmod(self.root / "b.txt", 0o640)
        return operations(self.root, ("modify", "a.txt", [{"old_text": "two", "new_text": "2"}]), ("delete", "b.txt"),
                          ("add", "made.txt", "new\n"), ("modify", "d.txt", [{"old_text": "dee", "new_text": "D"}]))

    def failing_write(self, fail_path, *, also=None):
        original = LocalBackend.write_file
        seen = []

        def write(backend, path, content, expected_sha256, expected_instructions):
            seen.append(path)
            if path == fail_path or (also is not None and also(path, seen)):
                raise OSError("injected disk failure")
            return original(backend, path, content, expected_sha256, expected_instructions)

        return patch.object(LocalBackend, "write_file", write)

    def test_failure_at_the_fourth_file_restores_modified_deleted_and_created_files(self):
        ops = self.batch()
        before = tree(self.root)
        with self.failing_write("d.txt"):
            result = self.execute(ops)
        self.assertEqual(result.error.code, ErrorCode.EXECUTION_FAILED)
        self.assertTrue(result.error.message.startswith("d.txt:"))
        self.assertIn("rolled back", result.error.message)
        self.assertEqual(tree(self.root), before)
        self.assertEqual(os.stat(self.root / "b.txt").st_mode & 0o777, 0o640)
        self.assertEqual(list(self.root.rglob(".baseagent-edit-*")), [])

    def test_post_edit_check_failure_rolls_back_the_batch(self):
        # AC 10: post_edit_check runs for each file; a failure rolls back the whole batch.
        check = [sys.executable, "-c", "import pathlib, sys; sys.exit(3 if pathlib.Path('made.txt').exists() else 0)"]
        workspace = self.workspace(edit_policy=EditPolicy(post_edit_check=check))
        ops = self.batch()
        before = tree(self.root)
        result = self.execute(ops, workspace)
        self.assertEqual(result.error.code, ErrorCode.EXECUTION_FAILED)
        self.assertTrue(result.error.message.startswith("made.txt:"))
        self.assertIn("post-edit check failed", result.error.message)
        self.assertEqual(tree(self.root), before)

    def test_cancellation_between_files_rolls_back(self):
        token = CancellationToken()
        original = LocalBackend.write_file

        def write(backend, *args):
            value = original(backend, *args)
            token.cancel()
            return value

        state = State(turn_id="t")
        context = ToolContext(state, lambda *args, **kwargs: None, token)
        ops = self.batch()
        before = tree(self.root)
        with patch.object(LocalBackend, "write_file", write), self.assertRaises(ToolFailure) as raised:
            self.workspace().apply_patch(ops, context=context)
        self.assertEqual(raised.exception.code, ErrorCode.CANCELLED)
        self.assertIn("rolled back", str(raised.exception))
        self.assertEqual(tree(self.root), before)
        self.assertEqual(state.metadata.get("observed_files", {}), {})

    def test_external_change_between_validation_and_write_is_a_conflict_and_rolls_back(self):
        # AC 8: optimistic detection only; the last CAS before each publish catches the change.
        ops = self.batch()
        original = LocalBackend.write_file
        calls = []

        def write(backend, path, *args):
            calls.append(path)
            if path == "made.txt":
                (self.root / "d.txt").write_text("edited by someone else\n")
            return original(backend, path, *args)

        with patch.object(LocalBackend, "write_file", write):
            result = self.execute(ops)
        self.assertEqual(result.error.code, ErrorCode.CONFLICT)
        self.assertTrue(result.error.message.startswith("d.txt:"))
        self.assertEqual((self.root / "a.txt").read_text(), "one\ntwo\nthree\n")
        self.assertEqual((self.root / "b.txt").read_bytes(), b"bee\r\n")
        self.assertFalse((self.root / "made.txt").exists())
        self.assertEqual((self.root / "d.txt").read_text(), "edited by someone else\n")

    def test_rollback_failure_names_files_with_unknown_state(self):
        ops = self.batch()
        with self.failing_write("d.txt", also=lambda path, seen: path == "a.txt" and seen.count("a.txt") == 2):
            result = self.execute(ops)
        self.assertEqual(result.error.code, ErrorCode.EXECUTION_FAILED)
        self.assertIn("rollback failed: state unknown for a.txt", result.error.message)
        self.assertIn("/undo", result.error.message)
        self.assertEqual((self.root / "a.txt").read_text(), "one\n2\nthree\n")   # the file left behind
        self.assertEqual((self.root / "b.txt").read_bytes(), b"bee\r\n")         # the others were restored
        self.assertFalse((self.root / "made.txt").exists())


if __name__ == "__main__":
    unittest.main()
