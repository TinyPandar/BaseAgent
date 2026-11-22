"""Small shared adapter policies. Diagnostics deliberately omit provider bodies."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
from urllib.parse import urlsplit

from baseagent.kernel.errors import (AuthError, BadRequestError, ContextLengthError,
                                    RateLimitError, TransientError, ModelTimeoutError)


# Assistant-message key holding the exact Anthropic content blocks (thinking with
# signatures, text, tool_use) of a response; only AnthropicModel reads it.
ANTHROPIC_CONTENT_KEY = "anthropic_content"


def without_provider_blocks(messages):
    """Drop Anthropic-only blocks before an OpenAI-compatible request.

    Returns the very same list when no message carries them, so existing
    requests (and cassettes) stay byte-identical.
    """
    if not isinstance(messages, list) or not any(isinstance(message, dict) and ANTHROPIC_CONTENT_KEY in message
                                                 for message in messages):
        return messages
    return [{key: value for key, value in message.items() if key != ANTHROPIC_CONTENT_KEY}
            if isinstance(message, dict) else message for message in messages]


def positive_seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be finite and positive")
    return value


def environment_key(name):
    if not isinstance(name, str) or not name:
        raise ValueError("api_key_env must name an environment variable")
    key = os.getenv(name)
    if not key:
        raise AuthError("model credential environment variable is not set")
    if not key.isascii() or any(character.isspace() for character in key):
        raise AuthError("model credential has an invalid header format")
    return key


def endpoint(url):
    try:
        parts = urlsplit(url)
        if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
            raise ValueError()
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("endpoint requires HTTPS (HTTP is allowed only for loopback tests), without credentials/query/fragment") from None
    return url.rstrip("/")


def generation_params(value, allowed):
    if value is None:
        return {}
    if not isinstance(value, dict) or not set(value) <= set(allowed):
        raise ValueError("unsupported generation parameter")
    return json.loads(json.dumps(value, allow_nan=False))


def retry_after(value):
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def http_error(status, body=None, headers=None):
    # Inspect a body only to classify it; never interpolate it into diagnostics.
    try:
        details = json.dumps(body).lower()
    except (TypeError, ValueError):
        details = ""
    if status in {401, 403}:
        cls = AuthError
    elif status == 429:
        cls = RateLimitError
    elif status in {408, 504}:
        cls = ModelTimeoutError
    elif status == 409 or 500 <= status <= 599:
        # 409 (conflict, for example a provider-side concurrent request) is transient like 5xx.
        cls = TransientError
    elif status in {400, 413, 422} and any(term in details for term in ("context_length", "context length", "context window", "too many tokens", "prompt is too long")):
        cls = ContextLengthError
    else:
        cls = BadRequestError
    return cls(f"model HTTP request failed ({status})", status_code=status,
               retry_after=retry_after(headers.get("retry-after")) if headers else None)
