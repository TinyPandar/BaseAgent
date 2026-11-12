"""Start configured MCP servers, register their tools in the ToolRegistry, stop them at the end.

Registered handlers are ordinary registry entries: a call reaches a server only
through the harness dispatch path (see ``agent/gateway.py``).
"""

from __future__ import annotations

import atexit
import os
import re
import signal
import sys
import threading

from baseagent.kernel.result import ErrorCode, ToolFailure

from .client import McpClient
from .config import ServerConfig, find_config, load_config
from .transport import McpError, StdioTransport

STARTUP_TIMEOUT = 10.0
# Model APIs accept function names of letters, digits, underscore and hyphen, at most 64 characters.
REGISTERED_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DESCRIPTION_CHARS = 1024
STDERR_NOTE_CHARS = 300
STDERR_JOIN_SECONDS = 1.0


class McpStartupError(Exception):
    """MCP could not start as required (--mcp-strict, or an unusable --mcp-config). Exit code 2."""


def tool_name(server: str, tool: str) -> str:
    return f"mcp__{server}__{tool}"


def _stdio(config: ServerConfig, cwd):
    return StdioTransport(config.command, config.args, env=config.env, cwd=cwd)


class McpManager:
    def __init__(self, servers, *, cwd=None, strict=False, warn=None, transport_factory=_stdio,
                 startup_timeout=STARTUP_TIMEOUT, client_version="0", source=None):
        self.servers, self.cwd, self.strict = list(servers), cwd, strict
        self.warn = warn or (lambda line: print(line, file=sys.stderr))
        self.transport_factory, self.startup_timeout = transport_factory, startup_timeout
        self.client_version, self.source = client_version, source
        self.clients: dict[str, McpClient] = {}
        self.tools: dict[str, list[dict]] = {}
        self.registered: list[str] = []
        self._closed = False
        self._lock = threading.Lock()
        self._previous_sigterm = None

    @classmethod
    def from_arguments(cls, flag_path, root, *, strict=False, warn=None, **options):
        """None when MCP is not configured. An unreadable file disables MCP (strict: fails)."""
        try:
            path = find_config(flag_path, root)
        except ValueError as exc:
            # A flag that names no file is a usage error in every mode.
            raise McpStartupError(str(exc)) from exc
        if path is None:
            return None
        manager = cls([], cwd=root, strict=strict, warn=warn, source=path, **options)
        try:
            servers, problems = load_config(path)
        except ValueError as exc:
            manager._problem(str(exc))
            return None
        for problem in problems:
            manager._problem(f"{path}: {problem}; server skipped")
        manager.servers = servers
        return manager

    def _problem(self, text):
        if self.strict:
            raise McpStartupError(text)
        self.warn(f"MCP warning: {text}")

    # ----- lifecycle ---------------------------------------------------------------------
    def start(self):
        atexit.register(self.close)
        self._install_sigterm()
        try:
            for config in self.servers:
                self._start_server(config)
        except BaseException:
            self.close()
            raise
        return self

    def _start_server(self, config):
        where = f" from {self.source}" if self.source else ""
        self.warn(f"MCP: starting server '{config.name}'{where}: {config.command}")
        transport = self.transport_factory(config, self.cwd)
        client = McpClient(config.name, transport, call_timeout=config.timeout, client_version=self.client_version)
        try:
            transport.start()
            client.initialize(self.startup_timeout)
            tools = client.list_tools(self.startup_timeout)
        except McpError as exc:
            # Stop the server first so its stderr reaches EOF, then wait briefly for the reader
            # thread: reading the buffer earlier races the thread and can miss the last lines.
            transport.close()
            detail = ""
            if hasattr(transport, "stderr_text"):
                # Bounded excerpt of the server's own log; it is not a protocol message.
                tail = transport.stderr_text(wait=STDERR_JOIN_SECONDS).strip()[-STDERR_NOTE_CHARS:]
                detail = f"; server stderr: {tail!r}" if tail else ""
            self._problem(f"server '{config.name}' failed to start ({exc}){detail}; server skipped")
            return
        self.clients[config.name] = client
        self.tools[config.name] = tools

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for client in self.clients.values():
            try:
                client.transport.close()
            except Exception:  # noqa: BLE001 -- Cleanup continues with the remaining servers.
                pass
        atexit.unregister(self.close)
        if self._previous_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, self._previous_sigterm)
            except (ValueError, OSError):
                pass
            self._previous_sigterm = None

    def _install_sigterm(self):
        """atexit does not run on SIGTERM; stop the servers first, then die by the same signal."""
        if os.name == "nt" or threading.current_thread() is not threading.main_thread():
            return
        if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
            return

        def handler(signum, frame):
            self.close()
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)

        self._previous_sigterm = signal.SIG_DFL
        signal.signal(signal.SIGTERM, handler)

    def contract(self):
        """Digests of the servers in use; a new, removed or changed server changes the session contract."""
        by_name = {config.name: config for config in self.servers}
        return {"servers": {name: by_name[name].digest() for name in sorted(self.clients)}}

    # ----- registration ------------------------------------------------------------------
    def register(self, registry, *, allow_write):
        """Add ``mcp__<server>__<tool>`` entries. Returns the registered names."""
        by_name = {config.name: config for config in self.servers}
        for server, client in self.clients.items():
            added, hidden = [], 0
            for tool in self.tools[server]:
                name = tool.get("name") if isinstance(tool, dict) else None
                if not isinstance(name, str) or not name:
                    self._problem(f"server '{server}' listed a tool without a name; tool skipped")
                    continue
                registered = tool_name(server, name)
                read_only = name in by_name[server].read_only_tools
                if not REGISTERED_NAME.match(registered):
                    self._problem(f"{registered!r} is not a usable tool name (letters, digits, _ and -, at most 64 characters); tool skipped")
                    continue
                if registered in registry._tools:
                    self._problem(f"{registered} would replace an existing tool; MCP tool skipped")
                    continue
                if not read_only and not allow_write:
                    # read-only mode exposes only tools the configuration declares readOnly.
                    hidden += 1
                    continue
                schema = tool.get("inputSchema")
                description = tool.get("description") if isinstance(tool.get("description"), str) else ""
                try:
                    if not isinstance(schema, dict):
                        raise ValueError("inputSchema must be an object")
                    registry.register(registered, f"[MCP server {server}] {description}"[:DESCRIPTION_CHARS], schema,
                                      self._handler(client, name), retry_safe=read_only, contextual=True,
                                      mcp_server_id=server)
                except Exception as exc:  # noqa: BLE001 -- Any invalid server schema skips just that tool.
                    self._problem(f"{registered} has an unusable input schema ({type(exc).__name__}); tool skipped")
                    continue
                added.append(registered)
            self.registered.extend(added)
            note = f" ({hidden} not declared readOnly are hidden in read-only mode)" if hidden else ""
            self.warn(f"MCP: server '{server}' ready, {len(added)} tool(s): {', '.join(added) or 'none'}{note}")
        if self.clients:
            # Part of the tool contract: adding, removing or replacing a server changes an unfinished
            # session's configuration and voids approvals recorded against the old servers.
            registry.capability_contract = {**(registry.capability_contract or {}), "mcp": self.contract()}
        return list(self.registered)

    @staticmethod
    def _handler(client, name):
        def call(*, context=None, **arguments):
            # Only the harness dispatch supplies a call id and the policy that authorized it.
            if context is None or getattr(context, "call_id", None) is None or getattr(context, "policy", None) is None:
                raise ToolFailure(ErrorCode.PERMISSION_DENIED, "MCP tools run only through the tool gateway")
            return client.call_tool(name, arguments, cancellation=context.cancellation,
                                    remaining_seconds=getattr(context, "remaining_seconds", None))

        return call
