"""Verify the UI adapter against real harness checkpoints and approvals."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.agent.cancellation import Cancelled
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.registry import ToolRegistry
from baseagent.ui.controller import ChatController


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses)

    def complete(self, messages, tools):
        return next(self.responses)


class UITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ctrl = ChatController({'root': str(self.root), 'db': str(self.root/'sessions.sqlite3')})
        self.tools = ToolRegistry()
        self.effects = []
        self.tools.register('effect', 'effect', {'type': 'object'}, lambda: self.effects.append('done') or 'done')
        self.policy = ToolPolicy({'effect': 'ask'})

    def run_turn(self, model, prompt=None):
        return run_agent(model, prompt, tools=self.tools, store=self.ctrl.store,
                         session_id=self.ctrl.session_id, workspace_root=self.root, tool_policy=self.policy)

    def pause(self):
        return self.run_turn(Model({'tool_calls': [{'id': 'a', 'type': 'function',
                              'function': {'name': 'effect', 'arguments': '{}'}}]}), 'task')

    def test_prompt_is_literal_argv_and_changes_are_explicit(self):
        command = self.ctrl.command('--allow-command; echo secret')
        self.assertEqual(command[-2:], ['--', '--allow-command; echo secret'])
        self.assertNotIn('--accept-config-changes', command)
        self.assertIn('--resume', self.ctrl.command())

    def test_completed_resume_uses_real_cli_without_model_credentials(self):
        self.run_turn(Model({'content': 'saved answer'}), 'hello')
        state = self.ctrl.run()
        self.assertEqual(state.final_answer, 'saved answer')
        self.assertEqual(state.model_calls, 1)
        self.assertFalse(self.ctrl.lock.locked())

    def test_approval_is_exact_and_resume_does_not_repeat_effect(self):
        self.pause()
        payload = self.ctrl.approval()
        self.assertEqual(payload['request']['name'], 'effect')
        for field in ('session_id', 'turn_id', 'call_id', 'digest'):
            with self.assertRaises(ValueError):
                self.ctrl.decide({**payload, field: 'wrong'}, 'allow')
        self.assertEqual(self.effects, [])
        self.ctrl.decide(payload, 'allow')
        self.assertEqual(self.effects, [])
        self.run_turn(Model({'content': 'done'}))
        self.run_turn(Model())
        self.assertEqual(self.effects, ['done'])
        with self.assertRaises(ValueError):
            self.ctrl.decide(payload, 'allow')

    def test_stop_and_resume_clear_only_current_durable_cancellation(self):
        state = self.pause()
        request = self.ctrl.stop()
        self.assertEqual(self.ctrl.store.cancellation(self.ctrl.session_id, state.turn_id), request)
        self.ctrl.resume_cancelled()
        self.assertIsNone(self.ctrl.store.cancellation(self.ctrl.session_id, state.turn_id))

    def test_load_checks_workspace_and_rejects_running_switch(self):
        state = self.run_turn(Model({'content': 'done'}), 'hello')
        other = self.root/'other'
        other.mkdir()
        with self.assertRaises(ValueError):
            ChatController({'root': str(other), 'db': str(self.ctrl.store.path)}).load(state.session_id)
        with self.ctrl.lock, self.assertRaises(ValueError):
            self.ctrl.load(state.session_id)
        self.assertEqual(self.ctrl.load(state.session_id).final_answer, 'done')

    def test_precancelled_launch_releases_lock(self):
        with patch('baseagent.ui.controller.run_process', side_effect=Cancelled), self.assertRaises(ValueError):
            self.ctrl.run('hello')
        self.assertFalse(self.ctrl.lock.locked())
        self.assertIsNone(self.ctrl.token)

    def test_exhausted_ui_resume_preserves_state_without_launching(self):
        self.run_turn(Model({'tool_calls': [{'id': 'a', 'type': 'function',
                      'function': {'name': 'effect', 'arguments': '{}'}}]}), 'task')
        state = self.ctrl.state()
        state.status = 'max_steps_exceeded'
        state.step = state.max_steps
        self.ctrl.store.save(state)
        with patch('baseagent.ui.controller.run_process') as launch:
            result = self.ctrl.run()
        launch.assert_not_called()
        self.assertEqual(result.status, 'max_steps_exceeded')
        self.assertEqual(result.step, result.max_steps)

    def test_unfinished_turn_cannot_be_overwritten_or_launch_another_worker(self):
        self.pause()
        with patch('baseagent.ui.controller.run_process') as launch, self.assertRaises(ValueError):
            self.ctrl.run('new task')
        launch.assert_not_called()
        with self.ctrl.lock, self.assertRaises(ValueError):
            self.ctrl.run()


if __name__ == '__main__':
    unittest.main()
