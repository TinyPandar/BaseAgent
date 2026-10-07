"""Use the existing CLI and SQLite ledger as the UI's execution authority."""

import json
from pathlib import Path
import re
import sys
import threading
from uuid import uuid4

from baseagent.agent.cancellation import CancellationToken, Cancelled
from baseagent.session import SessionStore
from baseagent.tools.process import run_process


class ChatController:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings['root']).resolve(strict=True)
        self.store = SessionStore(settings['db'])
        self.session_id = uuid4().hex
        self.lock = threading.Lock()
        self.token = None

    def state(self):
        state = self.store.load(self.session_id)
        if state and state.workspace_root != str(self.root):
            raise ValueError('会话不属于当前工作区')
        return state

    def load(self, identifier):
        if not self.lock.acquire(blocking=False):
            raise ValueError('请先停止当前任务')
        try:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
                raise ValueError('会话 ID 无效')
            state = self.store.load(identifier)
            if state is None or state.workspace_root != str(self.root):
                raise ValueError('会话不存在或不属于当前工作区')
            self.session_id = identifier
            return state
        finally:
            self.lock.release()

    def command(self, prompt=None, *, accept_changes=False):
        command = [sys.executable, '-m', 'baseagent', '--root', str(self.root),
                   '--db', str(self.store.path), '--session', self.session_id,
                   *self.settings.get('agent_args', [])]
        if accept_changes:
            command += ['--accept-config-changes', '--accept-workspace-changes']
        if prompt is None:
            command += ['--resume']
        else:
            command += ['--', prompt]
        return command

    def run(self, prompt=None, *, accept_changes=False):
        if not self.lock.acquire(blocking=False):
            raise ValueError('当前会话正在运行')
        self.token = CancellationToken()
        try:
            state = self.state()
            if prompt is None and state and state.status in {
                'max_steps_exceeded', 'max_model_calls_exceeded', 'max_tool_calls_exceeded',
                'tool_loop_detected', 'needs_recovery',
            }:
                # The UI cannot extend limits or reconcile uncertain effects.
                return state
            if prompt is not None and state and state.status != 'completed':
                raise ValueError('当前任务尚未完成，请先继续或新建聊天')
            result = run_process(self.command(prompt, accept_changes=accept_changes), self.root,
                                 timeout=3600, output_limit=4000, cancellation=self.token)
            state = self.state()
            if state is None:
                # Avoid publishing raw provider/exception output to the browser.
                raise ValueError('Agent 未创建会话，请检查启动配置与本机终端日志')
            if not result.ok and state.status == 'running':
                raise ValueError('执行进程已停止，状态可能需要核验；请查看会话详情')
            return state
        except Cancelled:
            raise ValueError('任务已停止；可查看状态或继续任务') from None
        finally:
            self.token = None
            self.lock.release()

    def stop(self):
        state = self.state()
        if state and state.status != 'completed':
            return self.store.request_cancellation(self.session_id)
        if self.token:
            self.token.cancel()
        return None

    def resume_cancelled(self):
        state = self.state()
        if state:
            request = self.store.cancellation(self.session_id, state.turn_id)
            if request:
                self.store.clear_cancellation(self.session_id, request)

    def approval(self):
        state = self.state()
        if not state:
            return None
        approval = state.metadata.get('approval_request')
        if state.status != 'awaiting_approval' or not approval:
            return None
        record = self.store.call(state, approval['call_id'])
        return {'session_id': self.session_id, 'turn_id': state.turn_id,
                'call_id': approval['call_id'], 'digest': approval['request_digest'],
                'request': json.loads(record['request_json'])['function']}

    def decide(self, payload, decision):
        current = self.approval()
        if not current or any(payload.get(key) != current[key] for key in ('session_id', 'turn_id', 'call_id', 'digest')):
            raise ValueError('审批请求已变化，请查看当前待审批项')
        self.store.decide_tool(self.session_id, current['call_id'], decision, current['digest'])

    def events(self, after=0):
        return self.store.events(self.session_id, after=after, limit=100)
