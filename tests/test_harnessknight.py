import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from baseagent.middleware.harnessknight import (
    HarnessKnightClient, HarnessKnightMiddleware, HarnessKnightToolPolicy,
    HarnessUnavailable, HttpTransport, build_event, idempotency_key, infer_impacts,
)
from baseagent.kernel.hooks import ToolCallRequest
from baseagent.kernel.result import ErrorCode, ToolResult
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.policy import request_digest
from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.session import SessionStore
from baseagent.tools.workspace import Workspace, coding_tools
from baseagent.tools.edit_policy import EditPolicy


class FakeTransport:
    def __init__(self, decision="ALLOW", *, error=None, degraded=False):
        self.decision, self.error, self.degraded, self.calls = decision, error, degraded, []

    def evaluate(self, event, idempotency_key, timeout):
        self.calls.append((event.copy(), idempotency_key, timeout))
        if self.error:
            raise self.error
        return {"decision": self.decision, "risk_signals": [{"message": "test risk"}], "metadata": {"degraded": self.degraded}}


def state():
    return SimpleNamespace(session_id="session", turn_id="turn", turn_start=1,
                           messages=[{"role": "user", "content": "hello"}], metadata={})


@pytest.mark.parametrize("decision,expected", [("ALLOW", "allow"), ("REVIEW", "ask"), ("BLOCK", "deny")])
def test_before_decisions_and_stable_event(decision, expected):
    transport = FakeTransport(decision)
    client = HarnessKnightClient(transport, agent_version="1.2")
    policy = HarnessKnightToolPolicy(ToolPolicy(), client, "http://localhost")
    s = state()
    middleware = HarnessKnightMiddleware(client)
    req = ToolCallRequest(s, "call", "write_file", '{"path":"a.txt"}')
    middleware.before_tool(req)
    assert policy.action(s, "call", "write_file", '{"path":"a.txt"}') == expected
    assert policy.action(s, "call", "write_file", '{"path":"a.txt"}') == expected
    assert len(transport.calls) == 1
    event = transport.calls[0][0]
    assert event["idempotency_key"] == "baseagent-session-call"
    assert event["event_id"] == transport.calls[0][0]["event_id"]
    assert "seq" not in event
    assert set(event) == {"schema_version", "event_id", "idempotency_key", "event_type", "occurred_at", "agent", "session_id", "causal_event_id", "payload", "sources", "trace", "impacts", "metadata"}
    assert event["impacts"] == [{"kind": "file", "target": "a.txt", "operation": "write"}]


def test_review_exact_call_approval_and_reconstruction_reuse_timestamp():
    transport = FakeTransport("REVIEW")
    s = state()
    wrapper = HarnessKnightToolPolicy(ToolPolicy(), HarnessKnightClient(transport), "http://localhost")
    args = '{"path":"reviewed"}'
    HarnessKnightMiddleware(wrapper.client).before_tool(ToolCallRequest(s, "review-call", "read_file", args))
    assert wrapper.action(s, "review-call", "read_file", args) == "ask"
    first = transport.calls[0][0]
    s.metadata["tool_approvals"] = {"review-call": {"turn_id": "turn", "policy": wrapper.fingerprint,
        "request_digest": request_digest("read_file", args), "decision": "allow"}}
    assert wrapper.action(s, "review-call", "read_file", args) == "allow"
    restarted = HarnessKnightToolPolicy(ToolPolicy(), HarnessKnightClient(transport), "http://localhost")
    HarnessKnightMiddleware(restarted.client).before_tool(ToolCallRequest(s, "review-call", "read_file", args))
    assert restarted.action(s, "review-call", "read_file", args) == "allow"
    assert transport.calls[1][0]["occurred_at"] == first["occurred_at"]
    assert transport.calls[1][0]["event_id"] == first["event_id"]


def test_fail_modes_degraded_and_prompt_block():
    for transport in (FakeTransport(error=OSError("offline")), FakeTransport(degraded=True)):
        closed = HarnessKnightClient(transport)
        assert closed.decide({"agent": {}, "idempotency_key": "k"})[0] == "BLOCK"
        opened = HarnessKnightClient(transport, fail_mode="open")
        with pytest.warns(RuntimeWarning):
            assert opened.decide({"agent": {}, "idempotency_key": "k"})[0] == "ALLOW"
    transport = FakeTransport("BLOCK")
    middleware = HarnessKnightMiddleware(HarnessKnightClient(transport))
    with pytest.raises(RuntimeError, match="blocked by HarnessKnight"):
        middleware.before_agent(state())


def test_after_block_withholds_output_and_sets_causality():
    transport = FakeTransport("BLOCK")
    client = HarnessKnightClient(transport)
    s = state()
    policy = HarnessKnightToolPolicy(ToolPolicy(), client, "http://localhost")
    HarnessKnightMiddleware(client).before_tool(ToolCallRequest(s, "call", "read_file", '{"path":"a"}'))
    request = ToolCallRequest(s, "call", "read_file", '{"path":"a"}')
    middleware = HarnessKnightMiddleware(client)
    result = middleware.wrap_tool_call(request, lambda _: ToolResult(data="secret output"))
    assert result.error.code == ErrorCode.PERMISSION_DENIED
    assert "secret output" not in result.error.message
    assert transport.calls[-1][0]["causal_event_id"] == transport.calls[0][0]["event_id"]


def test_event_keys_and_impacts():
    assert len(idempotency_key("x" * 300)) == 128
    assert infer_impacts("edit_file", {"path": "x"})[0]["operation"] == "write"
    assert infer_impacts("grep", {"pattern": "needle"})[0]["target"] == "needle"
    assert infer_impacts("run_command", {"argv": ["git", "push", "origin"]})[1]["kind"] == "network"
    assert infer_impacts("unknown", {}) == []


def test_http_transport_headers_and_connection_refused(monkeypatch):
    seen = {}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers.get("Authorization")
            seen["key"] = self.headers.get("Idempotency-Key")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"decision":"ALLOW"}')
        def log_message(self, *args):
            pass
    try:
        server = HTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("sandbox does not permit local socket binding")
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        monkeypatch.setenv("HARNESS_API_TOKEN", "unit-secret")
        event = {"idempotency_key": "key", "event_id": "id"}
        assert HttpTransport(f"http://127.0.0.1:{server.server_port}").evaluate(event, "key", 1)["decision"] == "ALLOW"
        assert seen == {"auth": "Bearer unit-secret", "key": "key", "body": event}
    finally:
        server.shutdown(); thread.join()
    with pytest.raises(HarnessUnavailable):
        HttpTransport("http://127.0.0.1:1").evaluate({}, "key", .1)


def test_cli_flags_are_optional_and_off_by_default(capsys):
    from baseagent.main import main
    # Non-running introspection verifies the real parser without model/network use.
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "--harness-url" in capsys.readouterr().out


class ScriptedModel:
    def __init__(self, *responses):
        self.script, self.responses, self.calls = list(responses), iter(responses), []

    def complete(self, messages, tools):
        self.calls.append(json.loads(json.dumps(messages)))
        return next(self.responses)


class PhaseTransport(FakeTransport):
    def __init__(self, tool_decision, prompt_decision="ALLOW", after_decision="ALLOW"):
        super().__init__(tool_decision)
        self.decisions = {"user.prompt.submitted": prompt_decision,
                          "tool.call.before": tool_decision,
                          "tool.call.after": after_decision}

    def evaluate(self, event, idempotency_key, timeout):
        self.calls.append((event.copy(), idempotency_key, timeout))
        return {"decision": self.decisions[event["event_type"]]}


def call(name, args, call_id="write"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args, separators=(",", ":"))}}


def run_write(tmp_path, transport, model, *, policy=None, store=None, session_id="hk", prompt="write it"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(tmp_path, allow_write=True)
    digest = workspace.get_instructions("made.txt")["digest"]
    for response in model.script:
        for item in response.get("tool_calls", []):
            args = json.loads(item["function"]["arguments"])
            args["expected_instructions"] = digest
            item["function"]["arguments"] = json.dumps(args, separators=(",", ":"))
    tools = coding_tools(workspace)
    host = policy or ToolPolicy()
    hk_policy, layers = __import__("baseagent.middleware.harnessknight", fromlist=["install_harnessknight"]).install_harnessknight(
        host, [], url="http://fake", transport=transport)
    args = dict(path="made.txt", content="created", expected_sha256="missing",
                expected_instructions=workspace.get_instructions("made.txt")["digest"])
    result = run_agent(model, prompt, tools=tools, store=store, session_id=session_id if store else None,
                       tool_policy=hk_policy, middleware=layers)
    return result, workspace, args


def test_agent_loop_block_review_after_block_and_prompt_block(tmp_path):
    blocked = PhaseTransport("BLOCK")
    model = ScriptedModel({"tool_calls": [call("write_file", {"path": "made.txt", "content": "created",
        "expected_sha256": "missing", "expected_instructions": "bad"})]}, {"content": "finished"})
    result, workspace, _ = run_write(tmp_path / "blocked", blocked, model)
    assert result.status == RunStatus.COMPLETED, result.error
    assert not (workspace.root / "made.txt").exists()
    assert "permission_denied" in model.calls[1][-1]["content"]

    after_block = PhaseTransport("ALLOW", after_decision="BLOCK")
    model = ScriptedModel({"tool_calls": [call("write_file", {"path": "made.txt", "content": "secret-output",
        "expected_sha256": "missing", "expected_instructions": "bad"})]}, {"content": "finished"})
    result, workspace, _ = run_write(tmp_path / "after", after_block, model)
    assert result.status == RunStatus.COMPLETED
    assert "secret-output" not in model.calls[1][-1]["content"]

    prompt = PhaseTransport("ALLOW", prompt_decision="BLOCK")
    model = ScriptedModel()
    result, _, _ = run_write(tmp_path / "prompt", prompt, model)
    assert result.status == RunStatus.FAILED
    assert result.error.startswith("RuntimeError: blocked by HarnessKnight")
    assert model.calls == []


def test_agent_loop_review_resume_and_approval_combinations(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    transport = PhaseTransport("REVIEW")
    model = ScriptedModel({"tool_calls": [call("write_file", {"path": "made.txt", "content": "created",
        "expected_sha256": "missing", "expected_instructions": "bad"})]})
    result, workspace, _ = run_write(tmp_path / "review", transport, model, store=store)
    assert result.status == RunStatus.AWAITING_APPROVAL
    before_event = next(event for event, _, _ in transport.calls if event["event_type"] == "tool.call.before")
    request = result.metadata["approval_request"]
    store.decide_tool("hk", request["call_id"], "allow", request["request_digest"])
    resumed = ScriptedModel({"content": "finished"})
    result, workspace, _ = run_write(tmp_path / "review", transport, resumed, store=store, prompt=None)
    assert result.status == RunStatus.COMPLETED and (workspace.root / "made.txt").exists()
    resumed_event = [event for event, _, _ in transport.calls if event["event_type"] == "tool.call.before"][-1]
    assert resumed_event["event_id"] == before_event["event_id"]
    assert resumed_event["occurred_at"] == before_event["occurred_at"]

    for decision in ("ALLOW", "REVIEW"):
        root = tmp_path / ("edit-" + decision.lower())
        root.mkdir(parents=True, exist_ok=True)
        workspace = Workspace(root, allow_write=True, edit_policy=EditPolicy(max_changed_lines=100))
        tools = coding_tools(workspace)
        transport = PhaseTransport(decision)
        from baseagent.middleware.harnessknight import install_harnessknight
        policy, layers = install_harnessknight(ToolPolicy(), [], url="http://fake", transport=transport)
        args = dict(path="made.txt", content="created", expected_sha256="missing",
                    expected_instructions=workspace.get_instructions("made.txt")["digest"])
        store_id = "edit-" + decision.lower()
        store = SessionStore(tmp_path / (store_id + ".sqlite3"))
        result = run_agent(ScriptedModel({"tool_calls": [call("write_file", args)]}, {"content": "done"}), "write", tools=tools,
                           store=store, session_id=store_id, tool_policy=policy, middleware=layers)
        if decision == "REVIEW":
            assert result.status == RunStatus.AWAITING_APPROVAL
            req = result.metadata["approval_request"]
            store.decide_tool(store_id, req["call_id"], "allow", req["request_digest"])
            result = run_agent(ScriptedModel({"content": "done"}), tools=tools, store=store,
                               session_id=store_id, tool_policy=policy, middleware=layers)
        assert result.status == RunStatus.COMPLETED and (root / "made.txt").exists(), (decision, result.error, result.metadata.get("approval_request"))

    root = tmp_path / "parent-ask"
    root.mkdir(parents=True, exist_ok=True)
    transport = PhaseTransport("ALLOW")
    model = ScriptedModel({"tool_calls": [call("write_file", {"path": "made.txt", "content": "created",
        "expected_sha256": "missing", "expected_instructions": "bad"})]})
    result, workspace, _ = run_write(root, transport, model, policy=ToolPolicy({"write_file": "ask"}),
                                     store=store, session_id="parent-ask")
    assert result.status == RunStatus.AWAITING_APPROVAL
    req = result.metadata["approval_request"]
    store.decide_tool("parent-ask", req["call_id"], "allow", req["request_digest"])
    result, workspace, _ = run_write(root, transport, ScriptedModel({"content": "done"}),
                                     policy=ToolPolicy({"write_file": "ask"}), store=store,
                                     session_id="parent-ask", prompt=None)
    assert result.status == RunStatus.COMPLETED and (workspace.root / "made.txt").exists()
