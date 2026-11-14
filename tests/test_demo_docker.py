"""docs/demo.md docker segment: scripts/demo_docker.py (AC 20). Fake model, cost 0."""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "scripts" / "demo_docker.py"


class DockerDemoTests(unittest.TestCase):
    def test_fake_docker_demo_writes_runs_is_offline_undoes_and_cleans_up(self):
        completed = subprocess.run([sys.executable, "-B", str(DEMO), "--fake-docker"], capture_output=True, text=True,
                                   timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(summary["file_written"])
        self.assertIn("hello from the container", summary["command_output"])
        self.assertTrue(summary["network_blocked"])
        self.assertTrue(summary["undo_restored"])
        self.assertTrue(summary["container_removed"])
        self.assertEqual(summary["cost_cny"], 0)
        self.assertIn("> /undo", completed.stdout)

    @unittest.skipIf(shutil.which("docker"), "Docker is installed here; the no-Docker path is not reachable")
    def test_without_docker_the_demo_reports_the_cli_error_and_exit_3(self):
        completed = subprocess.run([sys.executable, "-B", str(DEMO)], capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 3)
        summary = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["status"], "docker_unavailable")
        self.assertIn("Docker error (cli_missing)", completed.stderr)


if __name__ == "__main__":
    unittest.main()
