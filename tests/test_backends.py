from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from baseagent.agent import run_agent
from baseagent.agent.completion import CompletionPolicy
from baseagent.agent.state import State, RunStatus
from baseagent.backends import LocalBackend, WorkspaceBackend
from baseagent.middleware.repository import RepositoryMiddleware
from baseagent.session import SessionStore
from baseagent.tools.context import ToolContext
from baseagent.tools.instructions import instruction_digest
from baseagent.tools.result import ErrorCode, ToolFailure, ToolResult
from baseagent.tools.workspace import Workspace, coding_tools
from baseagent.tools.workflow import verify_command


class MemoryBackend:
    """Test environment whose namespace has no corresponding host directory."""
    def __init__(self, root):
        self.root = root
        self.protected_paths = ()
        self.files = {'x.txt': b'first\r\nsecond\r\n', 'AGENTS.md': b'Run tests.'}
        self.calls = []
        self.identity = 'environment-a'

    def contract(self):
        return {'kind': 'test-memory', 'id': self.identity}

    def resolve_path(self, path):
        return Path(os.path.abspath(self.root / path))

    def read_bytes(self, path, limit=200_000):
        self.calls.append(('read', path))
        value = self.files[path]
        if len(value) > limit:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, 'too large')
        return value

    def file_hash(self, path):
        return sha256(self.files[path]).hexdigest() if path in self.files else 'missing'

    def get_instructions(self, path='.'):
        values = [{'path': 'AGENTS.md', 'content': self.files['AGENTS.md'].decode(), 'sha256': self.file_hash('AGENTS.md')}]
        return {'scope': '.', 'instructions': values, 'digest': instruction_digest(values)}

    def search_files(self, query, glob='**/*'):
        self.calls.append(('search', query))
        return {'matches': [{'path': name, 'line': 1, 'text': value.decode()} for name, value in self.files.items() if query in value.decode()], 'truncated': False}

    def write_file(self, path, content, expected_sha256, expected_instructions):
        self.calls.append(('write', path))
        if self.file_hash(path) != expected_sha256 or self.get_instructions(path)['digest'] != expected_instructions:
            raise ToolFailure(ErrorCode.CONFLICT, 'stale')
        self.files[path] = content.encode()
        return {'path': path, 'bytes': len(self.files[path]), 'sha256': self.file_hash(path), 'instruction_digest': self.get_instructions(path)['digest']}

    def execute(self, argv, *, timeout, output_limit=20_000, cancellation=None):
        self.calls.append(('execute', list(argv), timeout, output_limit, cancellation))
        return ToolResult(data={'exit_code': 0, 'stdout': 'ok', 'stderr': ''})


class Model:
    def __init__(self, messages):
        self.messages, self.calls = iter(messages), 0

    def complete(self, messages, tools):
        self.calls += 1
        return next(self.messages)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = MemoryBackend(self.root/'absent-environment')
        self.workspace = Workspace(backend=self.backend, allow_write=True, allow_command=True)

    def test_files_guidance_edit_search_verification_completion_never_touch_host(self):
        state = State(turn_id='turn')
        context = ToolContext(state, lambda *args, **kwargs: None)
        with patch('pathlib.Path.open', side_effect=AssertionError('host I/O')), patch('os.walk', side_effect=AssertionError('host search')), patch('subprocess.Popen', side_effect=AssertionError('host process')):
            middleware = RepositoryMiddleware(self.workspace)
            middleware.before_agent(state)
            read = self.workspace.read_file('x.txt', context=context)
            self.workspace.edit_file('x.txt', [{'old_text': 'first', 'new_text': 'FIRST'}], read['sha256'], read['instruction_digest'], context=context)
            self.assertEqual(self.backend.files['x.txt'], b'FIRST\r\nsecond\r\n')
            self.assertEqual(len(self.workspace.search_files('FIRST')['matches']), 1)
            result = verify_command(self.workspace, ['python', '-m', 'test'], ['x.txt'], context=context)
            self.assertTrue(result.ok)
            policy = CompletionPolicy(self.workspace, {'require_plan': False, 'checks': [{'name': 'test', 'argv': ['python', '-m', 'test'], 'paths': ['x.txt']}], 'artifacts': ['x.txt']})
            self.assertTrue(policy.evaluate(state)['passed'])
            self.backend.files['x.txt'] = b'external change'
            self.assertFalse(policy.evaluate(state)['passed'])
            with self.assertRaises(Exception) as raised:
                middleware.before_model(state)
            self.assertIn('workspace', str(raised.exception))
        self.assertFalse(self.backend.root.exists())

    def test_capabilities_and_paths_refuse_before_backend_effects(self):
        workspace = Workspace(backend=self.backend)
        with self.assertRaises(ToolFailure):
            workspace.write_file('new.txt', 'new', 'missing', self.backend.get_instructions()['digest'])
        with self.assertRaises(ToolFailure):
            workspace.run_command(['python'])
        for path in ['../outside', '.env', '.baseagent/session.db']:
            with self.assertRaises(ToolFailure):
                workspace.read_file(path)
        self.assertEqual(self.backend.calls, [])

    def test_backend_identity_change_refuses_resume_before_dispatch(self):
        store = SessionStore(self.root/'session.db')
        tools = coding_tools(self.workspace)
        call = {'id': 'read', 'type': 'function', 'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'x.txt'})}}
        model = Model([{'tool_calls': [call]}])
        state = run_agent(model, 'task', tools=tools, store=store, session_id='demo', workspace_root=self.workspace.root, max_steps=1)
        self.assertEqual(state.status, RunStatus.MAX_STEPS_EXCEEDED)
        before = store.load('demo').to_dict()
        self.backend.identity = 'environment-b'
        with self.assertRaises(ValueError):
            run_agent(model, tools=coding_tools(self.workspace), store=store, session_id='demo', workspace_root=self.workspace.root)
        self.assertEqual(model.calls, 1)
        self.assertEqual(store.load('demo').to_dict(), before)

    def test_local_backend_executes_literal_argv_and_keeps_native_cas(self):
        backend = LocalBackend(self.root)
        self.assertIsInstance(backend, WorkspaceBackend)
        workspace = Workspace(backend=backend, allow_write=True, allow_command=True)
        digest = workspace.get_instructions()['digest']
        workspace.write_file('a.txt', 'hello\r\n', 'missing', digest)
        with self.assertRaises(ToolFailure):
            workspace.write_file('a.txt', 'replace', 'missing', digest)
        self.assertEqual((self.root/'a.txt').read_bytes(), b'hello\r\n')
        argv = ['tool', 'a; b', '$HOME', 'space value']
        with patch('baseagent.backends.local.run_process', return_value=ToolResult(data={'exit_code': 0})) as process:
            workspace.run_command(argv, 3)
        self.assertEqual(process.call_args.args, (argv, self.root))
        self.assertEqual(process.call_args.kwargs['timeout'], 3)
        self.assertEqual(backend.contract()['isolation'], 'none')
