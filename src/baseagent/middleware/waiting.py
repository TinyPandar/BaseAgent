"""Interruptible waits shared by model retry and rate limiting."""

import time


def wait(seconds, *, cancellation=None, remaining_seconds=None, clock=time.monotonic, sleep=time.sleep):
    """Return False at the task deadline; caller re-enters core budget checks."""
    end = clock() + seconds
    while True:
        if cancellation is not None:
            cancellation.check()
        available = remaining_seconds() if remaining_seconds else None
        if available is not None and available <= 0:
            return False
        duration = end - clock()
        if duration <= 0:
            return True
        chunk = min(0.1, duration, available if available is not None else duration)
        if cancellation is not None and sleep is time.sleep:
            cancellation.wait(chunk)
        else:
            sleep(chunk)
