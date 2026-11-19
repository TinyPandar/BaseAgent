"""Test-wide isolation: no real MCP trust store and no real ``.env`` (config_isolation.py).

This also gives each pytest test its own trust-store directory.
"""

import pytest

import config_isolation  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("BASEAGENT_CONFIG_HOME", str(tmp_path_factory.mktemp("config-home")))
