"""Versioned provider capacity bounds, deliberately not tokenizer estimates.

DeepSeek documents context_window as the total input+output token capacity:
https://api-docs.deepseek.com/api/list-models/
The profile is checked against live official metadata before generation.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

from .reservation import ReservationUnavailable, TokenReservation, request_digest


@dataclass(frozen=True)
class DeepSeekContextBound:
    model_name: str
    max_completion_tokens: int = 4096
    profile = "deepseek-context-v1"
    context_window = 1048576
    max_output_tokens = 393216
    versions = {"deepseek-flash": "DeepSeek-V4.1-Flash", "deepseek-v4-pro": "DeepSeek-V4-Pro"}

    def __post_init__(self):
        if self.model_name not in self.versions:
            raise ReservationUnavailable("capacity profile supports only canonical DeepSeek Flash/Pro names")
        if type(self.max_completion_tokens) is not int or not 1 <= self.max_completion_tokens <= self.max_output_tokens:
            raise ReservationUnavailable("capacity profile output cap must be 1-393216 tokens")

    def validate_endpoint(self, base_url):
        try:
            value = urlsplit(base_url or "")
            valid = (value.scheme == "https" and value.hostname == "api.deepseek.com"
                     and value.port in (None, 443) and not value.username and not value.password
                     and not value.query and not value.fragment and value.path.rstrip("/") in ("", "/v1"))
        except ValueError:
            valid = False
        if not valid:
            raise ReservationUnavailable("capacity profile requires the official HTTPS DeepSeek Chat Completions endpoint")

    def contract(self):
        return {"profile": self.profile, "model": self.model_name,
                "model_display_name": self.versions[self.model_name], "context_window": self.context_window,
                "provider_max_output_tokens": self.max_output_tokens, "max_completion_tokens": self.max_completion_tokens,
                "source": "https://api-docs.deepseek.com/api/list-models/", "metadata_verification": "before_generation"}

    def reserve_request(self, messages, tools):
        # Context capacity already includes output: adding its cap again would
        # double-reserve. The request digest still binds the final projection.
        return TokenReservation(self.context_window, self.max_completion_tokens, request_digest(messages, tools))

    def verify(self, client, *, timeout):
        try:
            rows = client.models.list(timeout=timeout).data
            matches = [row.model_dump() if hasattr(row, "model_dump") else row for row in rows
                       if (getattr(row, "id", None) or (row.get("id") if isinstance(row, dict) else None)) == self.model_name]
            if len(matches) != 1:
                raise ValueError("model metadata missing or ambiguous")
            row = matches[0]
            if (type(row.get("context_window")) is not int or row["context_window"] != self.context_window
                    or type(row.get("max_output_tokens")) is not int or row["max_output_tokens"] != self.max_output_tokens
                    or row.get("name") != self.versions[self.model_name]):
                raise ValueError("model metadata does not match the pinned capacity profile")
        except Exception as exc:
            # Do not include provider response/error bodies or credentials.
            raise ReservationUnavailable("official model metadata unavailable or changed; generation was refused") from exc


def request_bound_profile(name, model_name, base_url, max_completion_tokens):
    if name != DeepSeekContextBound.profile:
        raise ReservationUnavailable("unknown request bound profile")
    result = DeepSeekContextBound(model_name, max_completion_tokens)
    result.validate_endpoint(base_url)
    return result
