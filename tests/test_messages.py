import dataclasses
import json
import pickle
import unittest
from copy import deepcopy
from pathlib import Path

from baseagent.kernel import (Message, TextPart, ToolCall, ToolPairingError, ToolReply,
                              check_tool_pairing, unanswered_tool_calls)
from baseagent.kernel.messages import ABSENT, FrozenDict, RawPart

TESTS = Path(__file__).resolve().parent
FIXTURE_DIRS = [TESTS / "cassettes", TESTS / "fixtures"]


def call(identifier, name="add", arguments='{"a": 1}'):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def assistant(*calls, content=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = list(calls)
    return message


def tool(identifier, content='{"ok": true}'):
    return {"role": "tool", "tool_call_id": identifier, "content": content}


SYSTEM = {"role": "system", "content": "sys"}
USER = {"role": "user", "content": "hi"}

# Every shape produced in src/ plus provider variants seen in the wild.
HAND_WRITTEN = [
    SYSTEM,
    {"role": "developer", "content": "dev"},
    USER,
    {"role": "user", "content": ""},
    {"role": "user", "content": "中文 ✓ \"quoted\" \n tab\t"},
    # kernel/agent.py, session/store.py, task_recovery.py tool replies
    {"role": "tool", "tool_call_id": "call_1", "content": json.dumps({"ok": True, "data": "é"}, ensure_ascii=False)},
    # cassette (sorted keys) tool reply
    {"content": "{}", "role": "tool", "tool_call_id": "call_1"},
    # final answer, content present / None / empty
    {"role": "assistant", "content": "done"},
    {"role": "assistant", "content": None},
    {"role": "assistant", "content": ""},
    # adapters.AnthropicModel._response and streaming.message(): content None + tool calls
    {"role": "assistant", "content": None, "tool_calls": [call("toolu_1"), call("toolu_2", "read", "{}")]},
    # tbench/scripted.py: arguments with unicode and spacing must stay byte-exact
    {"role": "assistant", "content": None, "tool_calls": [call("c1", "write_file", '{ "path":"a.txt" , "text":"ünï" }')]},
    # model_dump(exclude_none=True) of an OpenAI message: content absent
    {"role": "assistant", "tool_calls": [{"id": "x", "function": {"arguments": "{}", "name": "f"}, "type": "function"}]},
    # DeepSeek cassette shape: sorted keys, reasoning_content, tool call index
    {"content": "", "reasoning_content": "thinking…", "role": "assistant",
     "tool_calls": [{"function": {"arguments": "{\"a\": 23, \"b\": 19}", "name": "add"},
                     "id": "call_00", "index": 0, "type": "function"}]},
    {"content": "42", "reasoning_content": "", "role": "assistant"},
    # empty / null tool_calls lists
    {"role": "assistant", "content": "x", "tool_calls": []},
    {"role": "assistant", "content": "x", "tool_calls": None},
    # tool call without "type", provider extras nested in function
    {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "function": {"name": "f", "arguments": "", "strict": True}}]},
    # OpenAI optional name, refusal, audio, nested provider data
    {"role": "user", "name": "alice", "content": "hey"},
    {"role": "assistant", "content": None, "refusal": None, "audio": {"id": "au", "data": [1, 2.5, None, {"k": [True]}]},
     "tool_calls": [call("r1")]},
    # content parts
    {"role": "user", "content": [{"type": "text", "text": "look"},
                                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]},
    {"role": "tool", "tool_call_id": "c", "content": [{"text": "reordered", "type": "text"}]},
]


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def fixture_messages():
    """Yield (location, message) for every message dict in committed fixtures."""
    def walk(value, location):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "messages" and isinstance(item, list):
                    for index, message in enumerate(item):
                        if isinstance(message, dict):
                            yield f"{location}/messages[{index}]", message
                elif key == "message" and isinstance(item, dict) and "role" in item:
                    yield f"{location}/message", item
                yield from walk(item, f"{location}/{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from walk(item, f"{location}[{index}]")

    for directory in FIXTURE_DIRS:
        for path in sorted(directory.rglob("*")):
            if path.suffix not in {".json", ".jsonl"}:
                continue
            text = path.read_text(encoding="utf-8")
            try:
                documents = [json.loads(text)]
            except json.JSONDecodeError:  # JSON Lines (some cassettes use .json)
                documents = [json.loads(line) for line in text.splitlines() if line.strip()]
            for number, document in enumerate(documents):
                yield from walk(document, f"{path.relative_to(TESTS)}#{number}")


class RoundTripTests(unittest.TestCase):
    def assertRoundTrip(self, value):
        original = deepcopy(value)
        restored = Message.from_dict(value).to_dict()
        self.assertEqual(restored, original)
        self.assertEqual(dumps(restored), dumps(original))
        self.assertEqual(json.dumps(restored), json.dumps(original))
        self.assertEqual(value, original, "from_dict must not mutate its input")

    def test_hand_written_shapes(self):
        for value in HAND_WRITTEN:
            with self.subTest(value=value):
                self.assertRoundTrip(value)

    def test_every_committed_fixture_message(self):
        found = list(fixture_messages())
        self.assertGreater(len(found), 1000)
        roles = set()
        for location, value in found:
            with self.subTest(location=location):
                self.assertRoundTrip(value)
                roles.add(value["role"])
        self.assertEqual(roles, {"system", "user", "assistant", "tool"})

    def test_committed_conversations_pair_tool_calls(self):
        for directory in FIXTURE_DIRS:
            for path in sorted(directory.rglob("*.json*")):
                text = path.read_text(encoding="utf-8")
                try:
                    documents = [json.loads(text)]
                except json.JSONDecodeError:
                    documents = [json.loads(line) for line in text.splitlines() if line.strip()]
                for document in documents:
                    request = document.get("request")
                    if isinstance(request, dict) and isinstance(request.get("messages"), list):
                        with self.subTest(path=path.name):
                            # Requests are sent to the model: nothing may remain open.
                            self.assertEqual(check_tool_pairing(request["messages"], allow_pending=False), ())

    def test_absent_none_and_empty_are_distinct(self):
        absent = Message.from_dict({"role": "assistant", "tool_calls": [call("a")]})
        none = Message.from_dict({"role": "assistant", "content": None, "tool_calls": [call("a")]})
        empty = Message.from_dict({"role": "assistant", "content": "", "tool_calls": [call("a")]})
        self.assertIs(absent.content, ABSENT)
        self.assertIsNone(none.content)
        self.assertEqual(empty.content, "")
        self.assertEqual(len({absent, none, empty}), 3)
        self.assertNotIn("content", absent.to_dict())

    def test_constructed_messages_use_canonical_order(self):
        message = Message("tool", "{}", tool_call_id="c")
        self.assertEqual(list(message.to_dict()), ["role", "content", "tool_call_id"])
        message = Message("assistant", None, (ToolCall("c", "f", "{}"),), meta={"reasoning_content": "r"})
        self.assertEqual(message.to_dict(), {"role": "assistant", "content": None, "tool_calls": [call("c", "f", "{}")],
                                             "reasoning_content": "r"})

    def test_semantic_accessors(self):
        message = Message.from_dict(HAND_WRITTEN[13])
        self.assertEqual(message.meta["reasoning_content"], "thinking…")
        [only] = message.calls
        self.assertEqual((only.id, only.name, only.arguments), ("call_00", "add", "{\"a\": 23, \"b\": 19}"))
        self.assertEqual(dict(only.extra), {"index": 0})
        parts = Message.from_dict(HAND_WRITTEN[-2]).content
        self.assertEqual(parts[0], TextPart("look"))
        self.assertIsInstance(parts[1], RawPart)
        self.assertEqual(Message.from_dict(HAND_WRITTEN[-2]).text, "look")
        # A text part whose keys are not in canonical order stays raw to preserve bytes.
        self.assertIsInstance(Message.from_dict(HAND_WRITTEN[-1]).content[0], RawPart)
        self.assertEqual(Message("user", "x").calls, ())

    def test_tool_reply_error_flag_follows_result_envelope(self):
        ok = Message.from_dict(tool("a", json.dumps({"ok": True, "data": 1})))
        failed = Message.from_dict(tool("a", json.dumps({"ok": False, "error": {"code": "x", "message": "m"}})))
        plain = Message.from_dict(tool("a", "not json"))
        self.assertEqual(ok.tool_reply(), ToolReply("a", ok.content, False))
        self.assertTrue(failed.tool_reply().is_error)
        self.assertFalse(plain.tool_reply().is_error)
        with self.assertRaises(ValueError):
            Message("user", "x").tool_reply()

    def test_pickle_and_equality(self):
        for value in HAND_WRITTEN:
            message = Message.from_dict(value)
            self.assertEqual(Message.from_dict(deepcopy(value)), message)
            self.assertEqual(pickle.loads(pickle.dumps(message)), message)
            hash(message)


class ValidationTests(unittest.TestCase):
    def assertInvalid(self, value):
        with self.assertRaises((ValueError, TypeError)):
            Message.from_dict(value)

    def test_rejects_illegal_messages(self):
        cases = [
            {},
            {"content": "no role"},
            {"role": "function", "content": "x"},
            {"role": "tool", "content": "x"},
            {"role": "tool", "content": "x", "tool_call_id": ""},
            {"role": "tool", "content": "x", "tool_call_id": 3},
            {"role": "tool", "tool_call_id": "a"},
            {"role": "tool", "tool_call_id": "a", "content": None},
            {"role": "tool", "tool_call_id": "a", "content": "x", "tool_calls": [call("b")]},
            {"role": "user", "content": "x", "tool_calls": [call("a")]},
            {"role": "system", "content": "x", "tool_calls": []},
            {"role": "user", "content": "x", "tool_call_id": "a"},
            {"role": "assistant", "content": "x", "tool_call_id": "a"},
            {"role": "assistant", "content": 5},
            {"role": "assistant", "content": {"text": "x"}},
            {"role": "assistant", "tool_calls": {"id": "a"}},
            {"role": "assistant", "tool_calls": [call("a"), call("a")]},
            {"role": "assistant", "tool_calls": [call("")]},
            {"role": "assistant", "tool_calls": [call(None)]},
            {"role": "assistant", "tool_calls": [call("a", name="")]},
            {"role": "assistant", "tool_calls": [call("a", name=3)]},
            {"role": "assistant", "tool_calls": [call("a", arguments={"a": 1})]},
            {"role": "assistant", "tool_calls": [{"id": "a", "type": "function"}]},
            {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "f"}}]},
            {"role": "assistant", "tool_calls": ["a"]},
            {"role": "user", "content": [5]},
            {"role": "user", "content": "x", "extra": object()},
            {"role": "user", "content": "x", "extra": {1: "non-string key"}},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertInvalid(value)

    def test_rejects_bad_constructor_arguments(self):
        with self.assertRaises(ValueError):
            Message("user", "x", meta={"role": "assistant"})
        with self.assertRaises(ValueError):
            Message("user", "x", key_order=("role",))
        with self.assertRaises(ValueError):
            ToolCall("a", "f", "{}", key_order=("id", "id", "function"))
        with self.assertRaises(ValueError):
            ToolCall("a", "f", "{}", extra={"id": "dup"})
        with self.assertRaises(ValueError):
            TextPart(None)


class ImmutabilityTests(unittest.TestCase):
    def test_fields_cannot_be_reassigned(self):
        message = Message.from_dict(HAND_WRITTEN[13])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            message.role = "user"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            message.calls[0].arguments = "{}"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            TextPart("a").text = "b"

    def test_containers_are_read_only_and_detached(self):
        source = deepcopy(HAND_WRITTEN[19])
        message = Message.from_dict(source)
        source["audio"]["data"].append("mutated")
        source["tool_calls"][0]["function"]["arguments"] = "changed"
        self.assertEqual(message.to_dict(), HAND_WRITTEN[19])
        self.assertIsInstance(message.tool_calls, tuple)
        self.assertIsInstance(message.meta, FrozenDict)
        with self.assertRaises(TypeError):
            message.meta["audio"] = None
        with self.assertRaises(TypeError):
            message.meta["audio"]["data"][0] = 9
        with self.assertRaises(AttributeError):
            message.meta.extra = 1
        output = message.to_dict()
        output["audio"]["data"].append("x")
        output["tool_calls"].clear()
        self.assertEqual(message.to_dict(), HAND_WRITTEN[19])


class PairingTests(unittest.TestCase):
    def test_complete_conversation(self):
        messages = [SYSTEM, USER, assistant(call("a"), call("b")), tool("b"), tool("a"),
                    assistant(content="done"), USER, assistant(call("a")), tool("a"), assistant(content="ok")]
        self.assertEqual(check_tool_pairing(messages, allow_pending=False), ())
        self.assertEqual(unanswered_tool_calls(messages), ())

    def test_accepts_message_objects(self):
        messages = [Message.from_dict(m) for m in (SYSTEM, USER, assistant(call("a")))]
        self.assertEqual([c.id for c in unanswered_tool_calls(messages)], ["a"])

    def test_trailing_batch_is_pending(self):
        messages = [SYSTEM, USER, assistant(call("a"), call("b"), call("c")), tool("b")]
        self.assertEqual([c.id for c in unanswered_tool_calls(messages)], ["a", "c"])
        with self.assertRaises(ToolPairingError) as raised:
            check_tool_pairing(messages, allow_pending=False)
        self.assertEqual(raised.exception.call_ids, ("a", "c"))
        self.assertIn("unanswered", str(raised.exception))

    def test_orphan_followed_by_user_is_detected(self):
        # The kernel's _pending_calls stops at the user message and ignores this.
        messages = [SYSTEM, USER, assistant(call("a")), USER]
        with self.assertRaises(ToolPairingError) as raised:
            unanswered_tool_calls(messages)
        self.assertEqual((raised.exception.index, raised.exception.call_ids), (3, ("a",)))
        self.assertIn("without tool replies", str(raised.exception))

    def test_orphan_followed_by_other_roles(self):
        for follower in (SYSTEM, assistant(content="x"), assistant(call("z"))):
            with self.subTest(follower=follower), self.assertRaises(ToolPairingError):
                check_tool_pairing([USER, assistant(call("a"), call("b")), tool("a"), follower])

    def test_unknown_and_duplicate_replies(self):
        cases = {
            "unknown": [USER, tool("a")],
            "unknown call id": [USER, assistant(call("a")), tool("b")],
            "previous batch": [USER, assistant(call("a")), tool("a"), assistant(content="x"), tool("a")],
            "duplicate": [USER, assistant(call("a"), call("b")), tool("a"), tool("a")],
        }
        for name, messages in cases.items():
            with self.subTest(name), self.assertRaises(ToolPairingError) as raised:
                check_tool_pairing(messages)
            self.assertEqual(raised.exception.index, len(messages) - 1)

    def test_call_id_reuse(self):
        with self.assertRaises(ToolPairingError):
            check_tool_pairing([USER, assistant(call("a")), tool("a"), assistant(call("a")), tool("a")])
        # A new user turn may reuse ids, matching the context builder.
        check_tool_pairing([USER, assistant(call("a")), tool("a"), USER, assistant(call("a")), tool("a")])

    def test_invalid_message_is_reported(self):
        with self.assertRaises(ValueError):
            check_tool_pairing([USER, {"role": "tool", "content": "x"}])

    def test_pairing_error_is_value_error(self):
        self.assertTrue(issubclass(ToolPairingError, ValueError))


if __name__ == "__main__":
    unittest.main()
