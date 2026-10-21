"""Opt-in synthetic DeepSeek recording. Never invoked by offline evaluation.

The entire invocation (all --runs combined) has a hard 20 HTTP-attempt cap.
Usage-based cost is charged after each response; an over-budget response stops
immediately and no further request is sent. This is a stop threshold, not a
provider-side prepaid ceiling: the last response can exceed it. Missing usage
fails closed. SDK retries are disabled by OpenAICompatModel.
"""

import argparse
import json
import os
import socket
import tempfile
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from applications.project_assistant.evaluation.coding import (
    run_scenario,
    scenario_workspace,
)
from baseagent.llm import OpenAICompatModel
from baseagent.testing import CassetteModel, Redactor

# CNY / million tokens, configurable; cache-miss input rate used conservatively.
# Source to verify BEFORE authorized recording: DeepSeek official pricing
# https://api-docs.deepseek.com/quick_start/pricing (not fetched offline).
# These are provisional deepseek-chat rates, not a claim about current pricing.
INPUT_CNY_PER_MILLION = Decimal(2)
OUTPUT_CNY_PER_MILLION = Decimal(3)
MAX_CALLS = 20


class RecordingStopped(RuntimeError):
    pass


class BoundedModel:
    """Shared across all runs; failed transport attempts also consume the cap."""
    def __init__(self, client, *, budget_cny=Decimal(2),
                 input_rate=INPUT_CNY_PER_MILLION, output_rate=OUTPUT_CNY_PER_MILLION):
        self.client = client
        self.budget = Decimal(str(budget_cny))
        self.input_rate, self.output_rate = Decimal(str(input_rate)), Decimal(str(output_rate))
        if any(not n.is_finite() or n <= 0 for n in (self.budget, self.input_rate, self.output_rate)):
            raise ValueError("budget and prices must be finite and positive")
        self.calls = 0
        self.cost_cny = Decimal(0)
        self.stopped = False
        self.model_name = getattr(client, "model_name", "fake")
        self.generation_params = getattr(client, "generation_params", {})
        self.max_completion_tokens = getattr(client, "max_completion_tokens", None)

    def complete(self, messages, tools=None):
        if self.stopped or self.calls >= MAX_CALLS or self.cost_cny >= self.budget:
            self.stopped = True
            raise RecordingStopped("recording call or budget limit")
        self.calls += 1
        try:
            response = self.client.complete(messages, tools)
        except Exception:  # noqa: BLE001 -- ambiguous billing must fail closed without provider diagnostics
            # Billing of an unsuccessful/ambiguous request cannot be known.
            self.stopped = True
            raise RecordingStopped("recording usage unavailable") from None
        if response.usage is None:
            self.stopped = True
            raise RecordingStopped("recording usage unavailable")
        usage = response.usage
        self.cost_cny += (usage.prompt_tokens * self.input_rate
                          + usage.completion_tokens * self.output_rate) / Decimal(1_000_000)
        print(json.dumps({"model_attempts": self.calls, "cost_cny": str(self.cost_cny)}))
        if self.cost_cny >= self.budget:
            self.stopped = True
            raise RecordingStopped("recording budget reached")
        return response


def cassette_redactor(root, key=""):
    # Normalize only workspace names and random verification IDs, preserving file
    # hashes, command output, argv and all semantically relevant request fields.
    return Redactor(patterns=(r"\b[0-9a-f]{32}\b",), secrets=(str(root), *([key] if key else [])))


def scan_cassette(path, key=""):
    text = Path(path).read_text(encoding="utf-8")
    Redactor(secrets=([key] if key else [])).assert_safe(text)
    # Scan decoded JSON too, so Unicode/backslash escaping cannot hide the key.
    decoded = json.dumps([json.loads(line) for line in text.splitlines()], ensure_ascii=False)
    if key and (key in text or key in decoded):
        raise ValueError("cassette contains credential")


@contextmanager
def disconnected():
    def blocked(*args, **kwargs):
        raise AssertionError("network forbidden during replay")
    with patch.object(socket.socket, "connect", blocked), patch.object(socket, "create_connection", blocked):
        yield


def replay(path):
    with (disconnected(), scenario_workspace() as root,
          CassetteModel(path, mode="replay", redactor=cassette_redactor(root)) as model):
        result, _, _ = run_scenario(model, root)
    return result


def signature(result):
    # Verification IDs differ between executions; compare outcomes and actual
    # usage/attempts/effects, not opaque workflow UUIDs.
    return {k: v for k, v in result.items() if k != "completion_report"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, choices=range(3, 6), default=3)
    parser.add_argument("--budget-cny", type=Decimal, default=Decimal(2))
    parser.add_argument("--input-cny-per-million", type=Decimal, default=INPUT_CNY_PER_MILLION)
    parser.add_argument("--output-cny-per-million", type=Decimal, default=OUTPUT_CNY_PER_MILLION)
    parser.add_argument("--output-dir", type=Path, default=Path("tests/cassettes/coding"))
    parser.add_argument("--replay-dir", type=Path, help="Offline replay only; no credential/client needed")
    args = parser.parse_args(argv)
    if args.replay_dir:
        for path in sorted(args.replay_dir.glob("coding-*.jsonl")):
            print(json.dumps(replay(path), ensure_ascii=False))
        return 0 if len(list(args.replay_dir.glob("coding-*.jsonl"))) == args.runs else 1
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        print("pending: DEEPSEEK_API_KEY required; no model calls made.")
        return 2
    published = []
    try:
        # Validate budget/rates before even constructing a provider client.
        meter = BoundedModel(None, budget_cny=args.budget_cny,
                             input_rate=args.input_cny_per_million, output_rate=args.output_cny_per_million)
        names = [f"coding-{i + 1}.jsonl" for i in range(args.runs)]
        if any((args.output_dir / name).exists() for name in names):
            raise FileExistsError("refusing to overwrite recording")
        with OpenAICompatModel(os.getenv("DEEPSEEK_MODEL", "deepseek-chat"), api_key_env="DEEPSEEK_API_KEY",
                               base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                               timeout=30, max_completion_tokens=1024, generation={"temperature": 0}) as client:
            meter.client = client
            meter.model_name, meter.generation_params = client.model_name, client.generation_params
            meter.max_completion_tokens = client.max_completion_tokens
            results = []
            with tempfile.TemporaryDirectory(prefix="coding-recording-") as directory:
                for name in names:
                    path = Path(directory) / name
                    with (scenario_workspace() as root,
                          CassetteModel(path, meter, mode="record", redactor=cassette_redactor(root, key)) as model):
                        result, _, _ = run_scenario(model, root)
                        if meter.stopped:
                            raise RecordingStopped("recording stopped")
                    scan_cassette(path, key)
                    if signature(replay(path)) != signature(result):
                        raise ValueError("offline replay differs")
                    results.append(result)
                # Publish only after privacy scanning and disconnected replay.
                args.output_dir.mkdir(parents=True, exist_ok=True)
                for name in names:
                    target = args.output_dir / name
                    with target.open("xb") as stream:
                        published.append(target)
                        stream.write((Path(directory) / name).read_bytes())
                print(json.dumps({"runs": results, "model_attempts": meter.calls,
                                  "cost_cny": str(meter.cost_cny)}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 -- do not expose credentials or provider/private diagnostics
        for path in published:
            path.unlink(missing_ok=True)
        print(f"pending: recording/check failed ({type(exc).__name__}); no cassettes published.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
