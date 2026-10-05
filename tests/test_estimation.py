import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import io
from contextlib import redirect_stdout, redirect_stderr

from tokenizers import Tokenizer, models, pre_tokenizers

from baseagent.agent import run_agent
from baseagent.agent.scope import ExecutionScope
from baseagent.agent.state import State, RunStatus
from baseagent.llm.estimation import TokenEstimate, TokenizerEstimator
from baseagent.llm.reservation import request_digest
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.session import SessionStore
from baseagent.tools.registry import ToolRegistry


class EstimatedModel:
    def __init__(self, usage=6, unknown=False):
        self.calls = 0
        self.usage, self.unknown = usage, unknown

    def estimate_request(self, messages, tools):
        return TokenEstimate(5, 3, request_digest(messages, tools))

    reserve_request = estimate_request

    def complete_reserved(self, messages, tools, **kwargs):
        self.calls += 1
        return ModelResponse({'content': 'done'}, None if self.unknown else TokenUsage(2, self.usage-2, self.usage))


class EstimationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root/'sessions.db')
        self.tools = ToolRegistry()

    def run_turn(self, model, prompt=None, **kwargs):
        return run_agent(model, prompt, tools=self.tools, store=self.store, session_id='demo', **kwargs)

    def test_undercount_within_budget_is_recorded_without_strict_violation(self):
        model = EstimatedModel()
        state = self.run_turn(model, 'task', estimate_model=True, max_total_tokens=10)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.total_tokens, 6)
        self.assertEqual(state.metadata['last_token_estimate']['error_tokens'], 1)
        self.assertNotIn('request_bound_violation', state.metadata)
        self.assertEqual(state.model_reservations, {})
        self.assertEqual(self.run_turn(model).final_answer, 'done')
        self.assertEqual(model.calls, 1)

    def test_excess_is_durable_and_resume_does_not_repeat_request(self):
        model = EstimatedModel(usage=12)
        state = self.run_turn(model, 'task', estimate_model=True, max_total_tokens=10)
        self.assertEqual(state.status, RunStatus.MAX_TOKENS_EXCEEDED)
        self.assertEqual(self.store.load('demo').total_tokens, 12)
        self.assertEqual(self.run_turn(model).status, RunStatus.MAX_TOKENS_EXCEEDED)
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.run_turn(model, max_total_tokens=20).status, RunStatus.COMPLETED)
        self.assertEqual(model.calls, 1)

    def test_estimate_refuses_before_dispatch_when_budget_is_insufficient(self):
        model = EstimatedModel()
        state = self.run_turn(model, 'task', estimate_model=True, max_total_tokens=4)
        self.assertEqual(state.status, RunStatus.MAX_TOKENS_EXCEEDED)
        self.assertEqual(model.calls, 0)

    def test_unknown_usage_keeps_quote_and_receipt_may_exceed_estimate(self):
        model = EstimatedModel(unknown=True)
        state = self.run_turn(model, 'task', estimate_model=True, max_total_tokens=10)
        self.assertEqual(state.status, RunStatus.USAGE_UNAVAILABLE)
        self.assertEqual(state.model_reservations, {'1': 5})
        state = self.store.resolve_usage('demo', TokenUsage(3, 4, 7), unknown_calls=1)
        self.assertEqual(state.model_reservations, {})
        self.assertNotIn('request_bound_violation', state.metadata)
        self.assertEqual(self.run_turn(model).status, RunStatus.COMPLETED)
        self.assertEqual(model.calls, 1)

    def test_estimate_cannot_be_used_as_strict_bound(self):
        model = EstimatedModel()
        state = self.run_turn(model, 'task', preauthorize_model=True, max_total_tokens=10)
        self.assertEqual(state.status, RunStatus.RESERVATION_UNAVAILABLE)
        self.assertEqual(model.calls, 0)

    def test_child_estimate_charges_ancestors_and_records_error(self):
        root = State(session_id='x', workspace_root='w', turn_id='root', estimate_model=True, max_total_tokens=10)
        child = State(session_id='x', workspace_root='w', turn_id='child')
        scope = ExecutionScope(child, (root,))
        self.assertTrue(scope.estimate_model)
        quote = TokenEstimate(5, 3, request_digest([], []))
        scope.admit_model(quote)
        keys = scope.model_started(quote)
        self.assertEqual(root.model_reservations, {'child:1': 5})
        scope.model_returned(TokenUsage(3, 4, 7), quote, keys)
        self.assertEqual((root.total_tokens, child.total_tokens), (7, 7))
        self.assertEqual(root.model_reservations, {})
        self.assertNotIn('request_bound_violation', root.metadata)
        child.preauthorize_model = True
        with self.assertRaises(ValueError):
            ExecutionScope(child, (root,))

    def test_tokenizer_counts_full_json_without_truncating_or_padding(self):
        tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0}, unk_token='[UNK]'))
        tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        tokenizer.enable_truncation(1)
        tokenizer.enable_padding(length=1000)
        path = self.root/'tokenizer.json'
        tokenizer.save(str(path))
        estimator = TokenizerEstimator(path, margin_percent=20, fixed_margin=2)
        messages = [{'role': 'system', 'content': '规则'}, {'role': 'user', 'content': 'hello world'}]
        tools = [{'type': 'function', 'function': {'name': 'read', 'description': 'a b c', 'parameters': {'type': 'object'}}}]
        quote = estimator.reserve_request(messages, tools, 3)
        raw = json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        count = len(estimator.tokenizer.encode(raw, add_special_tokens=False).ids)
        self.assertGreater(count, 1)
        self.assertLess(count, 1000)
        self.assertEqual(quote.total_tokens, count+(count*20+99)//100+2+3)
        self.assertGreater(quote.total_tokens, estimator.reserve_request(messages, [], 3).total_tokens)
        quote.validate(messages, tools)
        with self.assertRaises(ValueError):
            quote.validate(messages, [])
        self.assertFalse(estimator.contract()['provider_guaranteed'])

    def test_cli_insufficient_estimate_never_initializes_provider(self):
        from baseagent.main import main
        tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0}, unk_token='[UNK]'))
        path = self.root/'tokenizer.json'
        tokenizer.save(str(path))
        with patch('baseagent.main.Model', side_effect=AssertionError('provider initialized')), patch('sys.argv',
                ['baseagent', 'task', '--root', str(self.root), '--db', str(self.root/'cli.db'),
                 '--estimate-model', '--tokenizer-file', str(path), '--max-total-tokens', '1']), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 1)

    def test_cli_cached_completion_needs_no_tokenizer_or_provider(self):
        from baseagent.main import main
        self.run_turn(EstimatedModel(), 'task', estimate_model=True, max_total_tokens=10, workspace_root=self.root)
        output = io.StringIO()
        with patch('baseagent.main.Model', side_effect=AssertionError('provider initialized')), patch('sys.argv',
                ['baseagent', '--resume', '--session', 'demo', '--db', str(self.root/'sessions.db')]), redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertIn('done', output.getvalue())

    def test_model_output_cap_and_estimate_bind_exact_input(self):
        from baseagent.llm.model import Model
        from baseagent.agent.cancellation import CancellationToken
        tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0}, unk_token='[UNK]'))
        path = self.root/'tokenizer.json'
        tokenizer.save(str(path))
        estimator = TokenizerEstimator(path)
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5),
                                   choices=[SimpleNamespace(message={'content': 'done'})])
        with patch('baseagent.llm.model.OpenAI') as sdk:
            sdk.return_value.chat.completions.create.return_value = response
            model = Model('test', 'dummy', token_estimator=estimator, max_completion_tokens=3)
            messages = [{'role': 'user', 'content': 'hello'}]
            quote = model.estimate_request(messages, [])
            with self.assertRaises(ValueError):
                model.complete_reserved([], [], reservation=quote, cancellation=CancellationToken())
            sdk.return_value.chat.completions.create.assert_not_called()
            model.complete_reserved(messages, [], reservation=quote, cancellation=CancellationToken())
            self.assertEqual(sdk.return_value.chat.completions.create.call_args.kwargs['max_tokens'], 3)

    def test_node_unknown_receipt_exceeds_estimate_without_strict_violation(self):
        root = State(session_id='node', workspace_root=str(self.root), turn_id='root', estimate_model=True, max_total_tokens=20)
        self.store.save(root, [{'id': 'delegate', 'type': 'function', 'function': {'name': 'delegate', 'arguments': '{}'}}])
        self.store.start_call(root, 'delegate')
        child = State(session_id='node', workspace_root=str(self.root), turn_id='child')
        with self.store.exclusive('node'):
            record = self.store.create_task(root, call_id='delegate', name='reader', state=child)
            view = self.store.task_node(root, record['task_id'])
            scope = view.execution_scope()
            scope.model_started(TokenEstimate(5, 3, request_digest([], [])))
            view.save(view.state)
        state = self.store.resolve_task_usage('node', record['task_id'], TokenUsage(3, 4, 7), unknown_calls=1)
        self.assertEqual(state.total_tokens, 7)
        self.assertNotIn('request_bound_violation', state.metadata)
        restored = self.store.load('node')
        self.assertEqual(restored.total_tokens, 7)
        self.assertEqual(restored.model_reservations, {})
