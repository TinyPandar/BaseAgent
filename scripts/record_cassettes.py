"""Record three FIXED synthetic DeepSeek scenarios, at most nine billed calls.

No prompt/history input is accepted. Credentials come from the existing
DEEPSEEK environment/local .env. A missing credential exits 2; failed checks
exit 1. No fixtures are published until every scenario and privacy scan passes.
"""

import argparse
import json
import os
from pathlib import Path
import tempfile

from dotenv import find_dotenv, load_dotenv

from baseagent.agent import run_agent
from baseagent.kernel import ToolRegistry
from baseagent.kernel.result import ToolResult, ErrorCode
from baseagent.llm import OpenAICompatModel
from baseagent.kernel.errors import BadRequestError
from baseagent.testing import CassetteModel, Redactor


SCENARIOS = {
    "direct": ("Reply exactly SYNTHETIC_OK. Do not use tools.", "SYNTHETIC_OK", 0),
    "tool": ("Call add with a=23 and b=19 exactly once. Then reply with the returned result 42. Do not calculate it without the tool.", "42", 1),
    "correction": ("This is a synthetic error recovery test. First call divide with a=84 and b=0 exactly once, deliberately testing its error. After its error, call divide with a=84 and b=2 exactly once. Then reply with the successful result 42.", "42", 2),
}


def scenario_tools(name):
    registry = ToolRegistry()
    schema = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
              "required": ["a", "b"], "additionalProperties": False}
    if name == "tool":
        registry.register("add", "Add two integers and return their sum.", schema, lambda a, b: a + b)
    if name == "correction":
        def divide(a, b):
            if b == 0:
                return ToolResult.failure(ErrorCode.INVALID_ARGUMENTS, "Synthetic error: denominator must not be zero. Retry with b=2.")
            return a / b
        registry.register("divide", "Divide a by b. A zero denominator returns a structured error; correct the inputs on the next call.", schema, divide)
    return registry


def run_scenario(model, name):
    prompt, expected, calls = SCENARIOS[name]
    state = run_agent(model, prompt, tools=scenario_tools(name), max_model_calls=4, max_tool_calls=2)
    if state.status != "completed" or expected not in (state.final_answer or "") or state.tool_calls != calls:
        raise ValueError("synthetic scenario did not meet completion assertions")
    tool_messages = [m for m in state.messages if m["role"] == "tool"]
    if name == "correction":
        results = [json.loads(message["content"]) for message in tool_messages]
        invocations = [call for message in state.messages for call in message.get("tool_calls", [])]
        inputs = [json.loads(call["function"]["arguments"]) for call in invocations]
        if len(results) != 2 or results[0]["ok"] or not results[1]["ok"] or inputs != [{"a": 84, "b": 0}, {"a": 84, "b": 2}]:
            raise ValueError("synthetic correction must execute deliberate failure then corrected success")
    return state


class BoundedModel(OpenAICompatModel):
    """Total across scenarios, including failed HTTP attempts; SDK retry is off."""
    calls = 0

    def complete_with_timeout(self, messages, tools=None, **kwargs):
        if self.calls >= 9:
            raise BadRequestError("synthetic recording reached nine-call limit")
        self.calls += 1
        return super().complete_with_timeout(messages, tools, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "tests" / "cassettes")
    args = parser.parse_args(argv)
    dotenv = find_dotenv(usecwd=True)
    if dotenv:
        load_dotenv(dotenv, override=False)
    if not os.getenv("DEEPSEEK_API_KEY") or not os.getenv("DEEPSEEK_BASE_URL"):
        print("Real fixtures pending: DEEPSEEK_API_KEY and DEEPSEEK_BASE_URL are required; no model calls made.")
        return 2
    names = [f"deepseek_{name}.jsonl" for name in SCENARIOS]
    if any((args.output_dir / name).exists() for name in names):
        print("Refusing to overwrite existing fixtures; choose a fresh output directory.")
        return 1
    published = []
    try:
        with BoundedModel(os.getenv("DEEPSEEK_MODEL", "deepseek-chat"), api_key_env="DEEPSEEK_API_KEY",
                          base_url=os.environ["DEEPSEEK_BASE_URL"], timeout=30, max_completion_tokens=512,
                          generation={"temperature": 0}) as model:
            with tempfile.TemporaryDirectory(prefix="baseagent-synthetic-") as directory:
                for name in SCENARIOS:
                    path = Path(directory) / f"deepseek_{name}.jsonl"
                    with CassetteModel(path, model, mode="record") as recording:
                        run_scenario(recording, name)
                    Redactor().assert_safe(path.read_text(encoding="utf-8"))
                    with CassetteModel(path, mode="replay") as replay:
                        run_scenario(replay, name)
                args.output_dir.mkdir(parents=True, exist_ok=True)
                # Temporary directory may be on another drive: stage each file
                # in its destination directory, then link without overwrite.
                for filename in names:
                    target = args.output_dir / filename
                    with tempfile.NamedTemporaryFile(dir=args.output_dir, prefix=".cassette-", delete=False) as stream:
                        staging = Path(stream.name)
                        stream.write((Path(directory) / filename).read_bytes())
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(staging, target)
                        published.append(target)
                    finally:
                        staging.unlink()
        print(f"Recorded and validated all three synthetic fixtures; model calls: {model.calls} (limit 9).")
        return 0
    except Exception as exc:
        for path in published:
            path.unlink()
        # Diagnostic type only; provider/private content never reaches stdout.
        print(f"Real fixtures pending: recording/check failed ({type(exc).__name__}); no fixtures published.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
