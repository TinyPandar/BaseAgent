"""Synchronous bridge from the agent thread to Harbor's async ``environment.exec``.

The agent loop is synchronous and runs in a worker thread; Harbor's environment lives on
the trial's event loop. Each call is scheduled on that loop and polled so that both the
host-side deadline and the cancellation token stop it promptly. Cancelling the scheduled
coroutine makes Harbor terminate its ``docker exec`` client; the in-container ``timeout``
wrapper (when present) stops the process itself.

No environment variables are ever passed: provider credentials stay on the host.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import time

from baseagent.backends.exec_backend import ExecCancelled, ExecOutput, ExecTimeout

POLL_SECONDS = 0.05
# Harbor's own exec timeout is a backstop only; ours fires first.
HARBOR_BACKSTOP = 30


class AsyncExecChannel:
    def __init__(self, environment, loop: asyncio.AbstractEventLoop):
        self.environment = environment
        self.loop = loop

    def __call__(self, command, *, cwd, timeout, cancellation=None):
        if cancellation is not None and cancellation.cancelled():
            raise ExecCancelled()
        coroutine = self.environment.exec(command, cwd=cwd, timeout_sec=math.ceil(timeout) + HARBOR_BACKSTOP)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except BaseException:
            coroutine.close()
            raise
        deadline = time.monotonic() + timeout
        while True:
            try:
                result = future.result(timeout=POLL_SECONDS)
                break
            except concurrent.futures.TimeoutError:
                if cancellation is not None and cancellation.cancelled():
                    future.cancel()
                    raise ExecCancelled() from None
                if time.monotonic() >= deadline:
                    future.cancel()
                    raise ExecTimeout() from None
            except concurrent.futures.CancelledError:
                raise ExecCancelled() from None
        return ExecOutput(int(result.return_code), result.stdout or "", result.stderr or "")
