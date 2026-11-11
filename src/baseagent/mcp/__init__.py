"""Hand-written MCP client for the tools subset (stdio). See docs/mcp.md."""

from .client import McpClient
from .config import McpConfigError, ServerConfig, find_config, load_config
from .manager import McpManager, McpStartupError, tool_name
from .transport import McpError, StdioTransport, Transport

__all__ = ["McpClient", "McpConfigError", "McpError", "McpManager", "McpStartupError", "ServerConfig",
           "StdioTransport", "Transport", "find_config", "load_config", "tool_name"]
