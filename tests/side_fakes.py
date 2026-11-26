"""Fakes for the side chat tests (docs/side.md). Nothing here contacts a real provider.

* ``Scripted``: a thread-safe fake model. Main-agent requests (run_agent) and side requests (threads named
  ``side-*``) have separate scripts and separate records. ``Streaming`` adds ``open_stream`` so the side
  chat streams; its stream can block on an Event and is closed by the side chat's stop.
* ``FakeProvider``: a loopback HTTP server speaking the OpenAI-compatible or the Anthropic Messages wire
  format, used with the REAL adapters. It ENFORCES the documented history rules: tool call / result
  pairing, DeepSeek thinking mode's ``reasoning_content`` pass-back when a request carries tools, and
  Anthropic's prefix check on thinking blocks (a block is valid only while system, tools and every earlier
  block are unchanged, and the thinking blocks sent form an unbroken run). These are simulations of the
  documented rules, not the providers themselves; a request it accepts proves the rule is followed, not
  that the real API would accept it.
"""

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from baseagent.kernel.errors import TransientError
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.llm.streaming import StreamDelta

WAIT = 20


def call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def is_side_thread():
    return threading.current_thread().name.startswith("side-")


class Reply:
    """One scripted response. ``chunks``: streamed text pieces, an Event in between blocks until set (or the
    stream is closed). ``gate``: an Event a whole (non-streamed) reply waits for. ``usage=None``: no usage."""

    def __init__(self, text="", *, chunks=None, tool_calls=None, usage=(100, 20), gate=None, error=None,
                 extra=None):
        self.text, self.chunks, self.tool_calls = text, chunks, tool_calls
        self.usage, self.gate, self.error, self.extra = usage, gate, error, extra or {}

    def message(self):
        message = {"role": "assistant", "content": self.text if self.text or not self.tool_calls else None}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        message.update(self.extra)
        return message

    def response(self):
        usage = TokenUsage(self.usage[0], self.usage[1], sum(self.usage)) if self.usage else None
        return ModelResponse(self.message(), usage)


def as_reply(value):
    if isinstance(value, Reply):
        return value
    if isinstance(value, str):
        return Reply(value)
    if isinstance(value, dict):
        return Reply(value.get("content") or "", tool_calls=value.get("tool_calls"))
    raise TypeError(value)


class Scripted:
    def __init__(self, main=(), side=()):
        self.main, self.side = list(main), list(side)
        self.lock = threading.Lock()
        self.main_seen, self.side_seen = [], []
        self.main_entered, self.side_entered = {}, {}

    def entered(self, which, number):
        table = self.main_entered if which == "main" else self.side_entered
        with self.lock:
            return table.setdefault(number, threading.Event())

    def _next(self, messages, tools):
        side = is_side_thread()
        with self.lock:
            seen, script = (self.side_seen, self.side) if side else (self.main_seen, self.main)
            seen.append((json.loads(json.dumps(messages)), json.loads(json.dumps(tools or []))))
            number = len(seen)
            reply = script[number - 1] if number <= len(script) else Reply("(no more scripted replies)")
            event = (self.side_entered if side else self.main_entered).setdefault(number, threading.Event())
        event.set()
        if callable(reply) and not isinstance(reply, Reply):
            reply = reply(messages)
        return as_reply(reply)

    def complete_with_control(self, messages, tools, *, cancellation=None, timeout=None):
        reply = self._next(messages, tools)
        if reply.gate is not None:
            assert reply.gate.wait(WAIT), "scripted reply gate was never released"
        if reply.error is not None:
            raise reply.error
        return reply.response()


class FakeStream:
    def __init__(self, reply):
        self.reply = reply
        self.closed = threading.Event()
        self.text = []

    def __iter__(self):
        index = 0
        for piece in self.reply.chunks if self.reply.chunks is not None else [self.reply.text]:
            if isinstance(piece, threading.Event):
                while not piece.wait(0.01):
                    if self.closed.is_set():
                        raise TransientError("model stream ended before completion")
                continue
            if self.closed.is_set():
                raise TransientError("model stream ended before completion")
            if piece:
                self.text.append(piece)
                yield StreamDelta(kind="content", index=index, delta=piece)
                index += 1
        if self.reply.error is not None:
            raise self.reply.error

    def response(self):
        message = self.reply.message()
        if not self.reply.tool_calls:
            message["content"] = "".join(self.text)
        usage = TokenUsage(self.reply.usage[0], self.reply.usage[1], sum(self.reply.usage)) if self.reply.usage else None
        return ModelResponse(message, usage)

    def close(self):
        self.closed.set()


class Streaming(Scripted):
    """Side requests stream (open_stream); main requests stay whole, as with the CLI adapter."""

    def __init__(self, main=(), side=()):
        super().__init__(main, side)
        self.streams = []

    def open_stream(self, messages, tools=None, *, timeout=None, keep_reasoning=False):
        stream = FakeStream(self._next(messages, tools))
        self.streams.append(stream)
        return stream


# ----- loopback providers ----------------------------------------------------------------
def check_openai(body, *, need_reasoning=False):
    pending = set()
    for index, message in enumerate(body["messages"]):
        if message["role"] == "tool":
            if message.get("tool_call_id") not in pending:
                return f"messages[{index}]: tool message without a preceding tool call"
            pending.discard(message["tool_call_id"])
            continue
        if pending:
            return f"messages[{index}]: insufficient tool messages following tool_calls: {sorted(pending)}"
        if message["role"] == "assistant":
            pending = {item["id"] for item in message.get("tool_calls") or []}
            if need_reasoning and body.get("tools") and not message.get("reasoning_content"):
                return f"messages[{index}]: reasoning_content must be passed back when the request has tools"
    if pending:
        return f"conversation ends with unanswered tool calls {sorted(pending)}"
    return None


def _signature(system, tools, before, previous):
    digest = hashlib.sha256(json.dumps([system, tools, before], sort_keys=True).encode()).hexdigest()[:24]
    return base64.b64encode(json.dumps({"h": digest, "p": previous}).encode()).decode()


def check_anthropic(body):
    """(context for new signatures, error). Roles alternate, tool_use answered by tool_result first in the
    next user message, tools defined when tool blocks occur, thinking bound to its prefix and run."""
    system, tools = body.get("system"), body.get("tools")
    flat, previous, first, open_calls = [], None, True, set()
    for index, message in enumerate(body["messages"]):
        if index and message["role"] == body["messages"][index - 1]["role"]:
            return None, f"messages.{index}: roles must alternate"
        kinds = [block["type"] for block in message["content"]]
        if open_calls:
            answered = {block["tool_use_id"] for block in message["content"] if block["type"] == "tool_result"}
            if answered != open_calls:
                return None, f"messages.{index}: tool_use ids without tool_result: {sorted(open_calls - answered)}"
            if "text" in kinds and "tool_result" in kinds[kinds.index("text"):]:
                return None, f"messages.{index}: tool_result blocks must come first"
        if ("tool_use" in kinds or "tool_result" in kinds) and not tools:
            return None, f"messages.{index}: tool blocks require tools to be defined"
        open_calls = {block["id"] for block in message["content"] if block["type"] == "tool_use"}
        for position, block in enumerate(message["content"]):
            if block["type"] == "thinking":
                record = json.loads(base64.b64decode(block["signature"]))
                expected = json.loads(base64.b64decode(_signature(system, tools, flat, None)))["h"]
                if record["h"] != expected:
                    return None, (f"messages.{index}.content.{position}: thinking block is bound to a different "
                                  "conversation")
                if not first and record["p"] != previous:
                    return None, f"messages.{index}.content.{position}: thinking blocks are not an unbroken run"
                previous, first = hashlib.sha256(block["signature"].encode()).hexdigest()[:12], False
            else:
                flat.append([message["role"], block])
    if open_calls:
        return None, "conversation ends with tool_use blocks without tool_result"
    return (system, tools, flat, previous), None


class FakeProvider:
    """``kind``: "openai", "deepseek-thinking" or "anthropic". ``script(n, body) -> Reply`` (n counts every
    accepted request). ``gates[n]``: an Event reply n waits for before it is sent."""

    def __init__(self, kind, script):
        self.kind, self.script = kind, script
        self.bodies, self.accepted, self.errors = [], [], []
        self.gates, self.arrived, self.lock = {}, {}, threading.Lock()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                provider.handle(self, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="fake-provider", daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def arrival(self, number):
        with self.lock:
            return self.arrived.setdefault(number, threading.Event())

    def _send(self, handler, code, value):
        data = json.dumps(value).encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def handle(self, handler, body):
        with self.lock:
            self.bodies.append(body)
        if self.kind == "anthropic":
            context, error = check_anthropic(body)
        else:
            context, error = None, check_openai(body, need_reasoning=self.kind == "deepseek-thinking")
        if error:
            with self.lock:
                self.errors.append(error)
            return self._send(handler, 400, {"error": {"type": "invalid_request_error", "message": error}})
        with self.lock:
            self.accepted.append(body)
            number = len(self.accepted)
            self.arrived.setdefault(number, threading.Event()).set()
        reply = self.script(number, body)
        gate = self.gates.get(number)
        if gate is not None:
            gate.wait(WAIT)
        usage = reply.usage or (0, 0)
        if self.kind == "anthropic":
            system, tools, flat, previous = context
            content = [{"type": "thinking", "thinking": "", "signature": _signature(system, tools, flat, previous)}]
            if reply.text:
                content.append({"type": "text", "text": reply.text})
            content += [{"type": "tool_use", "id": item["id"], "name": item["function"]["name"],
                         "input": json.loads(item["function"]["arguments"])} for item in reply.tool_calls or []]
            return self._send(handler, 200, {"id": f"msg_{number}", "type": "message", "role": "assistant",
                                             "content": content, "stop_reason": "tool_use" if reply.tool_calls else "end_turn",
                                             "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}})
        totals = {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}
        message = {"role": "assistant", "content": reply.text or None}
        if reply.tool_calls:
            message["tool_calls"] = reply.tool_calls
        reasoning = reply.extra.get("reasoning_content") or (f"thinking {number}" if self.kind == "deepseek-thinking" else None)
        if reasoning:
            message["reasoning_content"] = reasoning
        if not body.get("stream"):
            return self._send(handler, 200, {"id": f"c{number}", "object": "chat.completion", "created": 1,
                                             "model": body["model"], "usage": totals,
                                             "choices": [{"index": 0, "finish_reason": "stop", "message": message}]})
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()

        def chunk(delta=None, usage=None):
            value = {"id": f"c{number}", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                     "choices": [] if delta is None else [{"index": 0, "delta": delta}]}
            if usage:
                value["usage"] = usage
            handler.wfile.write(b"data: " + json.dumps(value).encode() + b"\n\n")
            handler.wfile.flush()

        try:
            if reasoning:
                chunk({"reasoning_content": reasoning})
            for piece in reply.chunks if reply.chunks is not None else [reply.text]:
                if isinstance(piece, threading.Event):
                    piece.wait(WAIT)
                elif piece:
                    chunk({"content": piece})
            for index, item in enumerate(reply.tool_calls or []):
                chunk({"tool_calls": [{"index": index, "id": item["id"], "type": "function",
                                       "function": {"name": item["function"]["name"],
                                                    "arguments": item["function"]["arguments"]}}]})
            chunk(usage=totals)
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
        except OSError:
            with self.lock:
                self.errors.append("client disconnected")


class SideRouter:
    """Wrap a main-agent fake (for example test_main_agent.Duplex): side-thread requests use ``side`` replies,
    everything else goes to ``inner`` unchanged. A side request carries the main agent's tool list, so
    without this a Duplex would mistake it for a main-agent call."""

    def __init__(self, inner, side=()):
        self.inner = inner
        self.side_model = Scripted(side=side)
        self.side_seen = self.side_model.side_seen

    def complete_with_control(self, messages, tools, *, cancellation=None, timeout=None):
        if is_side_thread():
            return self.side_model.complete_with_control(messages, tools, cancellation=cancellation, timeout=timeout)
        return self.inner.complete_with_control(messages, tools, cancellation=cancellation, timeout=timeout)


def first_difference(left, right):
    """Index of the first differing element of two sequences (None when one is a prefix of the other)."""
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return None


def common_prefix_bytes(left, right):
    """Bytes two JSON arrays share before they differ (the closing bracket excluded)."""
    a = json.dumps(left, ensure_ascii=False, separators=(",", ":")).encode()[:-1]
    b = json.dumps(right, ensure_ascii=False, separators=(",", ":")).encode()[:-1]
    size = 0
    for x, y in zip(a, b):
        if x != y:
            break
        size += 1
    return size
