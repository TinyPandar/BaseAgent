"""docs/demo.md apply_patch segment: scripts/demo_patch.py runs offline, cost 0 (README 施工段 AC 28)."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PatchDemoTests(unittest.TestCase):
    def test_demo_patches_three_files_prints_the_diff_and_undoes_the_batch(self):
        completed = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "demo_patch.py")],
                                   capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["model_calls"], 2)          # one apply_patch call, one answer
        self.assertEqual(summary["after_patch"], {"app.py": True, "notes.md": True, "old.txt": False})
        self.assertEqual(summary["checkpoints"], [{"turn": 1, "restored": True, "files": [
            "modified app.py", "created notes.md", "deleted old.txt"]}])
        self.assertTrue(summary["diff_printed"])
        self.assertTrue(summary["files_restored"])
        self.assertIn("恢复 2 个文件，删除 1 个新建文件", completed.stdout)
        self.assertIn("apply_patch 已应用：3 个文件", completed.stderr)


if __name__ == "__main__":
    unittest.main()
