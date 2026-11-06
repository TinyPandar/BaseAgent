# Router / Worker

一个入口，两个隔离 Worker。Router 只分类和转交，不改文件，也不跑高风险命令。直接调用 kernel 的旧 CLI 仍然可用。

## 职责

| 角色 | 做什么 | 不做什么 |
| --- | --- | --- |
| Router | 白盒规则分类，选 `qa` 或 `coding`，裁剪回传 | 不调用 `edit_file` / `write_file` / `run_command` |
| qa | 只读检索和引用回答 | 没有写入工具 |
| coding | 计划、编辑、审批、`verify_command` | 不绕过原有审批和账本 |

不确定的请求先澄清，或默认走 `qa`。不会默认走 `coding`。让 Router 自己改文件会被拒绝，并转给 coding Worker。

LLM 分类器可以另接，测试和默认路径不调用它。

```bash
python -m applications.project_assistant route "knowledge_search 怎么防越权"
python -m applications.project_assistant route "Fix sample.py and run the tests"
```

## 上下文

Worker 复用现有 kernel。子任务有自己的账本和预算，祖先预算由 `ExecutionScope` 在真正派发时共享。Router 在派发前用 `SharedBudget.reserve` 做原子预留，避免两个 Worker 同时占满同一额度。

Worker 结束时只回传摘要、引用、call id 和 spill ref。完整工具输出留在 Worker 账本里。

失败时：

- 写入仍停在 `awaiting_approval`，文件不会先被改。
- 已完成的 Worker 可以从 session 恢复，已执行的工具不重复。
- 子账本用 `python -m baseagent --task-tree --session <id> --db <db>` 查看，和 Router 账本分开。

## 演示

```bash
python -B scripts/demo_router.py
```

离线，费用 0。先把防越权问题交给 qa，再把改文件并测试交给 coding。coding 的编辑会停在审批，两个 Worker 的名字出现在不同的 task node 里。
