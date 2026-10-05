"""Sequential execution-tree budgets shared at actual dispatch boundaries."""

import time

from baseagent.llm.estimation import TokenEstimate
from .errors import BudgetExceeded
from .state import RunStatus


COUNTERS = ("model_calls", "tool_calls", "unsafe_tool_calls", "prompt_tokens", "completion_tokens", "total_tokens", "unknown_usage_calls")


class ExecutionScope:
    def __init__(self, state, ancestors=(), *, cancellation=None):
        self.state, self.ancestors = state, tuple(ancestors)
        self.members = (state, *self.ancestors)
        self.cancellation = cancellation
        if self.preauthorize_model and self.estimate_model:
            raise ValueError("estimated and strict budgets cannot be mixed across task scopes")
        if (self.preauthorize_model or self.estimate_model) and not any(member.max_total_tokens is not None for member in self.members):
            raise ValueError("scoped model preauthorization requires a token budget")
        if len({id(member) for member in self.members}) != len(self.members):
            raise ValueError("execution scope cannot repeat a node")
        if len({member.turn_id for member in self.members}) != len(self.members):
            raise ValueError("execution scope nodes require distinct turn IDs")
        for member in self.members:
            if member.session_id != state.session_id or member.workspace_root != state.workspace_root:
                raise ValueError("execution scope must stay in one session and workspace")
            if member.max_duration_seconds is not None and member.turn_started_at is None:
                raise ValueError("a scoped deadline requires its original start time")
            member.metadata.setdefault("direct_usage", {key: getattr(member, key) for key in COUNTERS})

    @property
    def preauthorize_model(self):
        return any(member.preauthorize_model for member in self.members)

    @property
    def estimate_model(self):
        return any(member.estimate_model for member in self.members)

    def cancelled(self):
        return bool(self.cancellation and self.cancellation.cancelled())

    def remaining_seconds(self):
        deadlines = [member.turn_started_at + member.max_duration_seconds for member in self.members if member.max_duration_seconds is not None]
        return min(deadlines) - time.time() if deadlines else None

    def check(self):
        if self.cancellation:
            self.cancellation.check()
        if any(member.metadata.get("request_bound_violation") for member in self.members):
            raise BudgetExceeded(RunStatus.REQUEST_BOUND_VIOLATED)
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise BudgetExceeded(RunStatus.DEADLINE_EXCEEDED)
        for member in self.members:
            if member.max_total_tokens is not None:
                if member.unknown_usage_calls:
                    raise BudgetExceeded(RunStatus.USAGE_UNAVAILABLE)
                if member.total_tokens > member.max_total_tokens:
                    raise BudgetExceeded(RunStatus.MAX_TOKENS_EXCEEDED)

    def admit_model(self, reservation=None):
        for member in self.members:
            if member.model_calls >= member.max_model_calls:
                raise BudgetExceeded(RunStatus.MAX_MODEL_CALLS_EXCEEDED)
            if member.max_total_tokens is not None:
                required = reservation.total_tokens if reservation is not None else 1
                if member.total_tokens + sum(member.model_reservations.values()) + required > member.max_total_tokens:
                    raise BudgetExceeded(RunStatus.MAX_TOKENS_EXCEEDED)

    def admit_tool(self):
        if any(member.tool_calls >= member.max_tool_calls for member in self.members):
            raise BudgetExceeded(RunStatus.MAX_TOOL_CALLS_EXCEEDED)

    def _charge(self, key, amount):
        for member in self.members:
            setattr(member, key, getattr(member, key) + amount)
        self.state.metadata["direct_usage"][key] += amount

    def model_started(self, reservation):
        self._charge("model_calls", 1)
        self._charge("unknown_usage_calls", 1)
        attempt = str(self.state.model_calls)
        keys = (attempt, *(f"{self.state.turn_id}:{attempt}" for _ in self.ancestors))
        if reservation is not None:
            for member, key in zip(self.members, keys):
                member.model_reservations[key] = reservation.total_tokens
                if isinstance(reservation, TokenEstimate):
                    member.metadata.setdefault("estimated_reservations", {})[key] = reservation.total_tokens
        return keys

    def model_returned(self, usage, reservation, keys):
        if usage is None:
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self._charge(key, getattr(usage, key))
        self._charge("unknown_usage_calls", -1)
        if reservation is not None:
            for member, key in zip(self.members, keys):
                member.model_reservations.pop(key, None)
                member.metadata.get("estimated_reservations", {}).pop(key, None)
                if isinstance(reservation, TokenEstimate):
                    member.metadata["last_token_estimate"] = {"node_turn_id": self.state.turn_id, "attempt": self.state.model_calls,
                        "estimated_total_tokens": reservation.total_tokens, "reported_total_tokens": usage.total_tokens,
                        "error_tokens": usage.total_tokens - reservation.total_tokens}
            if not isinstance(reservation, TokenEstimate) and (usage.total_tokens > reservation.total_tokens or usage.completion_tokens > reservation.max_completion_tokens):
                for member in self.members:
                    member.metadata["request_bound_violation"] = {
                        "node_turn_id": self.state.turn_id, "attempt": self.state.model_calls,
                        "reserved_tokens": reservation.total_tokens, "reported_tokens": usage.total_tokens,
                        "max_completion_tokens": reservation.max_completion_tokens,
                        "reported_completion_tokens": usage.completion_tokens,
                    }

    def tool_started(self, *, retry_safe):
        self._charge("tool_calls", 1)
        if not retry_safe:
            self._charge("unsafe_tool_calls", 1)
