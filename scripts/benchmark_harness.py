"""Warm, single-thread offline harness smoke benchmark; no provider requests.

Reports small direct-answer and one-arithmetic-tool tasks. Includes per-task
state/model creation; excludes imports, tool registration and DB initialization.
This is not a production load benchmark or model-quality evaluation.
"""

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
import platform
import tempfile
import time

from baseagent.agent import run_agent
from baseagent.kernel import KernelState, ToolRegistry, iter_agent
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.session import SessionStore


class ImmediateModel:
    def __init__(self, tool):
        self.tool, self.calls = tool, 0

    def complete(self, messages, tools):
        self.calls += 1
        value = {"content": "42"}
        if self.tool and self.calls == 1:
            value = {"tool_calls": [{"id": "one", "type": "function", "function": {
                "name": "add", "arguments": '{"a":23,"b":19}'}}]}
        # Synthetic zero usage; this does not measure live token consumption.
        return ModelResponse(value, TokenUsage(0, 0, 0))


def tools_for(tool):
    registry = ToolRegistry()
    if tool:
        registry.register("add", "Add integers", {"type": "object", "properties": {
            "a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}, lambda a, b: a + b)
    return registry


def benchmark(samples=100, warmups=5):
    if type(samples) is not int or not 1 <= samples <= 10_000 or type(warmups) is not int or not 0 <= warmups <= 100:
        raise ValueError("samples must be 1..10000 and warmups 0..100")
    rows = []
    with tempfile.TemporaryDirectory(prefix="baseagent-perf-smoke-") as folder:
        for tool in (False, True):
            tools = tools_for(tool)
            for mode in ("kernel", "harness-memory", "harness-sqlite"):
                store = SessionStore(Path(folder) / f"{mode}-{tool}.sqlite3") if mode == "harness-sqlite" else None
                durations = []
                for index in range(samples + warmups):
                    start = time.perf_counter_ns()
                    model = ImmediateModel(tool)
                    if mode == "kernel":
                        state = KernelState(messages=[{"role": "system", "content": "You are a helpful agent. Use tools to verify facts before answering."},
                                                      {"role": "user", "content": "Calculate 23+19"}])
                        list(iter_agent(model, state, tools=tools))
                    else:
                        state = run_agent(model, "Calculate 23+19", tools=tools, store=store,
                                          **({"session_id": f"bench-{index}"} if store else {}))
                    elapsed = (time.perf_counter_ns() - start) / 1e6
                    if (state.status, state.final_answer, state.model_calls, state.tool_calls) != ("completed", "42", 2 if tool else 1, 1 if tool else 0):
                        raise AssertionError("benchmark failed its correctness assertions")
                    if index >= warmups:
                        durations.append(elapsed)
                ordered = sorted(durations)
                rows.append({"mode": mode, "scenario": "one-tool" if tool else "direct", "samples": samples, "successes": samples,
                             "p50_ms": ordered[math.ceil(samples * 0.5) - 1], "p95_ms": ordered[math.ceil(samples * 0.95) - 1], "raw_ms": durations})
    source_root = Path(__file__).resolve().parents[1] / "src" / "baseagent"
    source = {path.relative_to(source_root).as_posix(): sha256(path.read_bytes()).hexdigest() for path in sorted(source_root.rglob("*.py"))}
    return {"kind": "offline-warm-smoke", "python": platform.python_version(), "os": platform.system(),
            "samples_per_case": samples, "warmups_per_case": warmups, "provider_requests": 0,
            "source_digest": sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
            "scope": "single-thread synchronous; immediate scripted model; actual arithmetic tool; task/model/state preparation included; import/registry/DB initialization excluded; growing SQLite database with unique sessions; warm cache; not real task latency or production load",
            "cases": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("output exists; use a fresh filename")
    report = benchmark(args.samples, args.warmups)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    for row in report["cases"]:
        print(json.dumps({key: value for key, value in row.items() if key != "raw_ms"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
