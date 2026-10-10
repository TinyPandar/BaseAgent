from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.kernel import KernelState, ToolRegistry, iter_agent, CancellationToken
from baseagent.kernel.errors import RateLimitError, ModelError
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.llm.reservation import TokenReservation, request_digest
from baseagent.llm.estimation import TokenEstimate
from baseagent.testing import CassetteModel, CassetteError, CassetteMismatch, Redactor, comparable_events, comparable_state


def tool_call(name="add", identifier="one"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": '{"a":23,"b":19}'}}


class ScriptModel:
    model_name = "synthetic"
    generation_params = {"temperature": 0}
    max_completion_tokens = 16

    def __init__(self, responses):
        self.responses = iter(responses)

    def complete(self, messages, tools=None):
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        return ModelResponse(deepcopy(value), TokenUsage(3, 2, 5))


class CassetteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fixture.jsonl"
        self.messages = [{"role": "user", "content": "synthetic task"}]

    def record(self, values, messages=None):
        with CassetteModel(self.path, ScriptModel(values), mode="record") as model:
            return model.complete(messages or self.messages)

    def test_kernel_roundtrips_direct_tool_tool_failure_and_model_error(self):
        scenarios = [
            ([{"content": "42"}], None),
            ([{"tool_calls": [tool_call()]}, {"content": "42"}], lambda a, b: a + b),
            ([{"tool_calls": [tool_call()]}, {"content": "tool failed, handled"}], lambda a, b: (_ for _ in ()).throw(ValueError("synthetic failure"))),
            ([RateLimitError("synthetic rate limit", retry_after=2)], None),
        ]
        for index, (responses, handler) in enumerate(scenarios):
            path = self.path.with_name(f"scenario-{index}.jsonl")
            registry = ToolRegistry()
            if handler:
                registry.register("add", "Add integers", {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}}, handler)
            state = KernelState(messages=deepcopy(self.messages), turn_start=0)
            with CassetteModel(path, ScriptModel(responses), mode="record") as recording:
                recorded_events = list(iter_agent(recording, state, tools=registry))
            replay_state = KernelState(messages=deepcopy(self.messages), turn_start=0)
            with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
                with CassetteModel(path) as replay:
                    replay_events = list(iter_agent(replay, replay_state, tools=registry))
            self.assertEqual(comparable_state(state), comparable_state(replay_state))
            self.assertEqual(comparable_events(recorded_events), comparable_events(replay_events))

    def test_sync_roundtrip_preserves_usage_unknown_attempts_and_event_sequence(self):
        from baseagent.middleware import RetryMiddleware
        options = dict(tools=ToolRegistry(), middleware=[RetryMiddleware(model_retries=1, delay=0)])
        with patch("baseagent.agent.agent.uuid4") as uuid:
            uuid.return_value.hex = "fixed-invocation"
            with CassetteModel(self.path, ScriptModel([ConnectionError("synthetic transport"), {"content": "done"}]), mode="record") as recording:
                original = run_agent(recording, "synthetic task", **options)
            with CassetteModel(self.path) as replay:
                restored = run_agent(replay, "synthetic task", **options)
        self.assertEqual(original.model_calls, 2)
        self.assertEqual(original.unknown_usage_calls, 1)
        self.assertEqual(original.total_tokens, 5)
        self.assertEqual(comparable_state(original), comparable_state(restored))

    def test_sync_and_iterator_accept_same_recorded_requests(self):
        with CassetteModel(self.path, ScriptModel([{"content": "done"}]), mode="record") as recording:
            original = run_agent(recording, "synthetic task", tools=ToolRegistry())
        state = KernelState(messages=[{"role": "system", "content": original.messages[0]["content"]},
                                      {"role": "user", "content": "synthetic task"}])
        with CassetteModel(self.path) as replay:
            list(iter_agent(replay, state, tools=ToolRegistry()))
        self.assertEqual((state.status, state.final_answer, state.model_calls), (original.status, original.final_answer, original.model_calls))

    def test_mismatch_is_readable_bounded_and_does_not_consume(self):
        self.record([{"content": "done"}])
        replay = CassetteModel(self.path)
        with self.assertRaisesRegex(CassetteMismatch, r"messages\[0\].content"):
            replay.complete([{"role": "user", "content": "changed"}])
        self.assertEqual(replay.complete(self.messages).message["content"], "done")
        replay.close()
        replay = CassetteModel(self.path, generation={"temperature": 0.5})
        with self.assertRaisesRegex(CassetteMismatch, "generation.temperature"):
            replay.complete(self.messages)

    def test_tool_id_schema_and_generation_parameters_are_not_erased(self):
        self.record([{"content": "done"}], [{"role": "tool", "tool_call_id": "one", "content": "x"}])
        for field in ("tool_call_id", "content"):
            replay = CassetteModel(self.path)
            message = {"role": "tool", "tool_call_id": "one", "content": "x", field: "changed"}
            with self.assertRaisesRegex(CassetteMismatch, field):
                replay.complete([message])
        replay = CassetteModel(self.path)
        with self.assertRaisesRegex(CassetteMismatch, "tools"):
            replay.complete([{"role": "tool", "tool_call_id": "one", "content": "x"}], [{"name": "different"}])

    def test_overrun_and_unconsumed_operations_fail(self):
        self.record([{"content": "done"}])
        replay = CassetteModel(self.path)
        with self.assertRaisesRegex(CassetteMismatch, "unconsumed"):
            replay.assert_all_consumed()
        with self.assertRaises(CassetteMismatch):
            replay.close()
        replay.complete(self.messages)
        with self.assertRaisesRegex(CassetteMismatch, "exceeds"):
            replay.complete(self.messages)
        replay.close()

    def test_missing_corrupt_wrong_version_hash_and_invalid_usage_are_rejected(self):
        with self.assertRaises(CassetteError):
            CassetteModel(self.path)
        self.record([{"content": "done"}])
        saved = self.path.read_text(encoding="utf-8")
        rows = [json.loads(line) for line in saved.splitlines()]
        for data in ("{broken", saved.replace('"version":1', '"version":2'),
                     saved.replace('"total_tokens":5', '"total_tokens":1'),
                     saved.replace('"synthetic task"', '"changed task"'),
                     saved.replace('"version":1', '"version":1,"version":1')):
            self.path.write_text(data, encoding="utf-8")
            with self.assertRaises(CassetteError):
                CassetteModel(self.path)
        self.assertEqual(rows[0]["version"], 1)

    def test_redaction_covers_request_response_error_paths_and_custom_patterns(self):
        secret, email, home = "sk-synthetic-private-0123456789", "fixture@example.test", str(Path.home())
        content = f"{secret} Bearer synthetic.token {email} {home}/private ACCOUNT-1234"
        with patch.dict(os.environ, {"BASEAGENT_FIXTURE_SECRET": secret}):
            redactor = Redactor([r"ACCOUNT-\d+"])
            model = CassetteModel(self.path, ScriptModel([{"content": content}, ValueError(content)]), mode="record", redactor=redactor)
            model.complete([{"role": "user", "content": content}])
            with self.assertRaises(ValueError):
                model.complete(self.messages)
            model.close()
            text = self.path.read_text(encoding="utf-8")
            for raw in (secret, email, "synthetic.token", "ACCOUNT-1234", home.replace("\\", "\\\\")):
                self.assertNotIn(raw, text)
            Redactor([r"ACCOUNT-\d+"]).assert_safe(text)
            self.assertGreater(json.loads(text.splitlines()[0])["redaction_count"], 0)

    def test_reservation_estimate_rebind_digest_and_sync_budgets_without_network(self):
        class Reserved(ScriptModel):
            def reserve_request(self, messages, tools):
                return TokenReservation(20, 16, request_digest(messages, tools))

            def estimate_request(self, messages, tools):
                return TokenEstimate(20, 16, request_digest(messages, tools))

            def complete_reserved(self, messages, tools, *, reservation, cancellation, timeout=None):
                reservation.validate(messages, tools)
                return self.complete(messages, tools)
        for index, mode in enumerate(("preauthorize_model", "estimate_model")):
            path = self.path.with_name(f"budget-{index}.jsonl")
            with CassetteModel(path, Reserved([{"content": "done"}]), mode="record") as recording:
                state = run_agent(recording, "synthetic task", tools=ToolRegistry(), max_total_tokens=30, **{mode: True})
            with CassetteModel(path) as replay:
                restored = run_agent(replay, "synthetic task", tools=ToolRegistry(), max_total_tokens=30, **{mode: True})
            self.assertEqual((state.status, state.total_tokens, state.model_reservations), ("completed", 5, {}))
            self.assertEqual((restored.status, restored.total_tokens, restored.model_reservations), ("completed", 5, {}))
        # Raw sensitive values may differ after redaction; quote binds to input
        # actually supplied to the caller, not the private recorded digest.
        path = self.path.with_name("rebind.jsonl")
        with CassetteModel(path, Reserved([]), mode="record") as recording:
            recording.reserve_request([{"role": "user", "content": "sk-first-synthetic"}], [])
        with CassetteModel(path) as replay:
            messages = [{"role": "user", "content": "sk-second-synthetic"}]
            quote = replay.reserve_request(messages, [])
            quote.validate(messages, [])

    def test_no_overwrite_partial_publication_or_concurrent_use(self):
        self.record([{"content": "done"}])
        with self.assertRaises(CassetteError):
            CassetteModel(self.path, ScriptModel([]), mode="record")
        path = self.path.with_name("aborted.jsonl")
        with self.assertRaises(RuntimeError):
            with CassetteModel(path, ScriptModel([{"content": "done"}]), mode="record") as model:
                model.complete(self.messages)
                raise RuntimeError("abort scenario")
        self.assertFalse(path.exists())
        path = self.path.with_name("raced.jsonl")
        started, release = threading.Event(), threading.Event()
        class Blocking(ScriptModel):
            def complete(self, messages, tools=None):
                started.set()
                if not release.wait(timeout=3):
                    raise RuntimeError("test dispatch release timed out")
                return super().complete(messages, tools)
        model = CassetteModel(path, Blocking([{"content": "done"}]), mode="record")
        worker = threading.Thread(target=lambda: model.complete(self.messages))
        worker.start()
        try:
            self.assertTrue(started.wait(timeout=3))
            with self.assertRaisesRegex(CassetteError, "concurrent"):
                model.complete(self.messages)
            with self.assertRaisesRegex(CassetteError, "active"):
                model.close()
        finally:
            release.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        path.write_text("preserve existing", encoding="utf-8")
        with self.assertRaises(CassetteError):
            model.close()
        self.assertEqual(path.read_text(), "preserve existing")
        self.assertEqual(list(path.parent.glob(".cassette-*")), [])

    def test_generation_change_unsupported_response_and_short_secrets(self):
        source = ScriptModel([{"content": "done"}])
        source.generation_params = {"temperature": 0}
        model = CassetteModel(self.path, source, mode="record")
        source.generation_params["temperature"] = 1
        with self.assertRaisesRegex(CassetteError, "generation changed"):
            model.complete(self.messages)
        self.assertFalse(self.path.exists())
        class Unsupported:
            def complete(self, messages, tools):
                return object()
        path = self.path.with_name("unsupported.jsonl")
        model = CassetteModel(path, Unsupported(), mode="record")
        with self.assertRaises(CassetteError):
            model.complete(self.messages)
        with self.assertRaises(CassetteError):
            model.close()
        self.assertFalse(path.exists())
        redactor = Redactor(secrets=["S"])
        self.assertEqual(redactor.text("S"), "<SECRET>")
        redactor.assert_safe('{"content":"<SECRET>"}')

    def test_unknown_error_type_is_safe_generic_without_imports(self):
        class ArbitraryError(Exception):
            pass
        recording = CassetteModel(self.path, ScriptModel([ArbitraryError("synthetic")]), mode="record")
        with self.assertRaises(ArbitraryError):
            recording.complete(self.messages)
        recording.close()
        with CassetteModel(self.path) as replay:
            with self.assertRaises(ModelError):
                replay.complete(self.messages)


if __name__ == "__main__":
    unittest.main()
