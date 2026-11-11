"""Tool gateway: the one dispatch path shared by local tools and MCP tools.

There is no second loop here. The gateway is the existing harness dispatch in
``agent/agent.py`` plus the three small pieces in this module:

    model names a tool
      -> ToolRegistry lookup (local handler or MCP-backed handler, same registry)
      -> AgentMiddleware.before_tool            (hooks may call ``restrict``)
      -> GatewayPolicy.action                   (--tool-policy, edit policy, HarnessKnight,
                                                 MCP default ask, hook restrictions)
           ask  -> awaiting_approval (call id + digest), resume after a decision
           deny -> failed result, the tool does not run
      -> Watchdog before_tool -> ledger start_call
      -> AgentMiddleware.wrap_tool_call -> ToolRegistry.execute
           local handler            or            MCP ``tools/call``
      -> ledger complete_call -> tool_call_finished -> Watchdog after_tool
      -> AgentMiddleware.after_tool, then on_tool_failure for a failed result

A tool's backend (``local`` or ``mcp``) is a property of its registry entry
(``ToolRegistry.origin``) and travels on ``ToolCallRequest.source`` /
``mcp_server_id``. Protection is added by writing an ``AgentMiddleware``; the
MCP client and the model adapters never dispatch a call themselves.
"""

from __future__ import annotations

from hashlib import sha256
import json

from baseagent.tools.policy import request_digest

RESTRICTIONS = "tool_restrictions"
_RANK = {"allow": 0, "ask": 1, "deny": 2}


def origin_fields(request) -> dict:
    """Event/metadata fields naming the MCP backend of a call. Names only: no arguments, no secrets.

    Local tools add nothing, so their persisted events and approval records keep their shape
    (an absent ``mcp_server_id`` reads as null).
    """
    if request.source != "mcp":
        return {}
    return {"source": "mcp", "mcp_server_id": request.mcp_server_id}


def restrict(request, action: str, reason: str = "") -> None:
    """Tighten one pending call from ``before_tool``: "ask" (needs approval) or "deny" (blocked).

    The request is bound by call id and digest. A restriction can only add to
    what the policy already decided: the gateway takes the stricter of the two,
    so a hook cannot loosen ``--mode``, ``--tool-policy`` or workspace limits.
    """
    if action not in ("ask", "deny"):
        raise ValueError("a hook can only restrict a tool call to ask or deny")
    digest = request_digest(request.name, request.arguments)
    restrictions = request.state.metadata.setdefault(RESTRICTIONS, {})
    current = restrictions.get(request.call_id)
    if current and current.get("request_digest") == digest and _RANK[current["action"]] >= _RANK[action]:
        return
    restrictions[request.call_id] = {"action": action, "request_digest": digest, "reason": str(reason)[:200]}


class _WithoutApprovals:
    """Read-only view of a state whose metadata hides recorded approvals."""

    def __init__(self, state):
        self._state = state
        self.metadata = {key: value for key, value in state.metadata.items() if key != "tool_approvals"}

    def __getattr__(self, name):
        return getattr(self._state, name)


class GatewayPolicy:
    """Outermost policy wrapper. It only tightens the wrapped policy's decision.

    * An MCP tool that is not declared read-only needs approval for each call
      (fail closed). ``--tool-policy`` rules match the registered name and can
      deny it; they cannot turn the approval off.
    * A restriction recorded by a hook through ``restrict`` applies to that call.

    Without MCP tools the contract and fingerprint are those of the wrapped
    policy, so sessions that do not use MCP keep their approvals and contracts.
    """

    def __init__(self, parent, tools):
        self.parent = parent
        servers = getattr(tools, "_mcp_servers", {})
        self.mcp_ask = {name: server for name, server in servers.items() if not tools.retry_safe(name)}
        # Digests of the configured servers: an approval does not carry over to a replaced server.
        self.mcp_servers = (getattr(tools, "capability_contract", None) or {}).get("mcp")

    def contract(self):
        if not self.mcp_ask:
            return self.parent.contract()
        return {"tool_policy": self.parent.contract(), "mcp_default_ask": dict(sorted(self.mcp_ask.items())),
                "mcp_servers": self.mcp_servers}

    @property
    def fingerprint(self):
        if not self.mcp_ask:
            return self.parent.fingerprint
        return sha256(json.dumps(self.contract(), sort_keys=True).encode("utf-8")).hexdigest()

    def action(self, state, call_id, name, arguments):
        digest = request_digest(name, arguments)
        # With MCP tools the fingerprint differs from the wrapped policy's, so approvals are checked
        # here and the wrapped policy decides without them. Without MCP tools it sees the state as is.
        action = self.parent.action(_WithoutApprovals(state) if self.mcp_ask else state, call_id, name, arguments)
        floor = "ask" if name in self.mcp_ask else "allow"
        restriction = state.metadata.get(RESTRICTIONS, {}).get(call_id)
        if restriction and restriction.get("request_digest") == digest and _RANK.get(restriction.get("action"), 2) > _RANK[floor]:
            floor = restriction["action"] if restriction.get("action") in _RANK else "deny"
        if _RANK[floor] > _RANK.get(action, 2):
            action = floor
        if action != "ask":
            return action
        approval = state.metadata.get("tool_approvals", {}).get(call_id)
        if approval and approval.get("turn_id") == state.turn_id and approval.get("policy") == self.fingerprint and approval.get("request_digest") == digest:
            return approval["decision"]
        return "ask"
