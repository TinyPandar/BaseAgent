"""Opt-in OpenAI-compatible recording; offline replay never constructs a client.

Twenty provider attempts per run, shared usage-based budget across all runs.
The last response may cross the budget; no subsequent provider call is made.
Unknown usage fails closed. SDK retries are disabled. Prices supplied by the
user: qwen-plus non-thinking, Beijing, 2026-10-21 (not fetched offline):
https://help.aliyun.com/zh/model-studio/qwen-plus
"""

import argparse
import json
import re
import socket
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from applications.project_assistant.evaluation.coding import (
    run_scenario,
    scenario_workspace,
)
from baseagent.kernel.errors import ModelError
from baseagent.main import initialize_model, model_configuration
from baseagent.testing import CassetteModel, Redactor

INPUT_CNY_PER_MILLION = Decimal("0.8")
OUTPUT_CNY_PER_MILLION = Decimal(2)
REASONING_CNY_PER_MILLION = Decimal(8)
MAX_CALLS = 20
REPORTS_DIR = Path("applications/project_assistant/evaluation/reports")
# Use a registered cassette error so failed attempts reproduce exactly offline.
RecordingStopped = ModelError


class BoundedModel:
    """One meter per invocation; reset only the attempt counter between runs."""

    def __init__(self, client, *, budget_cny=Decimal(2),
                 input_rate=INPUT_CNY_PER_MILLION, output_rate=OUTPUT_CNY_PER_MILLION):
        self.client = client
        self.budget = Decimal(str(budget_cny))
        self.input_rate, self.output_rate = Decimal(str(input_rate)), Decimal(str(output_rate))
        if any(not n.is_finite() or n <= 0 for n in (self.budget, self.input_rate, self.output_rate)):
            raise ValueError("budget and prices must be finite and positive")
        self.cost_cny = Decimal(0)
        self.stopped = False
        self.model_name = getattr(client, "model_name", "fake")
        self.generation_params = getattr(client, "generation_params", {})
        self.max_completion_tokens = getattr(client, "max_completion_tokens", None)
        self.start_run()

    def start_run(self):
        self.calls = 0
        self.last_response = None
        self.run_cost_cny = Decimal(0)
        self.prompt_tokens = self.completion_tokens = self.reasoning_tokens = 0
        self.reason = None

    def complete(self, messages, tools=None):
        if self.stopped or self.cost_cny >= self.budget or self.calls >= MAX_CALLS:
            self.reason = self.reason or "recording call or budget limit"
            raise RecordingStopped(self.reason)
        self.calls += 1
        try:
            if hasattr(self.client, "complete_with_timeout"):
                response = self.client.complete_with_timeout(messages, tools, max_completion_tokens=self.max_completion_tokens)
            else:
                response = self.client.complete(messages, tools)
        except Exception:  # noqa: BLE001 -- never expose provider diagnostics
            self.stopped = True
            self.reason = "recording usage unavailable"
            raise RecordingStopped(self.reason) from None
        self.last_response = response
        if response.usage is None:
            self.stopped = True
            self.reason = "recording usage unavailable"
            raise RecordingStopped(self.reason)
        usage = response.usage
        reasoning = response.reasoning_tokens
        # OpenAI completion_tokens includes reasoning; avoid charging it twice.
        cost = (usage.prompt_tokens * self.input_rate
                + (usage.completion_tokens - reasoning) * self.output_rate
                + reasoning * REASONING_CNY_PER_MILLION) / Decimal(1_000_000)
        self.cost_cny += cost
        self.run_cost_cny += cost
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.reasoning_tokens += reasoning
        print(json.dumps({"model_attempts": self.calls, "cost_cny": str(self.cost_cny)}))
        if self.cost_cny >= self.budget:
            self.stopped = True
            self.reason = "recording budget reached"
        # Preserve the billed response/usage in the cassette, even at the threshold.
        return response


class EndpointRedactor(Redactor):
    def __init__(self, root, key="", base_url=""):
        super().__init__(patterns=(r"\b[0-9a-f]{32}\b",), secrets=(str(root), *([key] if key else [])))
        self.base_url = base_url

    def text(self, text):
        if self.base_url:
            text = text.replace(self.base_url, "<redacted-endpoint>")
        return super().text(text)


def cassette_redactor(root, key="", base_url=""):
    return EndpointRedactor(root, key, base_url)


def scan_cassette(path, key="", base_url=""):
    text = Path(path).read_text(encoding="utf-8")
    Redactor(secrets=([key] if key else [])).assert_safe(text)
    decoded = json.dumps([json.loads(line) for line in text.splitlines()], ensure_ascii=False)
    hostname = urlsplit(base_url).hostname or ""
    private_labels = [label for label in hostname.split(".") if label.lower().startswith("llm-")]
    if ((key and key in decoded) or (base_url and base_url in decoded)
            or any(label.lower() in decoded.lower() for label in private_labels)
            or re.search(r"\bllm-[a-z0-9-]+|authorization", decoded, re.IGNORECASE)):
        raise ValueError("cassette contains credential or private endpoint")


@contextmanager
def disconnected():
    def blocked(*args, **kwargs):
        raise AssertionError("network forbidden during replay")
    with (patch.object(socket.socket, "connect", blocked),
          patch.object(socket.socket, "connect_ex", blocked),
          patch.object(socket, "create_connection", blocked)):
        yield


def signature(result):
    return {k: v for k, v in result.items() if k != "completion_report"}


def outcome(result, state, ledger, root):
    """Compare final observable state and ordered tool requests, excluding UUIDs."""
    files = {name: sha256((root / name).read_bytes()).hexdigest()
             for name in ("sample.py", "REPORT.md") if (root / name).is_file()}
    return cassette_redactor(root).value({
        "result": signature(result),
        "final_state": {"status": state.status, "answer": state.final_answer,
                        "error": state.error, "plan": state.plan, "files": files},
        "tool_sequence": [json.loads(row["request_json"]) for row in ledger],
    })


def replay(path, *, details=False):
    with (disconnected(), scenario_workspace() as root,
          CassetteModel(path, mode="replay", redactor=cassette_redactor(root)) as model):
        result, state, ledger = run_scenario(model, root)
        actual = outcome(result, state, ledger, root)
    return actual if details else result


def smoke(model):
    tool = {"type": "function", "function": {"name": "ping", "description": "Return pong",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    try:
        response = model.complete([{"role": "user", "content": "Call ping with {} exactly once."}], [tool])
    except RecordingStopped:
        response = getattr(model, "last_response", None)
        if response is None:
            raise
    calls = response.to_dict()["message"].get("tool_calls") or []
    try:
        compliant = (len(calls) == 1 and calls[0]["type"] == "function"
                     and isinstance(calls[0]["id"], str) and bool(calls[0]["id"])
                     and calls[0]["function"]["name"] == "ping"
                     and json.loads(calls[0]["function"]["arguments"]) == {})
    except (KeyError, TypeError, ValueError):
        compliant = False
    report = {"compliant_tool_calls": compliant,
              "usage": asdict(response.usage) if response.usage else None,
              "reasoning_tokens": response.reasoning_tokens}
    print(json.dumps(report))
    return compliant and response.usage is not None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, choices=range(3, 6), default=3)
    parser.add_argument("--budget-cny", type=Decimal, default=Decimal(2))
    parser.add_argument("--input-cny-per-million", type=Decimal, default=INPUT_CNY_PER_MILLION)
    parser.add_argument("--output-cny-per-million", type=Decimal, default=OUTPUT_CNY_PER_MILLION)
    parser.add_argument("--output-dir", type=Path, default=Path("tests/cassettes/coding_scenario"))
    parser.add_argument("--report-dir", type=Path, default=REPORTS_DIR)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true")
    mode.add_argument("--replay", action="store_true")
    mode.add_argument("--replay-dir", type=Path, help="Alias for --replay with a directory")
    args = parser.parse_args(argv)
    if args.replay or args.replay_dir:
        directory = args.replay_dir or args.output_dir
        paths = sorted(path for path in directory.glob("run_*.json")
                       if re.fullmatch(r"run_[0-9]+\.json", path.name))
        passed = bool(paths)
        for path in paths:
            try:
                expected = json.loads(path.with_suffix(".result.json").read_text())["outcome"]
                actual = replay(path, details=True)
                checks = {name: actual[name] == expected[name]
                          for name in ("final_state", "tool_sequence", "result")}
                same = all(checks.values())
            except Exception:  # noqa: BLE001 -- sanitized replay diagnostics
                checks = {}
                same = False
            passed &= same
            print(json.dumps({"run": path.stem, "replay_consistent": same, "checks": checks}))
        return 0 if passed else 1
    try:
        configuration = model_configuration(generic_only=True)
    except ValueError:
        print("pending: complete BASEAGENT_LLM_* configuration required; no model calls made.")
        return 2
    key, url = configuration["api_key"], configuration["base_url"]
    try:
        meter = BoundedModel(None, budget_cny=args.budget_cny,
                             input_rate=args.input_cny_per_million, output_rate=args.output_cny_per_million)
        names = [f"run_{i + 1}.json" for i in range(args.runs)]
        if not args.smoke and any((args.output_dir / name).exists() or
                                  (args.output_dir / name).with_suffix(".result.json").exists() for name in names):
            raise FileExistsError("refusing to overwrite recording")
        with initialize_model(generic_only=True, timeout=30, max_completion_tokens=64 if args.smoke else 1024,
                              generation={"temperature": 0}) as client:
            meter.client = client
            meter.model_name, meter.generation_params = client.model_name, client.generation_params
            meter.max_completion_tokens = client.max_completion_tokens
            if args.smoke:
                return 0 if smoke(meter) else 1
            results = []
            for number, name in enumerate(names, 1):
                if meter.stopped:
                    break
                meter.start_run()
                with tempfile.TemporaryDirectory(prefix="coding-recording-") as directory:
                    path = Path(directory) / name
                    with scenario_workspace() as root:
                        model = CassetteModel(path, meter, mode="record", redactor=cassette_redactor(root, key, url))
                        try:
                            result, state, ledger = run_scenario(model, root)
                            observed = outcome(result, state, ledger, root)
                            reason = meter.reason or state.error or (None if result["external_acceptance_passed"] else "external acceptance failed")
                        except Exception as exc:  # noqa: BLE001 -- keep partial recordings, sanitize diagnostics
                            result = {"status": "failed", "external_acceptance_passed": False,
                                      "tool_attempts": None, "unauthorized_side_effects": None,
                                      "duplicate_side_effects": None}
                            observed = None
                            reason = "scenario failed: " + type(exc).__name__
                        finally:
                            # Include failed model attempts as well as successful responses.
                            model.close()
                    lines = path.read_text().splitlines()
                    header = json.loads(lines[0])
                    header["base_url"] = "<redacted-endpoint>"
                    if "extra_body" in configuration:
                        header["extra_body"] = configuration["extra_body"]
                    lines[0] = json.dumps(header, ensure_ascii=False)
                    path.write_text("\n".join(lines) + "\n")
                    scan_cassette(path, key, url)
                    try:
                        consistent = observed is not None and replay(path, details=True) == observed
                    except Exception:  # noqa: BLE001 -- retain the failed replay for diagnosis
                        consistent = False
                    record = {"run": number, **signature(result), "model_attempts": meter.calls,
                              "prompt_tokens": meter.prompt_tokens, "completion_tokens": meter.completion_tokens,
                              "reasoning_tokens": meter.reasoning_tokens, "cost_cny": str(meter.run_cost_cny),
                              "reason": reason, "replay_consistent": consistent, "outcome": observed}
                    metadata = Path(directory) / "metadata.json"
                    metadata.write_text(json.dumps(record, ensure_ascii=False) + "\n")
                    scan_cassette(metadata, key, url)
                    args.output_dir.mkdir(parents=True, exist_ok=True)
                    for source, target in ((path, args.output_dir / name),
                                           (metadata, (args.output_dir / name).with_suffix(".result.json"))):
                        with target.open("xb") as stream:
                            stream.write(source.read_bytes())
                    results.append(record)
            report = {"runs": results, "model_attempts": sum(r["model_attempts"] for r in results),
                      "cost_cny": str(meter.cost_cny), "budget_cny": str(meter.budget),
                      "input_cny_per_million": str(meter.input_rate),
                      "output_cny_per_million": str(meter.output_rate),
                      "reasoning_cny_per_million": str(REASONING_CNY_PER_MILLION)}
            # Provider labels and endpoint configuration are deliberately excluded.
            args.report_dir.mkdir(parents=True, exist_ok=True)
            report_path = args.report_dir / ("coding-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex + ".json")
            with tempfile.TemporaryDirectory(prefix="coding-report-") as directory:
                candidate = Path(directory) / "report.json"
                candidate.write_text(json.dumps(report, ensure_ascii=False) + "\n")
                scan_cassette(candidate, key, url)
                with report_path.open("xb") as stream:
                    stream.write(candidate.read_bytes())
            print(json.dumps({"runs_saved": len(results), "cost_cny": str(meter.cost_cny)}))
            return 0 if len(results) == args.runs and all(r["external_acceptance_passed"] and r["replay_consistent"] for r in results) else 1
    except Exception as exc:  # noqa: BLE001 -- never expose credentials/provider diagnostics
        print(f"pending: recording/check failed ({type(exc).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
