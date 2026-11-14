"""MCP server configuration: ``--mcp-config PATH`` or ``<workspace root>/.mcp.json``.

    {"mcpServers": {"notes": {"command": "python", "args": ["server.py"], "env": {"KEY": "value"},
                              "timeout": 60, "tools": {"search": {"readOnly": true}}}}}

Nothing is installed, expanded or downloaded: ``command`` runs as written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
from pathlib import Path
import re

from baseagent.jsondata import read_json_object

CONFIG_NAME = ".mcp.json"
SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")
DEFAULT_CALL_TIMEOUT = 60.0


class McpConfigError(ValueError):
    """The configuration file is missing or not usable as a whole."""


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict = field(default_factory=dict)
    read_only_tools: frozenset = frozenset()
    timeout: float = DEFAULT_CALL_TIMEOUT

    def digest(self) -> str:
        # Hash only: env values may be credentials and are never stored or printed.
        value = {"command": self.command, "args": list(self.args), "env": dict(sorted(self.env.items())),
                 "read_only_tools": sorted(self.read_only_tools), "timeout": self.timeout}
        return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def find_config(flag_path, root) -> Path | None:
    """The flag wins; otherwise ``.mcp.json`` in the workspace root; otherwise MCP is off."""
    if flag_path is not None:
        path = Path(flag_path)
        if not path.is_file():
            raise McpConfigError(f"--mcp-config file not found: {path}")
        return path
    path = Path(root) / CONFIG_NAME
    return path if path.is_file() else None


def parse_server(name, value) -> ServerConfig:
    """One ``mcpServers`` entry. Raises ValueError with a reason that names no values."""
    if not isinstance(name, str) or not SERVER_NAME.match(name):
        raise ValueError("server names use 1-32 letters, digits or hyphens")
    if not isinstance(value, dict):
        raise ValueError("server entry must be an object")
    if value.get("type", "stdio") != "stdio" or "url" in value:
        raise ValueError("only stdio servers (command/args/env) are supported")
    command, args, env = value.get("command"), value.get("args", []), value.get("env", {})
    if not isinstance(command, str) or not command:
        raise ValueError("command must be a nonempty string")
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise ValueError("args must be a list of strings")
    if not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in env.items()):
        raise ValueError("env must map names to strings")
    tools = value.get("tools", {})
    if not isinstance(tools, dict) or any(not isinstance(entry, dict) for entry in tools.values()):
        raise ValueError("tools must map tool names to objects")
    if any("readOnly" in entry and type(entry["readOnly"]) is not bool for entry in tools.values()):
        raise ValueError("readOnly must be true or false")
    timeout = value.get("timeout", DEFAULT_CALL_TIMEOUT)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError("timeout must be between 0 and 3600 seconds")
    return ServerConfig(name, command, tuple(args), dict(env),
                        frozenset(tool for tool, entry in tools.items() if entry.get("readOnly") is True), float(timeout))


def load_config(path) -> tuple[list[ServerConfig], list[str]]:
    """(usable servers, problems with individual entries). A file unusable as a whole raises."""
    try:
        value = read_json_object(Path(path), max_bytes=200_000, label="MCP configuration")
    except (ValueError, OSError) as exc:
        raise McpConfigError(f"cannot read MCP configuration {path}: {exc}") from exc
    return parse_servers(value, path)


def parse_servers(value, path) -> tuple[list[ServerConfig], list[str]]:
    """``mcpServers`` of an already decoded configuration object (shared with the trust check)."""
    servers = value.get("mcpServers") if isinstance(value, dict) else None
    if not isinstance(servers, dict):
        raise McpConfigError(f"MCP configuration {path} needs an mcpServers object")
    parsed, problems = [], []
    for name, entry in servers.items():
        try:
            parsed.append(parse_server(name, entry))
        except ValueError as exc:
            problems.append(f"server {name!r}: {exc}")
    return parsed, problems
