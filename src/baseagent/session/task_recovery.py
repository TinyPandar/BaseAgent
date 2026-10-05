"""Explicit node recovery; never dispatches a model or tool."""

from contextlib import contextmanager
from copy import deepcopy
import json

from baseagent.agent.events import event
from baseagent.agent.state import RunStatus
from baseagent.llm.response import TokenUsage
from baseagent.tools.policy import request_digest
from baseagent.tools.result import ToolResult
from .store import _json


@contextmanager
def _node(store, session_id, task_id):
    with store.exclusive(session_id):
        root = store.load(session_id)
        if root is None:
            raise ValueError("session does not exist")
        yield store.task_node(root, task_id)


def decide(store, session_id, task_id, call_id, decision, expected_digest):
    if decision not in {"allow", "deny"}:
        raise ValueError("approval decision must be allow or deny")
    with _node(store, session_id, task_id) as view, view._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        state = view.state
        approval = state.metadata.get("approval_request")
        if state.status != RunStatus.AWAITING_APPROVAL or not approval or approval["call_id"] != call_id or approval["turn_id"] != state.turn_id:
            raise ValueError("node call is not awaiting approval")
        call = connection.execute("SELECT status, request_json FROM tool_calls WHERE session_id=? AND turn_id=? AND call_id=?", (session_id, state.turn_id, call_id)).fetchone()
        if call is None or call["status"] != "pending":
            raise ValueError("approval requires an unexecuted pending node call")
        function = json.loads(call["request_json"])["function"]
        actual = request_digest(function["name"], function["arguments"])
        if actual != expected_digest or actual != approval["request_digest"]:
            raise ValueError("node approval request changed; inspect the exact pending request again")
        state.metadata.setdefault("tool_approvals", {})[call_id] = {**approval, "decision": decision}
        view._save(connection, state)
        view._event(connection, state, event("approval_decided", call_id=call_id, decision=decision))
        return state


def resolve_call(store, session_id, task_id, call_id, result):
    if not isinstance(result, ToolResult):
        raise ValueError("verified node result must be ToolResult")
    ToolResult.from_dict(result.to_dict())
    encoded = _json(result.to_dict())
    if len(encoded.encode("utf-8")) > 256_000:
        raise ValueError("recovery result exceeds 256 KB")
    with _node(store, session_id, task_id) as view:
        state = view.state
        if view.call(state, call_id)["status"] != "running":
            raise ValueError("only unresolved running node calls can be reconciled")
        state.messages.append({"role": "tool", "tool_call_id": call_id, "content": encoded})
        state.blocked_tool_calls = [item for item in state.blocked_tool_calls if item != call_id]
        state.status, state.error = RunStatus.INTERRUPTED, None
        view.complete_call(state, call_id, result)
        return state


def resolve_usage(store, session_id, task_id, usage, unknown_calls):
    if not isinstance(usage, TokenUsage):
        raise ValueError("verified node usage must be TokenUsage")
    with _node(store, session_id, task_id) as view:
        state = view.state
        direct = state.metadata.get("direct_usage")
        if type(unknown_calls) is not int or unknown_calls < 1 or not direct or unknown_calls != direct.get("unknown_usage_calls"):
            raise ValueError("unknown_calls must match all unknown attempts made directly by this node")
        scope = view.execution_scope()
        if any(member.unknown_usage_calls < unknown_calls for member in scope.members):
            raise ValueError("node and ancestor unknown usage counters are inconsistent")
        owned = {key: value for key, value in state.model_reservations.items() if key.isdecimal()}
        if len(owned) > unknown_calls:
            raise ValueError("node reservation count exceeds its unknown attempts")
        for ancestor in scope.ancestors:
            if any(ancestor.model_reservations.get(f"{state.turn_id}:{key}") != value for key, value in owned.items()):
                raise ValueError("node and ancestor reservations are inconsistent")
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            scope._charge(key, getattr(usage, key))
        scope._charge("unknown_usage_calls", -unknown_calls)
        estimated = any(key in state.metadata.get("estimated_reservations", {}) for key in owned)
        for key in owned:
            state.metadata.get("estimated_reservations", {}).pop(key, None)
            state.model_reservations.pop(key)
            for ancestor in scope.ancestors:
                ancestor.model_reservations.pop(f"{state.turn_id}:{key}")
                ancestor.metadata.get("estimated_reservations", {}).pop(f"{state.turn_id}:{key}", None)
        # Aggregate receipt cannot prove per-attempt caps. Only compare total
        # bounds when every reconciled attempt had a reservation.
        if not estimated and len(owned) == unknown_calls and usage.total_tokens > sum(owned.values()):
            violation = {"node_turn_id": state.turn_id, "attempt": "reconciled", "reserved_tokens": sum(owned.values()), "reported_tokens": usage.total_tokens}
            for member in scope.members:
                member.metadata["request_bound_violation"] = deepcopy(violation)
        state.error = None
        if state.status != RunStatus.COMPLETED:
            state.status = RunStatus.INTERRUPTED
        view.save(state, event=event("usage_reconciled", unknown_calls=unknown_calls, total_tokens=usage.total_tokens))
        return state


def acknowledge_bound(store, session_id, task_id):
    with _node(store, session_id, task_id) as view:
        state = view.state
        violation = state.metadata.get("request_bound_violation")
        if not violation or violation.get("node_turn_id") != state.turn_id:
            raise ValueError("acknowledgment must target the node that originated the violation")
        scope = view.execution_scope()
        for member in scope.members:
            if member.metadata.get("request_bound_violation") == violation:
                member.metadata["acknowledged_request_bound_violation"] = deepcopy(member.metadata.pop("request_bound_violation"))
        if state.status == RunStatus.REQUEST_BOUND_VIOLATED:
            state.status, state.error = RunStatus.INTERRUPTED, None
        view.save(state, event=event("request_bound_acknowledged"))
        return state
