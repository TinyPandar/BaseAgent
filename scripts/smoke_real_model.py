"""Manual DeepSeek smoke; deliberately outside unittest discover.

Expected consumption: five real requests (two per addition path, one cancelled
request); hard cap seven requests, max 128 completion tokens per request.
SDK retries are disabled by the existing repository adapter. A cancelled
request may still be billed. Credentials come ONLY from process environment.
"""

import json
import os
from pathlib import Path
import sys
import threading
import time


REQUEST_TIMEOUT = 15.0
MAX_REQUESTS = 7
PROMPT = "Call add exactly once with a=19 and b=23. Then answer with the tool's numeric result."


def main():
    key, url = os.environ.get("DEEPSEEK_API_KEY"), os.environ.get("DEEPSEEK_BASE_URL")
    missing = [name for name, value in (("DEEPSEEK_API_KEY", key), ("DEEPSEEK_BASE_URL", url)) if not value]
    if missing:
        print("real model smoke FAILED: missing environment variables: " + ", ".join(missing), file=sys.stderr)
        return 2
    # The imported adapter is the repository's OpenAI-compatible DeepSeek path;
    # no dotenv loader is invoked and explicit environment values are passed.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from baseagent.agent import run_agent
    from baseagent.agent.state import State
    from baseagent.kernel import CancellationToken, KernelState, RunStatus, ToolRegistry, iter_agent
    from baseagent.llm.model import Model

    total_calls = [0]

    class SmokeModel(Model):
        def __init__(self, *, entered=None, finished=None):
            super().__init__(os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"), key, url)
            self.entered, self.finished = entered, finished
            self.returned = False

        def complete_with_control(self, messages, tools, *, cancellation, timeout=None):
            cancellation.check()
            if total_calls[0] >= MAX_REQUESTS:
                raise RuntimeError("smoke request cap reached")
            total_calls[0] += 1
            if self.entered:
                self.entered.set()
            try:
                result = self.complete_with_timeout(messages, tools, timeout=REQUEST_TIMEOUT,
                                                    max_completion_tokens=128)
                self.returned = True
                return result
            finally:
                if self.finished:
                    self.finished.set()

    def tools_for(effects):
        tools = ToolRegistry()
        tools.register("add", "Return a + b", {"type": "object", "properties": {
            "a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]},
            lambda a, b: effects.append((a, b)) or a + b)
        return tools

    def check(state, effects, events, state_type):
        assert state.status == RunStatus.COMPLETED, "task did not complete"
        assert effects == [(19, 23)] and state.tool_calls == 1, "unexpected tool calls"
        assert "42" in (state.final_answer or ""), "answer lacks the verified result"
        kinds = [event["type"] for event in events]
        assert kinds[0] == "run_started" and kinds[-1] == "run_stopped", "invalid run event order"
        assert kinds.count("model_started") == kinds.count("model_returned") == state.model_calls, "invalid model event counts"
        assert kinds.count("tool_started") == kinds.count("tool_returned") == kinds.count("tool_completed") == 1, "invalid tool event counts"
        assert kinds.index("model_started") < kinds.index("model_returned") < kinds.index("tool_started") < kinds.index("tool_returned") < kinds.index("tool_completed"), "invalid dispatch order"
        assert state_type.from_dict(json.loads(json.dumps(state.to_dict(), ensure_ascii=False, allow_nan=False))) == state, "state JSON round trip failed"

    for mode in ("kernel", "sync"):
        effects, model = [], SmokeModel()
        try:
            tools = tools_for(effects)
            if mode == "kernel":
                state = KernelState(messages=[{"role": "system", "content": "Use the given tool and follow the task exactly."},
                                              {"role": "user", "content": PROMPT}], max_steps=3, max_model_calls=3, max_tool_calls=1)
                events = [event.to_dict() for event in iter_agent(model, state, tools=tools)]
                state_type = KernelState
            else:
                state = run_agent(model, PROMPT, tools=tools, max_steps=3, max_model_calls=3, max_tool_calls=1,
                                  system_prompt="Use the given tool and follow the task exactly.")
                events, state_type = state.events, State
            check(state, effects, events, state_type)
            print(mode + " addition smoke: PASS")
        finally:
            model.client.close()

    # Cancellation is cooperative: allow the bounded synchronous request to
    # return, then assert cancelled. Never leave a worker performing HTTP I/O.
    entered, finished, cancelled_in_flight = threading.Event(), threading.Event(), threading.Event()
    token = CancellationToken()
    before = {thread.ident for thread in threading.enumerate()}

    def cancel_in_flight():
        if entered.wait(REQUEST_TIMEOUT):
            if not finished.wait(0.2):
                cancelled_in_flight.set()
                token.cancel()

    canceller = threading.Thread(target=cancel_in_flight, name="real-model-smoke-cancellation")
    model = SmokeModel(entered=entered, finished=finished)
    state = KernelState(messages=[{"role": "system", "content": "Answer briefly."},
                                  {"role": "user", "content": "Explain why 19 + 23 is 42 in a few sentences."}],
                        max_model_calls=1, max_steps=1)
    started = time.monotonic()
    canceller.start()
    try:
        list(iter_agent(model, state, tools=ToolRegistry(), cancellation=token))
    finally:
        finished.set()
        model.client.close()
        canceller.join(REQUEST_TIMEOUT + 2)
    elapsed = time.monotonic() - started
    assert cancelled_in_flight.is_set() and model.returned, "no successful real request was cancelled in flight"
    assert state.status == RunStatus.CANCELLED and state.model_calls == 1, "cancellation did not stop the task"
    assert elapsed <= REQUEST_TIMEOUT + 5, "cancellation exceeded the bounded timeout"
    assert not canceller.is_alive(), "cancellation thread leaked"
    residual = [thread for thread in threading.enumerate() if thread.ident not in before]
    assert not residual, "new threads remain after cancellation"
    print("real in-flight cancellation: PASS (no thread residue)")
    print("real requests used:", total_calls[0])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        # Never print exception bodies, request data, credentials or responses.
        print("real model smoke FAILED: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
