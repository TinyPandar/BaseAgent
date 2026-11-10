"""docs/demo.md checkpoint segment: scripts/demo_checkpoints.py runs offline (AC 27)."""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CheckpointDemoTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_demo_changes_three_files_lists_undoes_and_ends_clean(self):
        completed = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "demo_checkpoints.py")],
                                   capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.strip().splitlines()
        summary = json.loads(lines[-1])
        self.assertEqual(summary["checkpoints"], [{"turn": 1, "restored": True, "files": [
            "modified app.py", "modified README.md", "created notes.md"]}])
        self.assertTrue(summary["files_restored"])
        self.assertTrue(summary["undo_notice_seen_by_agent"])
        self.assertTrue(summary["git_clean"])
        transcript = "\n".join(lines)
        for text in ("> /checkpoints", "新建 notes.md", "> /undo", "恢复 2 个文件，删除 1 个新建文件", "干净：分支 main"):
            self.assertIn(text, transcript)


if __name__ == "__main__":
    unittest.main()
