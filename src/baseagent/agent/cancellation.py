"""Cooperative cancellation shared by core, middleware and tool handlers."""

import threading
import time


class Cancelled(BaseException):
    """Control flow, intentionally not normalized as an ordinary tool error."""


class CancellationToken:
    def __init__(self, predicate=None):
        self._event = threading.Event()
        self._predicate = predicate

    def cancel(self):
        self._event.set()

    def cancelled(self):
        return self._event.is_set() or bool(self._predicate and self._predicate())

    def check(self):
        if self.cancelled():
            raise Cancelled()

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while True:
            self.check()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            self._event.wait(min(0.1, remaining))
