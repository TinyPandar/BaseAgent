from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from baseagent.agent import run_agent
from baseagent.agent.context import input_bytes
from baseagent.agent.state import RunStatus, State
from baseagent.middleware import AgentMiddleware
from baseagent.session import SessionStore
from baseagent.tools.context import ToolContext
from baseagent.tools.memory import project_history
from baseagent.tools.workspace import Workspace, coding_tools


class Model:
    def __init__(self, messages=()):
        self.messages = iter(messages)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(deepcopy(messages))
        return next(self.messages)


def call(name, args, identifier="a"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = coding_tools(Workspace(self.root))
        self.store = SessionStore(self.root / ".baseagent/sessions.sqlite3")

    def run_turn(self, model, prompt=None, **kwargs):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id="demo", **kwargs)

    def state(self):
        return State(messages=[{"role": "system", "content": "system"}, {"role": "user", "content": "use Python"},
                               {"role": "assistant", "content": "agreed"}, {"role": "user", "content": "next task"}], turn_start=3, turn_id="new")

    def execute(self, state, name, args):
        return self.tools.execute(name, args, context=ToolContext(state, lambda *args, **kwargs: None))

    def note(self, revision=0, key="language", content="Use Python"):
        return {"key": key, "content": content, "source_messages": [1], "expected_revision": revision}

    def summary(self, revision=0, content="Prior task used Python", through=3):
        return {"content": content, "through_message": through, "expected_revision": revision}

    def test_note_revision_source_and_delete(self):
        state = self.state()
        self.assertTrue(self.execute(state, "remember", self.note()).ok)
        recalled = self.execute(state, "recall_memory", {"query": "python"}).data
        self.assertEqual(recalled["notes"][0]["source_messages"], [1])
        self.assertEqual(recalled["revision"], 1)
        self.assertEqual(self.execute(state, "remember", self.note()).error.code, "conflict")
        self.assertTrue(self.execute(state, "forget_memory", {"key": "language", "expected_revision": 1}).ok)
        self.assertEqual(state.memory_notes, {})

    def test_notes_persist_across_turns_and_cached_resume(self):
        state = self.run_turn(Model([{"tool_calls": [call("remember", self.note())]}, {"content": "noted"}]), "use Python")
        self.assertEqual(state.memory_revision, 1)
        cached = self.run_turn(Model())
        self.assertEqual(cached.memory_notes, state.memory_notes)
        state = self.run_turn(Model([{"tool_calls": [call("recall_memory", {"query": "python"})]}, {"content": "remembered"}]), "next")
        result = json.loads(state.messages[-2]["content"])
        self.assertEqual(result["data"]["notes"][0]["content"], "Use Python")

    def test_summary_replaces_old_history_in_input_only(self):
        first = self.run_turn(Model([{"content": "old answer"}]), "old task")
        original = deepcopy(first.messages)
        model = Model([{"tool_calls": [call("set_history_summary", self.summary())]}, {"content": "new answer"}])
        state = self.run_turn(model, "new task")
        second_input = model.seen[-1]
        self.assertEqual(state.messages[:3], original)
        self.assertTrue(any(message["role"] == "assistant" and "Historical summary reference" in message.get("content", "") for message in second_input))
        self.assertFalse(any(message.get("content") == "old task" for message in second_input))
        self.assertTrue(any(message.get("content") == "new task" for message in second_input))
        self.assertEqual(self.store.load("demo").history_summary, state.history_summary)

    def test_summary_cannot_cover_active_or_incomplete_exchange(self):
        state = self.state()
        self.assertEqual(self.execute(state, "set_history_summary", self.summary(through=4)).error.code, "invalid_arguments")
        state.messages[2] = {"role": "assistant", "tool_calls": [call("read_file", {"path": "x"})]}
        self.assertEqual(self.execute(state, "set_history_summary", self.summary()).error.code, "invalid_arguments")

    def test_source_mutation_invalidates_summary_projection(self):
        state = self.state()
        self.execute(state, "set_history_summary", self.summary())
        state.messages[1]["content"] = "different goal"
        self.assertEqual(project_history(state), state.messages)

    def test_clear_summary_restores_original_projection(self):
        state = self.state()
        self.execute(state, "set_history_summary", self.summary())
        self.execute(state, "set_history_summary", self.summary(revision=1, content="", through=0))
        self.assertIsNone(state.history_summary)
        self.assertEqual(project_history(state), state.messages)

    def test_original_message_is_readable_in_character_pages(self):
        state = self.state()
        state.messages[1]["content"] = "中文🙂" * 200
        expected = json.dumps(state.messages[1], ensure_ascii=False)
        parts, offset = [], 0
        while True:
            result = self.execute(state, "read_history", {"message_index": 1, "offset": offset, "max_chars": 37}).data
            parts.append(result["content"])
            if result["next_offset"] is None:
                break
            offset = result["next_offset"]
        self.assertEqual("".join(parts), expected)
        self.assertEqual(self.execute(state, "read_history", {"message_index": 99}).error.code, "invalid_arguments")

    def test_memory_limits_use_utf8_bytes_and_atomic_revision(self):
        state = self.state()
        self.assertEqual(self.execute(state, "remember", self.note(content="中" * 2000)).error.code, "limit_exceeded")
        self.assertEqual(state.memory_revision, 0)
        self.assertEqual(self.execute(state, "set_history_summary", self.summary(content="中" * 3000)).error.code, "limit_exceeded")
        self.assertIsNone(state.history_summary)
        self.assertEqual(self.execute(state, "remember", {**self.note(), "source_messages": [99]}).error.code, "invalid_arguments")

    def test_note_search_pagination_and_session_isolation(self):
        state = self.state()
        for revision, key in enumerate(["a", "b", "c"]):
            self.execute(state, "remember", self.note(revision, key))
        first = self.execute(state, "recall_memory", {"limit": 2}).data
        self.assertEqual([note["key"] for note in first["notes"]], ["a", "b"])
        second = self.execute(state, "recall_memory", {"after_key": first["next_after_key"], "limit": 2}).data
        self.assertEqual([note["key"] for note in second["notes"]], ["c"])
        self.assertEqual(self.execute(self.state(), "recall_memory", {}).data["notes"], [])

    def test_context_budget_still_applies_to_summary(self):
        self.run_turn(Model([{"content": "old answer"}]), "old task")
        # First request fits, but the newly stored summary must not bypass input cap.
        state = self.run_turn(Model([{"tool_calls": [call("set_history_summary", self.summary(content="x" * 8000))]}]), "new task", max_context_bytes=16000)
        self.assertEqual(state.status, RunStatus.CONTEXT_LIMIT_EXCEEDED)
        self.assertEqual(state.model_calls, 1)

    def test_memory_terminal_snapshot_survives_outer_wrapper_interrupt(self):
        class Interrupt(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                handler(request)
                raise KeyboardInterrupt()
        state = self.run_turn(Model([{"tool_calls": [call("remember", self.note())]}]), "use Python", middleware=[Interrupt()])
        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        self.assertEqual(self.store.load("demo").memory_revision, 1)
        self.assertEqual(self.run_turn(Model()).status, RunStatus.NEEDS_RECOVERY)

    def test_note_and_summary_text_absent_from_metadata_events(self):
        state = self.run_turn(Model([{"tool_calls": [call("remember", self.note(content="unique-private-note"))]}, {"content": "done"}]), "task")
        self.assertNotIn("unique-private-note", json.dumps(self.store.events("demo")))
        self.assertIn("unique-private-note", json.dumps(state.memory_notes))


if __name__ == "__main__":
    unittest.main()
