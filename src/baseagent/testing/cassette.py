"""Sequential protocol-level JSONL fixtures. Replay never calls a live model."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import threading

from baseagent.kernel.cancellation import Cancelled
from baseagent.kernel import errors
from baseagent.llm.response import ModelResponse
from baseagent.llm.reservation import TokenReservation, ReservationUnavailable, request_digest
from baseagent.llm.estimation import TokenEstimate
from baseagent.jsondata import object_from_json
from .redaction import Redactor


class CassetteError(ValueError):
    pass


class CassetteMismatch(CassetteError):
    pass


_ERRORS = {cls.__name__: cls for cls in (
    errors.ModelError, errors.RateLimitError, errors.TransientError, errors.ModelTimeoutError,
    errors.AuthError, errors.BadRequestError, errors.ContextLengthError, errors.ResponseParseError,
    ConnectionError, TimeoutError, ValueError, ReservationUnavailable, Cancelled,
)}


def _json(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    text.encode("utf-8")
    return text


def _hash(value):
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _diff(a, b, path="request"):
    if type(a) is not type(b):
        return f"{path}: value type differs"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}.{key}: field missing"
            if a[key] != b[key]:
                return _diff(a[key], b[key], f"{path}.{key}")
    elif isinstance(a, list):
        for index, (left, right) in enumerate(zip(a, b)):
            if left != right:
                return _diff(left, right, f"{path}[{index}]")
        if len(a) != len(b):
            return f"{path}: list length differs"
    return f"{path}: value differs"  # Never include raw values in error output.


def _encode(value):
    if isinstance(value, ModelResponse):
        return {"kind": "response", "value": value.to_dict()}
    if isinstance(value, TokenReservation):
        return {"kind": "estimate" if isinstance(value, TokenEstimate) else "reservation",
                "value": {"total_tokens": value.total_tokens, "max_completion_tokens": value.max_completion_tokens}}
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    if not isinstance(value, dict):
        raise CassetteError("unsupported model return type")
    return {"kind": "message", "value": value}


def _decode(value, messages, tools):
    kind, data = value["kind"], value["value"]
    if kind == "response":
        return ModelResponse.from_dict(data)
    if kind in {"reservation", "estimate"}:
        cls = TokenEstimate if kind == "estimate" else TokenReservation
        if not isinstance(data, dict) or set(data) != {"total_tokens", "max_completion_tokens"}:
            raise ValueError("invalid recorded reservation")
        result = cls(**data, input_digest=request_digest(messages, tools))
        result.validate(messages, tools)
        return result
    if kind != "message" or not isinstance(data, dict):
        raise ValueError("invalid recorded message")
    return json.loads(_json(data))


def _encode_error(exc):
    result = {"type": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, errors.ModelError):
        result.update(status_code=exc.status_code, retry_after=exc.retry_after)
    return result


def _decode_error(value):
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or not isinstance(value.get("message"), str):
        raise ValueError("invalid recorded error")
    cls = _ERRORS.get(value["type"], errors.ModelError)
    if issubclass(cls, errors.ModelError):
        return cls(value["message"], status_code=value.get("status_code"), retry_after=value.get("retry_after"))
    return cls(value["message"])


class CassetteModel:
    def __init__(self, path, model=None, *, mode=None, model_name=None, generation=None, redactor=None):
        self.path = Path(path)
        self.mode = mode or os.getenv("BASEAGENT_CASSETTE_MODE", "replay")
        if self.mode not in {"record", "replay"}:
            raise CassetteError("cassette mode must be record or replay")
        self._model = model if self.mode == "record" else None
        self.redactor = redactor or Redactor()
        self._rows, self._index = [], 0
        self._lock = threading.Lock()
        self._closed, self._broken = False, False
        if self.mode == "record":
            if model is None:
                raise CassetteError("record mode requires a model")
            if self.path.exists():
                raise CassetteError("cassette destination already exists")
            self.model_name = model_name or getattr(model, "model_name", type(model).__name__)
            self.generation_params = json.loads(_json(generation if generation is not None else getattr(model, "generation_params", {})))
            if generation is not None and hasattr(model, "generation_params") and generation != model.generation_params:
                raise CassetteError("generation metadata does not match wrapped model")
            self.max_completion_tokens = getattr(model, "max_completion_tokens", None)
            self._header = self.redactor.value({"version": 1, "created": datetime.now(timezone.utc).isoformat(),
                                               "model_name": self.model_name, "generation": self.generation_params,
                                               "max_completion_tokens": self.max_completion_tokens})
        else:
            self._load()
            self.model_name = self._header["model_name"]
            self.generation_params = self._header["generation"] if generation is None else generation
            self.max_completion_tokens = self._header["max_completion_tokens"]
            if model_name is not None and self.redactor.value(model_name) != self.model_name:
                raise CassetteMismatch("model_name differs")

    def _load(self):
        try:
            with self.path.open("rb") as stream:
                data = stream.read(8_000_001)
            if len(data) > 8_000_000:
                raise ValueError()
            lines = data.decode("utf-8").splitlines()
            values = [object_from_json(line, max_bytes=1_000_000, label="cassette line") for line in lines]
            header = values[0]
            if type(header.get("version")) is not int or header["version"] != 1 or not isinstance(header.get("model_name"), str) or not isinstance(header.get("generation"), dict) or not isinstance(header.get("created"), str):
                raise ValueError()
            if type(header.get("redaction_count")) is not int or header["redaction_count"] < 0 or "max_completion_tokens" not in header:
                raise ValueError()
            for seq, row in enumerate(values[1:]):
                if type(row.get("seq")) is not int or row["seq"] != seq or not isinstance(row.get("request"), dict) or row.get("request_hash") != _hash(row["request"]):
                    raise ValueError()
                if ("response" in row) == ("error" in row):
                    raise ValueError()
                request = row["request"]
                operation = request.get("operation")
                if operation not in {"complete", "complete_reserved", "reserve", "estimate"} or not isinstance(request.get("messages"), list) or not isinstance(request.get("tools"), list) or not isinstance(request.get("generation"), dict):
                    raise ValueError()
                if "response" in row:
                    kinds = {"reserve": {"reservation"}, "estimate": {"estimate"}, "complete": {"message", "response"}, "complete_reserved": {"message", "response"}}
                    if row["response"]["kind"] not in kinds[operation]:
                        raise ValueError()
                    _decode(row["response"], [], [])
                else:
                    _decode_error(row["error"])
            self._header, self._rows = header, values[1:]
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise CassetteError("missing, corrupt, oversized or unsupported cassette") from None

    def _invoke(self, operation, messages, tools, call, *, reservation=None, max_completion_tokens=None):
        if self._closed or self._broken:
            raise CassetteError("cassette is closed or recording failed")
        if not self._lock.acquire(blocking=False):
            raise CassetteError("concurrent cassette use: use one wrapper per task")
        try:
            if self._closed or self._broken:
                raise CassetteError("cassette is closed or recording failed")
            if self.mode == "record" and hasattr(self._model, "generation_params") and self.generation_params != self._model.generation_params:
                raise CassetteError("wrapped model generation changed; create a fresh cassette")
            if self.mode == "record" and getattr(self._model, "max_completion_tokens", None) != self.max_completion_tokens:
                raise CassetteError("wrapped model output cap changed; create a fresh cassette")
            request = {"operation": operation, "model_name": self.model_name, "messages": messages, "tools": tools or [],
                       "generation": self.generation_params, "max_completion_tokens": max_completion_tokens if max_completion_tokens is not None else self.max_completion_tokens}
            if reservation is not None:
                reservation.validate(messages, tools)
                request["reservation"] = _encode(reservation)
            try:
                normalized = self.redactor.value(json.loads(_json(request)))
                digest = _hash(normalized)
            except (ValueError, TypeError, RecursionError):
                raise CassetteError("request is not supported JSON") from None
            if self.mode == "replay":
                if self._index == len(self._rows):
                    raise CassetteMismatch("model call exceeds recorded operations")
                row = self._rows[self._index]
                if digest != row["request_hash"]:
                    detail = self.redactor.text(_diff(row["request"], normalized))[:500]
                    raise CassetteMismatch(f"operation {self._index}: {detail}")
                self._index += 1
                if "error" in row:
                    raise _decode_error(row["error"])
                return _decode(row["response"], messages, tools)
            row = {"seq": len(self._rows), "request": normalized, "request_hash": digest}
            try:
                value = call()
            except (Exception, Cancelled) as exc:
                row["error"] = self.redactor.value(_encode_error(exc))
                self._rows.append(row)
                raise
            try:
                row["response"] = self.redactor.value(json.loads(_json(_encode(value))))
                _decode(row["response"], messages, tools)
            except (ValueError, TypeError, KeyError, RecursionError):
                self._broken = True
                raise CassetteError("model return cannot be recorded") from None
            self._rows.append(row)
            return value
        finally:
            self._lock.release()

    def complete(self, messages, tools=None):
        return self._invoke("complete", messages, tools, lambda: self._model.complete(messages, tools))

    def complete_with_timeout(self, messages, tools=None, *, timeout=None, max_completion_tokens=None):
        def call():
            method = getattr(self._model, "complete_with_timeout", None)
            if method is None:
                return self._model.complete(messages, tools)
            extra = {"max_completion_tokens": max_completion_tokens} if max_completion_tokens is not None else {}
            return method(messages, tools, timeout=timeout, **extra)
        return self._invoke("complete", messages, tools, call, max_completion_tokens=max_completion_tokens)

    def complete_with_control(self, messages, tools=None, *, cancellation, timeout=None):
        cancellation.check()
        def call():
            method = getattr(self._model, "complete_with_control", None)
            if method:
                return method(messages, tools, cancellation=cancellation, timeout=timeout)
            method = getattr(self._model, "complete_with_timeout", None)
            return method(messages, tools, timeout=timeout) if method else self._model.complete(messages, tools)
        return self._invoke("complete", messages, tools, call)

    def reserve_request(self, messages, tools):
        return self._invoke("reserve", messages, tools, lambda: self._quote("reserve_request", messages, tools))

    def estimate_request(self, messages, tools):
        return self._invoke("estimate", messages, tools, lambda: self._quote("estimate_request", messages, tools))

    def _quote(self, name, messages, tools):
        method = getattr(self._model, name, None)
        if method is None:
            raise ReservationUnavailable("wrapped model has no reservation/estimation interface")
        return method(messages, tools)

    def complete_reserved(self, messages, tools, *, reservation, cancellation, timeout=None):
        cancellation.check()
        return self._invoke("complete_reserved", messages, tools,
                            lambda: self._model.complete_reserved(messages, tools, reservation=reservation, cancellation=cancellation, timeout=timeout),
                            reservation=reservation, max_completion_tokens=reservation.max_completion_tokens)

    def assert_all_consumed(self):
        if self.mode == "replay" and self._index != len(self._rows):
            raise CassetteMismatch(f"unconsumed recorded operations: {len(self._rows) - self._index}")

    def close(self):
        if self._closed:
            return
        if not self._lock.acquire(blocking=False):
            raise CassetteError("cannot close an active cassette")
        try:
            if self._closed:
                return
            if self._broken:
                raise CassetteError("failed recording cannot be published")
            if self.mode == "replay":
                self.assert_all_consumed()
            else:
                header = {**self._header, "redaction_count": self.redactor.count}
                lines = [_json(header), *(_json(row) for row in self._rows)]
                if any(len(line.encode("utf-8")) > 1_000_000 for line in lines):
                    raise CassetteError("cassette line exceeds 1 MB")
                data = "\n".join(lines) + "\n"
                if len(data.encode("utf-8")) > 8_000_000:
                    raise CassetteError("cassette exceeds 8 MB")
                self.redactor.assert_safe(data)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                name = None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".cassette-", delete=False) as stream:
                        name = stream.name
                        stream.write(data.encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Atomic publication without overwriting a racing writer.
                    os.link(name, self.path)
                except OSError:
                    raise CassetteError("cassette publication failed or destination exists") from None
                finally:
                    if name is not None:
                        os.unlink(name)
            self._closed = True
        finally:
            self._lock.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.close()
        else:
            self._closed = True  # Never publish a partially executed recording.


def _without_timing(value):
    if isinstance(value, dict):
        return {key: _without_timing(item) for key, item in value.items()
                if key not in {"timestamp", "duration_seconds", "turn_started_at"}}
    if isinstance(value, list):
        return [_without_timing(item) for item in value]
    return value


def comparable_events(events):
    return _without_timing([event.to_dict() if hasattr(event, "to_dict") else event for event in events])


def comparable_state(state):
    return _without_timing(state.to_dict())
