"""Fixed metadata events: no prompt, arguments, result bodies or exception text."""

import time

from baseagent.kernel.events import AgentEvent


def event(kind: str, **data) -> dict:
    return {"type": kind, "timestamp": time.time(), "data": data}
