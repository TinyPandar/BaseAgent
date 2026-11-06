"""Task-level progress inspection. It does not make pre-call security decisions.

HarnessKnight still owns ALLOW/REVIEW/BLOCK before a tool runs. The M1
repeat-call guard still owns the single identical call. Watchdog counts the
trend and can stop the task before the next write.
"""

from __future__ import annotations

import hashlib
import json
import time

WRITE_TOOLS = {"write_file", "edit_file"}
VERIFY_TOOLS = {"verify_command"}
DEFAULTS = {
    "enabled": False,
    "stall_steps": 3,
    "stall_abort_steps": 5,
    "repeat_abort": 4,
    "max_steps": 0,
    "max_tokens": 0,
    "max_seconds": 0.0,
    "tool_timeout_seconds": 0.0,
}


def config_from(state):
    raw = state.metadata.get("watchdog_config") or {}
    config = dict(DEFAULTS)
    config.update({key: value for key, value in raw.items() if key in DEFAULTS})
    config["enabled"] = bool(config["enabled"])
    return config


def canonical(name, arguments):
    if not isinstance(arguments, dict):
        arguments = {}
    raw = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return name, hashlib.sha256(raw.encode()).hexdigest()


def _blob(state, clock):
    blob = state.metadata.setdefault("watchdog", {})
    blob.setdefault("started_at", clock())
    blob.setdefault("calls", [])
    blob.setdefault("steps", {})
    blob.setdefault("stall_streak", 0)
    blob.setdefault("stall_count", 0)
    blob.setdefault("repeat_count", 0)
    blob.setdefault("hinted", [])
    blob.setdefault("decisions", [])
    blob.setdefault("budget_remaining", {})
    blob.setdefault("open_tools", {})
    return blob


def _decision(rule, state, action, evidence, message):
    return {"rule": rule, "step": state.step, "action": action, "evidence": evidence, "message": message}


def _remember(blob, decision):
    blob["decisions"].append({key: decision[key] for key in ("rule", "step", "action", "evidence")})
    del blob["decisions"][:-50]
    if decision["rule"] == "stall":
        blob["stall_count"] += 1
    if decision["rule"] == "repeat_call":
        blob["repeat_count"] += 1
    return decision


def _hint_or_stop(blob, state, rule, evidence, hint, stop, abort):
    if abort:
        return _remember(blob, _decision(rule, state, "stop", evidence, stop))
    if rule not in blob["hinted"]:
        blob["hinted"].append(rule)
        return _remember(blob, _decision(rule, state, "hint", evidence, hint))
    return None


def observe(state, *, phase, clock=None, name=None, arguments=None, call_id=None, ok=None, duration=None):
    """Return a hint/stop decision, or None. The caller writes the event and stops."""
    config = config_from(state)
    if not config["enabled"]:
        return None
    clock = clock or time.monotonic
    blob = _blob(state, clock)
    if blob.get("stop"):
        return blob["stop"]
    if phase == "before_tool":
        blob["open_tools"][call_id] = clock()
    elif phase == "after_tool" and call_id:
        started = blob["open_tools"].pop(call_id, None)
        if duration is None and started is not None:
            duration = clock() - started
        identity = canonical(name, arguments if isinstance(arguments, dict) else {})
        blob["calls"].append({"name": name, "args_sha256": identity[1], "call_id": call_id, "step": state.step})
        step = blob["steps"].setdefault(str(state.step), {"write": False, "verify": False, "signatures": []})
        if name in WRITE_TOOLS and ok:
            step["write"] = True
        if name in VERIFY_TOOLS:
            step["verify"] = True
        step["signatures"].append(identity[1])
        limit = config["tool_timeout_seconds"]
        if limit and duration is not None and duration > limit:
            decision = _remember(blob, _decision(
                "timeout", state, "stop",
                {"call_id": call_id, "duration_seconds": duration, "limit_seconds": limit},
                f"tool {name} exceeded {limit} seconds"))
            blob["stop"] = decision
            return decision
    decision = _budget(config, state, blob, clock) or _repeat(config, state, blob) or _stall(config, state, blob, phase)
    if decision and decision["action"] == "stop":
        blob["stop"] = decision
    if decision:
        blob["budget_remaining"] = _remaining(config, state, blob, clock)
        decision["evidence"]["budget_remaining"] = blob["budget_remaining"]
        decision["evidence"]["harnessknight"] = _hk_note(state)
    return decision


def _hk_note(state):
    hk = state.metadata.get("harnessknight") or {}
    if not hk:
        return {"present": False}
    return {"present": True, "tool_decisions": len(hk.get("tool_decisions") or {}),
            "after_decisions": len(hk.get("after_decisions") or {})}


def _remaining(config, state, blob, clock):
    remaining = {}
    if config["max_steps"]:
        remaining["steps"] = config["max_steps"] - state.step
    if config["max_tokens"]:
        remaining["tokens"] = config["max_tokens"] - state.total_tokens
    if config["max_seconds"]:
        remaining["seconds"] = config["max_seconds"] - (clock() - blob["started_at"])
    return remaining


def _budget(config, state, blob, clock):
    remaining = _remaining(config, state, blob, clock)
    blob["budget_remaining"] = remaining
    for key, limit, label in (("steps", config["max_steps"], "steps"),
                              ("tokens", config["max_tokens"], "tokens"),
                              ("seconds", config["max_seconds"], "seconds")):
        if not limit:
            continue
        left = remaining[key]
        evidence = {"limit": limit, "remaining": left, "meter": label}
        if left <= 0:
            return _remember(blob, _decision("budget", state, "stop", evidence,
                                             f"watchdog {label} budget exhausted"))
        if left <= limit * 0.2:
            hinted = _hint_or_stop(blob, state, "budget:" + label, evidence,
                                   f"watchdog {label} budget is nearly exhausted", "", False)
            if hinted:
                return hinted
    return None


def _repeat(config, state, blob):
    limit = config["repeat_abort"]
    if not limit or not blob["calls"]:
        return None
    last = blob["calls"][-1]
    count = sum(1 for call in blob["calls"]
                if call["name"] == last["name"] and call["args_sha256"] == last["args_sha256"])
    if count < limit:
        return None
    return _remember(blob, _decision(
        "repeat_call", state, "stop",
        {"tool": last["name"], "call_id": last["call_id"], "count": count, "args_sha256": last["args_sha256"]},
        f"tool {last['name']} repeated {count} times with equivalent arguments"))


def _stall(config, state, blob, phase):
    if phase != "before_model":
        return None
    steps = [blob["steps"].get(str(index), {"write": False, "verify": False, "signatures": []})
             for index in range(1, state.step + 1)]
    streak = 0
    for step in reversed(steps):
        if step["write"] or step["verify"]:
            break
        streak += 1
    signatures = [tuple(step["signatures"]) for step in steps if step["signatures"]]
    repeated = 1
    if signatures:
        last = signatures[-1]
        repeated = 1
        for previous in reversed(signatures[:-1]):
            if previous != last:
                break
            repeated += 1
    blob["stall_streak"] = max(streak, repeated if repeated > 1 else 0)
    evidence = {"stall_streak": blob["stall_streak"], "steps_without_progress": streak,
                "repeated_observations": repeated}
    abort_at = config["stall_abort_steps"]
    hint_at = config["stall_steps"]
    if abort_at and blob["stall_streak"] >= abort_at:
        return _remember(blob, _decision("stall", state, "stop", evidence,
                                         f"stalled for {blob['stall_streak']} steps without progress"))
    if hint_at and blob["stall_streak"] >= hint_at:
        return _hint_or_stop(blob, state, "stall", evidence,
                             "watchdog stall: change approach, edit, or verify before continuing", "", False)
    return None


def summary(state):
    blob = state.metadata.get("watchdog") or {}
    return {"enabled": config_from(state)["enabled"], "status": str(state.status),
            "stall_count": blob.get("stall_count", 0), "repeat_count": blob.get("repeat_count", 0),
            "stall_streak": blob.get("stall_streak", 0), "budget_remaining": blob.get("budget_remaining", {}),
            "decisions": blob.get("decisions", []), "stopped": bool(blob.get("stop"))}
