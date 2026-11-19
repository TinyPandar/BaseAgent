"""Tests never read or write the real MCP trust store, under pytest or plain unittest."""

import re
import tempfile
import unittest
from pathlib import Path

import config_isolation

from baseagent.mcp import trust

TESTS = Path(__file__).resolve().parent
# In-process users of a workspace .mcp.json or the trust store. The demo scripts run in a
# subprocess and set their own temporary BASEAGENT_CONFIG_HOME.
USES_TRUST = re.compile(r"baseagent\.mcp\.trust|TrustStore|\bwrite_config\(")
ISOLATED = re.compile(r"^(import config_isolation|from mcp_helpers import)", re.M)


class ConfigIsolationTests(unittest.TestCase):
    def test_config_home_is_a_temporary_directory(self):
        # pytest's per-test directory and config_isolation's are both under the temp root.
        home = trust.config_home().resolve()
        self.assertTrue(home.is_relative_to(Path(tempfile.gettempdir()).resolve()), home)
        self.assertTrue(Path(config_isolation.CONFIG_HOME).is_dir())

    def test_every_trust_user_imports_the_isolation(self):
        sources = {path.name: path.read_text(encoding="utf-8") for path in sorted(TESTS.glob("*.py"))
                   if path.name not in {"conftest.py", "config_isolation.py", Path(__file__).name}}
        missing = [name for name, text in sources.items() if USES_TRUST.search(text) and not ISOLATED.search(text)]
        self.assertEqual(missing, [], "import config_isolation (or mcp_helpers) in these modules")


if __name__ == "__main__":
    unittest.main()
