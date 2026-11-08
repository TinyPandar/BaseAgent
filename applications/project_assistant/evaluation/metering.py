"""Usage-metered OpenAI-compatible model calls with a hard CNY cutoff.

Prices are the qwen-plus non-thinking Beijing rates already recorded in
``scripts/record_coding_scenario.py`` (input 0.8 CNY/M, output 2 CNY/M,
reasoning 8 CNY/M). The crossing response may be billed; every later call is
refused. Unknown usage fails closed. Nothing here prints prompts or endpoints.
"""

from decimal import Decimal

from baseagent.kernel.errors import ModelError

INPUT_CNY_PER_MILLION = Decimal("0.8")
OUTPUT_CNY_PER_MILLION = Decimal(2)
REASONING_CNY_PER_MILLION = Decimal(8)


class BudgetReached(ModelError):
    """Raised before a call once the meter has stopped."""


class UsageUnavailable(ModelError):
    """Raised when a provider response cannot be metered safely."""


def _decimal(value, name):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


class MeteredModel:
    """Wrap a model adapter and accumulate reported usage against a budget."""

    def __init__(self, client, *, budget_cny, input_rate=INPUT_CNY_PER_MILLION,
                 output_rate=OUTPUT_CNY_PER_MILLION,
                 reasoning_rate=REASONING_CNY_PER_MILLION, max_calls=None):
        self.client = client
        self.budget = _decimal(budget_cny, "budget")
        self.input_rate = _decimal(input_rate, "input rate")
        self.output_rate = _decimal(output_rate, "output rate")
        self.reasoning_rate = _decimal(reasoning_rate, "reasoning rate")
        if max_calls is not None and (type(max_calls) is not int or max_calls < 1):
            raise ValueError("max_calls must be a positive integer")
        self.max_calls = max_calls
        self.cost_cny = Decimal(0)
        self.last_cost_cny = Decimal(0)
        self.calls = 0
        self.stopped = False
        self.reason = None
        self.prompt_tokens = self.completion_tokens = self.reasoning_tokens = 0
        self.model_name = getattr(client, "model_name", "fake")
        self.generation_params = getattr(client, "generation_params", {})
        self.max_completion_tokens = getattr(client, "max_completion_tokens", None)

    def complete(self, messages, tools=None):
        if (self.stopped or self.cost_cny >= self.budget
                or (self.max_calls is not None and self.calls >= self.max_calls)):
            self.reason = self.reason or "model call or budget limit"
            raise BudgetReached(self.reason)
        self.calls += 1
        try:
            if hasattr(self.client, "complete_with_timeout"):
                response = self.client.complete_with_timeout(
                    messages, tools, max_completion_tokens=self.max_completion_tokens)
            else:
                response = self.client.complete(messages, tools)
        except Exception:  # noqa: BLE001 -- never expose provider diagnostics
            self.reason = "model request failed"
            self.last_cost_cny = Decimal(0)
            raise UsageUnavailable(self.reason) from None
        if response.usage is None:
            self.reason = "model usage unavailable"
            self.last_cost_cny = Decimal(0)
            raise UsageUnavailable(self.reason)
        usage = response.usage
        reasoning = response.reasoning_tokens
        if reasoning > usage.completion_tokens:
            self.reason = "model usage unavailable"
            self.last_cost_cny = Decimal(0)
            raise UsageUnavailable(self.reason)
        # OpenAI completion_tokens includes reasoning; avoid charging it twice.
        cost = (usage.prompt_tokens * self.input_rate
                + (usage.completion_tokens - reasoning) * self.output_rate
                + reasoning * self.reasoning_rate) / Decimal(1_000_000)
        self.cost_cny += cost
        self.last_cost_cny = cost
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.reasoning_tokens += reasoning
        if self.cost_cny >= self.budget:
            self.stopped = True
            self.reason = "budget reached"
        return response
