"""Side chat requests through the REAL adapters against loopback providers that enforce the documented
history rules (docs/side.md; README BAS-2 AC03, AC04, AC06, AC07, AC08, AC11). No real provider is contacted.

The composition is ``baseagent.main`` itself (single agent, --mode full --auto-approve) with a real workspace:
the main agent reads a file and starts a real local command that blocks until the test releases it, and
the side chat is opened while that command runs. The fake provider (tests/side_fakes.py) rejects a request
that breaks tool pairing, DeepSeek's reasoning_content pass-back or Anthropic's thinking-block prefix check.
It simulates those rules; an accepted request is not proof that the real API accepts it.
"""

import contextlib
import dataclasses
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import config_isolation  # noqa: F401 -- no real .env or trust store

from baseagent.agent.side import build_request, take_snapshot
from baseagent.kernel.errors import ModelError
from baseagent.main import main
from baseagent.session import SessionStore
from side_fakes import WAIT, FakeProvider, Reply, call, common_prefix_bytes, first_difference

PROVIDER_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "BASEAGENT_LLM_API_KEY",
                      "BASEAGENT_LLM_BASE_URL", "BASEAGENT_LLM_MODEL", "BASEAGENT_LLM_PROVIDER",
                      "BASEAGENT_LLM_EXTRA_BODY", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
                      "OPENAI_API_KEY")
KINDS = ("openai", "deepseek-thinking", "anthropic")


def wait_for(predicate, message="condition was never met"):
    deadline = time.monotonic() + WAIT
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(0.01)


def is_side(body):
    return "[侧聊]" in json.dumps(body.get("messages"), ensure_ascii=False)


def flat(body):
    """Comparable units of a wire body: (role, block) for Anthropic, whole messages for OpenAI."""
    units = []
    for message in body["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            units += [[message["role"], block] for block in content]
        else:
            units.append(message)
    return units


class ProviderChat(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / ".state" / "s.sqlite3"
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in PROVIDER_VARIABLES:
            os.environ.pop(name, None)
        self.go = self.root / "go"
        self.pids = self.root / "pids"
        self.addCleanup(lambda: self.go.write_text("x"))
        code = (f"import os,time\nopen(r'{self.pids}','a').write(str(os.getpid())+'\\n')\n"
                f"while not os.path.exists(r'{self.go}'): time.sleep(0.02)\n")
        self.argv = [sys.executable, "-c", code]
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))     # the main agent's trace lines

    def script(self, side=()):
        """Main replies in order; side requests (recognized by their marker) take ``side`` replies."""
        counters = {"main": 0, "side": 0}
        side = list(side)

        def reply(number, body):
            if is_side(body):
                counters["side"] += 1
                index = counters["side"] - 1
                return side[index] if index < len(side) else Reply(f"side answer {counters['side']}")
            counters["main"] += 1
            if counters["main"] == 1:
                return Reply(tool_calls=[call("c1", "read_file", {"path": "calc.py"})])
            if counters["main"] == 2:
                return Reply("running the tests",
                             tool_calls=[call("c2", "read_file", {"path": "calc.py"}),
                                         call("c3", "run_command", {"argv": self.argv})])
            return Reply("done: add should return a + b")
        return reply

    def chat(self, kind, provider, *extra):
        if kind == "anthropic":
            os.environ.update(ANTHROPIC_API_KEY="fake-anthropic-key", ANTHROPIC_BASE_URL=provider.url)
            flags = ["--provider", "anthropic"]
        else:
            os.environ.update(BASEAGENT_LLM_API_KEY="fake-key", BASEAGENT_LLM_BASE_URL=provider.url + "/v1",
                              BASEAGENT_LLM_MODEL="fake-model")
            flags = ["--provider", "openai-compatible"]
        captured = {}

        def capture(session, **options):
            captured["session"] = session
            return 0

        with patch("baseagent.cli_chat.run_chat", capture), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            code = main(["--root", str(self.root), "--db", str(self.db), "--session", "demo", "--no-main-agent",
                         "--mode", "full", "--auto-approve", *flags, *extra])
        self.assertEqual(code, 0)
        session = captured["session"]
        session.open()
        self.addCleanup(self.shutdown, session)
        return session

    def shutdown(self, session):
        self.go.write_text("x")
        wait_for(lambda: not session.busy, "main turn did not finish")
        session.exit(force=True, why="test end")

    def provider(self, kind, side=()):
        provider = FakeProvider(kind, self.script(side))
        self.addCleanup(provider.close)
        return provider

    def side_events(self, session, kind=None):
        return [item for item in session.side_events_after(0) if kind is None or item["kind"] == kind]

    def ask(self, session, text, op_id):
        result = session.side_ask(text, op_id=op_id)
        self.assertIn(result["result"], ("accepted",), result)
        attempt = session.side.attempts[-1]
        attempt.thread.join(WAIT)
        return attempt

    def start_main(self, session):
        self.assertEqual(session.send("fix add in calc.py"), "sent")
        wait_for(lambda: self.pids.exists() and self.pids.read_text().strip(), "command never started")


class LegalHistories(ProviderChat):
    def run_case(self, kind):
        provider = self.provider(kind, side=[Reply("参数是 argv=[python, -c, …]"), Reply("它在等 go 文件"),
                                             Reply("还在跑", tool_calls=[call("s1", "run_command", {"argv": ["rm", "-rf", "."]})]),
                                             Reply("不执行，只解释"), Reply("批次已经闭合")])
        session = self.chat(kind, provider)
        self.start_main(session)
        history = SessionStore(self.db).load("demo").messages
        projection = json.loads(session.request_tap.latest("demo").messages_json)   # main attempt 2, agent layer
        first, second = (self.ask(session, text, f"op{index}") for index, text in
                         enumerate(("这个命令用了什么参数？", "它为什么还没结束？")))
        self.assertEqual((first.status, second.status), ("completed", "completed"), provider.errors)
        main_bodies = [body for body in provider.accepted if not is_side(body)]
        side_bodies = [body for body in provider.accepted if is_side(body)]
        # --- one main attempt, four layers ------------------------------------------------------------
        main_wire, side_wire = main_bodies[1], side_bodies[0]
        if kind == "anthropic":
            self.assertEqual(side_wire["system"], main_wire["system"])
        self.assertEqual(side_wire.get("tools"), main_wire.get("tools"))
        self.assertEqual(flat(side_wire)[:len(flat(main_wire))], flat(main_wire))
        self.assertEqual(flat(side_bodies[1])[:len(flat(side_wire))], flat(side_wire))
        self.assertEqual(first_difference(history, projection), 1)       # guidance system message added
        self.assertEqual(side_wire["messages"][:len(main_wire["messages"]) - 1], main_wire["messages"][:-1])
        report = {"history->projection first diff": first_difference(history, projection),
                  "projection msgs": len(projection), "main wire units": len(flat(main_wire)),
                  "main->side wire first diff": first_difference(flat(main_wire), flat(side_wire)),
                  "shared message bytes": common_prefix_bytes(main_wire["messages"], side_wire["messages"])}
        # --- the model asks for a tool: nothing runs, the next question is still a valid request ----------
        third = self.ask(session, "现在帮我把它停掉", "op2")
        fourth = self.ask(session, "那你能做什么？", "op3")
        self.assertEqual((third.status, fourth.status), ("tool_requested", "completed"), provider.errors)
        self.assertEqual(len(self.pids.read_text().split()), 1)
        self.assertTrue(self.root.joinpath("calc.py").exists())
        # --- a new side chat after the batch closed: the closed batch is sent natively ---------------
        self.go.write_text("x")
        wait_for(lambda: not session.busy, "main turn did not finish")
        session.end_side()
        fifth = self.ask(session, "测试跑完了吗？", "op4")
        self.assertEqual(fifth.status, "completed", provider.errors)
        self.assertEqual(provider.errors, [])
        self.assertEqual(SessionStore(self.db).load("demo").status, "completed")
        opened = self.side_events(session, "side_opened")[0]["data"]
        print(f"\n   {kind}: {report}; notes={len(opened['notes'])}, streams={opened['streams']}")
        return opened

    def test_openai_compatible(self):
        opened = self.run_case("openai")
        self.assertTrue(opened["streams"])
        self.assertTrue(any("未做真实验证" in note or "没有做真实验证" in note for note in opened["notes"]))

    def test_deepseek_thinking_reasoning_is_passed_back(self):
        self.run_case("deepseek-thinking")

    def test_anthropic_signed_thinking_blocks(self):
        opened = self.run_case("anthropic")
        self.assertFalse(opened["streams"])
        self.assertTrue(any("不支持流式" in note for note in opened["notes"]))
        self.assertTrue(any("prompt cache" in note for note in opened["notes"]))

    def test_agent_layer_record_is_not_the_wire_request(self):
        """run_agent records the projection it hands to the adapter; the Anthropic wire body is a different
        shape (system lifted out, tool messages turned into blocks). Prefix reuse is checked on the wire."""
        provider = self.provider("anthropic")
        session = self.chat("anthropic", provider)
        self.start_main(session)
        snapshot = take_snapshot(store=SessionStore(self.db), session_id="demo", tap=None)
        self.assertEqual(snapshot.origin, "cold")     # no tap given: the saved history only
        main_wire = [body for body in provider.accepted if not is_side(body)][1]
        self.assertNotIn("system", {message["role"] for message in main_wire["messages"]})
        self.assertEqual(len(main_wire["system"]), 2)    # the prompt and the guidance message, lifted out


class ColdAndTrimmed(ProviderChat):
    def cold(self, kind):
        provider = self.provider(kind)
        session = self.chat(kind, provider)
        self.start_main(session)
        self.go.write_text("x")
        wait_for(lambda: not session.busy)
        session.exit(force=True, why="restart")
        again = self.chat(kind, provider)     # a new process: no request of this session recorded yet
        answer = self.ask(again, "上次做到哪了？", "cold1")
        follow = self.ask(again, "还有什么没做？", "cold2")
        self.assertEqual((answer.status, follow.status), ("completed", "completed"), provider.errors)
        body = [body for body in provider.accepted if is_side(body)][0]
        self.assertNotIn("tools", body)
        self.assertFalse(any(isinstance(message.get("content"), list) and any(
            block["type"] in ("tool_use", "tool_result", "thinking") for block in message["content"])
            for message in body["messages"]))
        self.assertEqual(self.side_events(again, "side_opened")[0]["data"]["origin"], "cold")
        self.assertEqual(provider.errors, [])

    def trimmed(self, kind):
        provider = self.provider(kind)
        session = self.chat(kind, provider)
        self.start_main(session)
        live = take_snapshot(store=SessionStore(self.db), session_id="demo", tap=session.request_tap)
        full, _ = build_request(dataclasses.replace(live, limit_bytes=10_000_000), [], "总结一下", mode="rendered")
        size = len(json.dumps({"messages": full, "tools": []}, ensure_ascii=False, separators=(",", ":")).encode())
        small = dataclasses.replace(live, limit_bytes=size - 200)
        messages, tools = build_request(small, [], "总结一下", mode="rendered")
        self.assertIn("已省略", messages[-1]["content"])
        self.assertEqual(tools, [])
        with self.assertRaises(Exception):
            build_request(small, [], "总结一下", mode="live")
        _adapter(kind, provider).complete(messages, tools)
        self.assertEqual(provider.errors, [])

    def test_cold_openai(self):
        self.cold("openai")

    def test_cold_deepseek_thinking(self):
        self.cold("deepseek-thinking")

    def test_cold_anthropic(self):
        self.cold("anthropic")

    def test_trimmed_openai(self):
        self.trimmed("openai")

    def test_trimmed_deepseek_thinking(self):
        self.trimmed("deepseek-thinking")

    def test_trimmed_anthropic(self):
        self.trimmed("anthropic")


def _adapter(kind, provider):
    if kind == "anthropic":
        from baseagent.llm.adapters import AnthropicModel
        return AnthropicModel("claude-opus-5-5", base_url=provider.url, timeout=20)
    from baseagent.llm.model import Model
    return Model("fake-model", "fake-key", provider.url + "/v1", timeout=20)


class SimulatorCatchesBrokenHistories(ProviderChat):
    """Negative controls: the fake provider really enforces each rule the side chat relies on."""

    def snapshot(self, kind):
        provider = self.provider(kind)
        session = self.chat(kind, provider)
        self.start_main(session)
        snapshot = take_snapshot(store=SessionStore(self.db), session_id="demo", tap=session.request_tap)
        self.assertEqual(snapshot.origin, "live")
        self.session = session
        return provider, snapshot, _adapter(kind, provider)

    def rejected(self, provider, adapter, messages, tools):
        before = len(provider.errors)
        with self.assertRaises(ModelError):
            adapter.complete(messages, tools)
        self.assertEqual(len(provider.errors), before + 1)
        return provider.errors[-1]

    def test_anthropic_rules(self):
        provider, snapshot, adapter = self.snapshot("anthropic")
        session = self.session
        good, tools = build_request(snapshot, [], "q", mode="live")
        adapter.complete(good, tools)
        state = SessionStore(self.db).load("demo")
        open_batch = state.messages[session.request_tap.latest("demo").covers:]      # assistant + c2 result, c3 open
        cases = {
            "open batch sent as tool history": ([*snapshot.base, *open_batch, {"role": "user", "content": "q"}], tools),
            "side instruction as a system message": ([*good[:-1], {"role": "system", "content": "side"}, good[-1]], tools),
            "tool definitions dropped": (good, []),
            "earlier message edited": ([{**good[0], "content": good[0]["content"] + " "}, *good[1:]], tools),
        }
        errors = {name: self.rejected(provider, adapter, *value) for name, value in cases.items()}
        self.assertIn("tool_result", errors["open batch sent as tool history"])
        for name in ("side instruction as a system message", "earlier message edited"):
            self.assertIn("bound to a different conversation", errors[name], name)
        self.assertRegex(errors["tool definitions dropped"], "bound to a different conversation|require tools")

    def test_deepseek_streamed_answer_without_reasoning_breaks_the_follow_up(self):
        provider, snapshot, adapter = self.snapshot("deepseek-thinking")
        messages, tools = build_request(snapshot, [], "q1", mode="live")
        stream = adapter.open_stream(messages, tools, keep_reasoning=False)
        list(stream)
        answer = stream.response().to_dict()["message"]
        self.assertNotIn("reasoning_content", answer)
        follow, tools = build_request(snapshot, [("q1", answer)], "q2", mode="live")
        self.assertIn("reasoning_content must be passed back", self.rejected(provider, adapter, follow, tools))
        stream = adapter.open_stream(messages, tools, keep_reasoning=True)
        list(stream)
        kept = stream.response().to_dict()["message"]
        adapter.complete(build_request(snapshot, [("q1", kept)], "q2", mode="live")[0], tools)


if __name__ == "__main__":
    unittest.main()
