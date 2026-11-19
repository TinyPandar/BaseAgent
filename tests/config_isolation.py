"""Keep tests away from the developer's real configuration under any test runner.

* MCP trust store: one temporary ``BASEAGENT_CONFIG_HOME`` for the process, removed at exit
  (pytest additionally gives each test a fresh one in conftest.py). A value already set is
  replaced on purpose; it may be the developer's real configuration.
* ``.env``: ``main()`` calls ``load_dotenv()``, which finds the repository's ``.env`` from
  baseagent's own location, whatever the working directory. In-process tests would then keep the
  real provider credentials in ``os.environ`` for every later test (and BASEAGENT_LLM_* changes
  which provider configuration main() picks). ``PYTHON_DOTENV_DISABLED`` turns that off.

conftest.py imports this for pytest. ``python -m unittest`` never reads conftest.py, so every test
module that calls ``main()`` or touches the trust store imports it, directly or through a helper
(test_config_isolation enforces that).
"""

import atexit
import os
import shutil
import tempfile

HOME_VARIABLE = "BASEAGENT_CONFIG_HOME"
CONFIG_HOME = tempfile.mkdtemp(prefix="baseagent-test-config-")
os.environ[HOME_VARIABLE] = CONFIG_HOME
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
atexit.register(shutil.rmtree, CONFIG_HOME, ignore_errors=True)
