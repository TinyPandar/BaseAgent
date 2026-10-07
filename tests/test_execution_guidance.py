import json
from pathlib import Path
import tempfile
import unittest

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus, State
from baseagent.middleware.execution_guidance import ExecutionGuidanceMiddleware
from baseagent.middleware import ModelRequest
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.workspace import Workspace, coding_tools


class RepeatingModel:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return {'tool_calls': [{'id': str(self.calls), 'type': 'function',
                 'function': {'name': 'read', 'arguments': '{ "path": "a" }' if self.calls % 2 else '{"path":"a"}'}}]}


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root)
        self.layer = ExecutionGuidanceMiddleware(self.workspace)
        self.tools = ToolRegistry()

    def test_unchanged_rounds_stop_before_fourth_model_request_and_after_resume(self):
        self.tools.register('read', 'read', {'type': 'object', 'properties': {'path': {'type': 'string'}}}, lambda path: 'same evidence')
        model = RepeatingModel()
        store = SessionStore(self.root/'sessions.sqlite3')
        state = run_agent(model, 'task', tools=self.tools, middleware=[self.layer], store=store,
                          session_id='test', workspace_root=self.root)
        self.assertEqual(state.status, RunStatus.TOOL_LOOP_DETECTED)
        self.assertEqual(model.calls, 3)
        self.assertEqual(state.tool_calls, 3)
        self.assertEqual(store.load('test').status, RunStatus.TOOL_LOOP_DETECTED)
        resumed = run_agent(model, tools=self.tools, middleware=[self.layer], store=store,
                            session_id='test', workspace_root=self.root)
        self.assertEqual(resumed.status, RunStatus.TOOL_LOOP_DETECTED)
        self.assertEqual(model.calls, 3)

    def test_changed_results_are_progress_not_a_loop(self):
        values = iter(range(10))
        self.tools.register('read', 'read', {'type': 'object', 'properties': {'path': {'type': 'string'}}}, lambda **args: next(values))
        state = run_agent(RepeatingModel(), 'task', tools=self.tools, middleware=[self.layer], max_steps=5)
        self.assertEqual(state.status, RunStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(state.model_calls, 5)

    def test_direct_answer_uses_no_tools_and_keeps_transcript_unmodified(self):
        class Direct:
            def complete(inner, messages, tools):
                self.assertIn('general questions', messages[1]['content'])
                self.assertIn('not the internet', messages[1]['content'])
                return {'content': 'An agent observes and acts toward a goal.'}
        state = run_agent(Direct(), 'define agent', tools=self.tools, middleware=[self.layer])
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.tool_calls, 0)
        self.assertEqual([m['role'] for m in state.messages], ['system', 'user', 'assistant'])

    def test_disabled_specs_hidden_without_removing_execution_gates(self):
        tools = coding_tools(self.workspace)
        state = State(messages=[{'role': 'system', 'content': 'test'}])
        request = ModelRequest(state, state.messages, tools.specs())
        projected = self.layer.wrap_model_call(request, lambda r: r)
        names = {t['function']['name'] for t in projected.tools}
        self.assertNotIn('run_command', names)
        self.assertNotIn('write_file', names)
        self.assertIn('read_file', names)
        self.assertIn('run_command', {t['function']['name'] for t in request.tools})
        self.assertFalse(tools.execute('run_command', json.dumps({'argv': ['python', '--version']})).ok)

    def test_enabled_tools_are_available(self):
        workspace = Workspace(self.root, allow_write=True, allow_command=True)
        tools = coding_tools(workspace)
        state = State()
        request = ModelRequest(state, [], tools.specs())
        projected = ExecutionGuidanceMiddleware(workspace).wrap_model_call(request, lambda r: r)
        names = {t['function']['name'] for t in projected.tools}
        self.assertIn('run_command', names)
        self.assertIn('write_file', names)


if __name__ == '__main__':
    unittest.main()
