"""Structural model boundary; concrete clients remain outside the kernel."""

from typing import Any, Protocol


class Model(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any: ...
