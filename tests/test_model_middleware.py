import tempfile
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.kernel import CancellationToken, Cancelled, KernelState, ModelRequest, ToolRegistry, iter_agent
from baseagent.kernel.errors import AuthError, RateLimitError, TransientError, ModelTimeoutError
from baseagent.llm import ModelResponse, TokenUsage
from baseagent.middleware import RateLimiter, RateLimitMiddleware, RetryMiddleware
from baseagent.session import SessionStore


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class ModelMiddlewareTests(unittest.TestCase):
    def test_retry_after_jitter_and_nonretryable_errors_with_fake_clock(self):
        clock = Clock()
        retry = RetryMiddleware(model_retries=2, delay=0.5, jitter=0.2, clock=clock, sleep=clock.sleep, random_source=lambda: 1)
        calls = []
        def handler(request):
            calls.append(clock())
            if len(calls) == 1:
                raise RateLimitError("limited", retry_after=2)
            if len(calls) == 2:
                raise TransientError("temporary")
            return "done"
        request = ModelRequest(KernelState(), [], [])
        self.assertEqual(retry.wrap_model_call(request, handler), "done")
        self.assertEqual(calls, [0, 2, 3.2])
        self.assertTrue(all(duration <= 0.100001 for duration in clock.sleeps))
        for error in (AuthError("auth"), ValueError("bad")):
            attempts = []
            def fail(request):
                attempts.append(1)
                raise error
            with self.assertRaises(type(error)):
                retry.wrap_model_call(request, fail)
            self.assertEqual(len(attempts), 1)

    def test_retry_cancellation_interrupts_wait_without_another_attempt(self):
        clock, token, calls = Clock(), CancellationToken(), []
        def sleep(duration):
            clock.sleep(duration)
            token.cancel()
        retry = RetryMiddleware(model_retries=2, delay=1, clock=clock, sleep=sleep)
        def handler(request):
            calls.append(1)
            raise TransientError("temporary")
        with self.assertRaises(Cancelled):
            retry.wrap_model_call(ModelRequest(KernelState(), [], [], token), handler)
        self.assertEqual(calls, [1])
        self.assertLessEqual(clock(), 0.1)

    def test_retry_deadline_reenters_core_without_extra_dispatch(self):
        clock, calls = Clock(), []
        class Model:
            def complete(self, messages, tools):
                calls.append(1)
                raise RateLimitError("synthetic", retry_after=100)
        retry = RetryMiddleware(model_retries=3, delay=1, clock=clock, sleep=clock.sleep)
        with patch("baseagent.agent.scope.time.time", clock):
            state = run_agent(Model(), "synthetic", tools=ToolRegistry(), max_duration_seconds=0.25, middleware=[retry])
        self.assertEqual(state.status, "deadline_exceeded")
        self.assertEqual((len(calls), state.model_calls, state.unknown_usage_calls), (1, 1, 1))
        self.assertAlmostEqual(clock(), 0.25)

    def test_rate_limiter_fake_clock_capacity_cancellation_and_deadline(self):
        clock = Clock()
        limiter = RateLimiter(2, capacity=2, clock=clock, sleep=clock.sleep)
        limiter.acquire()
        limiter.acquire()
        self.assertEqual(clock(), 0)
        limiter.acquire()
        self.assertAlmostEqual(clock(), 0.5)
        with self.assertRaises(ModelTimeoutError):
            limiter.acquire(remaining_seconds=lambda: 0.6 - clock())
        self.assertAlmostEqual(clock(), 0.6)
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(Cancelled):
            limiter.acquire(cancellation=token)

    def test_rate_wait_deadline_does_not_dispatch_or_create_unknown_usage(self):
        clock, calls = Clock(), []
        class Model:
            def complete(self, messages, tools):
                calls.append(1)
                return {"content": "done"}
        limiter = RateLimiter(1, clock=clock, sleep=clock.sleep)
        limiter.acquire()
        with patch("baseagent.agent.scope.time.time", clock):
            state = run_agent(Model(), "synthetic", tools=ToolRegistry(), max_duration_seconds=0.25,
                              middleware=[RateLimitMiddleware(limiter)])
        self.assertEqual(state.status, "deadline_exceeded")
        self.assertEqual((calls, state.model_calls, state.unknown_usage_calls), ([], 0, 0))

    def test_shared_limiter_is_thread_safe_and_bounds_concurrent_requests(self):
        limiter = RateLimiter(50)
        acquired, guard = [], threading.Lock()
        start = threading.Barrier(5)
        def run():
            start.wait(timeout=3)
            limiter.acquire()
            with guard:
                acquired.append(time.monotonic())
        workers = [threading.Thread(target=run) for _ in range(5)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(acquired), 5)
        self.assertGreaterEqual(max(acquired) - min(acquired), 0.075)

    def test_retry_inside_shared_limiter_accounts_every_attempt_and_persistent_event(self):
        clock, times = Clock(), []
        class Model:
            def complete(self, messages, tools):
                times.append(clock())
                if len(times) == 1:
                    raise TransientError("synthetic")
                return ModelResponse({"content": "done"}, TokenUsage(2, 1, 3))
        def layers():
            return [RetryMiddleware(model_retries=1, delay=0),
                    RateLimitMiddleware(RateLimiter(2, clock=clock, sleep=clock.sleep))]
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "session.db")
            state = run_agent(Model(), "synthetic", tools=ToolRegistry(), middleware=layers(), store=store, session_id="fixture")
            self.assertEqual(state.status, "completed")
            self.assertEqual((state.model_calls, state.unknown_usage_calls, state.total_tokens), (2, 1, 3))
            self.assertEqual(len([e for e in store.events("fixture") if e["type"] == "model_started"]), 2)
        self.assertEqual(times, [0, 0.5])
        times.clear()
        state = KernelState(messages=[{"role": "user", "content": "synthetic"}], turn_start=0)
        events = list(iter_agent(Model(), state, tools=ToolRegistry(), hooks=layers()))
        self.assertEqual(state.model_calls, 2)
        self.assertEqual(len([e for e in events if e.type == "model_started"]), 1)
        self.assertEqual(len([e for e in events if e.type == "model_returned"]), 1)

    def test_limiter_and_retry_do_not_bypass_usage_or_call_budgets(self):
        clock = Clock()
        class Model:
            calls = 0
            def complete(self, messages, tools):
                self.calls += 1
                raise TransientError("synthetic")
        for options, expected in [({"max_total_tokens": 20}, "usage_unavailable"), ({"max_model_calls": 1}, "max_model_calls_exceeded")]:
            model = Model()
            state = run_agent(model, "synthetic", tools=ToolRegistry(), **options,
                              middleware=[RetryMiddleware(model_retries=2, delay=0), RateLimitMiddleware(RateLimiter(10, clock=clock, sleep=clock.sleep))])
            self.assertEqual(state.status, expected)
            self.assertEqual((model.calls, state.model_calls), (1, 1))

    def test_invalid_limits_and_jitter_rejected(self):
        for value in (True, 0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                RateLimiter(value)
        for value in (True, -1, 2, float("nan")):
            with self.assertRaises(ValueError):
                RetryMiddleware(jitter=value)

    def test_direct_middleware_deadline_does_not_invoke_unchecked_handler(self):
        calls, clock = [], Clock()
        layer = RateLimitMiddleware(RateLimiter(1))
        request = ModelRequest(KernelState(), [], [], remaining_seconds=lambda: 0)
        with self.assertRaises(ModelTimeoutError):
            layer.wrap_model_call(request, lambda request: calls.append(1))
        self.assertEqual(calls, [])
        retry = RetryMiddleware(model_retries=2, delay=1, clock=clock, sleep=clock.sleep)
        def fail(request):
            calls.append(1)
            raise TransientError("synthetic")
        request = ModelRequest(KernelState(), [], [], remaining_seconds=lambda: 0.2 - clock())
        with self.assertRaises(ModelTimeoutError):
            retry.wrap_model_call(request, fail)
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
