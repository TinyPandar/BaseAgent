"""Offline coverage for streaming model deltas and parallel read-only tools."""

from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from baseagent.agent.agent import run_agent
from baseagent.kernel import AgentEvent, KernelState, RunStatus, ToolRegistry, iter_agent
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.llm.streaming import StreamAssembler, StreamDelta, drain_stream
from baseagent.testing import CassetteModel
from baseagent.tools.registry import ToolRegistry as HarnessToolRegistry


def call(name="add", arguments='{"a": 2, "b": 3}', identifier="one"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def prepared(**kwargs):
    return KernelState(messages=[{"role": "system", "content": "system"},
                                 {"role": "user", "content": "task"}], **kwargs)


class ScriptModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools=None):
        self.seen.append(deepcopy(messages))
        value = next(self.responses)
        if isinstance(value, ModelResponse):
            return value
        return value


class StreamingScriptModel:
    """Yields scripted StreamDelta then returns ModelResponse."""

    def __init__(self, plans):
        # plans: list of (list[StreamDelta]|None, final message/response)
        self.plans = iter(plans)
        self.seen = []
        self.stream_calls = 0
        self.complete_calls = 0

    def complete(self, messages, tools=None):
        self.complete_calls += 1
        self.seen.append(deepcopy(messages))
        deltas, final = next(self.plans)
        if isinstance(final, ModelResponse):
            return final
        return ModelResponse(final, None, 0)

    def complete_stream(self, messages, tools=None):
        self.stream_calls += 1
        self.seen.append(deepcopy(messages))
        deltas, final = next(self.plans)
        for delta in deltas or ():
            yield delta
        if isinstance(final, ModelResponse):
            return final
        return ModelResponse(final, None, 0)


class StreamAssemblerTests(unittest.TestCase):
    def test_assembles_content_and_tool_calls(self):
        assembler = StreamAssembler()
        chunk_content = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="Hi", tool_calls=None))],
            usage=None,
        )
        tool_start = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(index=0, id="c1", function=SimpleNamespace(name="add", arguments='{"a"'))],
            ))],
            usage=None,
        )
        tool_end = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(index=0, id=None, function=SimpleNamespace(name=None, arguments=':1}'))],
            ))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3,
                                  completion_tokens_details=SimpleNamespace(reasoning_tokens=0)),
        )
        self.assertEqual([d.delta for d in assembler.ingest_openai_chunk(chunk_content)], ["Hi"])
        self.assertEqual(assembler.ingest_openai_chunk(tool_start)[0].tool_name, "add")
        assembler.ingest_openai_chunk(tool_end)
        message = assembler.message()
        self.assertEqual(message["content"], "Hi")
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], '{"a":1}')
        self.assertEqual(assembler.response().usage.total_tokens, 3)


class KernelStreamingTests(unittest.TestCase):
    def test_stream_emits_model_delta_and_matches_non_stream(self):
        deltas = [
            StreamDelta(kind="content", index=0, delta="hello "),
            StreamDelta(kind="content", index=1, delta="world"),
        ]
        final = {"role": "assistant", "content": "hello world"}

        streamed = prepared()
        streamed.metadata = {"stream_config": {"enabled": True}}
        stream_model = StreamingScriptModel([(deltas, final)])
        stream_events = list(iter_agent(stream_model, streamed, tools=ToolRegistry()))
        self.assertEqual(streamed.final_answer, "hello world")
        self.assertEqual(stream_model.stream_calls, 1)
        delta_events = [item for item in streamed.events if item["type"] == "model_delta"]
        self.assertEqual([item["data"]["delta"] for item in delta_events], ["hello ", "world"])
        self.assertIn("turn_finished", [item.type for item in stream_events])

        plain = prepared()
        plain_model = StreamingScriptModel([(None, final)])
        list(iter_agent(plain_model, plain, tools=ToolRegistry()))
        self.assertEqual(plain.final_answer, streamed.final_answer)
        self.assertEqual(plain.messages[-1]["content"], streamed.messages[-1]["content"])
        self.assertEqual(plain_model.complete_calls, 1)
        self.assertEqual(plain_model.stream_calls, 0)

    def test_cancel_during_stream_skips_assistant_commit(self):
        from baseagent.kernel import CancellationToken

        token = CancellationToken()

        class CancelAfterFirst:
            def complete_stream(self, messages, tools=None):
                yield StreamDelta(kind="content", index=0, delta="partial")
                token.cancel()
                yield StreamDelta(kind="content", index=1, delta="more")
                return ModelResponse({"role": "assistant", "content": "partialmore"}, None, 0)

        state = prepared()
        state.metadata = {"stream_config": {"enabled": True}}
        events = list(iter_agent(CancelAfterFirst(), state, tools=ToolRegistry(), cancellation=token))
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertTrue(all(message["role"] != "assistant" or message.get("content") != "partialmore"
                            for message in state.messages))
        self.assertEqual(events[-1].type, "run_stopped")


class KernelParallelTests(unittest.TestCase):
    def test_parallel_readonly_runs_bodies_concurrently_and_commits_in_order(self):
        barrier = threading.Barrier(2, timeout=2)
        order = []
        tools = ToolRegistry()

        def read_a():
            order.append(("start", "a", time.time()))
            barrier.wait()
            order.append(("end", "a", time.time()))
            return "A"

        def read_b():
            order.append(("start", "b", time.time()))
            barrier.wait()
            order.append(("end", "b", time.time()))
            return "B"

        tools.register("read_a", "A", {"type": "object"}, read_a, retry_safe=True)
        tools.register("read_b", "B", {"type": "object"}, read_b, retry_safe=True)
        model = ScriptModel([
            {"tool_calls": [call("read_a", "{}", "c1"), call("read_b", "{}", "c2")]},
            {"content": "done"},
        ])
        state = prepared()
        state.metadata = {"parallel_config": {"readonly": True}}
        events = list(iter_agent(model, state, tools=tools))
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.tool_calls, 2)
        # Both started before either ended => overlapped.
        starts = [item for item in order if item[0] == "start"]
        ends = [item for item in order if item[0] == "end"]
        self.assertEqual(len(starts), 2)
        self.assertLess(max(item[2] for item in starts), min(item[2] for item in ends))
        tool_msgs = [message for message in state.messages if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_msgs], ["c1", "c2"])
        self.assertEqual(json.loads(tool_msgs[0]["content"])["data"], "A")
        self.assertEqual(json.loads(tool_msgs[1]["content"])["data"], "B")
        kinds = [item.type for item in events]
        self.assertIn("tool_call_started", kinds)
        self.assertIn("tool_call_finished", kinds)

    def test_mixed_batch_stays_serial_even_when_parallel_enabled(self):
        sequence = []
        tools = ToolRegistry()
        tools.register("read_x", "X", {"type": "object"}, lambda: sequence.append("r") or "x", retry_safe=True)
        tools.register("write_y", "Y", {"type": "object"}, lambda: sequence.append("w") or "y", retry_safe=False)
        model = ScriptModel([
            {"tool_calls": [call("read_x", "{}", "c1"), call("write_y", "{}", "c2")]},
            {"content": "done"},
        ])
        state = prepared()
        state.metadata = {"parallel_config": {"readonly": True}}
        list(iter_agent(model, state, tools=tools))
        self.assertEqual(sequence, ["r", "w"])
        self.assertEqual(state.unsafe_tool_calls, 1)

    def test_resume_skips_ledgered_tool_without_reexecution(self):
        effects = []
        tools = ToolRegistry()
        tools.register("read_z", "Z", {"type": "object"}, lambda: effects.append("ran") or "Z", retry_safe=True)
        state = prepared()
        state.metadata = {"parallel_config": {"readonly": True}}
        state.messages.extend([
            {"role": "assistant", "tool_calls": [call("read_z", "{}", "done"), call("read_z", "{}", "todo")]},
            {"role": "tool", "tool_call_id": "done",
             "content": json.dumps({"ok": True, "data": "cached", "error": None})},
        ])
        list(iter_agent(ScriptModel([{"content": "ok"}]), state, tools=tools))
        self.assertEqual(effects, ["ran"])
        self.assertEqual(state.tool_calls, 1)


class CassetteStreamTests(unittest.TestCase):
    def test_record_replay_chunks_and_nonstream_equivalence(self):
        deltas = [
            StreamDelta(kind="content", index=0, delta="ab"),
            StreamDelta(kind="content", index=1, delta="c"),
        ]
        final = ModelResponse({"role": "assistant", "content": "abc"}, TokenUsage(1, 1, 2), 0)

        class Live:
            model_name = "script"
            generation_params = {}
            max_completion_tokens = 16

            def complete_stream(self, messages, tools=None):
                for delta in deltas:
                    yield delta
                return final

            def complete(self, messages, tools=None):
                return final

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.jsonl"
            with CassetteModel(path, Live(), mode="record") as recorder:
                response = drain_stream(recorder.complete_stream([{"role": "user", "content": "q"}]))
                self.assertEqual(response.message["content"], "abc")
            rows = path.read_text(encoding="utf-8").splitlines()
            body = json.loads(rows[1])
            self.assertEqual(len(body["chunks"]), 2)

            with CassetteModel(path, mode="replay") as replay:
                streamed = []
                generator = replay.complete_stream([{"role": "user", "content": "q"}])
                try:
                    while True:
                        streamed.append(next(generator))
                except StopIteration as finished:
                    replayed = finished.value
                self.assertEqual([item.delta for item in streamed], ["ab", "c"])
                self.assertEqual(replayed.message["content"], "abc")

            # Non-stream complete against a chunked cassette still returns the final.
            with CassetteModel(path, mode="replay") as replay:
                again = replay.complete([{"role": "user", "content": "q"}])
                self.assertEqual(again.message["content"], "abc")


class HarnessFlagWiringTests(unittest.TestCase):
    def test_run_agent_honors_stream_and_parallel_runtime_config(self):
        deltas = [StreamDelta(kind="content", index=0, delta="ok")]
        model = StreamingScriptModel([(deltas, {"role": "assistant", "content": "ok"})])
        tools = HarnessToolRegistry()
        state = run_agent(
            model, "say ok", tools=tools, system_prompt="sys",
            runtime_config={"stream": {"enabled": True}, "parallel": {"readonly": True}},
            max_steps=2, max_tool_calls=4,
        )
        self.assertEqual(state.status, "completed")
        self.assertEqual(state.final_answer, "ok")
        self.assertTrue(state.metadata.get("stream_config", {}).get("enabled"))
        self.assertTrue(state.metadata.get("parallel_config", {}).get("readonly"))
        self.assertTrue(any(event["type"] == "model_delta" for event in state.events))
        self.assertTrue(any(event["type"] == "turn_finished" for event in state.events))


if __name__ == "__main__":
    unittest.main()
