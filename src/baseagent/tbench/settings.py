"""Agent kwargs for the Harbor adapter, validated without importing Harbor."""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation

from baseagent.agent.workers import INPUT_CNY_PER_MILLION, OUTPUT_CNY_PER_MILLION
from baseagent.tools.workspace import DEFAULT_COMMAND_TIMEOUT

HARBOR_VERSION = "0.26.0"
AGENT_IMPORT_PATH = "baseagent.tbench.harbor_agent:BaseAgentHarborAgent"
SCRIPTED_VARIANTS = ("correct", "wrong")
# Hard ceiling for one command; the task's remaining wall clock is always the tighter cap.
COMMAND_TIMEOUT_CEILING = 3600


def _integer(name, value, minimum, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if number < minimum or (maximum is not None and number > maximum):
        raise ValueError(f"{name} must be between {minimum} and {maximum}" if maximum else f"{name} must be at least {minimum}")
    return number


def _decimal(name, value, *, positive=True):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a decimal number") from None
    if not number.is_finite() or (positive and not number > 0):
        raise ValueError(f"{name} must be a positive number")
    return number


@dataclass(frozen=True)
class AgentSettings:
    scripted: str | None = None
    budget_cny: Decimal | None = None
    max_steps: int = 60
    max_model_calls: int | None = None
    max_tool_calls: int = 200
    max_command_timeout_sec: int = DEFAULT_COMMAND_TIMEOUT
    agent_timeout_sec: float | None = None
    input_cny_per_million: Decimal = INPUT_CNY_PER_MILLION
    output_cny_per_million: Decimal = OUTPUT_CNY_PER_MILLION
    usd_per_cny: Decimal | None = None

    FIELDS = ("scripted", "budget_cny", "max_steps", "max_model_calls", "max_tool_calls", "max_command_timeout_sec",
              "agent_timeout_sec", "input_cny_per_million", "output_cny_per_million", "usd_per_cny")

    @classmethod
    def from_kwargs(cls, kwargs: dict) -> "AgentSettings":
        unknown = set(kwargs) - set(cls.FIELDS)
        if unknown:
            raise ValueError(f"unknown BaseAgent kwargs: {sorted(unknown)}")
        values = {}
        for key, value in kwargs.items():
            if value is None or value == "":
                continue
            if key == "scripted":
                if value not in SCRIPTED_VARIANTS:
                    raise ValueError(f"scripted must be one of {SCRIPTED_VARIANTS}")
                values[key] = value
            elif key in ("max_steps", "max_tool_calls", "max_model_calls"):
                values[key] = _integer(key, value, 1)
            elif key == "max_command_timeout_sec":
                values[key] = _integer(key, value, 1, COMMAND_TIMEOUT_CEILING)
            elif key == "agent_timeout_sec":
                values[key] = float(_decimal(key, value))
            else:
                values[key] = _decimal(key, value)
        settings = cls(**values)
        if settings.scripted is None and settings.budget_cny is None:
            raise ValueError("a real model run requires budget_cny (paid calls need an explicit CNY cap)")
        return settings

    def as_kwargs(self) -> dict:
        """JSON-ready kwargs for a Harbor job config (only values that differ from defaults)."""
        result = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if value is None or value == item.default:
                continue
            result[item.name] = str(value) if isinstance(value, Decimal) else value
        return result
