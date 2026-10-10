import json
import io
import logging
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from baseagent.kernel import CancellationToken, Cancelled
from baseagent.kernel.errors import (AuthError, BadRequestError, ContextLengthError, ModelTimeoutError,
                                    RateLimitError, ResponseParseError, TransientError)
from baseagent.llm import AnthropicModel, OpenAICompatModel, ModelResponse, TokenUsage
from baseagent.testing import CassetteModel
import tempfile
from pathlib import Path


TOOLS = [{"type": "function", "function": {"name": "add", "description": "Add integers", "parameters": {
    "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}}}]
MESSAGE = [{"role": "system", "content": "Be helpful"}, {"role": "user", "content": "Add two numbers"}]


class ModelAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                self.server.requests.append((self.path, json.loads(raw)))
                status, payload, delay, headers = self.server.replies.pop(0) if self.server.replies else self.server.reply
                if delay:
                    time.sleep(delay)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        assert not cls.thread.is_alive()

    def setUp(self):
        self.secret = "sk-fake-adapter-credential-0123456789"
        self.env = patch.dict(os.environ, {"BASEAGENT_TEST_API_KEY": self.secret})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server.requests = []
        self.server.replies = []

    def model(self, provider, **kwargs):
        factory = OpenAICompatModel if provider == "openai" else AnthropicModel
        model = factory("fixture-model", api_key_env="BASEAGENT_TEST_API_KEY",
                        base_url=self.base + ("/v1" if provider == "openai" else ""), **kwargs)
        self.addCleanup(model.close)
        return model

    def reply(self, provider, *, calls=False, usage=True):
        if provider == "openai":
            message = {"role": "assistant", "content": None if calls else "42"}
            if calls:
                message["tool_calls"] = [{"id": "one", "type": "function", "function": {"name": "add", "arguments": '{"a":23,"b":19}'}}]
            result = {"id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture-model",
                      "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}]}
            if usage:
                result["usage"] = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
        else:
            result = {"content": [{"type": "tool_use", "id": "one", "name": "add", "input": {"a": 23, "b": 19}}] if calls else [{"type": "text", "text": "42"}]}
            if usage:
                result["usage"] = {"input_tokens": 4, "output_tokens": 2}
        self.server.reply = (200, result, 0, {})

    def test_shared_model_contract_text_tools_usage_and_serialization(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                model = self.model(provider, max_completion_tokens=123, generation={"temperature": 0})
                self.reply(provider)
                result = model.complete(MESSAGE, TOOLS)
                self.assertEqual(result.message["content"], "42")
                self.assertEqual(result.usage, TokenUsage(4, 2, 6))
                self.assertEqual(ModelResponse.from_dict(result.to_dict()), result)
                body = self.server.requests[-1][1]
                self.assertEqual(body["max_tokens"], 123)
                self.assertEqual(body["temperature"], 0)
                self.reply(provider, calls=True)
                result = model.complete(MESSAGE, TOOLS)
                call = result.message["tool_calls"][0]
                self.assertEqual(call["id"], "one")
                self.assertEqual(json.loads(call["function"]["arguments"]), {"a": 23, "b": 19})
                self.reply(provider, usage=False)
                self.assertIsNone(model.complete(MESSAGE).usage)

    def test_shared_http_error_contract_and_secret_free_diagnostics(self):
        cases = [(401, AuthError), (403, AuthError), (429, RateLimitError), (504, ModelTimeoutError), (500, TransientError),
                 (529, TransientError), (400, BadRequestError), (400, ContextLengthError)]
        for provider in ("openai", "anthropic"):
            model = self.model(provider)
            for status, expected in cases:
                with self.subTest(provider=provider, status=status, expected=expected):
                    text = "context_length_exceeded " if expected is ContextLengthError else "bad request "
                    self.server.reply = (status, {"error": {"type": "fixture", "message": text + self.secret}}, 0, {"Retry-After": "2"})
                    before = len(self.server.requests)
                    with self.assertRaises(expected) as caught:
                        model.complete(MESSAGE)
                    self.assertNotIn(self.secret, str(caught.exception))
                    self.assertNotIn(self.secret, repr(model))
                    self.assertEqual(len(self.server.requests), before + 1)  # SDK retries disabled.
                    if status == 429:
                        self.assertEqual(caught.exception.retry_after, 2)

    def test_shared_malformed_json_empty_response_and_timeout(self):
        for provider in ("openai", "anthropic"):
            model = self.model(provider, timeout=0.03)
            for payload in (b"{broken", {"choices": [], "content": []}):
                self.server.reply = (200, payload, 0, {})
                with self.subTest(provider=provider, payload=payload), self.assertRaises(ResponseParseError):
                    model.complete(MESSAGE)
            self.reply(provider)
            status, value, _, headers = self.server.reply
            self.server.reply = (status, value, 0.12, headers)
            with self.assertRaises(ModelTimeoutError):
                model.complete(MESSAGE)

    def test_anthropic_groups_multiple_tool_results_and_counts_cached_input(self):
        model = self.model("anthropic")
        calls = [{"id": name, "type": "function", "function": {"name": "add", "arguments": '{"a":1,"b":2}'}} for name in ("one", "two")]
        messages = MESSAGE + [{"role": "assistant", "content": "Use tools", "tool_calls": calls},
                              {"role": "tool", "tool_call_id": "one", "content": "3"},
                              {"role": "tool", "tool_call_id": "two", "content": "3"}]
        self.reply("anthropic")
        self.server.reply[1]["usage"].update(cache_creation_input_tokens=3, cache_read_input_tokens=2)
        result = model.complete(messages, TOOLS)
        body = self.server.requests[-1][1]
        self.assertEqual(body["system"], [{"type": "text", "text": "Be helpful"}])
        self.assertEqual([b["tool_use_id"] for b in body["messages"][-1]["content"]], ["one", "two"])
        self.assertEqual(result.usage, TokenUsage(9, 2, 11))
        self.assertEqual(body["tools"][0]["input_schema"], TOOLS[0]["function"]["parameters"])

    def test_control_cancellation_and_adapter_cassette_roundtrip(self):
        for provider in ("openai", "anthropic"):
            model = self.model(provider)
            self.reply(provider)
            token = CancellationToken()
            token.cancel()
            with self.assertRaises(Cancelled):
                model.complete_with_control(MESSAGE, cancellation=token)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "record.jsonl"
                with CassetteModel(path, model, mode="record") as recording:
                    original = recording.complete(MESSAGE)
                with patch("socket.socket.connect", side_effect=AssertionError("replay must be offline")):
                    with CassetteModel(path) as replay:
                        self.assertEqual(replay.complete(MESSAGE), ModelResponse.from_dict(original.to_dict()))

    def test_validation_rejects_unsafe_endpoint_params_and_invalid_credentials(self):
        for factory in (OpenAICompatModel, AnthropicModel):
            for url in ("http://example.com", "https://user:password@example.com", "https://example.com/?api_key=x"):
                with self.assertRaises(ValueError):
                    factory("fixture", api_key_env="BASEAGENT_TEST_API_KEY", base_url=url)
            with self.assertRaises(ValueError):
                factory("fixture", api_key_env="BASEAGENT_TEST_API_KEY", generation={"api_key": self.secret})
            with self.assertRaises(AuthError):
                factory("fixture", api_key_env="BASEAGENT_MISSING_TEST_CREDENTIAL")
            with patch.dict(os.environ, {"BASEAGENT_TEST_API_KEY": "synthetic\nmalformed-secret"}):
                with self.assertRaises(AuthError) as caught:
                    factory("fixture", api_key_env="BASEAGENT_TEST_API_KEY")
                self.assertNotIn("malformed-secret", str(caught.exception))

    def test_adapter_logs_and_tracebacks_do_not_include_provider_secret_body(self):
        import traceback
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        for provider in ("openai", "anthropic"):
            model = self.model(provider)
            self.server.reply = (401, {"error": {"message": self.secret}}, 0, {})
            try:
                model.complete(MESSAGE)
            except AuthError:
                self.assertNotIn(self.secret, traceback.format_exc())
        self.assertNotIn(self.secret, output.getvalue())

    def test_success_arriving_after_cancellation_is_accounted_before_agent_stops(self):
        from baseagent.agent import run_agent
        from baseagent.kernel import ToolRegistry
        for provider in ("openai", "anthropic"):
            model, token = self.model(provider), CancellationToken()
            self.reply(provider)
            method = model.complete_with_timeout
            def delivered(*args, **kwargs):
                result = method(*args, **kwargs)
                token.cancel()
                return result
            with patch.object(model, "complete_with_timeout", delivered):
                state = run_agent(model, "synthetic", tools=ToolRegistry(), cancellation=token)
            self.assertEqual((state.status, state.total_tokens, state.unknown_usage_calls), ("cancelled", 6, 0))

    def test_shared_http_server_failure_then_retry_success_is_accounted(self):
        from baseagent.agent import run_agent
        from baseagent.kernel import ToolRegistry
        from baseagent.middleware import RetryMiddleware
        for provider in ("openai", "anthropic"):
            for status in (429, 500):
                with self.subTest(provider=provider, status=status):
                    model = self.model(provider)
                    self.reply(provider)
                    self.server.replies = [(status, {"error": {"message": "synthetic transient"}}, 0, {"Retry-After": "0"}), self.server.reply]
                    before = len(self.server.requests)
                    state = run_agent(model, "synthetic", tools=ToolRegistry(), middleware=[RetryMiddleware(model_retries=1, delay=0)])
                    self.assertEqual((state.status, state.model_calls, state.unknown_usage_calls, state.total_tokens), ("completed", 2, 1, 6))
                    self.assertEqual(len(self.server.requests), before + 2)


if __name__ == "__main__":
    unittest.main()
