"""Opt-in bounded retries; every real dispatch still passes core budgets."""

import time
import random
import math

from .middleware import AgentMiddleware
from .waiting import wait
from baseagent.kernel.errors import ModelError, ModelTimeoutError
from baseagent.llm._common import retry_after


class RetryMiddleware(AgentMiddleware):
    def __init__(self, *, model_retries: int = 0, tool_retries: int = 0, delay: float = 0.25,
                 jitter=0.0, clock=time.monotonic, sleep=time.sleep, random_source=random.random):
        if any(type(value) is not int or not 0 <= value <= 4 for value in (model_retries, tool_retries)):
            raise ValueError("retry counts must be integers from 0 to 4")
        if type(delay) not in (int, float) or not 0 <= delay <= 8:
            raise ValueError("retry delay must be between 0 and 8 seconds")
        self.model_retries, self.tool_retries, self.delay = model_retries, tool_retries, delay
        if type(jitter) not in (int, float) or not math.isfinite(jitter) or not 0 <= jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")
        self.jitter, self.clock, self.sleep, self.random_source = jitter, clock, sleep, random_source

    def _wait(self, attempt, cancellation=None, remaining_seconds=None, retry_after_seconds=None):
        seconds = min(8, self.delay * 2 ** attempt)
        seconds += seconds * self.jitter * self.random_source()
        if retry_after_seconds is not None:
            seconds = max(seconds, retry_after_seconds)
        return wait(seconds, cancellation=cancellation, remaining_seconds=remaining_seconds,
                    clock=self.clock, sleep=self.sleep)

    def wrap_model_call(self, request, handler):
        for attempt in range(self.model_retries + 1):
            try:
                return handler(request)
            except Exception as exc:
                retryable = isinstance(exc, (TimeoutError, ConnectionError)) or getattr(exc, "status_code", None) in {408, 409, 429, 500, 502, 503, 504}
                # The OpenAI-compatible adapter's transport errors have no status.
                from openai import APIConnectionError
                retryable = retryable or isinstance(exc, APIConnectionError)
                if isinstance(exc, ModelError):
                    retryable = exc.retryable
                if not retryable or attempt == self.model_retries:
                    raise
                after = retry_after(getattr(exc, "retry_after", None))
                if after is None:
                    headers = getattr(getattr(exc, "response", None), "headers", None)
                    after = retry_after(headers.get("retry-after")) if headers else None
                if not self._wait(attempt, request.cancellation, request.remaining_seconds, after):
                    raise ModelTimeoutError("retry wait reached task deadline")

    def wrap_tool_call(self, request, handler):
        for attempt in range(self.tool_retries + 1):
            before_calls = request.state.tool_calls
            before_unsafe = request.state.unsafe_tool_calls
            result = handler(request)
            # Check actual terminal dispatches, including inner redirects/retries.
            # A safe original name alone cannot authorize retrying a redirected write.
            safe = request.state.tool_calls > before_calls and request.state.unsafe_tool_calls == before_unsafe
            if result.ok or not result.error.retryable or not safe or attempt == self.tool_retries:
                return result
            if not self._wait(attempt, request.cancellation, request.remaining_seconds):
                raise ModelTimeoutError("retry wait reached task deadline")
