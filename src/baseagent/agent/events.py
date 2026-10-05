"""Fixed metadata events: no prompt, arguments, result bodies or exception text."""

import time


def event(kind: str, **data) -> dict:
    return {"type": kind, "timestamp": time.time(), "data": data}
