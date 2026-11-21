"""The tool call pairing invariant wired into the kernel loop and session persistence."""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import config_isolation  # noqa: F401 -- no real .env or trust store, also under unittest

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus as HarnessStatus
from baseagent.kernel import (AgentMiddleware, CancellationToken, Cancelled, KernelState, RunStatus, ToolPairingError,
                              ToolRegistry, check_tool_pairing, iter_agent)
from baseagent.kernel.agent import SUPERSEDED_CALL_MESSAGE, _pending_calls
from baseagent.kernel.context import ContextPolicy
from baseagent.kernel.messages import open_tool_batches
from baseagent.kernel.result import ErrorCode, ToolResult
from baseagent.session import SessionStore
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.registry import ToolRegistry as HarnessRegistry
from baseagent.tools.result import ToolResult as HarnessResult


def call(identifier, name="write"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": "{}"}}


def reply(identifier, data="ok"):
    return {"role": "tool", "tool_call_id": identifier,
            "content": json.dumps(ToolResult(data=data).to_dict(), ensure_ascii=False)}


SYSTEM = {"role": "system", "content": "system"}


class CheckingModel:
    """Records every request and fails the test if one breaks the pairing invariant."""

    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        try:
            check_tool_pairing(messages, allow_pending=False)
        except ToolPairingError as exc:  # pragma: no cover -- reaching here is the bug under test
            raise AssertionError(f"model received a broken conversation: {exc}") from exc
        self.seen.append(deepcopy(messages))
        return next(self.responses)


class KernelWiringTests(unittest.TestCase):
    def setUp(self):
        self.effects = []
        self.tools = ToolRegistry()
        self.tools.register("write", "Side effect", {"type": "object"}, lambda: self.effects.append("write") or "written")

    def run_state(self, state, model, **kwargs):
        return [value.type for value in iter_agent(model, state, tools=self.tools, **kwargs)]

    def assert_superseded(self, message, identifier):
        self.assertEqual((message["role"], message["tool_call_id"]), ("tool", identifier))
        result = ToolResult.from_dict(json.loads(message["content"]))
        self.assertFalse(result.ok)
        self.assertEqual((result.error.code, result.error.message), (ErrorCode.CANCELLED, SUPERSEDED_CALL_MESSAGE))

    def test_orphan_then_user_is_answered_before_the_user_and_never_executed(self):
        messages = [SYSTEM, {"role": "user", "content": "first"},
                    {"role": "assistant", "content": None, "tool_calls": [call("a"), call("b")]}, reply("a"),
                    {"role": "user", "content": "never mind, answer directly"}]
        # The old backwards scan stopped at the user message and saw nothing pending.
        self.assertEqual(_pending_calls(KernelState(messages=deepcopy(messages))), [])
        state = KernelState(messages=deepcopy(messages), turn_start=4)
        model = CheckingModel([{"content": "direct answer"}])
        events = self.run_state(state, model)
        self.assertEqual((state.status, state.final_answer), (RunStatus.COMPLETED, "direct answer"))
        self.assertEqual(self.effects, [])
        self.assertEqual(state.tool_calls, 0)
        self.assertEqual(state.messages[:4], messages[:4])
        self.assert_superseded(state.messages[4], "b")
        self.assertEqual(state.messages[5], messages[4])
        # The synthetic reply belongs to the earlier turn; the current turn still starts at its prompt.
        self.assertEqual(state.turn_start, 5)
        self.assertEqual(len(model.seen), 1)
        self.assertEqual(model.seen[0], state.messages[:6])
        self.assertIn("tool_calls_superseded", events)
        self.assertEqual(next(item for item in state.events if item["type"] == "tool_calls_superseded")["data"],
                         {"call_ids": ["b"]})
        check_tool_pairing(state.messages, allow_pending=False)

    def test_orphan_inside_the_current_turn_keeps_turn_start(self):
        state = KernelState(messages=[SYSTEM, {"role": "user", "content": "task"},
                                      {"role": "assistant", "tool_calls": [call("a")]},
                                      {"role": "system", "content": "notice"},
                                      {"role": "user", "content": "more"}])
        model = CheckingModel([{"content": "done"}])
        self.run_state(state, model)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.turn_start, 1)
        # Inserted after the batch, before the following system notice.
        self.assertEqual([message["role"] for message in state.messages],
                         ["system", "user", "assistant", "tool", "system", "user", "assistant"])
        self.assert_superseded(state.messages[3], "a")
        self.assertEqual(self.effects, [])

    def test_trailing_batch_is_still_executed_on_resume(self):
        class CancelAfterFirst(AgentMiddleware):
            def __init__(self, token):
                self.token = token

            def after_tool(self, request, result):
                self.token.cancel()

        token = CancellationToken()
        state = KernelState(messages=[SYSTEM, {"role": "user", "content": "write twice"}])
        self.run_state(state, CheckingModel([{"tool_calls": [call("a"), call("b")]}]),
                       hooks=[CancelAfterFirst(token)], cancellation=token)
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual([item["id"] for item in _pending_calls(state)], ["b"])
        state.status = RunStatus.RUNNING
        model = CheckingModel([{"content": "done"}])
        events = self.run_state(state, model)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, ["write", "write"])
        self.assertNotIn("tool_calls_superseded", events)
        self.assertEqual([message.get("tool_call_id") for message in state.messages if message["role"] == "tool"],
                         ["a", "b"])

    def test_orphan_with_started_evidence_is_refused_unchanged(self):
        def interrupted():
            raise Cancelled()

        self.tools.register("slow", "Interrupted", {"type": "object"}, interrupted)
        state = KernelState(messages=[SYSTEM, {"role": "user", "content": "task"}])
        self.run_state(state, CheckingModel([{"tool_calls": [call("a", "slow")]}]))
        self.assertEqual(state.status, RunStatus.CANCELLED)
        state.messages.append({"role": "user", "content": "next"})
        state.status = RunStatus.RUNNING
        before = deepcopy(state.messages)
        model = CheckingModel([{"content": "unused"}])
        self.run_state(state, model)
        self.assertEqual(model.seen, [])
        self.assertEqual(state.status, RunStatus.NEEDS_RECOVERY)
        self.assertIn("not sent to the model", state.error)
        self.assertIn("started without a recorded result before a later message: a;", state.error)
        self.assertEqual(state.messages, before)

    def test_unknown_tool_call_id_is_refused_before_any_model_call(self):
        for messages in (
            [SYSTEM, {"role": "user", "content": "task"}, {"role": "assistant", "tool_calls": [call("a")]},
             reply("a"), reply("zzz")],
            [SYSTEM, {"role": "user", "content": "task"}, reply("zzz")],
            [SYSTEM, {"role": "user", "content": "task"}, {"role": "assistant", "tool_calls": [call("a")]},
             reply("a"), reply("a")],
        ):
            with self.subTest(messages=messages):
                state = KernelState(messages=deepcopy(messages))
                model = CheckingModel([{"content": "unused"}])
                self.run_state(state, model)
                self.assertEqual(model.seen, [])
                self.assertEqual((state.status, state.model_calls), (RunStatus.NEEDS_RECOVERY, 0))
                self.assertTrue(state.error.startswith("invalid tool call pairing; the conversation was not sent"))
                self.assertIn("zzz" if messages[-1]["tool_call_id"] == "zzz" else "duplicate", state.error)
                self.assertEqual(state.messages, messages)

    def test_orphan_repair_refuses_a_conversation_that_stays_broken(self):
        messages = [SYSTEM, {"role": "user", "content": "task"}, {"role": "assistant", "tool_calls": [call("a")]},
                    {"role": "user", "content": "next"}, reply("a")]
        state = KernelState(messages=deepcopy(messages))
        model = CheckingModel([{"content": "unused"}])
        self.run_state(state, model)
        self.assertEqual((model.seen, state.status), ([], RunStatus.NEEDS_RECOVERY))
        self.assertEqual(state.messages, messages)

    def test_middleware_cannot_send_a_broken_conversation(self):
        class Corrupt(AgentMiddleware):
            def before_model(self, state):
                state.messages.append({"role": "assistant", "tool_calls": [call("late")]})

        state = KernelState(messages=[SYSTEM, {"role": "user", "content": "task"}])
        model = CheckingModel([{"content": "unused"}])
        self.run_state(state, model, hooks=[Corrupt()])
        self.assertEqual((model.seen, state.status), ([], RunStatus.NEEDS_RECOVERY))
        self.assertIn("unanswered tool call(s): late", state.error)

    def test_context_projection_reuses_the_pairing_rules(self):
        with self.assertRaises(ToolPairingError):
            ContextPolicy().build([SYSTEM, {"role": "user", "content": "task"}, reply("zzz")], [])
        batches = open_tool_batches([SYSTEM, {"role": "user", "content": "x"},
                                     {"role": "assistant", "tool_calls": [call("a"), call("b")]}, reply("b"),
                                     {"role": "user", "content": "y"},
                                     {"role": "assistant", "tool_calls": [call("c")]}])
        self.assertEqual([(item.assistant_index, item.insert_at, item.call_ids, item.superseded) for item in batches],
                         [(2, 4, ("a",), True), (5, 6, ("c",), False)])


class SessionWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.sqlite3")
        self.effects = []
        self.tools = HarnessRegistry()
        self.tools.register("write", "Side effect", {"type": "object"}, lambda: self.effects.append("write") or "written")

    def run_turn(self, model, prompt=None, **kwargs):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id="demo",
                         workspace_root=self.root, **kwargs)

    def rewrite(self, change):
        with sqlite3.connect(self.store.path) as connection:
            row = connection.execute("SELECT state_json FROM sessions WHERE id='demo'").fetchone()
            data = json.loads(row[0])
            change(data)
            connection.execute("UPDATE sessions SET state_json=? WHERE id='demo'", (json.dumps(data),))
        connection.close()

    def test_resume_after_approval_pause_and_new_message_closes_the_call(self):
        policy = ToolPolicy({"write": "ask"})
        paused = self.run_turn(CheckingModel([{"tool_calls": [call("a")]}]), "write", tool_policy=policy)
        self.assertEqual(paused.status, HarnessStatus.AWAITING_APPROVAL)
        self.rewrite(lambda data: data["messages"].append({"role": "user", "content": "skip that, just answer"}))
        model = CheckingModel([{"content": "answered"}])
        resumed = self.run_turn(model, tool_policy=policy)
        self.assertEqual(resumed.status, HarnessStatus.COMPLETED)
        self.assertEqual(self.effects, [])
        self.assertEqual([message["role"] for message in model.seen[0][-3:]], ["assistant", "tool", "user"])
        self.assertEqual(json.loads(model.seen[0][-2]["content"])["error"]["code"], "cancelled")
        stored = self.store.load("demo")
        check_tool_pairing(stored.messages, allow_pending=False)
        self.assertIn("tool_calls_superseded", [item["type"] for item in self.store.events("demo", limit=1000)])
        # The ledger keeps the call as never started; nothing claims it ran.
        self.assertEqual([row["status"] for row in self.store.calls("demo", stored.turn_id)], ["pending"])

    def test_uncertain_call_followed_by_user_still_blocks_without_model(self):
        class Interrupt(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                handler(request)
                raise KeyboardInterrupt

        state = self.run_turn(CheckingModel([{"tool_calls": [call("a")]}]), "write", middleware=[Interrupt()])
        self.assertEqual(self.store.call(state, "a")["status"], "running")
        self.rewrite(lambda data: data["messages"].append({"role": "user", "content": "next"}))
        model = CheckingModel()
        resumed = self.run_turn(model)
        self.assertEqual((resumed.status, model.seen), (HarnessStatus.NEEDS_RECOVERY, []))
        # A verified result cannot be appended after the user message: the transcript would not pair.
        with self.assertRaisesRegex(ValueError, "breaks tool call pairing"):
            self.store.resolve_call("demo", "a", HarnessResult(data="verified"))
        self.assertEqual(self.store.call(state, "a")["status"], "running")
        self.assertEqual(self.effects, ["write"])

    def test_resolved_recovery_result_pairs(self):
        class Interrupt(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                handler(request)
                raise KeyboardInterrupt

        self.run_turn(CheckingModel([{"tool_calls": [call("a")]}]), "write", middleware=[Interrupt()])
        state = self.store.resolve_call("demo", "a", HarnessResult(data="verified"))
        check_tool_pairing(state.messages, allow_pending=False)
        finished = self.run_turn(CheckingModel([{"content": "done"}]))
        self.assertEqual(finished.status, HarnessStatus.COMPLETED)

    def test_corrupted_stored_message_gives_a_clear_error(self):
        for corrupt, detail in (
            (lambda data: data["messages"][2].pop("role"), "requires a role"),
            (lambda data: data["messages"].append({"role": "tool", "content": "x"}), "tool_call_id"),
            (lambda data: data["messages"].append({"role": "robot", "content": "x"}), "unsupported message role"),
        ):
            with self.subTest(detail=detail):
                self.store = SessionStore(self.root / f"{detail[:4]}.sqlite3")
                self.run_turn(CheckingModel([{"content": "answer"}]), "task")
                self.rewrite(corrupt)
                with self.assertRaisesRegex(ValueError, r"session 'demo': stored message \d+ is malformed: .*" + detail):
                    self.store.load("demo")
                model = CheckingModel()
                with self.assertRaisesRegex(ValueError, "is malformed"):
                    self.run_turn(model, "next")
                self.assertEqual(model.seen, [])


if __name__ == "__main__":
    unittest.main()
