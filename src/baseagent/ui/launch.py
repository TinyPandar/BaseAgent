"""Launch the optional local-only Chainlit UI."""

import argparse
import json
import importlib.util
from importlib.metadata import version
import os
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description='BaseAgent 浏览器聊天界面')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--db', type=Path)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--allow-write', action='store_true')
    parser.add_argument('--allow-command', action='store_true')
    parser.add_argument('--model')
    parser.add_argument('--max-steps', type=int, default=8)
    parser.add_argument('--max-total-tokens', type=int)
    parser.add_argument('--tokenizer-file', type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.max_steps < 1 or (args.max_total_tokens is not None and args.max_total_tokens < 1):
        parser.error('端口、轮数或 token 预算无效')
    if args.tokenizer_file and not args.max_total_tokens:
        parser.error('tokenizer-file 需要 max-total-tokens')
    root = args.root.resolve(strict=True)
    if not root.is_dir():
        parser.error('root 必须是目录')
    if importlib.util.find_spec("chainlit") is None:
        parser.error("请先运行 uv sync --extra ui")
    directory = root/'.baseagent'/'ui-runtime'/uuid4().hex
    directory.mkdir(parents=True)
    (directory/'chainlit.md').write_text('# BaseAgent\n\n直接输入任务开始。工具执行记录与审批会显示在聊天中。', encoding='utf-8')
    framework = directory/'.chainlit'
    framework.mkdir()
    (framework/'config.toml').write_text('[project]\nallow_origins = ["http://127.0.0.1:'+str(args.port)+'", "http://localhost:'+str(args.port)+'"]\n[features]\nedit_message = false\n[features.spontaneous_file_upload]\nenabled = false\n[UI]\nname = "BaseAgent"\n[meta]\ngenerated_by = "'+version("chainlit")+'"\n', encoding='utf-8')
    policy = directory/'policy.json'
    rules = {name: 'ask' for name in ('write_file', 'edit_file', 'run_command', 'verify_command')}
    if not args.allow_write:
        rules.update(write_file='deny', edit_file='deny')
    if not args.allow_command:
        rules.update(run_command='deny', verify_command='deny')
    policy.write_text(json.dumps({'default': 'allow', 'tools': rules}), encoding='utf-8')
    agent_args = ['--tool-policy', str(policy), '--max-steps', str(args.max_steps)]
    for flag, value in [('allow-write', args.allow_write), ('allow-command', args.allow_command)]:
        if value:
            agent_args.append('--'+flag)
    for flag, value in [('model', args.model), ('max-total-tokens', args.max_total_tokens)]:
        if value is not None:
            agent_args += ['--'+flag, str(value)]
    if args.tokenizer_file:
        agent_args += ['--estimate-model', '--tokenizer-file', str(args.tokenizer_file.resolve(strict=True))]
    config = directory/'settings.json'
    config.write_text(json.dumps({'root': str(root), 'db': str((args.db or root/'.baseagent'/'sessions.sqlite3').resolve()), 'agent_args': agent_args}), encoding='utf-8')
    os.environ['BASEAGENT_UI_SETTINGS'] = str(config)
    # Keep framework-generated settings and assets out of the repository root.
    os.environ['CHAINLIT_APP_ROOT'] = str(directory)
    os.environ['CHAINLIT_HOST'] = '127.0.0.1'
    os.environ['CHAINLIT_PORT'] = str(args.port)
    from chainlit.cli import cli
    cli.main(args=['run', str(Path(__file__).with_name('app.py')), '--headless', '--host', '127.0.0.1', '--port', str(args.port)], prog_name='baseagent-ui', standalone_mode=False)


if __name__ == '__main__':
    main()
