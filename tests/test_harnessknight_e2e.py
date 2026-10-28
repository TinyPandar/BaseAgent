import asyncio
import os
import sys
from pathlib import Path

import pytest

from baseagent.middleware.harnessknight import HarnessKnightClient, build_event


def _security_harness():
    root = Path(__file__).resolve().parents[1]
    candidates = [Path(os.environ["HARNESSKNIGHT_SRC"])] if os.environ.get("HARNESSKNIGHT_SRC") else []
    candidates += [Path("/workspace/HarnessKnight/src"), root.parent / "HarnessKnight" / "src"]
    for candidate in candidates:
        if (candidate / "security_harness").is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    try:
        from security_harness.domain import SecurityEvent
        from security_harness.bootstrap import HarnessConfig, build_runtime
        return SecurityEvent, HarnessConfig, build_runtime
    except ImportError:
        pytest.skip("HarnessKnight source is unavailable")


def test_runtime_event_contract_and_idempotent_replay(tmp_path):
    SecurityEvent, HarnessConfig, build_runtime = _security_harness()
    runtime = build_runtime(HarnessConfig(db=str(tmp_path / "audit.db"), mode="stage1"))

    class RuntimeTransport:
        def evaluate(self, event, idempotency_key, timeout):
            submitted_event, result = asyncio.run(runtime.submit(event, idempotency_key))
            return result.to_dict()

    from types import SimpleNamespace
    state = SimpleNamespace(session_id="hk-e2e", turn_id="turn1", messages=[{"role": "user", "content": "hello"}], turn_start=1)
    client = HarnessKnightClient(RuntimeTransport())
    prompt = build_event(state=state, event_type="user.prompt.submitted", call_id="turn1", phase="prompt",
                         payload={"prompt": "hello"}, sources=[{"source_id": "user-prompt-turn1", "kind": "user"}], occurred_at="2026-01-01T00:00:00Z")
    before = build_event(state=state, event_type="tool.call.before", call_id="call1", phase="before",
                         payload={"tool_name": "read_file", "arguments": {"path": "a"}}, occurred_at="2026-01-01T00:00:01Z")
    before["metadata"] = {"test.mock": {"action": "block"}}
    after = build_event(state=state, event_type="tool.call.after", call_id="call1", phase="after",
                        payload={"tool_name": "read_file", "output": "ok"}, causal_event_id=before["event_id"],
                        sources=[{"source_id": "tool-result-call1", "kind": "tool_result"}], occurred_at="2026-01-01T00:00:02Z")
    for event in (prompt, before, after):
        SecurityEvent.from_dict({**event, "seq": 1})
    assert client.decide(prompt)[0] == "ALLOW"
    assert client.decide(before)[0] == "BLOCK"
    assert client.decide(after)[0] in {"ALLOW", "REVIEW", "BLOCK"}
    assert client.decide(before)[0] == "BLOCK"
