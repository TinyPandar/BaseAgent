"""Reusable non-streaming models with environment-only credential entrypoints."""

import json
from http.client import HTTPException
import socket
from urllib import request, error

from baseagent.kernel.errors import BadRequestError, ModelTimeoutError, ResponseParseError, TransientError
from ._common import endpoint, environment_key, generation_params, http_error, positive_seconds
from .model import Model
from .response import ModelResponse, TokenUsage


class OpenAICompatModel(Model):
    def __init__(self, model, *, api_key_env="OPENAI_API_KEY", base_url=None, timeout=30.0,
                 max_completion_tokens=512, generation=None, token_counter=None,
                 request_bound=None, token_estimator=None):
        if base_url is not None:
            base_url = endpoint(base_url)
        super().__init__(model, environment_key(api_key_env), base_url, timeout=timeout,
                         max_completion_tokens=max_completion_tokens, generation=generation,
                         token_counter=token_counter, request_bound=request_bound, token_estimator=token_estimator)

    def complete_with_timeout(self, messages, tools=None, *, timeout=None, max_completion_tokens=None):
        cap = max_completion_tokens if max_completion_tokens is not None else self.max_completion_tokens
        if type(cap) is not int or cap < 1:
            raise ValueError("completion token cap must be a positive integer")
        value = super().complete_with_timeout(messages, tools, timeout=timeout, max_completion_tokens=cap)
        return ModelResponse.from_dict(value.to_dict())


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AnthropicModel:
    def __init__(self, model, *, api_key_env="ANTHROPIC_API_KEY", base_url="https://api.anthropic.com",
                 timeout=30.0, max_completion_tokens=512, generation=None):
        self.model_name = model
        self._key = environment_key(api_key_env)
        self.base_url = endpoint(base_url)
        self.timeout = positive_seconds(timeout)
        if type(max_completion_tokens) is not int or max_completion_tokens < 1:
            raise ValueError("completion token cap must be a positive integer")
        self.max_completion_tokens = max_completion_tokens
        self.generation_params = generation_params(generation, {"temperature", "top_p", "top_k", "stop_sequences"})
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        self._closed = False

    def _payload(self, messages, tools):
        system, conversation = [], []
        try:
            for message in messages:
                role, content = message["role"], message.get("content")
                if content is not None and not isinstance(content, str):
                    raise ValueError()
                if role == "system":
                    system.append({"type": "text", "text": content or ""})
                    continue
                if role == "tool":
                    blocks = [{"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": content or ""}]
                    try:
                        result = json.loads(content or "")
                        if isinstance(result, dict) and result.get("ok") is False and isinstance(result.get("error"), dict):
                            blocks[0]["is_error"] = True
                    except (ValueError, TypeError):
                        pass
                    mapped_role = "user"
                elif role in {"assistant", "user"}:
                    blocks = [{"type": "text", "text": content}] if content else []
                    mapped_role = role
                    for call in message.get("tool_calls") or []:
                        if role != "assistant":
                            raise ValueError()
                        args = json.loads(call["function"]["arguments"])
                        if not isinstance(args, dict):
                            raise ValueError()
                        blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": args})
                else:
                    raise ValueError()
                if not blocks:
                    raise ValueError()
                if conversation and conversation[-1]["role"] == mapped_role:
                    conversation[-1]["content"].extend(blocks)
                else:
                    conversation.append({"role": mapped_role, "content": blocks})
            payload = {"model": self.model_name, "messages": conversation, "max_tokens": self.max_completion_tokens,
                       **self.generation_params}
            if system:
                payload["system"] = system
            if tools:
                payload["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                                     "input_schema": t["function"]["parameters"]} for t in tools]
            json.dumps(payload, allow_nan=False)
            return payload
        except (KeyError, TypeError, ValueError, RecursionError):
            raise BadRequestError("unsupported text/tool message or tool schema") from None

    @staticmethod
    def _response(value):
        try:
            texts, calls = [], []
            for block in value["content"]:
                if block["type"] == "text" and isinstance(block["text"], str):
                    texts.append(block["text"])
                elif block["type"] == "tool_use" and isinstance(block["input"], dict) and isinstance(block["id"], str) and isinstance(block["name"], str):
                    calls.append({"id": block["id"], "type": "function", "function": {
                        "name": block["name"], "arguments": json.dumps(block["input"], ensure_ascii=False, allow_nan=False)}})
                else:
                    raise ValueError()
            if not texts and not calls:
                raise ValueError()
            message = {"role": "assistant", "content": "\n".join(texts) if texts else None}
            if calls:
                message["tool_calls"] = calls
            usage = None
            raw = value.get("usage")
            if isinstance(raw, dict):
                # Cached input is billed/reported separately by Anthropic.
                try:
                    counts = [raw["input_tokens"], raw["output_tokens"], raw.get("cache_creation_input_tokens", 0), raw.get("cache_read_input_tokens", 0)]
                    if any(type(n) is not int or n < 0 for n in counts):
                        raise ValueError()
                    prompt = counts[0] + counts[2] + counts[3]
                    usage = TokenUsage(prompt, counts[1], prompt + counts[1])
                except (KeyError, TypeError, ValueError):
                    pass
            return ModelResponse(message, usage)
        except (KeyError, TypeError, ValueError, RecursionError):
            raise ResponseParseError("invalid Anthropic text/tool response") from None

    def complete(self, messages, tools=None):
        return self.complete_with_timeout(messages, tools)

    def complete_with_timeout(self, messages, tools=None, *, timeout=None):
        if self._closed:
            raise BadRequestError("model adapter is closed")
        limit = min(self.timeout, positive_seconds(timeout)) if timeout is not None else self.timeout
        payload = self._payload(messages, tools)
        req = request.Request(self.base_url + "/v1/messages", data=json.dumps(payload, allow_nan=False).encode("utf-8"),
                              headers={"content-type": "application/json", "x-api-key": self._key, "anthropic-version": "2023-06-01"}, method="POST")
        try:
            with self._opener.open(req, timeout=limit) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ResponseParseError("model response exceeds 2 MB")
            try:
                value = json.loads(data)
                json.dumps(value, allow_nan=False)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise ResponseParseError("model response is not valid JSON") from None
            return self._response(value)
        except error.HTTPError as exc:
            with exc:
                try:
                    body = json.loads(exc.read(65536))
                except (ValueError, UnicodeError):
                    body = None
                failure = http_error(exc.code, body, exc.headers)
            raise failure from None
        except (socket.timeout, TimeoutError):
            raise ModelTimeoutError("model transport timed out") from None
        except error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise ModelTimeoutError("model transport timed out") from None
            raise TransientError("model transport connection failed") from None
        except (OSError, HTTPException):
            raise TransientError("model transport connection failed") from None

    def complete_with_control(self, messages, tools=None, *, cancellation, timeout=None):
        cancellation.check()
        try:
            # Return successful usage even if cancellation arrived in flight;
            # the execution boundary records it before stopping further work.
            return self.complete_with_timeout(messages, tools, timeout=timeout)
        except Exception:
            cancellation.check()
            raise

    def close(self):
        self._closed = True
        self._key = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __repr__(self):
        return "<AnthropicModel>"
