"""Provider-independent model failures; no SDK objects or response bodies."""

import math


class ModelError(Exception):
    retryable = False

    def __init__(self, message="model request failed", *, status_code=None, retry_after=None):
        if retry_after is not None and (type(retry_after) not in (int, float) or not math.isfinite(retry_after) or retry_after < 0):
            raise ValueError("retry_after must be finite and nonnegative")
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class RateLimitError(ModelError):
    retryable = True


class TransientError(ModelError):
    retryable = True


class ModelTimeoutError(ModelError, TimeoutError):
    retryable = True


class AuthError(ModelError):
    pass


class BadRequestError(ModelError):
    pass


class ContextLengthError(BadRequestError):
    pass


class ResponseParseError(ModelError):
    pass
