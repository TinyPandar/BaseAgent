"""Opt-in REAL Anthropic smoke: one plain call and one tool round-trip (3 paid requests at most).

Never part of the test suite or CI. It does nothing unless ANTHROPIC_API_KEY is set in the
process environment (this script does not load .env) AND --yes-spend-money is given.
Prints JSON with statuses, block types and token counts only: no prompts, keys or endpoints.

    ANTHROPIC_API_KEY=... python scripts/anthropic_smoke.py --yes-spend-money
    python scripts/anthropic_smoke.py --yes-spend-money --model claude-opus-5-5 --effort medium
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from baseagent.agent import run_agent  # noqa: E402
from baseagent.kernel import ToolRegistry  # noqa: E402
from baseagent.llm import AnthropicModel  # noqa: E402

ADD = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes-spend-money", action="store_true", help="Required: allow real, billed Anthropic requests")
    parser.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL") or "claude-haiku-5-5")
    parser.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--thinking", default="adaptive", choices=["adaptive", "omit"],
                        help="adaptive sends thinking {type: adaptive}; omit leaves the model default")
    parser.add_argument("--max-completion-tokens", type=int, default=4000)
    args = parser.parse_args(argv)
    if not args.yes_spend_money:
        print("pending: pass --yes-spend-money to make real, billed requests; no model calls made.")
        return 2
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("pending: ANTHROPIC_API_KEY is not set in the process environment; no model calls made.")
        return 2
    generation = {"output_config": {"effort": args.effort}}
    if args.thinking == "adaptive":
        generation["thinking"] = {"type": "adaptive"}
    report = {"model": args.model, "success": False}
    try:
        with AnthropicModel(args.model, base_url=os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
                            timeout=120, max_completion_tokens=args.max_completion_tokens,
                            generation=generation) as model:
            plain = model.complete([{"role": "user", "content": "Reply with exactly: SMOKE_OK"}])
            report["plain"] = {"text_ok": "SMOKE_OK" in (plain.message.get("content") or ""),
                               "usage": plain.to_dict()["usage"], "reasoning_tokens": plain.reasoning_tokens}
            calls = []
            registry = ToolRegistry()
            registry.register("add", "Add two integers and return the sum.", ADD,
                              lambda a, b: calls.append((a, b)) or a + b)
            state = run_agent(model, "Use the add tool to compute 23 + 19, then state the result.",
                              tools=registry, max_steps=4, max_model_calls=2)
            assistant = [message for message in state.messages if message.get("role") == "assistant"]
            report["tool_loop"] = {
                "status": str(state.status), "tool_calls": len(calls), "answer_has_42": "42" in (state.final_answer or ""),
                "total_tokens": state.total_tokens,
                # Block types only: shows whether thinking blocks were returned and replayed.
                "block_types": [[block.get("type") for block in message.get("anthropic_content", [])] for message in assistant]}
            report["success"] = report["plain"]["text_ok"] and report["tool_loop"]["status"] == "completed" \
                and report["tool_loop"]["tool_calls"] == 1 and report["tool_loop"]["answer_has_42"]
    except Exception as exc:  # noqa: BLE001 -- report the class only; never echo bodies or credentials
        report["error"] = type(exc).__name__ + ": " + str(exc)[:200]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
