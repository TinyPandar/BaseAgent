"""MCP client for the tools subset: initialize, tools/list, tools/call.

Lifecycle follows the 2025-11-25 revision (initialize handshake). Result
handling also accepts the 2026-07-28 shapes: ``resultType`` (absent means
complete; ``input_required`` is reported as a failure because elicitation and
sampling are not implemented) and cursor pagination of ``tools/list``.

Nothing here decides whether a call may run. ``call_tool`` is reached only
from the handler that ``McpManager`` registers in the ToolRegistry.
"""

from __future__ import annotations

from baseagent.kernel.result import ErrorCode, ToolFailure, ToolResult

from .transport import McpError, Transport

PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_TOOLS = 200
MAX_PAGES = 20
ERROR_TEXT_CHARS = 2000


class McpClient:
    def __init__(self, server_id: str, transport: Transport, *, call_timeout: float = 60.0, client_version: str = "0"):
        self.server_id, self.transport = server_id, transport
        self.call_timeout, self.client_version = call_timeout, client_version
        self.server_info: dict = {}

    def initialize(self, timeout: float) -> None:
        result = self.transport.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "baseagent", "version": self.client_version}}, timeout=timeout)
        version = result.get("protocolVersion")
        if version not in SUPPORTED_VERSIONS:
            raise McpError("protocol", f"unsupported protocol version {version!r}")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
            raise McpError("protocol", "server does not declare the tools capability")
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        self.transport.notify("notifications/initialized")

    def list_tools(self, timeout: float) -> list[dict]:
        tools, cursor = [], None
        for _ in range(MAX_PAGES):
            result = self.transport.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=timeout)
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpError("protocol", "tools/list result has no tools array")
            tools.extend(page)
            if len(tools) > MAX_TOOLS:
                raise McpError("protocol", f"server lists more than {MAX_TOOLS} tools")
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                return tools
        raise McpError("protocol", "tools/list pagination did not end")

    def call_tool(self, name: str, arguments: dict, *, cancellation=None, remaining_seconds=None) -> ToolResult:
        who = f"MCP server '{self.server_id}'"
        timeout = self.call_timeout
        remaining = remaining_seconds() if remaining_seconds is not None else None
        if remaining is not None:
            if remaining <= 0:
                raise ToolFailure(ErrorCode.TIMEOUT, "turn deadline expired before the MCP call")
            timeout = min(timeout, remaining)
        try:
            result = self.transport.request("tools/call", {"name": name, "arguments": arguments},
                                            timeout=timeout, cancellation=cancellation)
        except McpError as exc:
            if exc.kind == "timeout":
                raise ToolFailure(ErrorCode.TIMEOUT, f"{who}: {exc}; the call may still have had effects") from exc
            if exc.kind == "cancelled":
                raise ToolFailure(ErrorCode.CANCELLED, f"{who}: call cancelled; the call may still have had effects") from exc
            if exc.kind == "rpc":
                code = ErrorCode.INVALID_ARGUMENTS if exc.code == -32602 else ErrorCode.EXECUTION_FAILED
                raise ToolFailure(code, f"{who} returned error {exc.code}: {exc}") from exc
            raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"{who}: {exc}") from exc
        kind = result.get("resultType", "complete")
        if kind == "input_required":
            raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"{who} asked for additional input, which this client does not support")
        content = result.get("content", [])
        if kind != "complete" or not isinstance(content, list):
            raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"{who} returned a malformed tools/call result")
        texts, omitted = [], []
        for block in content:
            if not isinstance(block, dict):
                raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"{who} returned a malformed content block")
            resource = block.get("resource")
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "resource" and isinstance(resource, dict) and isinstance(resource.get("text"), str):
                texts.append(resource["text"])
            else:
                # Images, audio and links are named, not inlined into the transcript.
                omitted.append({key: block[key] for key in ("type", "mimeType", "uri") if isinstance(block.get(key), str)})
        text = "\n".join(texts)
        if result.get("isError") is True:
            return ToolResult.failure(ErrorCode.EXECUTION_FAILED, text[:ERROR_TEXT_CHARS] or "MCP tool reported an error")
        data = {"text": text}
        if "structuredContent" in result:
            data["structured"] = result["structuredContent"]
        if omitted:
            data["omitted_content"] = omitted
        return ToolResult(data=data)
