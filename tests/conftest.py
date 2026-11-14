"""Test-wide isolation: the MCP trust store never touches the real user configuration."""

import pytest


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("BASEAGENT_CONFIG_HOME", str(tmp_path_factory.mktemp("config-home")))
