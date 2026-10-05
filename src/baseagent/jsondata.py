"""Bounded strict JSON at host configuration and reconciliation boundaries."""

import json
from pathlib import Path


def _check_limit(max_bytes):
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("JSON byte limit must be a positive integer")


def object_from_json(text, *, max_bytes, label="JSON input"):
    _check_limit(max_bytes)
    if not isinstance(text, str):
        raise ValueError(f"{label} must be JSON text")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError(f"{label} must use valid UTF-8") from exc
    if size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite number")

    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(result, dict):
            raise ValueError("object required")
        # parse_constant alone does not reject exponent overflow such as 1e999.
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ValueError(f"{label} must be a JSON object with unique fields, finite values and supported nesting") from exc
    return result


def read_json_object(path, *, max_bytes, label="JSON file"):
    _check_limit(max_bytes)
    # Bound the actual read, including files that grow after an earlier stat.
    with Path(path).open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError(f"{label} must use valid UTF-8") from exc
    return object_from_json(text, max_bytes=max_bytes, label=label)
