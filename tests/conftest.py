"""Test-wide isolation: the MCP trust store never touches the real user configuration.

This gives each pytest test its own directory. Plain ``unittest`` relies on config_isolation.py.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("BASEAGENT_CONFIG_HOME", str(tmp_path_factory.mktemp("config-home")))
