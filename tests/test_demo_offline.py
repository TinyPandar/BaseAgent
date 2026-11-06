"""Offline demo walkthrough does not call a provider."""

import tempfile
import unittest
from pathlib import Path

from scripts.demo_offline import run_walkthrough


class DemoOfflineTests(unittest.TestCase):
    def test_walkthrough_streams_approves_and_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as folder:
            checks = run_walkthrough(Path(folder))
        self.assertTrue(checks["passed"], checks)
        self.assertEqual(checks["cost_cny"], 0)
        self.assertEqual(checks["provider_calls"], 0)


if __name__ == "__main__":
    unittest.main()
