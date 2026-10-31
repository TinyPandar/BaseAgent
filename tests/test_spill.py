import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.main import main
from baseagent.session import SessionStore
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.registry import ToolRegistry


class Model:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(copy.deepcopy(messages))
        return next(self.responses)


def call(name, arguments="{}", identifier="a"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def tools_with_large_output():
    tools = ToolRegistry()
    tools.register("large", "Return a large result", {"type": "object"}, lambda: "\n".join(f"line-{i}-" + "x" * 80 for i in range(12)))
    return tools


def serialized(messages):
    return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))


class SpillTests(unittest.TestCase):
    def run_large(self, root, threshold, *, policy=None, responses=None, middleware=()):
        store = SessionStore(root / "sessions.db")
        model = Model(responses or [{"tool_calls": [call("large")]}, {"content": "done"}])
        state = run_agent(model, "task", tools=tools_with_large_output(), store=store, session_id="same-session",
                          runtime_config={"spill": {"threshold_tokens": threshold, "head_lines": 2, "tail_lines": 1}},
                          tool_policy=policy, middleware=middleware)
        return state, model, store

    def test_below_threshold_preserves_model_input_and_default_is_off(self):
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            _, disabled, _ = self.run_large(Path(left), 0)
            _, below, _ = self.run_large(Path(right), 1000)
        self.assertEqual(serialized(disabled.seen), serialized(below.seen))

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
            self.assertEqual(set(event["data"]) & {"chars", "estimated_tokens", "lines", "ref", "call_id", "tool"},
                             {"chars", "estimated_tokens", "lines", "ref", "call_id", "tool"})
            ledger = store.calls("same-session", state.turn_id)
            self.assertEqual([row["status"] for row in ledger], ["completed", "completed"])
            self.assertEqual(json.loads(ledger[1]["terminal_request_json"])["name"], "read_spill")

    def test_reader_returns_slice_and_policy_denial_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, model, store = self.run_large(root, 1)
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
            stub = json.loads([m for m in persisted.messages if m["role"] == "tool"][-1]["content"])["data"]
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
