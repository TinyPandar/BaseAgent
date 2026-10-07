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


@dataclass
class ToolRegistry:
    max_result_bytes: int = 256_000
    _tools: dict[str, tuple[dict[str, Any], Callable[..., Any], Draft202012Validator]] = field(default_factory=dict)
    _retry_safe: set[str] = field(default_factory=set)
    _contextual: set[str] = field(default_factory=set)
    capability_contract: dict[str, Any] | None = None

    def register(self, name: str, description: str, parameters: dict[str, Any], handler: Callable[..., Any], *, retry_safe: bool = False, contextual: bool = False) -> None:
        if name in self._tools:
            raise ValueError(f"duplicate tool: {name}")
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

    def retry_safe(self, name: str) -> bool:
        return name in self._retry_safe

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
                            retry_safe=self.retry_safe(name), contextual=name in self._contextual)
        return result

    def contract(self) -> dict:
        value = {"specs": sorted(self.specs(), key=lambda spec: spec["function"]["name"]), "retry_safe_tools": sorted(self._retry_safe), "contextual_tools": sorted(self._contextual)}
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
