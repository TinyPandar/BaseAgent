import time
from openai import OpenAI, DefaultHttpxClient
from .response import ModelResponse, TokenUsage
from .reservation import TokenReservation, ReservationUnavailable, request_digest

class Model:
    def __init__(self, model_name: str, api_key: str, base_url: str | None = None, *, token_counter=None, max_completion_tokens=512, request_bound=None, token_estimator=None):
        self.model_name = model_name
        self.token_estimator = token_estimator
        if token_estimator is not None and (token_counter is not None or request_bound is not None):
            raise ValueError("estimated and strict counters cannot be combined")
        self.token_counter = token_counter
        self.request_bound = request_bound
        self._bound_verified = False
        if request_bound is not None:
            if token_counter is not None or request_bound.model_name != model_name:
                raise ValueError("request bound must match the model and cannot be combined with a token counter")
            request_bound.validate_endpoint(base_url)
            max_completion_tokens = request_bound.max_completion_tokens
        if type(max_completion_tokens) is not int or max_completion_tokens < 1:
            raise ValueError("completion token cap must be a positive integer")
        self.max_completion_tokens = max_completion_tokens
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
            timeout=30.0,
            **({"http_client": DefaultHttpxClient(follow_redirects=False, trust_env=False)} if request_bound is not None else {}),
        )

    def complete(self, messages, tools=None):
        return self.complete_with_timeout(messages, tools)

    def estimate_request(self, messages, tools):
        if self.token_estimator is None:
            raise ReservationUnavailable("adapter has no local tokenizer estimator")
        return self.token_estimator.reserve_request(messages, tools, self.max_completion_tokens)

    def reserve_request(self, messages, tools):
        if self.request_bound is not None:
            return self.request_bound.reserve_request(messages, tools)
        if self.token_counter is None:
            raise ReservationUnavailable("adapter needs a trusted provider-specific prompt token upper-bound counter")
        prompt = self.token_counter(messages, tools)
        if type(prompt) is not int or prompt < 0:
            raise ReservationUnavailable("token counter returned an invalid prompt bound")
        return TokenReservation(prompt + self.max_completion_tokens, self.max_completion_tokens, request_digest(messages, tools))

    def complete_reserved(self, messages, tools, *, reservation, cancellation, timeout=None):
        reservation.validate(messages, tools)
        cancellation.check()
        if self.request_bound is not None:
            if reservation != self.request_bound.reserve_request(messages, tools):
                raise ReservationUnavailable("reservation does not match the pinned capacity profile")
            started = time.monotonic()
            if not self._bound_verified:
                self.request_bound.verify(self.client, timeout=min(5.0, timeout) if timeout is not None else 5.0)
                self._bound_verified = True
            cancellation.check()
            if timeout is not None:
                timeout -= time.monotonic() - started
                if timeout <= 0:
                    raise TimeoutError("deadline elapsed during model metadata verification")
        return self.complete_with_timeout(messages, tools, timeout=timeout, max_completion_tokens=reservation.max_completion_tokens)

    def complete_with_timeout(self, messages, tools=None, *, timeout=None, max_completion_tokens=None):
        kwargs = {"model": self.model_name, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if timeout is not None:
            kwargs["timeout"] = min(30.0, timeout)
        if max_completion_tokens is not None:
            kwargs["max_tokens"] = max_completion_tokens
        response = self.client.chat.completions.create(**kwargs)
        usage = None
        if response.usage is not None:
            try:
                usage = TokenUsage(response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens)
            except (TypeError, ValueError):
                pass
        return ModelResponse(response.choices[0].message, usage)
