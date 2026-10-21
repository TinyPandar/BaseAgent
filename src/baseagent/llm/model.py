import json
import time

from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    DefaultHttpxClient,
    OpenAI,
)

from baseagent.kernel.errors import (
    ModelTimeoutError,
    ResponseParseError,
    TransientError,
)

from ._common import generation_params, http_error, positive_seconds
from .reservation import ReservationUnavailable, TokenReservation, request_digest
from .response import ModelResponse, TokenUsage


class Model:
    def __init__(self, model_name: str, api_key: str, base_url: str | None = None, *, token_counter=None, max_completion_tokens=512, request_bound=None, token_estimator=None, timeout=30.0, generation=None, extra_body=None):
        if extra_body is not None and not isinstance(extra_body, dict):
            raise ValueError("extra_body must be a JSON object")
        self.extra_body = json.loads(json.dumps(extra_body, allow_nan=False))
        self.model_name = model_name
        self.token_estimator = token_estimator
        self.timeout = positive_seconds(timeout)
        self.generation_params = generation_params(generation, {"temperature", "top_p", "stop", "seed", "frequency_penalty", "presence_penalty"})
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
            timeout=self.timeout,
            http_client=DefaultHttpxClient(follow_redirects=False, trust_env=False),
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
            kwargs["timeout"] = min(self.timeout, positive_seconds(timeout))
        if max_completion_tokens is not None:
            kwargs["max_tokens"] = max_completion_tokens
        kwargs.update(self.generation_params)
        if self.extra_body is not None:
            kwargs["extra_body"] = self.extra_body
        try:
            response = self.client.chat.completions.create(**kwargs)
        except APITimeoutError:
            raise ModelTimeoutError("model transport timed out") from None
        except APIConnectionError:
            raise TransientError("model transport connection failed") from None
        except APIStatusError as exc:
            raise http_error(exc.status_code, exc.body, exc.response.headers) from None
        except (json.JSONDecodeError, APIResponseValidationError):
            raise ResponseParseError("model response is not valid chat completion JSON") from None
        usage = None
        reasoning_tokens = 0
        if getattr(response, "usage", None) is not None:
            try:
                usage = TokenUsage(response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens)
                details = getattr(response.usage, "completion_tokens_details", None)
                reasoning_tokens = getattr(details, "reasoning_tokens", None)
                if reasoning_tokens is None:
                    reasoning_tokens = getattr(response.usage, "reasoning_tokens", 0) or 0
            except (AttributeError, TypeError, ValueError):
                pass
        try:
            message = response.choices[0].message
            ModelResponse(message, usage, reasoning_tokens).to_dict()
        except (AttributeError, IndexError, TypeError, ValueError):
            raise ResponseParseError("model response has an invalid message envelope") from None
        return ModelResponse(message, usage, reasoning_tokens)

    def complete_with_control(self, messages, tools=None, *, cancellation, timeout=None):
        cancellation.check()
        try:
            return self.complete_with_timeout(messages, tools, timeout=timeout)
        except Exception:
            cancellation.check()
            raise

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __repr__(self):
        return f"<{type(self).__name__}>"
