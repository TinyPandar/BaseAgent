"""docs/demo.md first-use segment: scripts/demo_project_setup.py runs offline (README 施工段 AC 22)."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProjectSetupDemoTests(unittest.TestCase):
    def test_demo_asks_trust_once_shows_rules_and_continues(self):
        completed = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "demo_project_setup.py")],
                                   capture_output=True, text=True, timeout=120, env={**os.environ})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.strip().splitlines()
        summary = json.loads(lines[-1])
        self.assertEqual(summary["trust_prompts"], ["Trust this .mcp.json and start these servers? [y/N] "])
        self.assertEqual(summary["trust_prompts_second_run"], [])
        self.assertTrue(summary["model_saw_rules"])
        self.assertEqual((summary["turn_status"], summary["tool_calls"]), ("completed", 4))
        transcript = "\n".join(lines)
        for text in ("env keys: DEMO_TOKEN (values not shown)", "Project rules: AGENTS.md loaded (",
                     "Turn started: ", " UTC", "type /continue", "Continuing this turn with raised limits"):
            self.assertIn(text, transcript)
        self.assertNotIn("not-shown", transcript)


if __name__ == "__main__":
    unittest.main()
