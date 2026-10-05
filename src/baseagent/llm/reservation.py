"""Adapter-declared upper bounds bound to the exact projected request."""

from dataclasses import dataclass
from hashlib import sha256
import json


def request_digest(messages, tools):
    return sha256(json.dumps({"messages": messages, "tools": tools}, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


class ReservationUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class TokenReservation:
    total_tokens: int
    max_completion_tokens: int
    input_digest: str

    def validate(self, messages, tools):
        if type(self.total_tokens) is not int or type(self.max_completion_tokens) is not int or not 1 <= self.max_completion_tokens <= self.total_tokens:
            raise ReservationUnavailable("reservation requires positive integer bounds")
        if self.input_digest != request_digest(messages, tools):
            raise ReservationUnavailable("reservation does not match projected input")
