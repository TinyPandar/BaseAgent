"""Tool definitions and execution boundary."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .result import ErrorCode, ToolFailure, ToolResult


@dataclass(frozen=True)
class ToolAnnotations:
    """Host-side behaviour hints of a tool, following MCP ToolAnnotations semantics.

    Never sent to the model and not part of ``ToolRegistry.contract()``. The defaults
    are the conservative MCP defaults: not read-only, possibly destructive, not
    idempotent, open world. ``destructive`` and ``idempotent`` only describe tools
    that are not read-only; a read-only tool must not be destructive.
    """

    read_only: bool = False
    destructive: bool = True
    idempotent: bool = False
    open_world: bool = True

    def __post_init__(self):
        if any(type(getattr(self, key)) is not bool for key in ANNOTATION_KEYS):
            raise ValueError("tool annotations must be booleans")
        if self.read_only and self.destructive:
            raise ValueError("a read-only tool cannot be destructive")

    def to_dict(self) -> dict[str, bool]:
        return {key: getattr(self, key) for key in ANNOTATION_KEYS}


ANNOTATION_KEYS = ("read_only", "destructive", "idempotent", "open_world")
# Default record of a retry_safe tool registered without explicit annotations.
# open_world stays True: retry_safe says nothing about what the tool reaches.
RETRY_SAFE_ANNOTATIONS = ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=True)
_MCP_HINTS = {"readOnlyHint": "read_only", "destructiveHint": "destructive",
              "idempotentHint": "idempotent", "openWorldHint": "open_world"}


def mcp_annotation_hints(value: Any) -> dict[str, bool]:
    """Boolean MCP ``annotations`` hints as ``{read_only: ...}``; anything else is ignored."""
    if not isinstance(value, dict):
        return {}
    return {key: value[hint] for hint, key in _MCP_HINTS.items() if type(value.get(hint)) is bool}


@dataclass
class ToolRegistry:
    max_result_bytes: int = 256_000
    _tools: dict[str, tuple[dict[str, Any], Callable[..., Any], Draft202012Validator]] = field(default_factory=dict)
    _retry_safe: set[str] = field(default_factory=set)
    _contextual: set[str] = field(default_factory=set)
    # Tools executed by an MCP server: registered name -> server id. Absent means a local handler.
    _mcp_servers: dict[str, str] = field(default_factory=dict)
    capability_contract: dict[str, Any] | None = None
    # Host-side metadata only: never in specs() or contract().
    _annotations: dict[str, ToolAnnotations] = field(default_factory=dict)
    _annotation_hints: dict[str, dict[str, bool]] = field(default_factory=dict)
    # Host-side only: tools whose cut-off call is closed with current facts instead of being run again.
    _interrupted: dict[str, Callable[..., Any]] = field(default_factory=dict)

    def register(self, name: str, description: str, parameters: dict[str, Any], handler: Callable[..., Any], *, retry_safe: bool = False, contextual: bool = False,
                 mcp_server_id: str | None = None, annotations: ToolAnnotations | None = None,
                 annotation_hints: dict[str, bool] | None = None,
                 on_interrupted: Callable[..., Any] | None = None) -> None:
        if name in self._tools:
            raise ValueError(f"duplicate tool: {name}")
        if mcp_server_id is not None and (not isinstance(mcp_server_id, str) or not mcp_server_id):
            raise ValueError("mcp_server_id must be a nonempty string")
        if annotations is not None and not isinstance(annotations, ToolAnnotations):
            raise ValueError("annotations must be a ToolAnnotations record")
        if retry_safe is True and annotations is not None and not (annotations.read_only and annotations.idempotent):
            # retry_safe (parallel, retried, offered to read-only workers) implies read-only and idempotent.
            raise ValueError("a retry_safe tool must be annotated read_only and idempotent")
        if annotation_hints is not None and (not isinstance(annotation_hints, dict) or any(
                key not in ANNOTATION_KEYS or type(value) is not bool for key, value in annotation_hints.items())):
            raise ValueError("annotation hints must map annotation names to booleans")
        if on_interrupted is not None and (annotations is None or annotations.destructive or annotations.open_world):
            # Only a call whose loss cannot hide an outside effect may be closed without reconciliation.
            raise ValueError("on_interrupted needs annotations that are neither destructive nor open world")
        parameters = deepcopy(parameters)
        if type(contextual) is not bool or (contextual and "context" in parameters.get("properties", {})):
            raise ValueError("contextual tools reserve the context argument")
        if parameters.get("type") != "object":
            raise ValueError("tool parameters must describe an object")
        parameters.setdefault("additionalProperties", False)
        Draft202012Validator.check_schema(parameters)
        self._tools[name] = (
            {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}},
            handler,
            Draft202012Validator(parameters),
        )
        if type(retry_safe) is not bool:
            del self._tools[name]
            raise ValueError("retry_safe must be a boolean")
        if retry_safe:
            self._retry_safe.add(name)
        if contextual:
            self._contextual.add(name)
        if mcp_server_id is not None:
            self._mcp_servers[name] = mcp_server_id
        if annotations is None:
            annotations = RETRY_SAFE_ANNOTATIONS if retry_safe else ToolAnnotations()
        self._annotations[name] = annotations
        if annotation_hints:
            self._annotation_hints[name] = dict(annotation_hints)
        if on_interrupted is not None:
            self._interrupted[name] = on_interrupted

    def origin(self, name: str) -> tuple[str, str | None]:
        """(source, mcp_server_id): ("mcp", server) for MCP-backed tools, ("local", None) otherwise."""
        server = self._mcp_servers.get(name)
        return ("mcp", server) if server is not None else ("local", None)

    def retry_safe(self, name: str) -> bool:
        return name in self._retry_safe

    def closes_interrupted(self, name: str) -> bool:
        return name in self._interrupted

    def close_interrupted(self, name: str, arguments: dict[str, Any], *, context=None) -> ToolResult:
        """The result of a call that started but never returned: current facts, the tool does not run."""
        try:
            return self.normalize_result(self._interrupted[name](arguments, context=context))
        except ToolFailure as exc:
            return ToolResult.failure(exc.code, str(exc), retryable=exc.retryable)
        except Exception as exc:
            return ToolResult.failure(ErrorCode.EXECUTION_FAILED, f"tool failed: {type(exc).__name__}")

    def annotations(self, name: str) -> ToolAnnotations:
        """Effective annotations; unknown names get the conservative defaults."""
        return self._annotations.get(name, ToolAnnotations())

    def annotation_hints(self, name: str) -> dict[str, bool]:
        """Untrusted hints a server declared (MCP); informational, never used for authorization."""
        return dict(self._annotation_hints.get(name, {}))

    def annotation_table(self) -> dict[str, dict[str, bool]]:
        return {name: self.annotations(name).to_dict() for name in sorted(self._tools)}

    def subset(self, names):
        """Reuse authorized handlers without replacing implementations/capabilities."""
        result = ToolRegistry(max_result_bytes=self.max_result_bytes)
        result.capability_contract = deepcopy(self.capability_contract)
        for name in names:
            if name not in self._tools:
                raise ValueError(f"tool is not available in parent registry: {name}")
            spec, handler, _ = self._tools[name]
            definition = spec["function"]
            result.register(name, definition["description"], definition["parameters"], handler,
                            retry_safe=self.retry_safe(name), contextual=name in self._contextual,
                            mcp_server_id=self._mcp_servers.get(name), annotations=self.annotations(name),
                            annotation_hints=self._annotation_hints.get(name),
                            on_interrupted=self._interrupted.get(name))
        return result

    def contract(self) -> dict:
        value = {"specs": sorted(self.specs(), key=lambda spec: spec["function"]["name"]), "retry_safe_tools": sorted(self._retry_safe), "contextual_tools": sorted(self._contextual)}
        if self._mcp_servers:
            # Only present with MCP tools, so sessions without MCP keep their contract digest.
            value["mcp_tools"] = dict(sorted(self._mcp_servers.items()))
        if self.capability_contract is not None:
            value["capabilities"] = deepcopy(self.capability_contract)
        return value

    def specs(self) -> list[dict[str, Any]]:
        return [deepcopy(spec) for spec, _, _ in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any], *, context=None) -> ToolResult:
        entry = self._tools.get(name)
        if entry is None:
            return ToolResult.failure(ErrorCode.UNKNOWN_TOOL, f"unknown tool: {name}")
        try:
            # Library callers bypass the model's strict JSON parsing boundary.
            # Reject non-JSON/non-finite values before any handler side effect.
            json.dumps(arguments, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            return ToolResult.failure(ErrorCode.INVALID_ARGUMENTS, "arguments must be finite JSON data")
        try:
            entry[2].validate(arguments)
        except ValidationError as exc:
            location = ".".join(map(str, exc.absolute_path)) or "$"
            # Do not echo parameter values (which may contain secrets) into errors.
            return ToolResult.failure(ErrorCode.INVALID_ARGUMENTS, f"{location}: schema rule '{exc.validator}' failed")
        try:
            if name in self._contextual:
                from .tool_context import ToolContext
                if context is not None and not isinstance(context, ToolContext):
                    return ToolResult.failure(ErrorCode.EXECUTION_FAILED, "invalid execution context")
                result = entry[1](context=context, **arguments)
            else:
                result = entry[1](**arguments)
        except ToolFailure as exc:
            return ToolResult.failure(exc.code, str(exc), retryable=exc.retryable)
        except PermissionError:
            return ToolResult.failure(ErrorCode.PERMISSION_DENIED, "access denied")
        except FileNotFoundError:
            return ToolResult.failure(ErrorCode.NOT_FOUND, "file or executable not found")
        except TimeoutError:
            return ToolResult.failure(ErrorCode.TIMEOUT, "tool timed out")
        except Exception as exc:
            return ToolResult.failure(ErrorCode.EXECUTION_FAILED, f"tool failed: {type(exc).__name__}")
        return self.normalize_result(result)

    def normalize_result(self, result: Any) -> ToolResult:
        result = result if isinstance(result, ToolResult) else ToolResult(data=result)
        try:
            value = result.to_dict()
            ToolResult.from_dict(value)
            serialized = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            return ToolResult.failure(ErrorCode.INVALID_RESULT, "tool result must satisfy the result contract and serialize with finite numbers")
        if len(serialized) > self.max_result_bytes:
            return ToolResult.failure(ErrorCode.LIMIT_EXCEEDED, "tool result exceeds transcript size limit")
        return result
