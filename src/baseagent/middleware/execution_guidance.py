"""Project actual capabilities and stop repeated, unchanged tool rounds."""

from dataclasses import replace
import json

from baseagent.agent.errors import BudgetExceeded
from baseagent.agent.state import RunStatus
from .middleware import AgentMiddleware


TASK_GUIDANCE = """Follow the user's actual request. For general questions, explain directly;
do not turn them into repository inspection, a coding plan, or document creation.
Only inspect workspace files when the question concerns those files or the user
asks for changes. Workspace search searches local files, not the internet. There
is no web search tool here: disclose that limitation when current external research
is required. Do not invent citations or claim to have browsed. Use tools only when
they materially help this request, and stop exploring once you can answer.
Do not retry unchanged requests after non-retryable errors. Approval does not grant
disabled capabilities. If blocked, explain the limitation and what is needed.
"""


def unchanged_rounds(state):
    """Compare completed tool rounds including arguments AND outcomes, within this turn."""
    rounds = []
    calls = None
    results = {}
    for message in state.messages[state.turn_start:]:
        if message['role'] == 'assistant':
            calls = message.get('tool_calls')
            results = {}
        elif message['role'] == 'tool' and calls:
            results[message['tool_call_id']] = message['content']
            if all(call['id'] in results for call in calls):
                batch = []
                for call in calls:
                    fn = call['function']
                    try:
                        args = json.loads(fn['arguments'])
                    except (ValueError, TypeError):
                        args = fn['arguments']
                    try:
                        outcome = json.loads(results[call['id']])
                    except (ValueError, TypeError):
                        outcome = results[call['id']]
                    batch.append(json.dumps([fn['name'], args, outcome], sort_keys=True, ensure_ascii=False))
                rounds.append(tuple(sorted(batch)))
                calls = None
    count = 0
    for batch in reversed(rounds):
        if batch != rounds[-1]:
            break
        count += 1
    return count


class ExecutionGuidanceMiddleware(AgentMiddleware):
    def __init__(self, workspace):
        self.workspace = workspace

    def before_model(self, state):
        if unchanged_rounds(state) >= 3:
            raise BudgetExceeded(RunStatus.TOOL_LOOP_DETECTED)

    def wrap_model_call(self, request, handler):
        disabled = set()
        if not self.workspace.allow_write:
            disabled.update(('write_file', 'edit_file'))
        if not self.workspace.allow_command:
            disabled.update(('run_command', 'verify_command'))
        if not self.workspace.allow_memory_publish:
            disabled.update(('publish_memory', 'withdraw_memory'))
        instructions = TASK_GUIDANCE + '\nDisabled tools: ' + ', '.join(sorted(disabled))
        if unchanged_rounds(request.state) >= 2:
            instructions += '\nThe last two tool rounds returned identical outcomes. Answer using available evidence or explain the blocker; do not repeat that round.'
        messages = list(request.messages)
        messages.insert(1 if messages and messages[0]['role'] == 'system' else 0,
                        {'role': 'system', 'content': instructions})
        return handler(replace(request, messages=messages,
                               tools=[tool for tool in request.tools if tool['function']['name'] not in disabled]))
