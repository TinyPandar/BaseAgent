"""--provider anthropic, extended thinking and stop reasons. Offline: a scripted fake HTTP layer only.

No test here opens a socket or reads a real key; every credential is a fake placeholder.
"""

import contextlib
import importlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import config_isolation  # noqa: F401 -- no real .env or trust store, also under unittest

from baseagent.agent import run_agent
from baseagent.kernel import ToolRegistry
from baseagent.kernel.errors import BadRequestError, ContextLengthError, ResponseParseError
from baseagent.kernel.messages import Message, check_tool_pairing
from baseagent.llm import AnthropicModel, Model, ModelResponse, TokenUsage
from baseagent.session import SessionStore
cli = importlib.import_module("baseagent.main")

ANTHROPIC_KEY = "sk-ant-fake-anthropic-0123456789"
GENERIC_KEY = "sk-fake-generic-0123456789"
DEEPSEEK_KEY = "sk-fake-deepseek-0123456789"
PROVIDER_VARIABLES = [prefix + name for prefix in ("BASEAGENT_LLM_", "DEEPSEEK_")
                      for name in ("API_KEY", "BASE_URL", "MODEL", "PROVIDER", "EXTRA_BODY")] + [
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"]
TOOLS = [{"type": "function", "function": {"name": "add", "description": "Add integers", "parameters": {
    "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}}}]
THINK_1 = {"type": "thinking", "thinking": "Need the add tool.", "signature": "sig-one=="}
REDACTED = {"type": "redacted_thinking", "data": "opaque-redacted=="}
THINK_2 = {"type": "thinking", "thinking": "", "signature": "sig-two=="}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "add", "input": {"a": 23, "b": 19}}


def reply(content, stop="end_turn", **usage):
    return {"id": "msg", "type": "message", "role": "assistant", "content": content, "stop_reason": stop,
            "usage": {"input_tokens": 10, "output_tokens": 8, **usage}}


class _Response:
    def __init__(self, data):
        self.data = data

    def read(self, limit):
        return self.data[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeHTTP:
    """Scripted stand-in for the adapter's urllib opener; records every request."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def open(self, req, timeout):
        self.requests.append({"url": req.full_url, "headers": {k.lower(): v for k, v in req.header_items()},
                              "body": json.loads(req.data), "timeout": timeout})
        return _Response(json.dumps(self.replies.pop(0)).encode())


class ProviderEnvironment(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in PROVIDER_VARIABLES:
            os.environ.pop(name, None)


class AnthropicAdapterTests(ProviderEnvironment):
    def model(self, replies, **kwargs):
        os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_KEY
        model = AnthropicModel("claude-opus-5-5", **kwargs)
        self.addCleanup(model.close)
        model._opener = FakeHTTP(replies)
        return model

    def test_thinking_blocks_survive_a_two_step_tool_loop_unchanged(self):
        first = [THINK_1, REDACTED, {"type": "text", "text": "Adding."}, TOOL_USE]
        model = self.model([reply(first, "tool_use", output_tokens_details={"thinking_tokens": 5}),
                            reply([THINK_2, {"type": "text", "text": "42"}])],
                           generation={"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}})
        registry = ToolRegistry()
        registry.register("add", "Add integers", TOOLS[0]["function"]["parameters"], lambda a, b: a + b)
        state = run_agent(model, "Add 23 and 19", tools=registry)
        self.assertEqual(state.status, "completed")
        self.assertEqual(state.final_answer, "42")
        first_request, second_request = (request["body"] for request in model._opener.requests)
        self.assertEqual(first_request["thinking"], {"type": "adaptive"})
        self.assertEqual(first_request["output_config"], {"effort": "high"})
        # The assistant turn goes back exactly as returned: thinking (with signatures) first, same order.
        assistant = [turn for turn in second_request["messages"] if turn["role"] == "assistant"]
        self.assertEqual(assistant, [{"role": "assistant", "content": first}])
        self.assertEqual(second_request["messages"][-1]["content"][0]["tool_use_id"], "toolu_1")
        stored = [message for message in state.messages if message["role"] == "assistant"]
        self.assertEqual(stored[0]["anthropic_content"], first)
        self.assertEqual(stored[0]["content"], "Adding.")
        for message in state.messages:
            self.assertEqual(Message.from_dict(message).to_dict(), message)
            self.assertEqual(json.dumps(Message.from_dict(message).to_dict()), json.dumps(message))
        check_tool_pairing(state.messages, allow_pending=False)

    def test_reported_thinking_tokens_and_cassette_envelope(self):
        model = self.model([reply([THINK_1, {"type": "text", "text": "ok"}], output_tokens_details={"thinking_tokens": 5})])
        result = model.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(result.usage, TokenUsage(10, 8, 18))
        self.assertEqual(result.reasoning_tokens, 5)
        self.assertEqual(ModelResponse.from_dict(result.to_dict()), result)
        model._opener.replies.append(reply([{"type": "text", "text": "ok"}], output_tokens_details={"thinking_tokens": 99}))
        self.assertEqual(model.complete([{"role": "user", "content": "hi"}]).reasoning_tokens, 0)  # inconsistent count ignored

    def test_edited_assistant_message_keeps_thinking_first(self):
        model = self.model([reply([{"type": "text", "text": "ok"}])])
        call = {"id": "toolu_1", "type": "function", "function": {"name": "add", "arguments": '{"a":1,"b":2}'}}
        messages = [{"role": "user", "content": "Add"},
                    {"role": "assistant", "content": "Edited summary", "tool_calls": [call],
                     "anthropic_content": [THINK_1, {"type": "text", "text": "Original"}, TOOL_USE, REDACTED]},
                    {"role": "tool", "tool_call_id": "toolu_1", "content": "3"}]
        model.complete(messages, TOOLS)
        sent = model._opener.requests[0]["body"]["messages"][1]["content"]
        self.assertEqual(sent, [THINK_1, REDACTED, {"type": "text", "text": "Edited summary"},
                                {"type": "tool_use", "id": "toolu_1", "name": "add", "input": {"a": 1, "b": 2}}])

    def test_responses_without_thinking_keep_their_shape(self):
        model = self.model([reply([{"type": "text", "text": "42"}])])
        self.assertEqual(model.complete([{"role": "user", "content": "q"}]).message, {"role": "assistant", "content": "42"})
        self.assertNotIn("thinking", model._opener.requests[0]["body"])
        self.assertNotIn("output_config", model._opener.requests[0]["body"])

    def test_stop_reasons(self):
        user = [{"role": "user", "content": "q"}]
        cases = [
            (reply([{"type": "text", "text": "partial"}], "max_tokens"), None, "partial"),
            (reply([THINK_1], "max_tokens"), ResponseParseError, "--max-completion-tokens"),
            (reply([{"type": "text", "text": "x"}, TOOL_USE], "max_tokens"), ResponseParseError, "during a tool call"),
            (reply([], "model_context_window_exceeded"), ContextLengthError, "model_context_window_exceeded"),
            ({**reply([], "refusal"), "stop_details": {"type": "refusal", "category": "cyber"}}, BadRequestError, "category cyber"),
            ({**reply([{"type": "text", "text": "no"}], "refusal"), "stop_details": {"category": "<b>bad</b>"}}, BadRequestError, "refusal"),
            (reply([{"type": "text", "text": "x"}], "pause_turn"), ResponseParseError, "pause_turn"),
            (reply([], None), ResponseParseError, "invalid Anthropic"),
            (reply([{"type": "image", "source": {}}]), ResponseParseError, "invalid Anthropic"),
        ]
        for payload, error, text in cases:
            with self.subTest(stop=payload["stop_reason"], error=error):
                model = self.model([payload])
                if error is None:
                    self.assertEqual(model.complete(user).message["content"], text)
                    continue
                with self.assertRaises(error) as caught:
                    model.complete(user)
                self.assertIn(text, str(caught.exception))
                self.assertNotIn("<b>", str(caught.exception))

    def test_empty_end_turn_is_an_empty_answer_and_is_not_replayed(self):
        model = self.model([reply([]), reply([THINK_2]), reply([{"type": "text", "text": "next"}])])
        self.assertEqual(model.complete([{"role": "user", "content": "q"}]).message, {"role": "assistant", "content": ""})
        thought = model.complete([{"role": "user", "content": "q"}]).message
        self.assertEqual(thought, {"role": "assistant", "content": "", "anthropic_content": [THINK_2]})
        model.complete([{"role": "user", "content": "q"}, {"role": "assistant", "content": ""},
                        {"role": "user", "content": "again"}, thought, {"role": "user", "content": "more"}])
        self.assertEqual(model._opener.requests[-1]["body"]["messages"], [
            {"role": "user", "content": [{"type": "text", "text": "q"}, {"type": "text", "text": "again"}]},
            {"role": "assistant", "content": [THINK_2]},
            {"role": "user", "content": [{"type": "text", "text": "more"}]}])

    def test_thinking_configuration_is_validated_locally(self):
        os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_KEY
        for generation, cap in [({"thinking": {"type": "enabled", "budget_tokens": 4096}}, 4096),
                                ({"thinking": {"type": "enabled", "budget_tokens": 512}}, 4096),
                                ({"thinking": {"type": "sometimes"}}, 4096),
                                ({"thinking": {"type": "adaptive", "display": "loud"}}, 4096),
                                ({"output_config": {"effort": "extreme"}}, 4096),
                                ({"output_config": {"effort": "low", "format": {}}}, 4096)]:
            with self.subTest(generation=generation), self.assertRaises(ValueError):
                AnthropicModel("m", max_completion_tokens=cap, generation=generation)
        model = AnthropicModel("m", max_completion_tokens=8000, generation={"thinking": {"type": "enabled", "budget_tokens": 2048}})
        model.close()


class OpenAICompatiblePathTests(unittest.TestCase):
    def test_anthropic_blocks_never_reach_openai_compatible_requests(self):
        sdk = Mock()
        sdk.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message={"role": "assistant", "content": "ok"})], usage=None)
        with patch("baseagent.llm.model.OpenAI", Mock(return_value=sdk)), \
                patch("baseagent.llm.model.DefaultHttpxClient", Mock()):
            model = Model("qwen-plus", "fake-key", "https://compatible.invalid/v1")
            plain = [{"role": "user", "content": "q"}, {"content": "", "reasoning_content": "r", "role": "assistant"}]
            model.complete(plain)
            self.assertIs(sdk.chat.completions.create.call_args.kwargs["messages"], plain)  # untouched, same object
            mixed = plain + [{"role": "assistant", "content": "a", "anthropic_content": [THINK_1]}]
            model.complete(mixed)
            sent = sdk.chat.completions.create.call_args.kwargs["messages"]
            self.assertEqual(sent, plain + [{"role": "assistant", "content": "a"}])
            self.assertIn("anthropic_content", mixed[-1])  # the stored conversation is not modified


class ProviderSelectionTests(ProviderEnvironment):
    def setUp(self):
        super().setUp()
        os.environ.update({"BASEAGENT_LLM_API_KEY": GENERIC_KEY, "BASEAGENT_LLM_BASE_URL": "https://generic.invalid/v1",
                           "BASEAGENT_LLM_MODEL": "qwen-plus", "DEEPSEEK_API_KEY": DEEPSEEK_KEY,
                           "DEEPSEEK_BASE_URL": "https://deepseek.invalid"})

    def test_default_selection_is_unchanged(self):
        self.assertEqual(cli.model_configuration(), {"api_key": GENERIC_KEY, "base_url": "https://generic.invalid/v1",
                                                     "model_name": "qwen-plus"})
        self.assertEqual(cli.model_configuration(provider="openai-compatible"), cli.model_configuration())
        self.assertEqual(cli.model_configuration(provider="deepseek"),
                         {"api_key": DEEPSEEK_KEY, "base_url": "https://deepseek.invalid", "model_name": "deepseek-chat"})

    def test_anthropic_reads_only_anthropic_variables(self):
        with self.assertRaises(ValueError) as caught:
            cli.model_configuration(provider="anthropic")
        self.assertIn("ANTHROPIC_API_KEY", str(caught.exception))
        os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_KEY
        self.assertEqual(cli.model_configuration(provider="anthropic"), {
            "provider": "anthropic", "model_name": "claude-opus-5-5", "api_key_env": "ANTHROPIC_API_KEY",
            "base_url": "https://api.anthropic.com"})  # BASEAGENT_LLM_MODEL (qwen-plus) is not sent to Anthropic
        os.environ.update({"ANTHROPIC_BASE_URL": "https://proxy.invalid", "ANTHROPIC_MODEL": "claude-sonnet-5-5"})
        model = cli.initialize_model(provider="anthropic", max_completion_tokens=100)
        self.addCleanup(model.close)
        self.assertIsInstance(model, AnthropicModel)
        self.assertEqual((model.model_name, model.base_url), ("claude-sonnet-5-5", "https://proxy.invalid"))
        self.assertEqual(cli.initialize_model("claude-haiku-5-5", provider="anthropic").model_name, "claude-haiku-5-5")
        os.environ.pop("ANTHROPIC_API_KEY")
        with self.assertRaises(ValueError):
            cli.initialize_model(provider="anthropic")  # never falls back to another provider's key
        with self.assertRaises(ValueError):
            cli.model_configuration(provider="gemini")


class CliTests(ProviderEnvironment):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "work"
        self.root.mkdir()
        (self.root / "a.txt").write_text("alpha\n", encoding="utf-8")
        self.db = Path(self.temp.name).resolve() / "state" / "s.sqlite3"
        os.environ.update({"BASEAGENT_LLM_API_KEY": GENERIC_KEY, "BASEAGENT_LLM_BASE_URL": "https://generic.invalid/v1",
                           "BASEAGENT_LLM_MODEL": "qwen-plus", "DEEPSEEK_API_KEY": DEEPSEEK_KEY,
                           "DEEPSEEK_BASE_URL": "https://deepseek.invalid"})

    def run_cli(self, argv, http=None):
        out, err = io.StringIO(), io.StringIO()
        opener = Mock(return_value=http or FakeHTTP([]))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch("baseagent.llm.adapters.request.build_opener", opener), \
                patch("baseagent.main.Model", side_effect=AssertionError("OpenAI-compatible model must not be built")):
            try:
                code = cli.main(["--root", str(self.root), "--db", str(self.db), *argv])
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def test_parser_rejects_unsupported_combinations(self):
        for argv, text in [(["--thinking", "adaptive", "q"], "--provider anthropic"),
                           (["--effort", "high", "--provider", "deepseek", "q"], "--provider anthropic"),
                           (["--provider", "anthropic", "--stream", "q"], "--stream is not supported"),
                           (["--provider", "anthropic", "--estimate-model", "q"], "no request reservations"),
                           (["--provider", "anthropic", "--preauthorize-model", "q"], "no request reservations"),
                           (["--provider", "anthropic", "--thinking", "512", "q"], "thinking token budget"),
                           (["--provider", "anthropic", "--thinking", "4096", "--max-completion-tokens", "4096", "q"], "below --max-completion-tokens"),
                           (["--provider", "openai", "q"], "invalid choice")]:
            with self.subTest(argv=argv):
                code, _, stderr = self.run_cli(argv)
                self.assertEqual(code, 2)
                self.assertIn(text, stderr)

    def test_missing_key_is_reported_before_any_model_call(self):
        http = FakeHTTP([])
        code, _, stderr = self.run_cli(["--provider", "anthropic", "--session", "k", "hello"], http)
        self.assertEqual(code, 1)
        self.assertIn("ANTHROPIC_API_KEY", stderr)
        self.assertEqual(http.requests, [])
        self.assertIsNone(SessionStore(self.db).load("k"))

    def test_cli_runs_anthropic_with_thinking_and_records_the_provider(self):
        os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_KEY
        read = {"type": "tool_use", "id": "toolu_r", "name": "read_file", "input": {"path": "a.txt"}}
        http = FakeHTTP([reply([THINK_1, read], "tool_use"), reply([THINK_2, {"type": "text", "text": "It says alpha."}])])
        code, stdout, stderr = self.run_cli(["--provider", "anthropic", "--thinking", "adaptive", "--effort", "high",
                                             "--session", "a1", "What is in a.txt?"], http)
        self.assertEqual(code, 0, stderr)
        self.assertIn("It says alpha.", stdout)
        self.assertEqual(len(http.requests), 2)
        for request in http.requests:
            self.assertEqual(request["url"], "https://api.anthropic.com/v1/messages")
            self.assertEqual(request["headers"]["x-api-key"], ANTHROPIC_KEY)
            serialized = json.dumps(request)
            self.assertNotIn(GENERIC_KEY, serialized)
            self.assertNotIn(DEEPSEEK_KEY, serialized)
            body = request["body"]
            self.assertEqual((body["model"], body["max_tokens"]), ("claude-opus-5-5", 16000))
            self.assertEqual((body["thinking"], body["output_config"]), ({"type": "adaptive"}, {"effort": "high"}))
        assistant = [turn for turn in http.requests[1]["body"]["messages"] if turn["role"] == "assistant"]
        self.assertEqual(assistant[-1]["content"], [THINK_1, read])
        state = SessionStore(self.db).load("a1")
        self.assertEqual(state.status, "completed")
        # A finished turn followed by a new one on another provider is a plain new turn, like a model change.
        ask = Path(self.temp.name) / "ask.json"
        ask.write_text(json.dumps({"default": "allow", "tools": {"read_file": "ask"}}), encoding="utf-8")
        http = FakeHTTP([reply([THINK_1, read], "tool_use")])
        code, _, stderr = self.run_cli(["--provider", "anthropic", "--tool-policy", str(ask), "--session", "u", "Read a.txt"], http)
        self.assertEqual(code, 1, stderr)
        self.assertEqual(SessionStore(self.db).load("u").status, "awaiting_approval")
        # Resuming the unfinished turn with another provider is a runtime contract change.
        for argv in (["--provider", "deepseek"], ["--provider", "openai-compatible"], []):
            with self.subTest(argv=argv):
                code, _, stderr = self.run_cli([*argv, "--tool-policy", str(ask), "--session", "u", "--resume"])
                self.assertEqual(code, 1)
                self.assertIn("session runtime configuration changed: runtime", stderr)
        code, _, stderr = self.run_cli(["--provider", "anthropic", "--thinking", "disabled", "--tool-policy", str(ask),
                                        "--session", "u", "--resume"])
        self.assertIn("session runtime configuration changed: runtime", stderr)
        code, _, stderr = self.run_cli(["--provider", "anthropic", "--tool-policy", str(ask), "--session", "u", "--resume"])
        self.assertNotIn("configuration changed", stderr)

    def test_config_file_selects_the_provider(self):
        from baseagent.config import load_harness_config
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps({"version": 1, "run": {"provider": "anthropic", "thinking": 2048, "effort": "low",
                                                          "model": "claude-haiku-5-5"}}), encoding="utf-8")
        self.assertEqual(load_harness_config(path), {"provider": "anthropic", "thinking": 2048, "effort": "low",
                                                     "model": "claude-haiku-5-5"})
        for run in ({"provider": "openai"}, {"thinking": "always"}, {"thinking": 100}, {"effort": "extreme"}):
            with self.subTest(run=run):
                path.write_text(json.dumps({"version": 1, "run": run}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_harness_config(path)
        path.write_text(json.dumps({"version": 1, "run": {"provider": "anthropic", "thinking": 2048, "effort": "low",
                                                          "model": "claude-haiku-5-5"}}), encoding="utf-8")
        os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_KEY
        http = FakeHTTP([reply([{"type": "text", "text": "hi"}])])
        code, _, stderr = self.run_cli(["--config", str(path), "--session", "c", "hello"], http)
        self.assertEqual(code, 0, stderr)
        body = http.requests[0]["body"]
        self.assertEqual((body["model"], body["thinking"], body["output_config"]),
                         ("claude-haiku-5-5", {"type": "enabled", "budget_tokens": 2048}, {"effort": "low"}))



class SmokeScriptTests(ProviderEnvironment):
    def test_smoke_script_makes_no_request_without_key_and_explicit_flag(self):
        smoke = importlib.import_module("scripts.anthropic_smoke")
        for argv, key in (([], ANTHROPIC_KEY), (["--yes-spend-money"], None)):
            with self.subTest(argv=argv, key=bool(key)):
                if key:
                    os.environ["ANTHROPIC_API_KEY"] = key
                else:
                    os.environ.pop("ANTHROPIC_API_KEY", None)
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                        patch("baseagent.llm.adapters.request.build_opener", side_effect=AssertionError("network forbidden")):
                    self.assertEqual(smoke.main(argv), 2)
                self.assertIn("no model calls made", out.getvalue())


if __name__ == "__main__":
    unittest.main()
