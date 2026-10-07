"""Chainlit supplies the frontend; callbacks adapt the existing harness."""

import asyncio
import json
import os

import chainlit as cl

from baseagent.jsondata import read_json_object
from baseagent.ui.controller import ChatController


SETTINGS = read_json_object(os.environ['BASEAGENT_UI_SETTINGS'], max_bytes=20_000, label='UI settings')

STOP_REASONS = {
    'max_steps_exceeded': '已达到本轮模型轮数上限。不会自动继续或重置额度。请新建聊天明确任务；需要增加额度时使用 CLI。',
    'max_model_calls_exceeded': '已达到模型调用上限，请通过 CLI 检查或调整额度。',
    'max_tool_calls_exceeded': '已达到工具调用上限，请通过 CLI 检查或调整额度。',
    'tool_loop_detected': '连续三轮工具请求和结果完全相同，已停止重复调用。请新建聊天调整任务或用 CLI 核验后继续。',
    'needs_recovery': '工具结果不确定，需先用 CLI 核验和恢复，不能直接再次执行。',
}


def controller():
    return cl.user_session.get('controller')


async def show_state(state=None):
    ctrl = controller()
    state = state or await asyncio.to_thread(ctrl.state)
    if state is None:
        await cl.Message(content=f'会话：`{ctrl.session_id}`。发送任务即可开始。').send()
        return
    actions = []
    approval = await asyncio.to_thread(ctrl.approval)
    if approval:
        request = approval.pop('request')
        await cl.Message(content='待审批工具请求：\n```json\n'+json.dumps(request, ensure_ascii=False, indent=2)+'\n```', actions=[
            cl.Action(name='approve', payload=approval, label='允许这次调用'),
            cl.Action(name='deny', payload=approval, label='拒绝这次调用')]).send()
    elif state.status not in ('completed', 'running') and state.status not in STOP_REASONS:
        actions.append(cl.Action(name='resume', payload={'session_id': ctrl.session_id}, label='继续当前任务'))
    status = f'会话：`{ctrl.session_id}` · 状态：`{state.status}` · 模型轮数：{state.step}/{state.max_steps} · 工具执行次数：{state.tool_calls}/{state.max_tool_calls} · tokens：{state.total_tokens} · 未知用量：{state.unknown_usage_calls}'
    if state.status in STOP_REASONS:
        status += '\n\n' + STOP_REASONS[state.status]
    if state.status in ('workspace_changed', 'failed'):
        actions.append(cl.Action(name='accept_changes', payload={'session_id': ctrl.session_id}, label='接受配置/工作区变化并继续'))
    await cl.Message(content=status, actions=actions).send()


async def render_events(ctrl):
    cursor = cl.user_session.get('event_cursor', 0)
    events = await asyncio.to_thread(ctrl.events, cursor)
    for event in events:
        kind = event['type']
        if kind in ('model_started', 'model_returned', 'tool_started', 'tool_completed', 'verification_completed', 'approval_requested'):
            async with cl.Step(name=kind, type='tool' if 'tool' in kind else 'run') as step:
                step.output = json.dumps(event['data'], ensure_ascii=False)
        cursor = event['sequence']
    cl.user_session.set('event_cursor', cursor)


async def execute(prompt=None, *, accept_changes=False):
    ctrl = controller()
    if ctrl.lock.locked():
        await cl.Message(content='当前任务正在运行，请先停止。').send()
        return
    if prompt is None:
        await asyncio.to_thread(ctrl.resume_cancelled)
    task = asyncio.create_task(asyncio.to_thread(ctrl.run, prompt, accept_changes=accept_changes))
    # A stopped callback can leave the bounded worker finishing in the background.
    # Retrieve its exception even when this callback no longer awaits it.
    task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
    cl.user_session.set('running_task', task)
    try:
        while not task.done():
            await render_events(ctrl)
            await asyncio.sleep(0.3)
        state = await task
        # Drain all committed metadata, including a batch after the final poll.
        while await asyncio.to_thread(ctrl.events, cl.user_session.get('event_cursor', 0)):
            await render_events(ctrl)
        if state.final_answer:
            await cl.Message(content=state.final_answer).send()
        await show_state(state)
    except asyncio.CancelledError:
        await asyncio.to_thread(ctrl.stop)
        raise
    except (ValueError, OSError) as exc:
        await cl.Message(content=str(exc)).send()
    finally:
        # Keep the worker reference when Chainlit cancels this callback.
        if task.done():
            cl.user_session.set('running_task', None)


@cl.on_chat_start
async def start():
    cl.user_session.set('controller', ChatController(SETTINGS))
    cl.user_session.set('event_cursor', 0)
    await cl.Message(content='**BaseAgent**\n\n发送任务开始，一般问题可直接回答。当前没有联网搜索工具，文件搜索只查本地工作区。`/sessions` 查看会话，`/load 会话ID` 恢复历史，`/status` 查看状态，`/resume` 继续暂停任务。写文件和命令权限由启动参数配置，已启用的敏感操作需逐次审批。').send()
    await show_state()


@cl.on_message
async def message(message: cl.Message):
    ctrl = controller()
    try:
        text = message.content.strip()
        if text == '/sessions':
            rows = await asyncio.to_thread(ctrl.store.list_sessions, limit=100)
            rows = [row for row in rows if row.get('workspace_root') == str(ctrl.root)]
            await cl.Message(content='```json\n'+json.dumps(rows, ensure_ascii=False, indent=2)+'\n```').send()
        elif text.startswith('/load '):
            state = await asyncio.to_thread(ctrl.load, text[6:].strip())
            cl.user_session.set('event_cursor', 0)
            for item in state.messages:
                if item['role'] in ('user', 'assistant') and item.get('content'):
                    await cl.Message(content=item['content'], author='用户' if item['role'] == 'user' else 'BaseAgent', type='user_message' if item['role'] == 'user' else 'assistant_message').send()
            await show_state(state)
        elif text == '/status':
            await show_state()
        elif text == '/resume':
            await execute()
        elif text:
            await execute(text)
    except (ValueError, OSError) as exc:
        await cl.Message(content=str(exc)).send()


@cl.action_callback('approve')
@cl.action_callback('deny')
async def approval_action(action):
    try:
        await asyncio.to_thread(controller().decide, action.payload, 'allow' if action.name == 'approve' else 'deny')
        await action.remove()
        await execute()
    except ValueError as exc:
        await cl.Message(content=str(exc)).send()


@cl.action_callback('resume')
@cl.action_callback('accept_changes')
async def resume_action(action):
    if action.payload.get('session_id') != controller().session_id:
        await cl.Message(content='按钮属于其他会话，请查看当前状态。').send()
        return
    await execute(accept_changes=action.name == 'accept_changes')


@cl.on_stop
@cl.on_chat_end
async def stop():
    ctrl = controller()
    if ctrl and ctrl.lock.locked():
        await asyncio.to_thread(ctrl.stop)
