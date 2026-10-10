"""Show a real DeepSeek recording advancing the harness entirely offline.

Run: .venv/Scripts/python.exe -B scripts/demo_replay.py
No credentials are read. Network connections are blocked during the demo.
"""

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.kernel import AgentMiddleware
from baseagent.testing import CassetteModel, CassetteMismatch

# Keep prompts/tool schemas exactly equal to those used for the real recording.
from record_cassettes import SCENARIOS, scenario_tools


class VisibleReplay(CassetteModel):
    calls = 0

    def complete_with_control(self, messages, tools=None, *, cancellation, timeout=None):
        self.calls += 1
        print(f"\n[{self.calls}] Harness requests a model response")
        if messages[-1]["role"] == "tool":
            print("    Current tool result:", messages[-1]["content"])
        else:
            print("    User request: call add(23, 19), then report the result")
        result = super().complete_with_control(messages, tools, cancellation=cancellation, timeout=timeout)
        print("    Request hash matched; reading the saved DeepSeek response")
        for call in result.message.get("tool_calls") or []:
            print("    Saved model response: call", call["function"]["name"], call["function"]["arguments"])
        if result.message.get("content"):
            print("    Saved model answer:", result.message["content"])
        return result


class VisibleTool(AgentMiddleware):
    def wrap_tool_call(self, request, handler):
        print("\n    Harness executes the local tool:", request.name, request.arguments)
        result = handler(request)
        print("    Actual local tool result:", json.dumps(result.to_dict(), ensure_ascii=False))
        return result


def main():
    path = Path(__file__).resolve().parents[1] / "tests" / "cassettes" / "deepseek_tool.jsonl"
    if not path.exists():
        print("Real tool recording is missing; record the synthetic scenarios first.")
        return 1
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    expected_answer = rows[-1]["response"]["value"]["message"]["content"]
    network_attempts = []

    def block_network(*args, **kwargs):
        network_attempts.append(1)
        raise AssertionError("network is disabled for this replay demo")

    print("REAL RECORDING, OFFLINE REPLAY: tests/cassettes/deepseek_tool.jsonl")
    with patch("socket.socket.connect", block_network):
        with VisibleReplay(path, mode="replay") as model:
            state = run_agent(model, SCENARIOS["tool"][0], tools=scenario_tools("tool"),
                              middleware=[VisibleTool()], max_model_calls=4, max_tool_calls=2)
            if state.status != "completed" or state.final_answer != expected_answer or state.tool_calls != 1:
                raise AssertionError("replay did not reproduce the recorded tool scenario")
        print("\nRESULT:", state.final_answer)
        print("Cached model responses:", state.model_calls, "| Actual tool executions:", state.tool_calls)
        print("Network attempts:", len(network_attempts), "| New provider requests: 0")

        print("\nCHANGE THE REQUEST: change 23 to 24")
        # Inspect only this synthetic fixture, then alter its first request.
        first = rows[1]["request"]
        changed = deepcopy(first["messages"])
        changed[-1]["content"] = changed[-1]["content"].replace("a=23", "a=24")
        try:
            with CassetteModel(path, mode="replay") as mismatch:
                mismatch.complete(changed, first["tools"])
        except CassetteMismatch as exc:
            print("    Replay refused:", str(exc))
        else:
            raise AssertionError("changed request should have failed strict replay")
    if network_attempts:
        raise AssertionError("demo attempted a network connection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
