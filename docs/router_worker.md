# 主沟通 Agent + 通用执行 Worker

无参数连续对话（`baseagent`，以及不带问题的 `python -m applications.project_assistant ask`）默认是主沟通 Agent。它只和人对话、分派任务，自己不读文件、不改文件、不跑命令。需要动手时，它派生一个通用执行 Worker。一次性 `baseagent "task"` 仍是原来的单 Agent 路径，不走双层，也不需要 `--subtasks-config`。

## 职责

| 角色 | 做什么 | 不做什么 |
| --- | --- | --- |
| 主 Agent | 闲聊和澄清直接回答；需要仓库时调用唯一的工具 `delegate_task(task, allow_writes=false)`；转述 Worker 的汇报 | 没有 `read_file` / `grep` / `glob` / `edit_file` / `write_file` / `run_command`；Worker 成功后不重做同一件事 |
| Worker | 用入口已授权的工具完成任务，最后写一段汇报 | 不直接对用户说话；不能再派生 Worker；不会多出入口没有的工具 |

Worker 没有类型。`qa` / `coding` / `reader` 不是 Worker 种类，也不是用户选项。区别只在本次是否允许写入：

- `allow_writes=false`（默认）：Worker 只拿到入口工具里的只读子集（`retry_safe` 的工具，如 `read_file`、`grep`、`glob`、`git_diff`，Project Assistant 入口还有 `knowledge_search` 和 git 历史工具）。
- `allow_writes=true`：只在用户明确要求改文件或跑命令时使用。Worker 拿到入口工具全集。写入仍要过 `--allow-write` / `--allow-command` 和 `--tool-policy`，要审批的调用照常停在 `awaiting_approval`。

不确定是问答还是改代码（例如「能不能帮我处理一下这个文件」）时，主 Agent 先问一句，不派生写入。即使模型直接派生了，没写 `allow_writes` 也拿不到写入工具。

主 Agent 的提示词约束模型这样做，这个约束不是硬保证；硬保证是工具面：主 Agent 只有 `delegate_task`，只读 Worker 没有写入工具。

## 一次派生的流程

1. 主 Agent 调用 `delegate_task`。在现有 schema v6 里创建一个子节点（名字固定为 `worker`），有独立的 turn、账本和上下文窗口。
2. 预算从父级预留：Worker 的模型调用上限 = min(12, 祖先剩余模型调用 − 1)，留 1 次给主 Agent 回复用户；工具调用上限 = min(24, 祖先剩余工具调用)。剩余不够时派生直接失败，主 Agent 用已有信息回答。`ExecutionScope` 在每次真正派发时仍检查所有祖先额度。
3. Worker 继承父级的 wrap-up 提示、spill 和 compaction 设置，以及父级中间件。
4. 结束时主 Agent 只收到：`task_id`、`status`、`summary`（Worker 的汇报，最多 4000 字符）、`citations`、`paths`、`call_ids`、`spill_refs`。完整工具输出留在子节点账本和 spill 文件里，需要时再派一个 Worker 按路径读回。
5. 一次只跑一个 Worker，做完、停在审批或失败后才回到主 Agent。不做并行 Worker 池。

## 审批、失败和 `/status`

- **审批**：Worker 的待批准调用直接显示在对话里（`Approval required: write_file`，并标明 `requested by: worker xxxxxxxx`），按 `y` / `N` 回答即可，不用手填 `--task-id`。批准后续跑同一个子节点，父级这次 `delegate_task` 不会重复计一次工具尝试。退出后再用同一 `--session` 进对话，会先问这条待批准调用。
- **可续跑的停止**（模型或工具出错、取消、Ctrl+C）：父回合暂停。对话用中文说明哪个 Worker 停了、为什么，下一条消息会先继续这个 Worker，已完成的工具调用不重做。
- **Worker 自己的上限用完**（步数、模型调用、工具调用、重复循环、上下文超限）：原样续跑只会再次停下，所以这个节点被关闭（`closed_by_parent`），主 Agent 收到一个失败结果并告诉用户，对话仍可继续。终端另外打印一段中文说明。
- **`/status`**：除步数/模型/工具用量外，还显示本轮最近一个 Worker 的状态：运行中 / 等待审批 / 已完成 / 失败（含具体状态）/ 已结束（失败），以及它自己的用量。

`--task-tree` 和 `--task-id` 节点恢复命令保留给排障：

```bash
python -m baseagent --task-tree --session <id> --db <db>
```

## 成本

派生会多花模型调用：一次需要动手的请求至少是「主 Agent 分派 1 次 + Worker 若干次 + 主 Agent 回复 1 次」，比单 Agent 多 2 次左右，都是付费 API 调用。连续对话默认上限仍是 `--max-steps 16` / `--max-model-calls 16`，这个上限同时覆盖主 Agent 和 Worker（Worker 的调用计入祖先）。主模型和 Worker 默认用同一个 `.env` 模型。

## 模式开关

- 新的连续对话默认开启主 Agent。`--no-main-agent` 回到以前自己执行工具的单 Agent 对话。
- 已有会话保持创建时的模式（按系统提示判断），`--main-agent` / `--no-main-agent` 对它不生效并给出提示。
- `--main-agent` 只用于连续对话；和 `--subtasks-config` 互斥。
- `--subtasks-config` / `run_subtask(name, prompt)` 仍是库接口和显式调试入口（预命名配方），不是对话的产品路径。

## 调试分类器

`route` 是白盒规则，只用于调试和演示，不参与对话决策。输出 `action`（`answer` / `delegate` / `clarify`）和 `writes`，不输出 Worker 类型：

```bash
python -m applications.project_assistant route "hi"                              # answer
python -m applications.project_assistant route "knowledge_search 怎么防越权"       # delegate, writes=false
python -m applications.project_assistant route "Fix sample.py and run the tests"   # delegate, writes=true
python -m applications.project_assistant route "能不能帮我处理一下这个文件"          # clarify
```

## 演示

```bash
python -B scripts/demo_router.py
```

离线，费用 0。同一个会话三轮：`hi` 不派生；防越权问题派生一个只读 Worker，主窗口只有摘要和引用，看不到工具原文；改文件并测试派生一个可写 Worker，编辑停在审批。

## 实现位置

- `src/baseagent/agent/router.py`：`MAIN_AGENT_PROMPT`、`WorkerRuntime`、`worker_report`、调试用 `route`。
- `WorkerRuntime` 继承 `SubtaskRuntime`，复用节点创建、环检测、深度/数量上限、子结果缓存、managed delegation recovery 和策略交集。
- `src/baseagent/cli_chat.py`：子节点审批、中文失败说明、`/status` 的 Worker 行。
