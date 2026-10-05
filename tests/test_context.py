from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from baseagent.agent import run_agent
from baseagent.agent.context import ContextLimitExceeded, ContextPolicy, input_bytes
from baseagent.agent.state import RunStatus
from baseagent.middleware import AgentMiddleware
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.result import ErrorCode, ToolResult


def call(identifier="a"):
    return {"id": identifier, "type": "function", "function": {"name": "read", "arguments": "{}"}}


def reply(identifier="a", data="result"):
    return {"role": "tool", "tool_call_id": identifier, "content": json.dumps(ToolResult(data=data).to_dict(), ensure_ascii=False)}


class Model:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(deepcopy(messages))
        return next(self.responses)


class ContextTests(unittest.TestCase):
    def test_old_turn_removed_as_unit_and_original_unchanged(self):
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "old" * 600},
                    {"role": "assistant", "tool_calls": [call()]}, reply(), {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "new"}]
        original = deepcopy(messages)
        view = ContextPolicy(800, 512).build(messages, [])
        self.assertLessEqual(view.input_bytes, 800)
        self.assertEqual(view.removed_messages, 4)
        self.assertEqual([message["role"] for message in view.messages], ["system", "system", "user"])
        self.assertEqual(messages, original)

    def test_active_turn_keeps_prompt_and_latest_complete_batch(self):
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"},
                    {"role": "assistant", "content": "old reasoning" * 300, "tool_calls": [call("old")]}, reply("old"),
                    {"role": "assistant", "tool_calls": [call("b"), call("c")]}, reply("b"), reply("c")]
        view = ContextPolicy(1_400, 512).build(messages, [])
        self.assertEqual(view.removed_messages, 2)
        self.assertEqual([message["tool_call_id"] for message in view.messages if message["role"] == "tool"], ["b", "c"])
        self.assertIn({"role": "user", "content": "task"}, view.messages)
        ContextPolicy(1_400, 512).build(view.messages, [])

    def test_unicode_and_escaped_tool_data_are_bounded_and_marked(self):
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"},
                    {"role": "assistant", "tool_calls": [call()]}, reply(data='中文"\\\n' * 2_000)]
        original = deepcopy(messages)
        view = ContextPolicy(1_400, 512).build(messages, [])
        content = view.messages[-1]["content"]
        self.assertLessEqual(len(content.encode("utf-8")), 512)
        result = json.loads(content)
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["context_truncated"])
        self.assertEqual(view.clipped_tool_results, 1)
        self.assertEqual(messages, original)

    def test_error_contract_preserved_when_clipping_data(self):
        result = ToolResult.failure(ErrorCode.TIMEOUT, "timed out", data={"stdout": "x" * 5_000})
        messages = [{"role": "user", "content": "task"}, {"role": "assistant", "tool_calls": [call()]},
                    {"role": "tool", "tool_call_id": "a", "content": json.dumps(result.to_dict())}]
        view = ContextPolicy(1_400, 512).build(messages, [])
        actual = ToolResult.from_dict(json.loads(view.messages[-1]["content"]))
        self.assertFalse(actual.ok)
        self.assertEqual(actual.error, result.error)
        self.assertTrue(actual.data["context_truncated"])

    def test_latest_batch_previews_shrink_to_fit_global_budget(self):
        messages = [{"role": "user", "content": "task"},
                    {"role": "assistant", "tool_calls": [call("a"), call("b")]},
                    reply("a", "x" * 3_000), reply("b", "y" * 3_000)]
        view = ContextPolicy(1_600, 4_000).build(messages, [])
        self.assertLessEqual(view.input_bytes, 1_600)
        self.assertEqual(view.clipped_tool_results, 2)
        self.assertEqual([message["tool_call_id"] for message in view.messages if message["role"] == "tool"], ["a", "b"])

    def test_large_error_in_removed_old_turn_does_not_block_new_prompt(self):
        failure = ToolResult.failure(ErrorCode.EXECUTION_FAILED, "large error" * 1_000)
        messages = [{"role": "user", "content": "old"}, {"role": "assistant", "tool_calls": [call()]},
                    {"role": "tool", "tool_call_id": "a", "content": json.dumps(failure.to_dict())},
                    {"role": "assistant", "content": "done"}, {"role": "user", "content": "new"}]
        view = ContextPolicy(800, 256).build(messages, [])
        self.assertLessEqual(view.input_bytes, 800)
        self.assertEqual(view.messages[-1]["content"], "new")

    def test_required_prompt_and_tool_schemas_cannot_be_dropped(self):
        with self.assertRaises(ContextLimitExceeded):
            ContextPolicy(500, 512).build([{"role": "system", "content": "rules"}, {"role": "user", "content": "x" * 1_000}], [])
        with self.assertRaises(ContextLimitExceeded):
            ContextPolicy(500, 512).build([{"role": "user", "content": "task"}], [{"schema": "x" * 1_000}])

    def test_orphan_duplicate_or_unfinished_tool_replies_rejected(self):
        malformed = [
            [{"role": "user", "content": "task"}, reply()],
            [{"role": "user", "content": "task"}, {"role": "assistant", "tool_calls": [call()]}, reply(), reply()],
            [{"role": "user", "content": "task"}, {"role": "assistant", "tool_calls": [call()]}],
        ]
        for messages in malformed:
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                ContextPolicy().build(messages, [])

    def test_no_projection_change_when_input_fits(self):
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
        view = ContextPolicy().build(messages, [])
        self.assertEqual(view.messages, messages)
        self.assertEqual(view.input_bytes, input_bytes(messages, []))
        self.assertEqual(view.removed_messages, 0)

    def test_terminal_projection_enforces_limit_after_middleware(self):
        class Expand(AgentMiddleware):
            def wrap_model_call(self, request, handler):
                return handler(replace(request, messages=[{"role": "user", "content": "x" * 2_000}]))

        model = Model()
        state = run_agent(model, "task", tools=ToolRegistry(), middleware=[Expand()], max_context_bytes=500)
        self.assertEqual(state.status, RunStatus.CONTEXT_LIMIT_EXCEEDED)
        self.assertEqual(state.model_calls, 0)
        self.assertEqual(model.seen, [])
        self.assertEqual(state.messages[-1]["content"], "task")

    def test_context_limit_and_full_history_survive_reopen_then_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.db"
            store = SessionStore(path)
            state = run_agent(Model(), "x" * 1_000, tools=ToolRegistry(), store=store, session_id="demo", max_context_bytes=500)
            self.assertEqual(state.status, RunStatus.CONTEXT_LIMIT_EXCEEDED)
            self.assertEqual(state.model_calls, 0)
            store = SessionStore(path)
            self.assertEqual(store.load("demo").max_context_bytes, 500)
            state = run_agent(Model([{"content": "done"}]), tools=ToolRegistry(), store=store, session_id="demo", max_context_bytes=2_000)
            self.assertEqual(state.status, RunStatus.COMPLETED)
            self.assertEqual(state.messages[1]["content"], "x" * 1_000)
            self.assertEqual(state.model_calls, 1)

    def test_model_mutation_does_not_change_durable_transcript(self):
        class MutatingModel:
            def complete(self, messages, tools):
                messages[1]["content"] = "mutated"
                return {"content": "done"}

        state = run_agent(MutatingModel(), "original", tools=ToolRegistry())
        self.assertEqual(state.messages[1]["content"], "original")


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / "session.db")
        self.tools = ToolRegistry()
        self.effects = []
        self.tools.register("read", "read", {"type": "object"}, lambda: self.effects.append("called") or "data")

    def pending(self, runtime_config=None):
        class Interrupt(AgentMiddleware):
            def after_model(self, state, message):
                raise KeyboardInterrupt

        return run_agent(Model([{"tool_calls": [call()]}]), "task", tools=self.tools, store=self.store, session_id="demo", runtime_config=runtime_config, middleware=[Interrupt()])

    def test_tool_drift_refused_before_pending_side_effect_and_state_unchanged(self):
        state = self.pending()
        original = state.to_dict()
        changed = ToolRegistry()
        changed.register("read", "different semantics", {"type": "object"}, lambda: self.effects.append("changed") or "data")
        with self.assertRaisesRegex(ValueError, "configuration changed: tools"):
            run_agent(Model(), tools=changed, store=self.store, session_id="demo")
        self.assertEqual(self.effects, [])
        self.assertEqual(self.store.load("demo").to_dict(), original)
        finished = run_agent(Model([{"content": "done"}]), tools=changed, store=self.store, session_id="demo", accept_config_changes=True)
        self.assertEqual(finished.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, ["changed"])

    def test_runtime_version_drift_refused_and_only_hashes_saved(self):
        state = self.pending({"provider": "v1", "private_setting": "do-not-store-verbatim"})
        self.assertNotIn("do-not-store-verbatim", json.dumps(state.to_dict()))
        with self.assertRaisesRegex(ValueError, "runtime"):
            run_agent(Model(), tools=self.tools, store=self.store, session_id="demo", runtime_config={"provider": "v2"})
        self.assertEqual(self.effects, [])

    def test_legacy_unfinished_session_requires_explicit_acceptance(self):
        state = self.pending()
        del state.metadata["runtime_contract"]
        self.store.save(state)
        with self.assertRaisesRegex(ValueError, "legacy session"):
            run_agent(Model(), tools=self.tools, store=self.store, session_id="demo")
        finished = run_agent(Model([{"content": "done"}]), tools=self.tools, store=self.store, session_id="demo", accept_config_changes=True)
        self.assertEqual(finished.status, RunStatus.COMPLETED)

    def test_new_turn_can_use_new_runtime_after_completed_turn(self):
        run_agent(Model([{"content": "done"}]), "task", tools=self.tools, store=self.store, session_id="demo", runtime_config={"version": 1})
        finished = run_agent(Model([{"content": "again"}]), "next", tools=self.tools, store=self.store, session_id="demo", runtime_config={"version": 2})
        self.assertEqual(finished.status, RunStatus.COMPLETED)
        self.assertEqual(finished.model_calls, 1)


if __name__ == "__main__":
    unittest.main()
