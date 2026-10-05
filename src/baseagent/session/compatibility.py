"""Non-secret contract digests for safe continuation across invocations."""

from hashlib import sha256
import json


def digest(value) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return sha256(data.encode("utf-8")).hexdigest()


def runtime_contract(tools, system_prompt: str, runtime_config: dict | None) -> dict:
    # Store only hashes. Caller config may contain endpoint/user settings and is
    # never written verbatim; credentials should not be supplied here at all.
    return {"version": 2, "tools": digest(tools.contract()),
            "system_prompt": digest(system_prompt), "runtime": digest(runtime_config) if runtime_config is not None else None}


def check_contract(saved: dict | None, current: dict, *, accept_changes: bool) -> None:
    if saved == current or accept_changes:
        return
    if saved is None:
        raise ValueError("legacy session has no runtime contract; explicitly accept configuration changes before resuming")
    changed = [key for key in current if saved.get(key) != current[key]]
    raise ValueError("session runtime configuration changed: " + ", ".join(changed) + "; explicitly accept changes before resuming")
