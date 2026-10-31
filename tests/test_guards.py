import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from baseagent.agent import run_agent
from baseagent.config import load_harness_config
from baseagent.main import main
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace, coding_tools


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(copy.deepcopy(messages))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def call(name, args="{}", call_id="c"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def result_for(messages, call_id):
    message = next(m for m in messages if m.get("tool_call_id") == call_id)
    return json.loads(message["content"])


class GuardTests(unittest.TestCase):
    def test_off_inputs_identical_and_wrapup_once(self):
        def scripted():
            return [{"tool_calls": [call("ping", "{}", f"p{i}")]} for i in range(2)] + [{"content": "done"}]
        baseline = ToolRegistry()
        guarded = ToolRegistry()
        for tools in (baseline, guarded):
            tools.register("ping", "ping", {"type": "object"}, lambda: "ok")
        a, b = Model(scripted()), Model(scripted())
        run_agent(a, "task", tools=baseline, max_steps=4,
                  runtime_config={"guards": {"wrapup_hint_steps": 0, "repeat_call_guard": False, "dedupe_reads": False}})
        disabled = Model(scripted())
        run_agent(disabled, "task", tools=guarded, max_steps=4)
        self.assertEqual([json.dumps(x, sort_keys=True) for x in a.seen], [json.dumps(x, sort_keys=True) for x in disabled.seen])
        state = run_agent(b, "task", tools=guarded, max_steps=4,
                          runtime_config={"guards": {"wrapup_hint_steps": 4}})
        self.assertEqual(len([m for m in b.seen[0] if m.get("role") == "system"]), 2)  # base prompt plus hint
        self.assertEqual(sum(e["type"] == "wrapup_hint_injected" for e in state.events), 1)
        self.assertEqual(state.events[1]["data"]["remaining_steps"], 4)

    def test_repeat_guard_warns_then_blocks_and_distinct_args_are_independent(self):
        tools = ToolRegistry()
        executed = []
        tools.register("work", "work", {"type": "object", "properties": {"x": {"type": "integer"}},
                                            "required": ["x"]}, lambda x: executed.append(x) or {"x": x})
        model = Model([{"tool_calls": [call("work", '{"x":1}', "a")]},
                       {"tool_calls": [call("work", '{ "x" : 1 }', "b")]},
                       {"tool_calls": [call("work", '{"x":1}', "c")]}, {"content": "done"}])
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "s.db")
            state = run_agent(model, "task", tools=tools, store=store, session_id="s",
                              runtime_config={"guards": {"repeat_call_guard": True}})
            self.assertEqual(executed, [1, 1])
            self.assertIn("guard_warning", result_for(model.seen[2], "b")["data"])
            blocked = result_for(model.seen[3], "c")
            self.assertEqual(blocked["error"]["code"], "invalid_arguments")
            self.assertEqual([e["type"] for e in store.events("s")].count("repeat_call_warned"), 1)
            self.assertEqual([e["type"] for e in store.events("s")].count("repeat_call_blocked"), 1)
            rows = store.calls("s", state.turn_id)
            self.assertEqual(len(rows), 3)
            self.assertTrue(json.loads(rows[2]["result_json"])["error"])

    def test_repeat_count_rebuilds_after_resume_and_resets_for_new_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "s.db")
            tools = ToolRegistry()
            executed = []
            tools.register("work", "work", {"type": "object"}, lambda: executed.append(1) or "ok")
            options = {"guards": {"repeat_call_guard": True}}
            first = Model([{"tool_calls": [call("work", "{}", "one")]},
                           {"tool_calls": [call("work", "{}", "two")]}, RuntimeError("offline interruption")])
            interrupted = run_agent(first, "task", tools=tools, store=store, session_id="resume", runtime_config=options)
            self.assertEqual(interrupted.status, "failed")
            resumed_model = Model([{"tool_calls": [call("work", "{}", "three")]}, {"content": "done"}])
            resumed = run_agent(resumed_model, tools=tools, store=store, session_id="resume", runtime_config=options)
            self.assertEqual(result_for(resumed_model.seen[1], "three")["error"]["code"], "invalid_arguments")
            self.assertIn("repeat_call_blocked", [e["type"] for e in store.events("resume")])
            next_turn_model = Model([{"tool_calls": [call("work", "{}", "fresh")]}, {"content": "done"}])
            completed = run_agent(next_turn_model, "new task", tools=tools, store=store, session_id="resume", runtime_config=options)
            self.assertEqual(completed.status, "completed")
            self.assertEqual(executed, [1, 1, 1])

    def test_unchanged_read_dedupe_and_changed_file_reread(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
            tools = coding_tools(Workspace(root))
            model = Model([{"tool_calls": [call("read_file", '{"path":"a.txt"}', "r1")]},
                           {"tool_calls": [call("read_file", '{"path":"a.txt"}', "r2")]},
                           {"content": "done"}])
            state = run_agent(model, "task", tools=tools, runtime_config={"guards": {"dedupe_reads": True}})
            short = result_for(model.seen[2], "r2")["data"]
            self.assertTrue(short["unchanged"])
            self.assertIn("call_id r1", short["note"])
            self.assertIn("read_deduplicated", [e["type"] for e in state.events])

            class ChangeBetweenReads(Model):
                def complete(self, messages, specs):
                    if len(self.seen) == 1:
                        (root / "a.txt").write_text("changed\n", encoding="utf-8")
                    return super().complete(messages, specs)
            again = ChangeBetweenReads([{"tool_calls": [call("read_file", '{"path":"a.txt"}', "r1")]},
                                        {"tool_calls": [call("read_file", '{"path":"a.txt"}', "r2")]}, {"content": "done"}])
            run_agent(again, "task", tools=coding_tools(Workspace(root)), runtime_config={"guards": {"dedupe_reads": True}})
            self.assertIn("changed", result_for(again.seen[2], "r2")["data"]["content"])

            (root / "a.txt").write_text("before edit\n", encoding="utf-8")
            workspace = Workspace(root, allow_write=True)

            class EditBetweenReads(Model):
                def complete(self, messages, specs):
                    index = len(self.seen)
                    if index == 0:
                        response = {"tool_calls": [call("read_file", '{"path":"a.txt"}', "read-before-edit")]}
                    elif index == 1:
                        data = result_for(messages, "read-before-edit")["data"]
                        edit = {"path": "a.txt", "content": "after edit\n", "expected_sha256": data["sha256"],
                                "expected_instructions": data["instruction_digest"]}
                        response = {"tool_calls": [call("write_file", json.dumps(edit), "edit")]}
                    elif index == 2:
                        response = {"tool_calls": [call("read_file", '{"path":"a.txt"}', "read-after-edit")]}
                    else:
                        response = {"content": "done"}
                    self.seen.append(copy.deepcopy(messages))
                    return response

            edited = EditBetweenReads([])
            run_agent(edited, "task", tools=coding_tools(workspace), runtime_config={"guards": {"dedupe_reads": True}})
            reread = result_for(edited.seen[3], "read-after-edit")["data"]
            self.assertIn("after edit", reread["content"])

    def test_cli_flags_and_versioned_config_persist_guard_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "sessions.db"
            config = root / "harness.json"
            config.write_text(json.dumps({"version": 1, "workspace": {"root": str(root), "db": str(db)},
                                          "capabilities": {"tool_packages": []},
                                          "run": {"wrapup_hint_steps": 3, "repeat_call_guard": True, "dedupe_reads": True}}), encoding="utf-8")
            parsed = load_harness_config(config)
            self.assertEqual(parsed["wrapup_hint_steps"], 3)
            self.assertTrue(parsed["repeat_call_guard"] and parsed["dedupe_reads"])
            for source in ("flags", "config"):
                session = "guard-" + source
                args = ["task", "--root", str(root), "--db", str(db), "--session", session]
                if source == "flags":
                    args += ["--wrapup-hint-steps", "3", "--repeat-call-guard", "--dedupe-reads"]
                else:
                    args += ["--config", str(config)]
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(args, model_factory=lambda _: Model([{"content": "done"}])), 0)
                    resume = ["--resume", "--session", session, "--db", str(db)]
                    if source == "config":
                        resume += ["--config", str(config)]
                    self.assertEqual(main(resume, model_factory=lambda _: Model([])), 0)
                settings = SessionStore(db).load(session).metadata["guard_config"]
                self.assertEqual(settings, {"wrapup_hint_steps": 3, "repeat_call_guard": True, "dedupe_reads": True})


if __name__ == "__main__":
    unittest.main()
