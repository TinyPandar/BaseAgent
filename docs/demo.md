# 三分钟演示

不调用付费模型。四条命令都能在本机走完。数字来自已经入库的报告，这次演示不重新计算它们。

## 0:00 带引用的检索

```bash
python -B scripts/demo_knowledge_search.py
```

临时夹具里跑真实的 `knowledge_search`。回答里会出现 `path:start-end` 引用。没有模型请求。

## 0:45 流式、账本、审批、恢复

```bash
python -B scripts/demo_offline.py
```

脚本用假模型走真实 harness，并调用同一次 `--inspect-session`：

1. `--stream` 路径写出 `model_delta`，答案里带 `README.md:1-2`。
2. `verify_command` 在审批前不执行；批准后 `ran.txt` 只写一次，再 resume 不重复。
3. 工具已经产生副作用后被打断，状态先是 `interrupted`，未对账时是 `needs_recovery`，对账后续跑不追加第二行。

对应的 CLI 形状（要等模型配置，现在不要跑）：

```text
python -m baseagent --inspect-session --session demo-stream --db .baseagent/sessions.sqlite3
python -m baseagent --approve-tool CALL_ID --approval-digest DIGEST --session demo-verify
python -m baseagent --session demo-resume --resume
```

## 1:30 全双工主 Agent：并行 Worker、边等边聊、取消、推送

```bash
python -B scripts/demo_duplex.py
```

离线回放，费用 0。脚本用真实的对话入口（`baseagent` 不带任务）加脚本化的输入和假模型，最后一行是 JSON 摘要（`workers: {"w1": "cancelled", "w2": "completed"}`、`chatted_while_running: true`）。讲的时候按这个顺序指给对方看：

1. `> 并行帮我查两件事：入口在哪？有没有测试？` —— 主 Agent 同一轮调用两次 `delegate_task`，每次立刻返回 `{"task_id": "w1", "status": "running"}`，然后回一句「已派出 w1 和 w2」。两个都是只读 Worker，可以并行；`--max-workers` 默认 2，写入 Worker 同一时间只能有一个。
2. `> 顺便问一句，你是谁？` —— 两个 Worker 都还卡在后台，主 Agent 照常回答。Worker 的工具跟踪行（`[w1] tool: grep`）在两次提示之间打印，不会插进正在输入的那一行。
3. `> /status` —— 列出 w1、w2：状态、已用时间、模型调用/步数/工具调用的已用和上限，以及这一轮主 Agent 和 Worker 共用的预算。
4. `> /cancel w1` —— w1 记为 `cancelled`，中文说明已完成的调用留在它自己的会话 `demo-w1` 里。（`run_command` 起的子进程也会被整组杀掉，测试里用睡眠子进程验证过。）
5. `> /wait` —— w2 完成，推送自动出现：`[w2 已完成] …`，带 `引用: tests/test_app.py:1-5`。只有摘要和引用，没有工具原文。
6. `> w2 查到什么了？` —— 推送 JSON 附在这条消息前进了主 Agent 的上下文，它据此转述。推送本身不花模型调用。

对应的真实命令（要等模型配置；付费冒烟未跑，现在不要跑）：

```text
baseagent --session demo --max-workers 2 --budget-cny 0.5
```

设计取舍（拒绝而不排队、线程而不是 asyncio、推送只在提示之间打印）见 [router_worker.md](router_worker.md)。

## 加演：TUI → Web，同一个会话

```bash
uv sync --extra frontends
python scripts/demo_ui.py --fresh        # 终端界面
python scripts/demo_ui.py --ui web       # 同一会话的网页，http://127.0.0.1:8765/
```

离线，费用 0。脚本启动真实的 `baseagent --ui tui` / `--ui web` 入口，只是把模型换成脚本化的假模型；工作区和会话固定在 `.baseagent/demo-ui/`（`--fresh` 清掉重来）。`tests/test_demo_ui.py` 按下面的步骤自动走一遍。讲的时候按这个顺序：

1. **开 TUI。** 左边对话区和输入框，右边 Worker 面板，底栏是会话 id、本轮预算已用/上限和快捷键（F2 取消选中、F3 全部取消、F5 刷新、Ctrl+Q 退出）。
2. **派两个 Worker。** 输入「查一下 TODO，再把 VERSION 写成 1.0」。主 Agent 同一轮派出 w1（写 VERSION，`allow_writes=true`）和 w2（只读扫描，每 3 秒一步），马上回一句话，不等它们。
3. **看面板。** 两行 Worker：状态、已运行时间、模型调用已用/上限；选中一行，下面一栏显示它的任务和最近一行进度。这时可以在输入框里继续聊，推送和进度不会打断正在输入的内容。
4. **批一次审批。** w1 的 `write_file` 弹出模态框：Worker、工具名、参数预览、call id、digest。按 `y`（或点「批准」）。w1 写完，对话区出现 `[w1 已完成] Wrote VERSION (1.0).`。按 `n` 则拒绝，文件不变。
5. **取消一个。** 点一下面板里 w2 那行（或 Tab 到面板再用方向键），按 F2。w2 记为已取消，对话区出现中文说明。然后 Ctrl+Q 退出（如果还有 Worker 在跑，会先问「取消它们并退出 / 继续等待」）。
6. **换 Web 接着看。** `python scripts/demo_ui.py --ui web`，浏览器打开 `http://127.0.0.1:8765/`：同一个会话的对话历史、w1 已完成、w2 已取消都在；再发一条消息，回复从 SSE 推到页面。同一个 `--session` 也能回到 CLI：`baseagent --root .baseagent/demo-ui/workspace --db .baseagent/demo-ui/sessions.sqlite3 --session demo`（需要模型配置；假模型只在脚本里）。

架构、事件表和「为什么用 SSE」见 [ui.md](ui.md)。截图：`docs/images/tui-approval.svg`、`docs/images/tui-workers.svg`（`python scripts/ui_screenshots.py` 重新生成）。

## 加演：改 3 个文件 → `/checkpoints` → `/undo` → `git_status` 干净

```bash
python -B scripts/demo_checkpoints.py
```

离线，费用 0，需要本机有 git。脚本在临时目录里建一个 git 仓库（`app.py`、`README.md` 已提交），用真实的对话入口 `baseagent --mode edit --no-main-agent` 加脚本化输入和假模型，最后一行是 JSON 摘要（`files_restored: true`、`git_clean: true`）。`tests/test_demo_checkpoints.py` 自动走一遍。讲的时候按这个顺序：

1. `> 把 app.py 的问候改成中文，README 跟着改，再加一份 notes.md` —— agent 一步调用三次 `write_file`，回「改好了」。每个文件第一次被写之前，原貌（新建文件记 `missing`）先按 sha256 存进会话库；存不下就不写。
2. `> /checkpoints` —— `第 1 轮 … 3 个文件`，下面是 `修改 app.py`、`修改 README.md`、`新建 notes.md`。主 Agent 模式下还会标出是哪个 worker 写的。
3. `> /undo` —— 「已撤回第 1 轮的文件改动：恢复 2 个文件，删除 1 个新建文件」。写回前逐个比对写后哈希，有人手改过就整批不动、要 `--force`；`run_command` 的副作用不在范围内，这句话直接打在结果里。
4. `> 现在 git 状态干净吗？` —— 这一轮开头 agent 先收到一句「第 1 轮的文件改动已撤回」，然后调用只读工具 `git_status`（`git status --porcelain=v1` 的解析结果），回答「干净：分支 main 上没有改动」。

同样的事不进对话也能做：`baseagent --session demo --checkpoints`、`baseagent --session demo --rewind last`（不调模型、不要密钥）。为什么按文件快照而不用影子 git，见 [checkpoints.md](checkpoints.md)。

## 加演：MCP 工具 → 审批 → 调用成功

```bash
python -B scripts/demo_mcp.py
```

离线，费用 0，不联网。脚本在临时工作区里写一份 `.mcp.json`，指向仓库里的假 stdio 服务器 `tests/fixtures/fake_mcp_server.py`，再用真实的对话入口 `baseagent --mode edit --no-main-agent` 加脚本化输入和假模型。最后一行是 JSON 摘要（`note_written: true`、`server_stopped: true`）。`tests/test_demo_mcp.py` 自动走一遍。讲的时候按这个顺序：

1. `[stderr] MCP: server 'demo' ready, 7 tool(s): mcp__demo__echo, mcp__demo__add_note, …` —— 启动时握手、`tools/list`，工具以 `mcp__<服务器>__<工具>` 注册进同一个 Registry。摘要里的 `mcp_tools` 是模型实际收到的工具名。
2. agent 先调 `mcp__demo__echo`。配置里标了 `readOnly: true`，直接执行，不问。
3. agent 再调 `mcp__demo__add_note`。没标只读，对话停下来：`Approval required: mcp__demo__add_note`，下面是参数和 call id，然后 `Allow mcp__demo__add_note? [y/N]`。回 `y` 之前笔记文件不存在；回 `y` 之后才发 `tools/call`，agent 回「笔记已记下」。
4. `/exit` 后服务器进程已经退出（`server_stopped: true`）。

走的是和 `write_file` 一样的那条路径：同一套策略、审批、账本和钩子。MCP 工具的副作用 `/undo` 撤不回。细节见 [mcp.md](mcp.md)。

## 2:00 指标

```bash
python scripts/evaluate_suite.py offline
```

报告写到 `applications/project_assistant/evaluation/reports/suite/eval-<timestamp>.md`。已提交摘要在 `docs/reports/phase1-eval.md`。费用是 0。

已经测过、这次不重跑的数字：

| 指标 | 数字 | 来源 |
| --- | ---: | --- |
| coding 成功率 | 5/5（基线 2/3） | `docs/reports/m1_coding_comparison-20261102T111829Z-b21f9bdd6a3745a4b572a5e3dcf309e1.md` |
| 平均 token | 80780 → 36185.4（−55.2%） | 同上，五次 after 平均约 0.030 元 |
| test hybrid MRR | 0.845 | `applications/project_assistant/evaluation/reports/bailian-80q-summary-2026-10-21.md` |

rerank 默认关闭：R@5 从 0.782 降到 0.770，每次查询大约 1.4 万 token。

生成质量这条离线报告用的是合成答案，只证明打分管道。引用、拒答和词面重叠分开记，不是模型分数。
