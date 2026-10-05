import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

import test_scope as fixtures
from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.agent.subtasks import SubtaskDefinition, SubtaskRuntime
from baseagent.middleware import AgentMiddleware
from baseagent.session import SessionStore
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.result import ToolResult


def delegate(name="reader", identifier="delegate"):
    return {"id": identifier, "type": "function", "function": {"name": "run_subtask", "arguments": json.dumps({"name": name, "prompt": "child task"})}}


class SubtaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "sessions.sqlite3")
        self.tools = ToolRegistry()
        self.effects = []
        self.tools.register("read", "effect", {"type": "object"}, lambda: self.effects.append(True) or "ok")

    def runtime(self, child, **kwargs):
        return SubtaskRuntime([SubtaskDefinition("reader", ("read",), model=child, **kwargs)])

    def run_turn(self, model, prompt=None, *, runtime, **kwargs):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id="demo", workspace_root=self.root,
                         subtasks=runtime, runtime_config={"test": 1}, **kwargs)

    def test_child_runs_and_result_is_delivered_once_with_shared_counts(self):
        child = fixtures.Model([{"tool_calls": [fixtures.call()]}, {"content": "child done"}])
        runtime = self.runtime(child)
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}, {"content": "done"}]), "task", runtime=runtime)
        self.assertEqual(root.status, RunStatus.COMPLETED)
        self.assertEqual((root.model_calls, root.tool_calls, root.total_tokens), (4, 2, 16))
        self.assertEqual(root.metadata["direct_usage"]["model_calls"], 2)
        self.assertEqual(self.effects, [True])
        replies = [message for message in root.messages if message["role"] == "tool"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(json.loads(replies[0]["content"])["data"]["answer"], "child done")
        cached = self.run_turn(fixtures.Model(), runtime=runtime)
        self.assertEqual(cached.to_dict(), root.to_dict())
        self.assertEqual(child.calls, 2)

    def test_node_approval_pauses_parent_and_resume_does_not_charge_delegate_again(self):
        child = fixtures.Model([{"tool_calls": [fixtures.call()]}, {"content": "child done"}])
        runtime = self.runtime(child, policy=ToolPolicy({"read": "ask"}))
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}]), "task", runtime=runtime)
        self.assertEqual(root.status, RunStatus.AWAITING_APPROVAL)
        self.assertEqual(self.effects, [])
        node = self.store.task_tree("demo")["nodes"][0]
        self.assertEqual(self.store.call(root, "delegate")["attempts"], 1)
        self.store.decide_task_tool("demo", node["task_id"], "read", "allow", node["state"]["metadata"]["approval_request"]["request_digest"])
        root = self.run_turn(fixtures.Model([{"content": "done"}]), runtime=runtime)
        self.assertEqual(root.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, [True])
        self.assertEqual((root.model_calls, root.tool_calls), (4, 2))
        self.assertEqual(self.store.call(root, "delegate")["attempts"], 1)

    def test_interrupted_child_effect_needs_node_reconciliation_then_never_replays(self):
        def effect():
            self.effects.append(True)
            raise KeyboardInterrupt()
        self.tools = ToolRegistry()
        self.tools.register("read", "effect", {"type": "object"}, effect)
        child = fixtures.Model([{"tool_calls": [fixtures.call()]}, {"content": "child done"}])
        runtime = self.runtime(child)
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}]), "task", runtime=runtime)
        self.assertEqual(root.status, RunStatus.INTERRUPTED)
        root = self.run_turn(fixtures.Model(), runtime=runtime)
        self.assertEqual(root.status, RunStatus.NEEDS_RECOVERY)
        node = self.store.task_tree("demo")["nodes"][0]
        self.store.resolve_task_call("demo", node["task_id"], "read", ToolResult(data="verified"))
        root = self.run_turn(fixtures.Model([{"content": "done"}]), runtime=runtime)
        self.assertEqual(root.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, [True])
        self.assertEqual(self.store.call(root, "delegate")["attempts"], 1)

    def test_completed_child_cached_delivery_after_outer_wrapper_interrupt(self):
        class Interrupt(AgentMiddleware):
            def wrap_tool_call(inner, request, handler):
                result = handler(request)
                if request.name == "run_subtask":
                    raise KeyboardInterrupt()
                return result
        child = fixtures.Model([{"content": "child done"}])
        runtime = self.runtime(child)
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}]), "task", runtime=runtime, middleware=[Interrupt()])
        self.assertEqual(root.status, RunStatus.INTERRUPTED)
        self.assertEqual(self.store.task_tree("demo")["nodes"][0]["state"]["status"], RunStatus.COMPLETED)
        root = self.run_turn(fixtures.Model([{"content": "done"}]), runtime=runtime, middleware=[Interrupt()])
        self.assertEqual(root.status, RunStatus.COMPLETED)
        self.assertEqual(child.calls, 1)
        self.assertEqual(root.tool_calls, 1)

    def test_parent_policy_deny_cannot_be_weakened_by_child_allow(self):
        child = fixtures.Model([{"tool_calls": [fixtures.call()]}, {"content": "denied"}])
        runtime = self.runtime(child, policy=ToolPolicy(default="allow"))
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}, {"content": "done"}]), "task", runtime=runtime, tool_policy=ToolPolicy({"read": "deny"}))
        self.assertEqual(root.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, [])
        self.assertEqual(root.tool_calls, 1)

    def test_config_change_requires_acceptance_before_continuation(self):
        child = fixtures.Model([{"tool_calls": [fixtures.call()]}, {"content": "child done"}])
        runtime = self.runtime(child, policy=ToolPolicy({"read": "ask"}))
        self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}]), "task", runtime=runtime)
        changed = self.runtime(child, policy=ToolPolicy({"read": "ask"}), max_steps=9)
        with self.assertRaises(ValueError):
            self.run_turn(fixtures.Model(), runtime=changed)
        self.assertEqual(child.calls, 1)

    def test_root_budget_stops_child_and_extension_preserves_earlier_counts(self):
        child = fixtures.Model([{"tool_calls": [fixtures.call()]}, {"content": "child done"}])
        runtime = self.runtime(child)
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}]), "task", runtime=runtime, max_model_calls=2)
        self.assertEqual(root.status, RunStatus.MAX_MODEL_CALLS_EXCEEDED)
        self.assertEqual(root.model_calls, 2)
        self.assertEqual(self.effects, [True])
        root = self.run_turn(fixtures.Model([{"content": "done"}]), runtime=runtime, max_model_calls=4)
        self.assertEqual(root.status, RunStatus.COMPLETED)
        self.assertEqual(root.model_calls, 4)
        self.assertEqual(root.tool_calls, 2)
        self.assertEqual(self.effects, [True])

    def test_unknown_child_usage_must_be_reconciled_before_parent_continues(self):
        child = fixtures.Model([ConnectionError(), {"content": "child done"}])
        runtime = self.runtime(child)
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}]), "task", runtime=runtime, max_total_tokens=100)
        self.assertEqual(root.status, RunStatus.FAILED)
        self.assertEqual(self.run_turn(fixtures.Model(), runtime=runtime).status, RunStatus.USAGE_UNAVAILABLE)
        node = self.store.task_tree("demo")["nodes"][0]
        self.store.resolve_task_usage("demo", node["task_id"], fixtures.TokenUsage(2, 2, 4), 1)
        root = self.run_turn(fixtures.Model([{"content": "done"}]), runtime=runtime)
        self.assertEqual(root.status, RunStatus.COMPLETED)
        self.assertEqual(root.total_tokens, 16)
        self.assertEqual(root.model_calls, 4)

    def test_nested_delegation_reuses_live_ancestors_and_charges_once(self):
        leaf = fixtures.Model([{"tool_calls": [fixtures.call()]}, {"content": "leaf"}])
        middle = fixtures.Model([{"tool_calls": [delegate("leaf")]}, {"content": "middle"}])
        runtime = SubtaskRuntime([SubtaskDefinition("reader", ("read", "run_subtask"), model=middle), SubtaskDefinition("leaf", ("read",), model=leaf)])
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}, {"content": "done"}]), "task", runtime=runtime)
        self.assertEqual(root.status, RunStatus.COMPLETED)
        self.assertEqual((root.model_calls, root.tool_calls, root.total_tokens), (6, 3, 24))
        nodes = self.store.task_tree("demo")["nodes"]
        self.assertEqual([node["state"]["total_tokens"] for node in nodes], [16, 8])
        self.assertEqual([node["state"]["metadata"]["direct_usage"]["total_tokens"] for node in nodes], [8, 8])
        self.assertEqual(self.effects, [True])

    def test_same_named_nested_tasks_with_different_prompts_are_isolated(self):
        nested = delegate()
        nested["function"]["arguments"] = json.dumps({"name": "reader", "prompt": "different leaf task"})
        child = fixtures.Model([{"tool_calls": [nested]}, {"tool_calls": [fixtures.call()]}, {"content": "leaf"}, {"content": "middle"}])
        runtime = self.runtime(child)
        from dataclasses import replace
        runtime = SubtaskRuntime([replace(runtime.definitions["reader"], tool_names=("read", "run_subtask"))])
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}, {"content": "done"}]), "task", runtime=runtime)
        self.assertEqual(root.status, RunStatus.COMPLETED)
        nodes = self.store.task_tree("demo")["nodes"]
        self.assertEqual([node["name"] for node in nodes], ["reader", "reader"])
        self.assertEqual(len({node["task_id"] for node in nodes}), 2)
        self.assertEqual(len({node["state"]["turn_id"] for node in nodes}), 2)
        self.assertEqual([node["spawn_call_id"] for node in nodes], ["delegate", "delegate"])
        self.assertEqual((root.model_calls, root.tool_calls, root.total_tokens), (6, 3, 24))
        self.assertEqual(self.effects, [True])

    def test_accepted_child_limit_extension_keeps_original_turn_and_usage(self):
        child = fixtures.Model([{"tool_calls": [fixtures.call()]}, {"content": "child done"}])
        runtime = self.runtime(child, max_model_calls=1)
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}]), "task", runtime=runtime)
        self.assertEqual(root.status, RunStatus.MAX_MODEL_CALLS_EXCEEDED)
        original = self.store.task_tree("demo")["nodes"][0]["state"]
        changed = self.runtime(child, max_model_calls=2)
        root = self.run_turn(fixtures.Model([{"content": "done"}]), runtime=changed, accept_config_changes=True)
        self.assertEqual(root.status, RunStatus.COMPLETED)
        resumed = self.store.task_tree("demo")["nodes"][0]["state"]
        self.assertEqual(resumed["turn_id"], original["turn_id"])
        self.assertEqual(resumed["turn_started_at"], original["turn_started_at"])
        self.assertEqual(resumed["max_model_calls"], 2)
        self.assertEqual(resumed["model_calls"], 2)
        self.assertEqual(resumed["total_tokens"], 8)

    def test_cyclic_delegation_is_refused_without_creating_another_node(self):
        child = fixtures.Model([{"tool_calls": [delegate()]}, {"content": "cycle refused"}])
        runtime = SubtaskRuntime([SubtaskDefinition("reader", ("run_subtask",), model=child)])
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}, {"content": "done"}]), "task", runtime=runtime)
        self.assertEqual(root.status, RunStatus.COMPLETED)
        nodes = self.store.task_tree("demo")["nodes"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(json.loads(nodes[0]["tool_calls"][0]["result_json"])["error"]["code"], "limit_exceeded")

    def test_nested_task_cannot_get_tool_missing_from_parent(self):
        self.tools.register("write", "write", {"type": "object"}, lambda: self.effects.append(True))
        leaf = fixtures.Model([{"tool_calls": [fixtures.call("write", "write")]}, {"content": "unavailable"}])
        middle = fixtures.Model([{"tool_calls": [delegate("leaf")]}, {"content": "middle"}])
        runtime = SubtaskRuntime([SubtaskDefinition("reader", ("run_subtask",), model=middle), SubtaskDefinition("leaf", ("write",), model=leaf)])
        root = self.run_turn(fixtures.Model([{"tool_calls": [delegate()]}, {"content": "done"}]), "task", runtime=runtime)
        self.assertEqual(root.status, RunStatus.COMPLETED)
        self.assertEqual(self.effects, [])
        leaf_node = self.store.task_tree("demo")["nodes"][1]
        self.assertEqual(json.loads(leaf_node["tool_calls"][0]["result_json"])["error"]["code"], "unknown_tool")

    def test_hard_exit_after_completed_child_recovers_cached_delivery(self):
        effect = self.root / "effect.txt"
        script = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from test_scope import Model
from baseagent.agent import run_agent
from baseagent.agent.subtasks import SubtaskRuntime, SubtaskDefinition
from baseagent.middleware import AgentMiddleware
from baseagent.tools.registry import ToolRegistry
from baseagent.session import SessionStore
class Exit(AgentMiddleware):
    def wrap_tool_call(self, request, handler):
        result = handler(request)
        if request.name == 'run_subtask': os._exit(41)
        return result
tools = ToolRegistry()
tools.register('read', 'effect', {'type':'object'}, lambda: Path(sys.argv[3]).write_text('once') or 'ok')
child = Model([{'tool_calls':[{'id':'read','type':'function','function':{'name':'read','arguments':'{}'}}]}, {'content':'child done'}])
runtime = SubtaskRuntime([SubtaskDefinition('reader', ('read',), model=child)])
request = {'id':'delegate','type':'function','function':{'name':'run_subtask','arguments':'{"name":"reader","prompt":"child task"}'}}
run_agent(Model([{'tool_calls':[request]}]), 'task', tools=tools, store=SessionStore(sys.argv[1]), session_id='demo', workspace_root=sys.argv[2], subtasks=runtime, runtime_config={'test':1}, middleware=[Exit()])
"""
        process = subprocess.run([sys.executable, "-c", script, str(self.store.path), str(self.root), str(effect), str(Path(__file__).parent)], capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 41, process.stderr)
        child = fixtures.Model()
        runtime = self.runtime(child)
        root = self.run_turn(fixtures.Model([{"content": "done"}]), runtime=runtime)
        self.assertEqual(root.status, RunStatus.COMPLETED)
        self.assertEqual(child.calls, 0)
        self.assertEqual(root.model_calls, 4)
        self.assertEqual(root.tool_calls, 2)
        self.assertEqual(self.effects, [])
        self.assertEqual(effect.read_text(), "once")

    def test_actual_delegation_node_approval_and_resume_across_processes(self):
        script = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from test_scope import Model, call
from baseagent.agent import run_agent
from baseagent.agent.subtasks import SubtaskDefinition, SubtaskRuntime
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.policy import ToolPolicy
phase = sys.argv[4]
root = Path(sys.argv[2])
def effect():
    with (root / 'effects.txt').open('a', encoding='utf-8') as output:
        output.write('once\\n')
    return 'ok'
tools = ToolRegistry()
tools.register('read', 'effect', {'type':'object'}, effect)
child = Model([{'tool_calls':[call()]}] if phase == 'begin' else ([{'content':'child done'}] if phase == 'resume' else []))
runtime = SubtaskRuntime([SubtaskDefinition('reader', ('read',), model=child, policy=ToolPolicy({'read':'ask'}))])
request = {'id':'delegate','type':'function','function':{'name':'run_subtask','arguments':'{"name":"reader","prompt":"child task"}'}}
model = Model([{'tool_calls':[request]}] if phase == 'begin' else ([{'content':'done'}] if phase == 'resume' else []))
state = run_agent(model, 'task' if phase == 'begin' else None, tools=tools, store=SessionStore(sys.argv[1]), session_id='demo', workspace_root=root, subtasks=runtime, runtime_config={'test':1})
print(json.dumps({'status':state.status,'model_calls':state.model_calls,'tool_calls':state.tool_calls,'total_tokens':state.total_tokens,'child_dispatches':child.calls,'root_dispatches':model.calls}))
"""
        command = [sys.executable, "-c", script, str(self.store.path), str(self.root), str(Path(__file__).parent)]
        initial = subprocess.run([*command, "begin"], capture_output=True, timeout=10)
        self.assertEqual(initial.returncode, 0, initial.stderr)
        self.assertEqual(json.loads(initial.stdout)["status"], RunStatus.AWAITING_APPROVAL)
        node = self.store.task_tree("demo")["nodes"][0]
        approve = [sys.executable, "-m", "baseagent", "--db", str(self.store.path), "--session", "demo", "--task-id", node["task_id"], "--approve-tool", "read", "--approval-digest"]
        wrong = subprocess.run([*approve, "wrong"], capture_output=True, timeout=10)
        self.assertEqual(wrong.returncode, 1)
        self.assertFalse((self.root / "effects.txt").exists())
        approved = subprocess.run([*approve, node["state"]["metadata"]["approval_request"]["request_digest"]], capture_output=True, timeout=10)
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.assertFalse((self.root / "effects.txt").exists())
        resumed = subprocess.run([*command, "resume"], capture_output=True, timeout=10)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout), {"status": "completed", "model_calls": 4, "tool_calls": 2, "total_tokens": 16, "child_dispatches": 1, "root_dispatches": 1})
        self.assertEqual((self.root / "effects.txt").read_text(), "once\n")
        node = self.store.task_tree("demo")["nodes"][0]
        self.assertEqual(node["tool_calls"][0]["attempts"], 1)
        cached = subprocess.run([*command, "cached"], capture_output=True, timeout=10)
        self.assertEqual(cached.returncode, 0, cached.stderr)
        self.assertEqual((json.loads(cached.stdout)["child_dispatches"], json.loads(cached.stdout)["root_dispatches"]), (0, 0))
        self.assertEqual((self.root / "effects.txt").read_text(), "once\n")

    def test_hard_exit_during_child_model_retains_ancestor_quote_and_unknown_usage(self):
        script = """
import json, os, sys
sys.path.insert(0, sys.argv[3])
from test_scope import Model
from baseagent.agent import run_agent
from baseagent.agent.subtasks import SubtaskDefinition, SubtaskRuntime
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
phase = sys.argv[4]
class Exit(Model):
    def complete_with_control(self, *args, **kwargs):
        if phase == 'begin':
            os._exit(43)
        return super().complete_with_control(*args, **kwargs)
child = Exit([{'content':'child done'}])
runtime = SubtaskRuntime([SubtaskDefinition('reader', (), model=child)])
request = {'id':'delegate','type':'function','function':{'name':'run_subtask','arguments':'{"name":"reader","prompt":"child task"}'}}
model = Model([{'tool_calls':[request]}] if phase == 'begin' else [{'content':'done'}])
state = run_agent(model, 'task' if phase == 'begin' else None, tools=ToolRegistry(), store=SessionStore(sys.argv[1]), session_id='demo', workspace_root=sys.argv[2], subtasks=runtime, runtime_config={'test':1}, preauthorize_model=True, max_total_tokens=100)
print(json.dumps({'status':state.status,'model_calls':state.model_calls,'tool_calls':state.tool_calls,'total_tokens':state.total_tokens,'unknown':state.unknown_usage_calls,'reservations':state.model_reservations,'child_dispatches':child.calls}))
"""
        command = [sys.executable, "-c", script, str(self.store.path), str(self.root), str(Path(__file__).parent)]
        exited = subprocess.run([*command, "begin"], capture_output=True, timeout=10)
        self.assertEqual(exited.returncode, 43, exited.stderr)
        root = self.store.load("demo")
        node = self.store.task_tree("demo")["nodes"][0]
        self.assertEqual((root.model_calls, root.unknown_usage_calls), (2, 1))
        self.assertEqual(root.model_reservations, {node["state"]["turn_id"] + ":1": 6})
        self.assertEqual(node["state"]["model_reservations"], {"1": 6})
        blocked = subprocess.run([*command, "blocked"], capture_output=True, timeout=10)
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual((json.loads(blocked.stdout)["status"], json.loads(blocked.stdout)["child_dispatches"]), ("usage_unavailable", 0))
        # The controlled process exited before its synthetic provider operation;
        # the independently known outcome here is zero billed tokens.
        self.store.resolve_task_usage("demo", node["task_id"], fixtures.TokenUsage(0, 0, 0), 1)
        resumed = subprocess.run([*command, "resume"], capture_output=True, timeout=10)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout), {"status": "completed", "model_calls": 4, "tool_calls": 1, "total_tokens": 12, "unknown": 0, "reservations": {}, "child_dispatches": 1})

    def test_hard_exit_after_delegated_child_effect_requires_reconcile_without_replay(self):
        script = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from test_scope import Model, call
from baseagent.agent import run_agent
from baseagent.agent.subtasks import SubtaskDefinition, SubtaskRuntime
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
phase = sys.argv[4]
def effect():
    with (Path(sys.argv[2]) / 'effects.txt').open('a', encoding='utf-8') as output:
        output.write('once\\n')
    os._exit(44)
tools = ToolRegistry()
tools.register('read', 'effect', {'type':'object'}, effect)
child = Model([{'tool_calls':[call()]}] if phase == 'begin' else [{'content':'child done'}])
runtime = SubtaskRuntime([SubtaskDefinition('reader', ('read',), model=child)])
request = {'id':'delegate','type':'function','function':{'name':'run_subtask','arguments':'{"name":"reader","prompt":"child task"}'}}
model = Model([{'tool_calls':[request]}] if phase == 'begin' else [{'content':'done'}])
state = run_agent(model, 'task' if phase == 'begin' else None, tools=tools, store=SessionStore(sys.argv[1]), session_id='demo', workspace_root=sys.argv[2], subtasks=runtime, runtime_config={'test':1})
print(json.dumps({'status':state.status,'model_calls':state.model_calls,'tool_calls':state.tool_calls,'total_tokens':state.total_tokens,'child_dispatches':child.calls}))
"""
        command = [sys.executable, "-c", script, str(self.store.path), str(self.root), str(Path(__file__).parent)]
        exited = subprocess.run([*command, "begin"], capture_output=True, timeout=10)
        self.assertEqual(exited.returncode, 44, exited.stderr)
        self.assertEqual((self.root / "effects.txt").read_text(), "once\n")
        node = self.store.task_tree("demo")["nodes"][0]
        self.assertEqual((node["tool_calls"][0]["status"], node["tool_calls"][0]["terminal_result_json"]), ("running", None))
        blocked = subprocess.run([*command, "blocked"], capture_output=True, timeout=10)
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual((json.loads(blocked.stdout)["status"], json.loads(blocked.stdout)["child_dispatches"]), ("needs_recovery", 0))
        self.store.resolve_task_call("demo", node["task_id"], "read", ToolResult(data="verified file effect"))
        resumed = subprocess.run([*command, "resume"], capture_output=True, timeout=10)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout), {"status": "completed", "model_calls": 4, "tool_calls": 2, "total_tokens": 16, "child_dispatches": 1})
        self.assertEqual((self.root / "effects.txt").read_text(), "once\n")
        self.assertEqual(self.store.task_tree("demo")["nodes"][0]["tool_calls"][0]["attempts"], 1)

    def test_cli_configuration_is_explicit_strict_and_refuses_unsupported_reservation(self):
        config = {"tasks": [{"name": "reader", "tools": ["read_file"], "max_steps": 3}]}
        path = self.root / "subtasks.json"
        path.write_text(json.dumps(config))
        result = subprocess.run([sys.executable, "-m", "baseagent", "task", "--root", str(self.root), "--db", str(self.store.path), "--session", "cli", "--subtasks-config", str(path), "--preauthorize-model", "--max-total-tokens", "100"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.store.load("cli").status, RunStatus.RESERVATION_UNAVAILABLE)
        self.assertEqual(self.store.load("cli").model_calls, 0)
        for text in ('{"tasks":[],"tasks":[]}', '{"tasks":[],"max_depth":NaN}', '{"tasks":[{"name":"reader","tools":[],"model":"other"}]}', '{"tasks":[{"name":"reader","tools":[{}]}]}'):
            with self.assertRaises(ValueError):
                SubtaskRuntime.from_json(text)


if __name__ == "__main__":
    unittest.main()
