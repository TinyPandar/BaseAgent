"""Three-minute offline walkthrough. No provider calls and no credentials.

Shows a streamed answer, session ledger, approval around verify_command,
and an interrupted effect that resume does not run twice.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from baseagent.agent import run_agent  # noqa: E402
from baseagent.llm.response import ModelResponse, TokenUsage  # noqa: E402
from baseagent.llm.streaming import StreamDelta  # noqa: E402
from baseagent.middleware import AgentMiddleware  # noqa: E402
from baseagent.session import SessionStore  # noqa: E402
from baseagent.tools.policy import ToolPolicy  # noqa: E402
from baseagent.tools.registry import ToolRegistry  # noqa: E402
from baseagent.tools.result import ToolResult  # noqa: E402
from baseagent.tools.workspace import Workspace, coding_tools  # noqa: E402


def _call(name, args, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class StreamModel:
    def complete_stream(self, messages, tools=None):
        for piece in ("Citation ", "ready."):
            yield StreamDelta(kind="content", index=0, delta=piece)
        return ModelResponse({"content": "Citation ready. README.md:1-2"}, TokenUsage(4, 2, 6))

    def complete(self, messages, tools=None):
        return ModelResponse({"content": "Citation ready. README.md:1-2"}, TokenUsage(4, 2, 6))


class ScriptModel:
    def __init__(self, responses):
        self.responses = iter(responses)

    def complete(self, messages, tools=None):
        return ModelResponse(next(self.responses), TokenUsage(3, 1, 4))


class Interrupt(AgentMiddleware):
    def wrap_tool_call(self, request, handler):
        handler(request)
        raise KeyboardInterrupt()


def run_walkthrough(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    db = root / "sessions.sqlite3"
    store = SessionStore(db)
    stream = run_agent(StreamModel(), "Summarize the citation", tools=ToolRegistry(), store=store,
                       session_id="demo-stream", workspace_root=root,
                       runtime_config={"stream": {"enabled": True}})
    deltas = [event for event in store.events("demo-stream") if event.get("type") == "model_delta"]
    from baseagent.main import main as harness_main
    inspect_code = harness_main(["--inspect-session", "--session", "demo-stream", "--db", str(db), "--root", str(root)])
    marker = root / "mark.py"
    marker.write_text("from pathlib import Path\nPath('ran.txt').write_text('once\\n', encoding='utf-8')\n", encoding="utf-8")
    argv = [sys.executable, "-B", "mark.py"]
    workspace = Workspace(root, allow_command=True)
    tools = coding_tools(workspace)
    policy = ToolPolicy({"verify_command": "ask"})
    pending = run_agent(ScriptModel([{"tool_calls": [_call("verify_command", {"argv": argv, "paths": []}, "verify")]}]),
                        "verify the marker", tools=tools, store=store, session_id="demo-verify",
                        workspace_root=root, tool_policy=policy)
    before = (root / "ran.txt").exists()
    store.decide_tool("demo-verify", "verify", "allow", pending.metadata["approval_request"]["request_digest"])
    verified = run_agent(ScriptModel([{"content": "verified"}]), tools=tools, store=store,
                         session_id="demo-verify", workspace_root=root, tool_policy=policy)
    again = run_agent(ScriptModel([]), tools=tools, store=store, session_id="demo-verify",
                      workspace_root=root, tool_policy=policy)
    ran = (root / "ran.txt").read_text(encoding="utf-8")

    def effect():
        with (root / "effects.txt").open("a", encoding="utf-8") as handle:
            handle.write("executed\n")
        return {"executed": True}

    effect_tools = ToolRegistry()
    effect_tools.register("effect", "Controlled demo effect", {"type": "object"}, effect)
    interrupted = run_agent(ScriptModel([{"tool_calls": [_call("effect", {}, "a")]}]), "effect",
                            tools=effect_tools, store=store, session_id="demo-resume", middleware=[Interrupt()])
    blocked = run_agent(ScriptModel([]), tools=effect_tools, store=store, session_id="demo-resume")
    record = store.call(interrupted, "a")
    store.resolve_call("demo-resume", "a", ToolResult.from_dict(json.loads(record["terminal_result_json"])))
    resumed = run_agent(ScriptModel([{"content": "reconciled"}]), tools=effect_tools, store=store,
                        session_id="demo-resume")
    effects = (root / "effects.txt").read_text(encoding="utf-8")
    checks = {
        "streamed": stream.status == "completed" and bool(deltas),
        "citation": "README.md:1-2" in (stream.final_answer or ""),
        "inspect_exit": inspect_code == 0,
        "verify_pending_without_side_effect": pending.status == "awaiting_approval" and not before,
        "verify_ran_once": verified.status == "completed" and ran == "once\n" and again.tool_calls == verified.tool_calls,
        "interrupted": interrupted.status == "interrupted",
        "resume_blocked_until_reconcile": blocked.status == "needs_recovery",
        "resume_does_not_repeat": resumed.status == "completed" and effects == "executed\n" and resumed.tool_calls == 1,
        "provider_calls": 0,
        "cost_cny": 0,
    }
    checks["passed"] = all(value is True or value == 0 for key, value in checks.items() if key not in {"provider_calls", "cost_cny"})
    return checks


def main(argv=None):
    import tempfile
    folder = Path(tempfile.mkdtemp(prefix="baseagent-demo-"))
    checks = run_walkthrough(folder)
    print("Offline demo. No model API. Cost 0 CNY.")
    print("1. Streamed answer with a citation, then --inspect-session on demo-stream.")
    print("2. verify_command stayed pending until approval; ran.txt was written once.")
    print("3. Interrupted effect needed reconcile; resume did not append a second line.")
    print("Retained metrics, not recomputed here: coding 5/5, tokens -55.2%, hybrid MRR 0.845.")
    print(json.dumps({key: checks[key] for key in ("passed", "streamed", "verify_ran_once", "resume_does_not_repeat", "cost_cny")},
                     ensure_ascii=False, indent=2))
    return 0 if checks["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
