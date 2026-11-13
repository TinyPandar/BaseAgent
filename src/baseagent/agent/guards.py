"""Opt-in, transcript-derived success-rate guards."""

import hashlib
import json

from baseagent.tools.result import ErrorCode, ToolResult


def canonical_call(name, arguments):
    raw = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return name, raw


def calls_in_turn(state):
    found = []
    for index, message in enumerate(state.messages[state.turn_start:], start=state.turn_start):
        for call in message.get("tool_calls", ()):
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (ValueError, TypeError):
                continue
            found.append((fn.get("name"), args, call.get("id")))
    return found


def repeat_result(state, request, arguments, checkpoint):
    key = canonical_call(request.name, arguments)
    matching = [entry for entry in calls_in_turn(state) if canonical_call(entry[0], entry[1]) == key]
    count = len(matching)
    if count < 2:
        return None
    args_hash = hashlib.sha256(key[1].encode()).hexdigest()
    if count == 2:
        checkpoint("repeat_call_warned", tool=request.name, call_id=request.call_id, count=count, args_sha256=args_hash)
        return "warn"
    checkpoint("repeat_call_blocked", tool=request.name, call_id=request.call_id, count=count, args_sha256=args_hash)
    return ToolResult.failure(ErrorCode.INVALID_ARGUMENTS, "identical tool call repeated three times; change the arguments or approach")


def prior_reads(state, arguments):
    result = []
    calls = {call_id: (name, args) for name, args, call_id in calls_in_turn(state)}
    for index, message in enumerate(state.messages[state.turn_start:], start=state.turn_start):
        if message.get("role") != "tool":
            continue
        call = calls.get(message.get("tool_call_id"))
        if not call or canonical_call("read_file", call[1]) != canonical_call("read_file", arguments):
            continue
        try:
            body = json.loads(message.get("content", "{}"))
            data = body.get("data")
            if body.get("ok") and isinstance(data, dict) and data.get("sha256"):
                result.append((message["tool_call_id"], data["sha256"], index))
        except (ValueError, TypeError):
            pass
    return result


def successful_write_since(state, path, after_index):
    calls = {call_id: (name, args) for name, args, call_id in calls_in_turn(state)}
    for index, message in enumerate(state.messages[state.turn_start:], start=state.turn_start):
        if index <= after_index:
            continue
        if message.get("role") != "tool":
            continue
        call = calls.get(message.get("tool_call_id"))
        if not call or call[0] not in {"write_file", "edit_file", "apply_patch"}:
            continue
        if call[0] == "apply_patch":
            operations = call[1].get("operations")
            if not isinstance(operations, list) or not any(isinstance(item, dict) and item.get("path") == path
                                                           for item in operations):
                continue
        elif call[1].get("path") != path:
            continue
        try:
            if json.loads(message.get("content", "{}")).get("ok"):
                return True
        except (ValueError, TypeError):
            pass
    return False
