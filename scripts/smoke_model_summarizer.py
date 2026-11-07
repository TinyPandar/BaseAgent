"""Opt-in ModelSummarizer smoke through a hard CNY meter. Default compaction is unchanged.

This exercises the optional ``ModelSummarizer`` adapter only. It builds a tiny
projected history above the compaction trigger, allows at most ONE summarizer
call (no 3-failure breaker loop), and prints JSON without prompts or endpoints.
"""

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from applications.project_assistant.evaluation.metering import MeteredModel  # noqa: E402
from baseagent.agent.compaction import ModelSummarizer, compact_history  # noqa: E402
from baseagent.agent.state import State  # noqa: E402
from baseagent.main import initialize_model, model_configuration  # noqa: E402

DEFAULT_COMPACTION_MAX_CHARS = 100
DEFAULT_MAX_SUMMARY_INPUT_CHARS = 1500
DEFAULT_MIN_PREFIX_CHARS = 10
DEFAULT_TAIL_CHARS = 10
DEFAULT_MIN_TAIL_ITEMS = 2


def _positive_decimal(value, name="budget"):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite positive amount") from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be a finite positive amount")
    return number


def _positive_int(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def build_state(*, compaction_max_chars=DEFAULT_COMPACTION_MAX_CHARS,
                max_summary_input_chars=DEFAULT_MAX_SUMMARY_INPUT_CHARS,
                min_prefix_chars=DEFAULT_MIN_PREFIX_CHARS,
                tail_chars=DEFAULT_TAIL_CHARS,
                min_tail_items=DEFAULT_MIN_TAIL_ITEMS):
    """A small history whose projection exceeds the trigger and has two prefix turns."""
    messages = [
        {"role": "system", "content": "Smoke test rules."},
        {"role": "user", "content": "First goal. " * 12},
        {"role": "assistant", "content": "First step completed. " * 10},
        {"role": "user", "content": "Second goal. " * 12},
        {"role": "assistant", "content": "Second step completed. " * 10},
        {"role": "user", "content": "Recent request."},
        {"role": "assistant", "content": "Recent answer."},
        {"role": "user", "content": "Current request."},
    ]
    return State(
        messages=messages,
        turn_start=7,
        compaction_max_chars=_positive_int(compaction_max_chars, "compaction_max_chars"),
        compaction_keep_turns=2,
        metadata={"compaction_config": {
            "tail_chars": tail_chars,
            "min_tail_items": min_tail_items,
            "max_tail_items": 20,
            "min_prefix_chars": min_prefix_chars,
            "max_summary_input_chars": _positive_int(
                max_summary_input_chars, "max_summary_input_chars"),
        }},
    )


def run_smoke(*, model, budget_cny, compaction_max_chars=DEFAULT_COMPACTION_MAX_CHARS,
              max_summary_input_chars=DEFAULT_MAX_SUMMARY_INPUT_CHARS,
              min_prefix_chars=DEFAULT_MIN_PREFIX_CHARS,
              tail_chars=DEFAULT_TAIL_CHARS,
              min_tail_items=DEFAULT_MIN_TAIL_ITEMS):
    """Run one compaction attempt. The caller supplies a metered model adapter."""
    budget = _positive_decimal(budget_cny)
    state = build_state(
        compaction_max_chars=compaction_max_chars,
        max_summary_input_chars=max_summary_input_chars,
        min_prefix_chars=min_prefix_chars,
        tail_chars=tail_chars,
        min_tail_items=min_tail_items)
    events = []

    def checkpoint(kind, **data):
        events.append({"type": kind, "data": data})

    compact_history(state, ModelSummarizer(model), checkpoint)
    kinds = [event["type"] for event in events]
    summary = (state.history_summary or {}).get("content") or ""
    cost = Decimal(str(getattr(model, "cost_cny", 0) or 0))
    return {
        "success": "context_compacted" in kinds and bool(summary.strip()),
        "events": kinds,
        "cost_cny": str(cost),
        "budget_cny": str(budget),
        "stopped_for_budget": cost >= budget,
        "trimmed": "context_compaction_input_trimmed" in kinds,
        "provider_calls": int(getattr(model, "calls", 0) or 0),
        "summary_excerpt": " ".join(summary.split())[:200],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget-cny", type=Decimal, required=True,
                        help="Hard CNY cutoff for summarizer usage; must be positive")
    parser.add_argument("--compaction-max-chars", type=int, default=DEFAULT_COMPACTION_MAX_CHARS)
    parser.add_argument("--max-summary-input-chars", type=int,
                        default=DEFAULT_MAX_SUMMARY_INPUT_CHARS)
    parser.add_argument("--min-prefix-chars", type=int, default=DEFAULT_MIN_PREFIX_CHARS)
    parser.add_argument("--tail-chars", type=int, default=DEFAULT_TAIL_CHARS)
    parser.add_argument("--min-tail-items", type=int, default=DEFAULT_MIN_TAIL_ITEMS)
    parser.add_argument("--max-completion-tokens", type=int, default=256)
    args = parser.parse_args(argv)
    try:
        budget = _positive_decimal(args.budget_cny)
    except ValueError as exc:
        print(json.dumps({"success": False, "status": "pending", "reason": str(exc)}))
        return 2
    try:
        model_configuration(generic_only=True)
    except ValueError:
        print("pending: complete BASEAGENT_LLM_* configuration required; no model calls made.")
        return 2
    try:
        with initialize_model(generic_only=True, timeout=30,
                              max_completion_tokens=args.max_completion_tokens,
                              generation={"temperature": 0}) as client:
            # One paid call at most. This smoke never runs the 3-failure breaker.
            meter = MeteredModel(client, budget_cny=budget, max_calls=1)
            report = run_smoke(
                model=meter, budget_cny=budget,
                compaction_max_chars=args.compaction_max_chars,
                max_summary_input_chars=args.max_summary_input_chars,
                min_prefix_chars=args.min_prefix_chars,
                tail_chars=args.tail_chars,
                min_tail_items=args.min_tail_items)
    except Exception as exc:  # noqa: BLE001 -- never expose credentials or payloads
        print(json.dumps({"success": False, "error": type(exc).__name__}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
