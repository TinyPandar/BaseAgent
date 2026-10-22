"""Offline automatic compaction and harness integration."""

import contextlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from baseagent.agent import run_agent
from baseagent.agent.compaction import (
    ModelSummarizer,
    TruncatingSummarizer,
    compact_history,
)
from baseagent.agent.state import State
from baseagent.config import load_harness_config
from baseagent.kernel.context import ContextLimitExceeded, ContextPolicy, _exchanges
from baseagent.llm.response import ModelResponse
from baseagent.main import main
from baseagent.session import SessionStore
from baseagent.tools.memory import project_history
from baseagent.tools.registry import ToolRegistry


class Model:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(deepcopy(messages))
        return next(self.responses)


def call(identifier="a"):
    return {"id": identifier, "type": "function", "function": {"name": "read", "arguments": "{}"}}


def reply(identifier="a"):
    return {"role": "tool", "tool_call_id": identifier, "content": '{"ok":true,"data":"result","error":null}'}


class RecordingSummary:
    def __init__(self):
        self.seen = []

    def summarize(self, messages):
        self.seen.append(deepcopy(messages))
        return "saved facts"


def history():
    return [{"role": "system", "content": "rules"},
            {"role": "user", "content": "old" * 1000},
            {"role": "assistant", "tool_calls": [call("a"), call("b")]},
            reply("b"), reply("a"), {"role": "assistant", "content": "done"},
            {"role": "user", "content": "recent" * 300}, {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "current"}]


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self.state = State(messages=history(), turn_start=8, compaction_max_chars=1000)
        self.summary = RecordingSummary()
        self.events = []

    def compact(self, summary=None):
        compact_history(self.state, summary or self.summary, lambda kind, **data: self.events.append((kind, data)))

    def test_disabled_and_below_budget_do_not_call_summarizer(self):
        for limit in (None, 100_000):
            self.state.compaction_max_chars = limit
            self.compact()
        self.assertEqual(self.summary.seen, [])
        self.assertEqual(self.events, [])
        self.assertIsNone(self.state.history_summary)

    def test_preserve_system_recent_turns_and_complete_tool_batches(self):
        original = deepcopy(self.state.messages)
        self.compact()
        projected = project_history(self.state)
        self.assertEqual(projected[0], original[0])
        self.assertEqual(projected[2:], original[6:])
        self.assertEqual(self.summary.seen[0], original[1:6])
        _exchanges(self.summary.seen[0])
        _exchanges(projected)
        self.assertEqual(self.state.messages, original)
        self.assertEqual(self.events[0][0], "context_compacted")

    def test_system_and_developer_in_historical_turn_remain_in_projection(self):
        self.state.messages.insert(2, {"role": "system", "content": "extra rules"})
        self.state.messages.insert(3, {"role": "developer", "content": "developer rules"})
        self.state.turn_start += 2
        self.compact()
        self.assertEqual([m["content"] for m in project_history(self.state)[:3]],
                         ["rules", "extra rules", "developer rules"])

    def test_recent_tool_batch_is_retained_whole(self):
        self.state.compaction_keep_turns = 1
        self.state.messages.extend([{"role": "assistant", "tool_calls": [call("x"), call("y")]}, reply("y"), reply("x")])
        self.compact()
        projected = project_history(self.state)
        self.assertEqual(projected[-4:], self.state.messages[-4:])
        _exchanges(projected)

    def test_repeated_compaction_includes_previous_summary(self):
        self.compact()
        self.state.messages.extend([{"role": "assistant", "content": "done"}, {"role": "user", "content": "next"}])
        self.state.turn_start = len(self.state.messages) - 1
        self.compact()
        self.assertEqual(self.summary.seen[1][0], {"role": "assistant", "content": "saved facts"})
        self.assertEqual(self.state.history_summary["through_message"], 8)
        self.assertEqual(len(self.events), 2)

    def test_failure_or_invalid_summary_is_atomic_and_records_metadata_only(self):
        class Broken:
            def summarize(self, messages):
                messages.clear()
                raise RuntimeError("secret text")
        for summary in (Broken(), ModelSummarizer(Model([{"content": ""}]))):
            before = self.state.to_dict()
            self.compact(summary)
            self.assertEqual(self.state.to_dict(), before)
            self.assertEqual(self.events[-1], ("context_compaction_failed", {"error_type": "RuntimeError" if isinstance(summary, Broken) else "ValueError"}))

    def test_no_eligible_turn_and_nonshrinking_summary_leave_history(self):
        self.state.compaction_keep_turns = 3
        self.compact()
        self.assertFalse(self.events)
        self.state.compaction_keep_turns = 2
        self.compact(TruncatingSummarizer(100_000))
        self.assertEqual(self.events[-1][0], "context_compaction_failed")
        self.assertIsNone(self.state.history_summary)

    def test_model_adapter_response_and_no_tools(self):
        model = Model([ModelResponse({"content": "facts"})])
        self.assertEqual(ModelSummarizer(model).summarize(history()), "facts")
        self.assertEqual(model.seen[0][0]["role"], "system")
        self.assertEqual(json.loads(model.seen[0][1]["content"]), history())
        self.assertEqual(TruncatingSummarizer(10).summarize(history()), json.dumps(history())[:10])

    def test_hard_limit_cannot_silently_remove_protected_turns(self):
        with self.assertRaises(ContextLimitExceeded):
            ContextPolicy(500, 512).build(history(), [], preserve_turns=True)

    def test_scripted_model_end_to_end_persistence_repeat_and_disable(self):
        tools = ToolRegistry()
        tools.register("read", "read", {"type": "object"}, lambda: "result")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.db"
            store = SessionStore(path)
            run_agent(Model([{"tool_calls": [call()]}, {"content": "done"}]), "old" * 1000,
                          tools=tools, store=store, session_id="s")
            store = SessionStore(path)
            adapter = Model([ModelResponse({"content": "first summary"}), {"content": "second summary"}])
            for prompt in ("recent" * 300, "next"):
                model = Model([{"content": "done"}])
                state = run_agent(model, prompt, tools=tools, store=store, session_id="s",
                                  compaction_max_chars=1000, compaction_keep_turns=1,
                                  summarizer=ModelSummarizer(adapter))
                self.assertEqual(state.status, "completed")
                self.assertEqual(model.seen[0][0]["role"], "system")
                self.assertEqual(model.seen[0][-1]["content"], prompt)
                _exchanges(model.seen[0])
            self.assertIn("first summary", adapter.seen[1][1]["content"])
            self.assertEqual(store.load("s").history_summary["content"], "second summary")
            events = store.events("s")
            self.assertEqual(sum(e["type"] == "context_compacted" for e in events), 2)
            model = Model([{"content": "done"}])
            state = run_agent(model, "disabled", tools=tools, store=store, session_id="s", compaction_max_chars=0)
            self.assertIsNone(state.compaction_max_chars)


    def test_failed_summarizer_falls_back_in_run_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")
            tools = ToolRegistry()
            run_agent(Model([{"content": "done"}]), "old" * 1000, tools=tools, store=store, session_id="s")
            before = deepcopy(store.load("s").messages)
            model = Model([{"content": "done"}])
            state = run_agent(model, "current", tools=tools, store=store, session_id="s",
                              compaction_max_chars=1000, compaction_keep_turns=1,
                              summarizer=ModelSummarizer(Model()))
            self.assertEqual(state.status, "completed")
            self.assertEqual(model.seen[0], [*before, {"role": "user", "content": "current"}])
            self.assertIsNone(state.history_summary)
            self.assertTrue(any(e["type"] == "context_compaction_failed" for e in store.events("s")))

    def test_config_cli_override_and_default_offline_summarizer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "harness.json"
            config.write_text(json.dumps({"version": 1, "workspace": {"root": ".", "db": "sessions.db"},
                                          "capabilities": {"tool_packages": []},
                                          "run": {"compaction_max_chars": 2000, "compaction_keep_turns": 2}}))
            self.assertEqual(load_harness_config(config)["compaction_keep_turns"], 2)
            for prompt in ("old" * 2000, "current"):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = main([prompt, "--config", str(config), "--session", "s", "--compaction-keep-turns", "1"],
                                model_factory=lambda args: Model([{"content": "done"}]))
                self.assertEqual(code, 0)
            state = SessionStore(root / "sessions.db").load("s")
            self.assertEqual(state.compaction_max_chars, 2000)
            self.assertEqual(state.compaction_keep_turns, 1)
            self.assertIsNotNone(state.history_summary)

    def test_invalid_compaction_configuration(self):
        for options in ({"compaction_max_chars": -1}, {"compaction_max_chars": True},
                        {"compaction_keep_turns": 0}, {"compaction_keep_turns": False}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                run_agent(Model(), "task", tools=ToolRegistry(), **options)


    def test_increasing_retained_turns_restores_raw_history(self):
        self.state.compaction_keep_turns = 1
        self.compact()
        self.assertEqual(self.state.history_summary["through_message"], 8)
        self.state.compaction_keep_turns = 3
        self.assertEqual(project_history(self.state), self.state.messages)
        self.state.compaction_keep_turns = 2
        self.compact()
        self.assertEqual(project_history(self.state)[2:], self.state.messages[6:])
