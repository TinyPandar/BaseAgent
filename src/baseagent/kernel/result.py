"""Stable result contract shared by tools, middleware, and the model transcript."""

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    LIMIT_EXCEEDED = "limit_exceeded"
    TIMEOUT = "timeout"
    EXECUTION_FAILED = "execution_failed"
    INVALID_RESULT = "invalid_result"
    CONFLICT = "conflict"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ToolError:
    code: ErrorCode
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class ToolResult:
    data: Any = None
    error: ToolError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def failure(cls, code: ErrorCode, message: str, *, data: Any = None, retryable: bool = False) -> "ToolResult":
        return cls(data=data, error=ToolError(code, message, retryable))

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "data": self.data, "error": asdict(self.error) if self.error else None}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ToolResult":
        if not isinstance(value, dict) or set(value) != {"ok", "data", "error"} or type(value["ok"]) is not bool:
            raise ValueError("result must contain ok (boolean), data, and error")
        error = value["error"]
        if value["ok"]:
            if error is not None:
                raise ValueError("successful result must have a null error")
            return cls(data=value["data"])
        if not isinstance(error, dict) or set(error) != {"code", "message", "retryable"}:
            raise ValueError("failed result requires error code, message, and retryable")
        if not isinstance(error["message"], str) or type(error["retryable"]) is not bool:
            raise ValueError("invalid error field types")
        return cls.failure(ErrorCode(error["code"]), error["message"], data=value["data"], retryable=error["retryable"])


class ToolFailure(Exception):
    """An expected tool failure, distinct from a programming error."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
