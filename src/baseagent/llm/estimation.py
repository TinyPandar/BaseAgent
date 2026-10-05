"""Local tokenizer estimates; never a provider-guaranteed request bound."""

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from .reservation import TokenReservation, ReservationUnavailable, request_digest


@dataclass(frozen=True)
class TokenEstimate(TokenReservation):
    """A budget reservation that is allowed to underestimate actual usage."""


class TokenizerEstimator:
    def __init__(self, path, *, margin_percent=20, fixed_margin=256):
        if type(margin_percent) is not int or not 0 <= margin_percent <= 1000:
            raise ValueError("estimate margin percent must be an integer from 0 to 1000")
        if type(fixed_margin) is not int or not 0 <= fixed_margin <= 1_000_000:
            raise ValueError("estimate fixed margin must be an integer from 0 to 1000000")
        with Path(path).open("rb") as stream:
            data = stream.read(16_000_001)
        if len(data) > 16_000_000:
            raise ValueError("tokenizer file exceeds 16 MB")
        from tokenizers import Tokenizer
        try:
            self.tokenizer = Tokenizer.from_str(data.decode("utf-8"))
        except Exception as exc:
            raise ValueError("invalid local tokenizer JSON") from exc
        # A supplied tokenizer must not truncate the request before counting.
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.sha256 = sha256(data).hexdigest()
        self.margin_percent, self.fixed_margin = margin_percent, fixed_margin

    def contract(self):
        return {"mode": "estimated-json-v1", "tokenizer_sha256": self.sha256,
                "margin_percent": self.margin_percent, "fixed_margin": self.fixed_margin,
                "provider_guaranteed": False}

    def reserve_request(self, messages, tools, max_completion_tokens):
        if type(max_completion_tokens) is not int or max_completion_tokens < 1:
            raise ReservationUnavailable("estimated output cap must be a positive integer")
        # Includes roles, tool schemas, previous calls/results and all fields.
        # This is JSON tokenization, not the provider's hidden chat serialization.
        text = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
        count = len(self.tokenizer.encode(text, add_special_tokens=False).ids)
        prompt = count + (count * self.margin_percent + 99) // 100 + self.fixed_margin
        return TokenEstimate(prompt + max_completion_tokens, max_completion_tokens, request_digest(messages, tools))
