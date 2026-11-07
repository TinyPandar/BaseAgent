import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus, State
from baseagent.main import main
from baseagent.session import SessionStore
from baseagent.tools.memory import history_digest, project_history
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace, coding_tools


class Model:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(copy.deepcopy(messages))
        response = next(self.responses)
        return response(messages) if callable(response) else response


def call(name, arguments="{}", identifier="a"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def tools_with_large_output():
    tools = ToolRegistry()
    tools.register("large", "Return a large result", {"type": "object"}, lambda: "\n".join(f"line-{i}-" + "x" * 80 for i in range(12)))
    return tools


def tools_with_outputs(**sizes):
    tools = ToolRegistry()
    for name, size in sizes.items():
        tools.register(name, "Return a sized result", {"type": "object"},
                       lambda size=size: "x" * size)
    return tools


def serialized(messages):
    return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))


def spilled_events(store, session_id="same-session"):
    return [event["data"] for event in store.events(session_id) if event["type"] == "tool_result_spilled"]


class SpillTests(unittest.TestCase):
    def run_large(self, root, threshold, *, policy=None, responses=None, middleware=()):
        store = SessionStore(root / "sessions.db")
        model = Model(responses or [{"tool_calls": [call("large")]}, {"content": "done"}])
        state = run_agent(model, "task", tools=tools_with_large_output(), store=store, session_id="same-session",
                          runtime_config={"spill": {"threshold_tokens": threshold, "head_lines": 2, "tail_lines": 1}},
                          tool_policy=policy, middleware=middleware)
        return state, model, store

    def test_default_on_and_explicit_disable(self):
        # Spilling is on by default: a result above the production 50k-char
        # single budget is replaced in the model window.
        tools = tools_with_outputs(large=60_000)
        default_model = Model([{"tool_calls": [call("large")]}, {"content": "done"}])
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")
            state = run_agent(default_model, "task", tools=tools, store=store, session_id="same-session")
            stub = json.loads(next(m for m in default_model.seen[1] if m["role"] == "tool")["content"])["data"]
            self.assertIn("spill_ref", stub)
            self.assertEqual(stub["reason"], "single")
            self.assertGreater(len(stub["preview"]), 500)
            self.assertLessEqual(len(stub["preview"]), 2048)
            raw = next(m for m in state.messages if m["role"] == "tool")
            self.assertEqual(len(json.loads(raw["content"])["data"]), 60_000)
            self.assertEqual([event["reason"] for event in spilled_events(store)], ["single"])

            disabled_store = SessionStore(Path(directory) / "off.db")
            disabled_model = Model([{"tool_calls": [call("large")]}, {"content": "done"}])
            disabled_state = run_agent(
                disabled_model, "task", tools=tools, store=disabled_store, session_id="same-session",
                max_tool_context_bytes=100_000, runtime_config={"spill": {"enabled": False}})
            full = json.loads(next(m for m in disabled_model.seen[1] if m["role"] == "tool")["content"])["data"]
            self.assertEqual(len(full), 60_000)
            self.assertNotIn("spill_replacements", disabled_state.metadata)
            self.assertEqual(spilled_events(disabled_store), [])

    def test_spill_preview_storage_metadata_event_and_reader_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, model, store = self.run_large(root, 1, responses=[
                {"tool_calls": [call("large")]},
                {"tool_calls": [call("read_spill", '{"ref":"PLACEHOLDER","offset":0,"limit":2}', "read")]},
                {"content": "done"},
            ])
            # Extract the deterministic ref emitted in the first projected tool message,
            # then check the same session's persisted transcript and reader effect.
            tool_message = next(m for m in model.seen[1] if m["role"] == "tool")
            spilled = json.loads(tool_message["content"])
            self.assertIn("spill_ref", spilled["data"])
            path_content = "\n".join(f"line-{i}-" + "x" * 80 for i in range(12))
            self.assertEqual(spilled["data"]["chars"], len(path_content))
            self.assertEqual(spilled["data"]["head"], [f"line-{i}-" + "x" * 80 for i in range(2)])
            self.assertEqual(spilled["data"]["tail"], ["line-11-" + "x" * 80])
            ref = spilled["data"]["spill_ref"]
            # The scripted placeholder is intentionally invalid, so this run verifies
            # preview, persistence and metadata without exposing content in the event.
            self.assertEqual(state.status, RunStatus.COMPLETED)
            path = root / "spill" / "same-session" / f"{ref}.txt"
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), path_content)
            event = next(e for e in store.events("same-session") if e["type"] == "tool_result_spilled")
            self.assertNotIn("content", event["data"])
            self.assertNotIn("line-0-", json.dumps(event))
            self.assertEqual(set(event["data"]) & {"chars", "estimated_tokens", "lines", "ref", "call_id", "tool", "reason"},
                             {"chars", "estimated_tokens", "lines", "ref", "call_id", "tool", "reason"})
            self.assertEqual(event["data"]["reason"], "single")
            ledger = store.calls("same-session", state.turn_id)
            self.assertEqual([row["status"] for row in ledger], ["completed", "completed"])
            self.assertEqual(json.loads(ledger[1]["terminal_request_json"])["name"], "read_spill")

    def test_reader_returns_slice_and_policy_denial_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _first, model, store = self.run_large(root, 1)
            stub = json.loads(next(m for m in model.seen[1] if m["role"] == "tool")["content"])["data"]
            args = json.dumps({"ref": stub["spill_ref"], "offset": 0, "limit": 2})
            reader = Model([{"tool_calls": [call("read_spill", args, "reader")]}, {"content": "done"}])
            resumed = run_agent(reader, "read it", tools=tools_with_large_output(), store=SessionStore(store.path),
                                session_id="same-session",
                                runtime_config={"spill": {"threshold_tokens": 1, "head_lines": 2, "tail_lines": 1}})
            read_message = [m for m in reader.seen[1] if m["role"] == "tool"][-1]
            result = json.loads(read_message["content"])["data"]
            self.assertEqual(result["lines"], ["line-0-" + "x" * 80, "line-1-" + "x" * 80])
            ledger = store.calls("same-session", resumed.turn_id)
            self.assertEqual(json.loads(ledger[0]["terminal_request_json"])["name"], "read_spill")
            denied = Model([{"tool_calls": [call("read_spill", args, "deny")]}, {"content": "done"}])
            denied_state = run_agent(denied, "read it", tools=tools_with_large_output(), store=SessionStore(store.path),
                                     session_id="same-session",
                                     runtime_config={"spill": {"threshold_tokens": 1}},
                                     tool_policy=ToolPolicy({"read_spill": "deny"}))
            denial = json.loads([m for m in denied.seen[1] if m["role"] == "tool"][-1]["content"])
            self.assertEqual(denial["error"]["code"], "permission_denied")
            row = SessionStore(store.path).calls("same-session", denied_state.turn_id)[0]
            self.assertEqual(json.loads(row["request_json"])["function"]["name"], "read_spill")
            self.assertEqual(json.loads(row["result_json"])["error"]["code"], "permission_denied")

    def test_new_turn_in_fresh_store_reads_previous_spill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions.db")
            initial = Model([{"tool_calls": [call("large")]}, {"content": "saved"}])
            interrupted = run_agent(initial, "task", tools=tools_with_large_output(), store=store,
                                    session_id="same-session",
                                    runtime_config={"spill": {"threshold_tokens": 1}})
            self.assertEqual(interrupted.status, RunStatus.COMPLETED)
            persisted = store.load("same-session")
            # The raw transcript retains the full result; the projected window
            # carries the recorded stub.
            raw_data = json.loads([m for m in persisted.messages if m["role"] == "tool"][-1]["content"])["data"]
            self.assertEqual(raw_data, "\n".join(f"line-{i}-" + "x" * 80 for i in range(12)))
            stub = json.loads([m for m in project_history(persisted) if m["role"] == "tool"][-1]["content"])["data"]
            self.assertIn("spill_ref", stub)
            fresh_store = SessionStore(store.path)
            read = Model([{"tool_calls": [call("read_spill", json.dumps({"ref": stub["spill_ref"], "offset": 0, "limit": 1}), "resume-read")]}, {"content": "done"}])
            finished = run_agent(read, "read the saved result", tools=tools_with_large_output(), store=fresh_store, session_id="same-session",
                                 runtime_config={"spill": {"threshold_tokens": 1}})
            self.assertEqual(finished.status, RunStatus.COMPLETED)
            self.assertIn("line-0-", serialized(read.seen))

    def test_replay_model_inputs_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            _, a, _ = self.run_large(Path(left), 1)
            _, b, _ = self.run_large(Path(right), 1)
        self.assertEqual([serialized(x) for x in a.seen], [serialized(x) for x in b.seen])

    def test_structured_spill_is_indented_and_long_lines_are_wrapped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = ToolRegistry()
            tools.register("structured", "Return structured data", {"type": "object"},
                           lambda: {"text": "x" * 2101, "items": [1, 2]})
            model = Model([{"tool_calls": [call("structured")]}, {"content": "done"}])
            run_agent(model, "task", tools=tools, store=SessionStore(root / "sessions.db"), session_id="structured",
                      runtime_config={"spill": {"threshold_tokens": 1}})
            stub = json.loads(next(m for m in model.seen[1] if m["role"] == "tool")["content"])["data"]
            stored = (root / "spill" / "structured" / f"{stub['spill_ref']}.txt").read_text(encoding="utf-8")
            self.assertIn('\n "items": [\n  1,\n  2\n ]', stored)
            self.assertTrue(all(len(line) <= 1000 for line in stored.splitlines()))
            self.assertGreater(stub["lines"], 5)

    def test_single_total_and_age_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            # Single-result budget.
            store = SessionStore(root / "single.db")
            model = Model([{"tool_calls": [call("large")]}, {"content": "done"}])
            run_agent(model, "task", tools=tools_with_outputs(large=400), store=store,
                      session_id="s", runtime_config={"spill": {
                          "single_chars": 100, "total_chars": 100_000, "max_age_turns": 100,
                          "preview_chars": 100, "head_lines": 10, "tail_lines": 2}})
            self.assertEqual([event["reason"] for event in spilled_events(store, "s")], ["single"])
            self.assertIn("spill_ref", json.loads(next(
                m for m in model.seen[1] if m["role"] == "tool")["content"])["data"])

            # Aggregate budget: spill the largest remaining result until the rest fit.
            store = SessionStore(root / "total.db")
            model = Model([{"tool_calls": [call("a", "{}", "a1"), call("b", "{}", "b1")]},
                           {"content": "done"}])
            run_agent(model, "task", tools=tools_with_outputs(a=80, b=60), store=store,
                      session_id="s", runtime_config={"spill": {
                          "single_chars": 1000, "total_chars": 100, "max_age_turns": 100}})
            tools_seen = {m["tool_call_id"]: json.loads(m["content"])["data"]
                          for m in model.seen[1] if m["role"] == "tool"}
            self.assertIn("spill_ref", tools_seen["a1"])
            self.assertEqual(tools_seen["b1"], "x" * 60)
            self.assertEqual([event["reason"] for event in spilled_events(store, "s")], ["total"])

            # Age budget: an old result gets a further preview cut.
            store = SessionStore(root / "age.db")
            first = Model([{"tool_calls": [call("large")]}, {"content": "saved"}])
            run_agent(first, "task", tools=tools_with_outputs(large=400), store=store, session_id="s",
                      runtime_config={"spill": {"single_chars": 1000, "total_chars": 100_000,
                                                "max_age_turns": 10}})
            self.assertEqual(spilled_events(store, "s"), [])
            second = Model([{"content": "done"}])
            state = run_agent(second, "next", tools=tools_with_outputs(large=400), store=store,
                              session_id="s", runtime_config={"spill": {
                                  "single_chars": 1000, "total_chars": 100_000,
                                  "max_age_turns": 0, "old_preview_chars": 40,
                                  "preview_chars": 200}})
            events = spilled_events(store, "s")
            self.assertEqual([event["reason"] for event in events], ["age"])
            stub = json.loads(next(m for m in second.seen[0] if m["role"] == "tool")["content"])["data"]
            self.assertTrue(stub["aged"])
            self.assertLessEqual(len(stub["preview"]), 40)
            self.assertEqual(json.loads(next(m for m in state.messages if m["role"] == "tool")["content"])["data"], "x" * 400)

    def test_workspace_read_opens_spilled_file_and_raw_transcript_is_preserved(self):
        content = "\n".join(f"line-{i}-" + "x" * 80 for i in range(12))

        def read_stub(messages):
            data = json.loads(next(m for m in messages if m["role"] == "tool")["content"])["data"]
            return {"tool_calls": [call("read_file", json.dumps({"path": data["path"]}), "read")]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            tools = coding_tools(workspace)
            tools.register("large", "Return a large result", {"type": "object"}, lambda: content)
            model = Model([{"tool_calls": [call("large")]}, read_stub, {"content": "done"}])
            store = SessionStore(root / ".baseagent" / "sessions.db")
            state = run_agent(model, "task", tools=tools, store=store, session_id="read",
                              workspace_root=root, runtime_config={"spill": {
                                  "single_chars": 100, "total_chars": 100_000,
                                  "max_age_turns": 100, "preview_chars": 300,
                                  "head_lines": 2, "tail_lines": 1}})
            self.assertEqual(state.status, RunStatus.COMPLETED)
            stub = json.loads(next(m for m in model.seen[1] if m["role"] == "tool")["content"])["data"]
            self.assertEqual(stub["path"], f".baseagent/spill/read/{stub['spill_ref']}.txt")
            # The ordinary read tool returned the complete file. Its own result
            # may then be spilled, so inspect the authoritative transcript.
            read = next(m for m in state.messages if m["role"] == "tool" and m["tool_call_id"] == "read")
            read_result = json.loads(read["content"])
            self.assertTrue(read_result["ok"], read_result)
            self.assertEqual(read_result["data"]["content"], content)
            # The raw transcript remains authoritative; projection applies the stub.
            raw = next(m for m in state.messages if m["role"] == "tool" and m["tool_call_id"] == "a")
            self.assertEqual(json.loads(raw["content"])["data"], content)
            projected = next(m for m in project_history(state) if m["role"] == "tool")
            self.assertIn("spill_ref", json.loads(projected["content"])["data"])
            self.assertTrue((root / stub["path"]).is_file())

    def test_projection_maps_spill_records_across_compaction(self):
        messages = [{"role": "system", "content": "rules"},
                    {"role": "user", "content": "old" * 1000},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "recent"},
                    {"role": "assistant", "tool_calls": [call("large")]},
                    {"role": "tool", "tool_call_id": "a",
                     "content": json.dumps({"ok": True, "data": "raw" * 1000, "error": None})},
                    {"role": "assistant", "content": "done"},
                    {"role": "user", "content": "current"}]
        record = {"ref": "a" * 64, "path": "spill/s/a.txt", "chars": 3000,
                  "estimated_tokens": 750, "lines": 1, "head": ["preview"],
                  "tail": [], "reason": "single", "call_id": "a", "aged": False}
        summary = {"content": "saved facts", "through_message": 2,
                   "source_digest": history_digest(messages[1:2]), "turn_id": "",
                   "preserve_instructions": True}
        state = State(messages=messages, turn_start=7, compaction_max_chars=1000,
                      compaction_keep_turns=1,
                      metadata={"spill_replacements": {"5": record, "1": {**record, "call_id": "old"}},
                                "spill_config": {"enabled": True}})
        state.history_summary = summary
        projected = project_history(state)
        self.assertEqual(projected[0], messages[0])
        self.assertIn("Historical summary reference", projected[1]["content"])
        # Raw index 5 maps after the summary reference; the summarized-away raw
        # index 1 is ignored instead of corrupting another message.
        self.assertIn("spill_ref", json.loads(projected[5]["content"])["data"])
        self.assertEqual(json.loads(messages[5]["content"])["data"], "raw" * 1000)
        self.assertEqual(projected[-1], messages[-1])

    def test_cli_disable_and_budget_keys_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "sessions.db"
            config = root / "harness.json"
            config.write_text(json.dumps({
                "version": 1, "workspace": {"root": str(root), "db": str(db)},
                "capabilities": {"tool_packages": []},
                "run": {"spill_enabled": False, "spill_total_chars": 123,
                        "spill_max_age_turns": 2, "compaction_window_tokens": 500}}),
                encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["task", "--config", str(config), "--session", "cli"],
                                      model_factory=lambda args: Model([{"content": "done"}])), 0)
                self.assertEqual(main(["task", "--root", str(root), "--db", str(db),
                                       "--session", "off", "--no-spill"],
                                      model_factory=lambda args: Model([{"content": "done"}])), 0)
            state = SessionStore(db).load("cli")
            self.assertFalse(state.metadata["spill_config"]["enabled"])
            self.assertEqual(state.metadata["spill_config"]["total_chars"], 123)
            self.assertEqual(state.metadata["spill_config"]["max_age_turns"], 2)
            self.assertEqual(state.metadata["compaction_config"]["window_tokens"], 500)
            self.assertFalse(SessionStore(db).load("off").metadata["spill_config"]["enabled"])

    def test_cli_threshold_persists_from_flag_and_config_on_resume(self):
        for source in ("flag", "config"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                db = root / "sessions.db"
                config_args = []
                if source == "config":
                    config = root / "harness.json"
                    config.write_text(json.dumps({"version": 1, "workspace": {"root": str(root), "db": str(db)},
                                                  "capabilities": {"tool_packages": []},
                                                  "run": {"spill_threshold_tokens": 17}}), encoding="utf-8")
                    config_args = ["--config", str(config)]
                calls = ["task", "--root", str(root), "--db", str(db), "--session", "cli", *config_args]
                if source == "flag":
                    calls += ["--spill-threshold-tokens", "17"]
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(calls, model_factory=lambda args: Model([{"content": "done"}])), 0)
                    self.assertEqual(main(["--resume", "--session", "cli", "--db", str(db), *config_args],
                                          model_factory=lambda args: Model()), 0)
                state = SessionStore(db).load("cli")
                self.assertEqual(state.metadata["spill_config"]["threshold_tokens"], 17)


if __name__ == "__main__":
    unittest.main()
