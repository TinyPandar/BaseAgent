"""Tests never touch the real MCP trust store or load the real .env, under pytest or unittest."""

import os
import re
import tempfile
import unittest
from pathlib import Path

import config_isolation
from dotenv import load_dotenv

from baseagent.mcp import trust

TESTS = Path(__file__).resolve().parent
# In-process callers of main() (it runs load_dotenv) and users of a workspace .mcp.json or the
# trust store. Demo scripts run in a subprocess and inherit the environment set here.
NEEDS_ISOLATION = re.compile(
    r"baseagent\.mcp\.trust|TrustStore|\bwrite_config\("
    r"|^from (?:baseagent\.main|applications\.project_assistant(?:\.app)?) import [^\n]*\bmain\b"
    r"|^from baseagent import [^\n]*\bmain\b", re.M)
LOCAL_IMPORT = re.compile(r"^(?:from|import) ([A-Za-z_]\w*)", re.M)


class ConfigIsolationTests(unittest.TestCase):
    def test_config_home_is_a_temporary_directory(self):
        # pytest's per-test directory and config_isolation's are both under the temp root.
        home = trust.config_home().resolve()
        self.assertTrue(home.is_relative_to(Path(tempfile.gettempdir()).resolve()), home)
        self.assertTrue(Path(config_isolation.CONFIG_HOME).is_dir())

    def test_dotenv_loading_is_disabled(self):
        self.assertEqual(os.environ.get("PYTHON_DOTENV_DISABLED"), "1")
        self.assertFalse(load_dotenv(TESTS.parent / ".env"))

    def test_every_module_that_needs_it_reaches_the_isolation(self):
        sources = {path.stem: path.read_text(encoding="utf-8") for path in sorted(TESTS.glob("*.py"))
                   if path.name != "conftest.py"}
        imports = {name: set(LOCAL_IMPORT.findall(text)) & set(sources) for name, text in sources.items()}

        def reaches(name, seen):
            return any(dep == "config_isolation" or (dep not in seen and reaches(dep, seen | {dep}))
                       for dep in imports[name])

        missing = [name for name, text in sources.items()
                   if name != "config_isolation" and NEEDS_ISOLATION.search(text) and not reaches(name, {name})]
        self.assertEqual(missing, [], "import config_isolation in these modules (or a helper that does)")


if __name__ == "__main__":
    unittest.main()
