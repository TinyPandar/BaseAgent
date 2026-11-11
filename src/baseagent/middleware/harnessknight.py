"""Optional synchronous adapter for the HarnessKnight security service."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from types import SimpleNamespace
import uuid
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
import warnings

from baseagent.kernel.result import ErrorCode, ToolResult
from baseagent.middleware import AgentMiddleware
from baseagent.tools.policy import request_digest


class HarnessUnavailable(Exception):
    """The service could not provide a usable decision."""


def idempotency_key(value: str) -> str:
    if len(value) <= 128:
        return value
    return value[:63] + "-" + sha256(value.encode()).hexdigest()


def deterministic_event_id(session_id: str, call_id: str, phase: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"baseagent:{session_id}:{call_id}:{phase}"))


def _bounded_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) <= 256:
        return value
    return value[:191] + "-" + sha256(value.encode()).hexdigest()


def infer_impacts(name: str, arguments: dict) -> list[dict]:
    def target(*keys):
        return next((arguments[k] for k in keys if isinstance(arguments.get(k), str) and arguments[k]), ".")[:8192]
    if name in {"write_file", "edit_file"}:
        return [{"kind": "file", "target": target("path"), "operation": "write"}]
    if name in {"read_file", "grep", "glob"}:
        return [{"kind": "file", "target": target("path", "pattern"), "operation": "read"}]
    if name in {"run_command", "verify_command"}:
        argv = arguments.get("argv", arguments.get("command", []))
        command = " ".join(str(x) for x in argv) if isinstance(argv, list) else str(argv)
        impacts = [{"kind": "agent", "target": (command or name)[:8192], "operation": "execute"}]
        words = argv if isinstance(argv, list) else command.split()
        if words and words[0] in {"curl", "wget", "ssh", "scp", "git"}:
            if words[0] != "git" or (len(words) > 1 and words[1] in {"clone", "fetch", "push", "pull"}):
                impacts.append({"kind": "network", "target": (" ".join(str(x) for x in words[1:]) or words[0])[:8192], "operation": (words[1] if words[0] == "git" and len(words) > 1 else words[0])[:8192]})
        return impacts
    if name in {"git_log", "git_show", "git_pickaxe", "git_diff", "git_status", "git_history"}:
        return [{"kind": "git", "target": target("repo", "rev", "path"), "operation": "read"}]
    return []


def build_event(*, state, event_type: str, call_id: str, phase: str, payload: dict,
                sources=(), causal_event_id=None, impacts=(), occurred_at=None, mcp_server_id=None) -> dict:
    sid = state.session_id or "baseagent-session"
    raw_key = (f"baseagent-{sid}-{call_id}-after" if phase == "after" else
               f"baseagent-{sid}-prompt-{call_id}" if phase == "prompt" else f"baseagent-{sid}-{call_id}")
    return {"schema_version": "1.0", "event_id": deterministic_event_id(sid, call_id, phase),
            "idempotency_key": idempotency_key(raw_key), "event_type": event_type,
            "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "agent": {"agent_id": "baseagent-main", "agent_type": "baseagent", "agent_version": "unknown"},
            "session_id": _bounded_identifier(sid), "causal_event_id": causal_event_id, "payload": payload,
            "sources": list(sources), "trace": {"root_session_id": _bounded_identifier(sid), "parent_agent_id": None,
            "mcp_server_id": _bounded_identifier(mcp_server_id), "call_id": _bounded_identifier(call_id) if phase != "prompt" else None},
            "impacts": list(impacts), "metadata": {}}


class HttpTransport:
    def __init__(self, base_url, token=None):
        self.base_url = base_url.rstrip("/")
        self.token = os.environ.get("HARNESS_API_TOKEN") if token is None else token

    def evaluate(self, event: dict, idempotency_key: str, timeout: float) -> dict:
        headers = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(self.base_url + "/v1/events/evaluate", data=json.dumps(event).encode(), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("response must be an object")
            return value
        except (URLError, HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise HarnessUnavailable(f"HarnessKnight request failed ({type(exc).__name__})") from exc


class HarnessKnightClient:
    def __init__(self, transport, fail_mode="closed", timeout=3.0, agent_version="unknown"):
        if fail_mode not in {"closed", "open"}:
            raise ValueError("fail_mode must be closed or open")
        self.transport, self.fail_mode, self.timeout, self.agent_version = transport, fail_mode, timeout, agent_version
        self._warned = False
        self._tool_decisions = {}

    def decide(self, event: dict) -> tuple[str, list[str]]:
        event["agent"]["agent_version"] = self.agent_version
        try:
            response = self.transport.evaluate(event, event["idempotency_key"], self.timeout)
            if response.get("metadata", {}).get("degraded"):
                raise HarnessUnavailable("degraded response")
            decision = response.get("decision")
            if decision not in {"ALLOW", "REVIEW", "BLOCK"}:
                raise HarnessUnavailable("invalid decision")
            messages = [str(x.get("message", "")) for x in response.get("risk_signals", []) if x.get("message")]
            return decision, messages
        except Exception as exc:
            if not isinstance(exc, HarnessUnavailable):
                exc = HarnessUnavailable(type(exc).__name__)
            if self.fail_mode == "closed":
                return "BLOCK", ["HarnessKnight unavailable (fail-closed)"]
            if not self._warned:
                warnings.warn("HarnessKnight unavailable (fail-open)", RuntimeWarning)
                logging.getLogger(__name__).warning("HarnessKnight unavailable (fail-open)")
                self._warned = True
            return "ALLOW", ["HarnessKnight unavailable (fail-open)"]


class HarnessKnightToolPolicy:
    def __init__(self, parent, client, url):
        self.parent, self.client, self.url = parent, client, url.rstrip("/")

    def contract(self):
        return {"tool_policy": self.parent.contract(), "harnessknight": {"url": self.url, "fail_mode": self.client.fail_mode}}

    @property
    def fingerprint(self):
        return sha256(json.dumps(self.contract(), sort_keys=True).encode()).hexdigest()

    def action(self, state, call_id, name, arguments):
        parent = self.parent.action(SimpleNamespace(turn_id=state.turn_id, metadata={}), call_id, name, arguments)
        if parent == "deny":
            return "deny"
        key = (call_id, request_digest(name, arguments))
        cached = self.client._tool_decisions.get(key)
        if cached is None:
            return "allow" if self.client.fail_mode == "open" else "deny"
        decision, messages = cached
        if decision == "BLOCK":
            return "deny"
        if decision == "REVIEW" or parent == "ask":
            approval = state.metadata.get("tool_approvals", {}).get(call_id)
            if approval and approval.get("turn_id") == state.turn_id and approval.get("policy") == self.fingerprint and approval.get("request_digest") == request_digest(name, arguments):
                return approval["decision"]
            return "ask"
        return "allow"


class HarnessKnightMiddleware(AgentMiddleware):
    def __init__(self, client):
        self.client = client
        self._after_events = {}

    def before_tool(self, request):
        state, call_id, name, arguments = request.state, request.call_id, request.name, request.arguments
        digest = request_digest(name, arguments)
        cache_key = (call_id, digest)
        if cache_key in self.client._tool_decisions:
            return
        hk = state.metadata.setdefault("harnessknight", {})
        stamp_key = ":".join(map(str, (state.session_id, call_id, digest, "before")))
        stamps = hk.setdefault("occurred_at", {})
        try:
            parsed = json.loads(arguments or "{}")
            if not isinstance(parsed, dict):
                raise ValueError()
        except (ValueError, TypeError):
            parsed = {"_raw": arguments}
        event = build_event(state=state, event_type="tool.call.before", call_id=call_id, phase="before",
                            payload={"tool_name": name, "arguments": parsed}, impacts=infer_impacts(name, parsed),
                            mcp_server_id=getattr(request, "mcp_server_id", None),
                            occurred_at=stamps.setdefault(stamp_key, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
        decision, messages = self.client.decide(event)
        self.client._tool_decisions[cache_key] = (decision, messages)
        hk.setdefault("tool_decisions", {})[call_id] = {"decision": decision, "messages": messages, "event_id": event["event_id"]}

    def before_agent(self, state):
        prompt = next((m.get("content", "") for m in reversed(state.messages[state.turn_start:]) if m.get("role") == "user"), "")
        hk = state.metadata.setdefault("harnessknight", {})
        if hk.get("prompt_turn_id") == state.turn_id:
            return
        hk["prompt_turn_id"] = state.turn_id
        key = (state.session_id, state.turn_id, "prompt")
        stamp = hk.setdefault("occurred_at", {}).setdefault(":".join(map(str, key)), datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        event = build_event(state=state, event_type="user.prompt.submitted", call_id=state.turn_id, phase="prompt",
                            payload={"prompt": prompt}, sources=[{"source_id": _bounded_identifier(f"user-prompt-{state.turn_id}"), "kind": "user"}], occurred_at=stamp)
        decision, messages = self.client.decide(event)
        hk["prompt_decision"] = {"decision": decision, "messages": messages, "event_id": event["event_id"]}
        if decision == "BLOCK":
            raise RuntimeError("blocked by HarnessKnight: " + ("; ".join(messages) or "request blocked"))

    def wrap_tool_call(self, request, handler):
        result = handler(request)
        state, call_id = request.state, request.call_id
        before = state.metadata.get("harnessknight", {}).get("tool_decisions", {}).get(call_id, {})
        cache_key = (state.session_id, call_id, "after")
        hk = state.metadata.setdefault("harnessknight", {})
        stamps = hk.setdefault("occurred_at", {})
        stamp_key = ":".join(map(str, cache_key))
        event = self._after_events.get(cache_key)
        if event is None:
            output = json.dumps(result.to_dict(), ensure_ascii=False, default=str)
            event = build_event(state=state, event_type="tool.call.after", call_id=call_id, phase="after",
                                payload={"tool_name": request.name, "output": output},
                                sources=[{"source_id": _bounded_identifier(f"tool-result-{call_id}"), "kind": "tool_result"}],
                                causal_event_id=before.get("event_id"), mcp_server_id=getattr(request, "mcp_server_id", None),
                                occurred_at=stamps.setdefault(stamp_key, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
            self._after_events[cache_key] = event
        decision, messages = self.client.decide(event)
        hk.setdefault("after_decisions", {})[call_id] = {"decision": decision, "messages": messages}
        if decision == "BLOCK":
            return ToolResult.failure(ErrorCode.PERMISSION_DENIED, "tool output withheld by HarnessKnight: " + ("; ".join(messages) or "output blocked"))
        if decision == "REVIEW":
            hk.setdefault("notes", []).append("HarnessKnight requested review of tool output for " + call_id)
        return result


def install_harnessknight(policy, middleware_list, *, url, fail_mode="closed", timeout=3.0, transport=None, agent_version="unknown"):
    client = HarnessKnightClient(transport or HttpTransport(url), fail_mode=fail_mode, timeout=timeout, agent_version=agent_version)
    return HarnessKnightToolPolicy(policy, client, url), [*middleware_list, HarnessKnightMiddleware(client)]
