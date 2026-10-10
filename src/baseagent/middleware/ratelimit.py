"""Shared thread-safe token bucket; no implicit global limiter."""

import math
import threading
import time

from .middleware import AgentMiddleware
from .waiting import wait
from baseagent.kernel.errors import ModelTimeoutError


class RateLimiter:
    def __init__(self, requests_per_second, *, capacity=1, clock=time.monotonic, sleep=time.sleep):
        if type(requests_per_second) not in (int, float) or not math.isfinite(requests_per_second) or requests_per_second <= 0:
            raise ValueError("requests_per_second must be finite and positive")
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self.rate, self.capacity = requests_per_second, capacity
        self.clock, self.sleep = clock, sleep
        self._tokens, self._updated = float(capacity), clock()
        self._lock = threading.Lock()

    def acquire(self, *, cancellation=None, remaining_seconds=None):
        while True:
            if cancellation is not None:
                cancellation.check()
            if remaining_seconds is not None:
                remaining = remaining_seconds()
                if remaining is not None and remaining <= 0:
                    raise ModelTimeoutError("rate limit wait reached task deadline")
            with self._lock:
                now = self.clock()
                self._tokens = min(self.capacity, self._tokens + max(0, now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                seconds = (1 - self._tokens) / self.rate
            if not wait(seconds, cancellation=cancellation, remaining_seconds=remaining_seconds, clock=self.clock, sleep=self.sleep):
                raise ModelTimeoutError("rate limit wait reached task deadline")


class RateLimitMiddleware(AgentMiddleware):
    def __init__(self, limiter):
        if not isinstance(limiter, RateLimiter):
            raise TypeError("limiter must be RateLimiter")
        self.limiter = limiter

    def wrap_model_call(self, request, handler):
        self.limiter.acquire(cancellation=request.cancellation, remaining_seconds=request.remaining_seconds)
        return handler(request)
