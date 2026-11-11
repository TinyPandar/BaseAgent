"""docs/demo.md MCP segment: scripts/demo_mcp.py runs offline (README plan E, AC 23)."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class McpDemoTests(unittest.TestCase):
    def test_demo_lists_mcp_tools_asks_once_and_the_call_succeeds(self):
        completed = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "demo_mcp.py")],
                                   capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.strip().splitlines()
        summary = json.loads(lines[-1])
        self.assertEqual(summary["approval_prompts"], ["Allow mcp__demo__add_note? [y/N] "])
        self.assertIn("mcp__demo__echo", summary["mcp_tools"])
        self.assertEqual((summary["echo"], summary["note_written"], summary["server_stopped"]), ("hello", True, True))
        transcript = "\n".join(lines)
        for text in ("Allow mcp__demo__add_note? [y/N] y", "笔记已记下。", "MCP: server 'demo' ready, 7 tool(s): mcp__demo__echo"):
            self.assertIn(text, transcript)


if __name__ == "__main__":
    unittest.main()
