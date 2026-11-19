"""Keep the MCP trust store out of the real user configuration under any test runner.

pytest gives each test a fresh directory (conftest.py). ``python -m unittest`` never reads
conftest.py, so modules that put a ``.mcp.json`` in a workspace import this module (mcp_helpers
does it for them): the whole process then uses one temporary ``BASEAGENT_CONFIG_HOME``, removed
at exit. A value already set is replaced on purpose; it may be the developer's real configuration.
"""

import atexit
import os
import shutil
import tempfile

HOME_VARIABLE = "BASEAGENT_CONFIG_HOME"
CONFIG_HOME = tempfile.mkdtemp(prefix="baseagent-test-config-")
os.environ[HOME_VARIABLE] = CONFIG_HOME
atexit.register(shutil.rmtree, CONFIG_HOME, ignore_errors=True)
