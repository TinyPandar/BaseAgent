# 对标框架对照（Codex / Claude Code / DeepSeek Harness）

> 更新日期：2026-11-19。用途：为 [ROADMAP.md](../ROADMAP.md) 的设计决定提供参照，不是性能基准（性能见 [PERFORMANCE.md](../PERFORMANCE.md)）。
>
> **可信度标记：**
> - ✅ 官方文档，直接读到
> - ⚠️ 第三方文章或解读，待对照官方文档/源码
>
> Codex 官方文档页本次无法读到，该栏全部为 ⚠️。DeepSeek Harness 的官方 `docs/architecture.md` 本次返回 503，该栏为 ⚠️。

## 对照表

| 议题 | 我们现状 | Claude Code | Codex | DeepSeek Harness (dsh) |
| --- | --- | --- | --- | --- |
| 权限模式 | `--mode read-only/edit/full` 三档 | ✅ 6 种：`default`、`acceptEdits`、`plan`、`auto`、`dontAsk`、`bypassPermissions` | ⚠️ 沙箱与审批拆成两个轴。沙箱：`read-only` / `workspace-write` / `danger-full-access`；审批：`on-request` / `never`（`untrusted` 各来源说法不一） | ⚠️ 沙箱三档同 Codex；`tools/pre-execute` 返回 allow/deny/ask |
| 规则优先级 | `ToolPolicy` 只能收紧 | ✅ deny 规则在**所有模式下生效，包括 bypass**；显式 ask 规则、关键路径 `rm` 也始终拦截 | ⚠️ 审批与沙箱组合决定 | ⚠️ guard 只能收紧，之后不能放宽 |
| LLM 分类器 | 无；可选外部 HarnessKnight | ✅ `auto` 模式：①用户规则 → ②内置自动放行（只读、工作区内编辑） → ③分类器。默认拦 `curl \| bash`、force push、生产部署等。连续 3 次或累计 20 次拦截后退回人工；分类器失败/无裁决一律拒绝；进入 auto 时撤销 `Bash(*)` 这类宽泛 allow | ⚠️ 有 Auto-review 的说法，细节未读到 | 无 |
| OS 沙箱 | 无（仅 Docker 后端） | ✅ 基于 `sandbox-runtime`：macOS Seatbelt、Linux/WSL2 bubblewrap；**默认关闭**；原生 Windows 不支持。网络走本地代理 + 域名白名单；`.claude/settings*`、`.git/hooks` 等配置文件即使在可写目录也禁写。有 `dangerouslyDisableSandbox` 逃生口，可用 `allowUnsandboxedCommands:false` 关掉 | ⚠️ Seatbelt/Landlock；默认无网络；`.git`、`.agents`、`.codex`、`.aws` 在可写根内仍只读 | ⚠️ bubblewrap/Landlock（Linux）、Seatbelt（macOS）、受限令牌+ACL（Windows）；**无可用后端时 fail closed**（`SANDBOX_UNAVAILABLE`）；只管文件系统，不管网络 |
| 沙箱与权限的关系 | 混在 `--mode` / `--tool-policy` | ✅ 互补两层：权限决定工具调用是否执行，沙箱限制命令执行后能碰什么；文件工具、MCP、hooks 不在沙箱内 | ⚠️ 两个独立的轴 | ⚠️ 同左 |
| 运行中插话 | 直接拒绝（`send()` 抢不到锁就报错） | ✅ 默认**排队**：工具调用结束后同一轮内送达，轮次结束后依序发出；Esc 中断并保留已做工作 | ⚠️ **Enter 注入当前轮**（下一个思考步）；**Tab 排队到下一轮**；0.159 起可选 `instant_interrupt` | ⚠️ FIFO inbox，下一步边界接纳；steering 与取消是实时 `agent/*` 控制事件，与持久事实分开 |
| 长命令 / 训练 | 同步；默认 20 秒超时即失败；取消杀子进程 | ✅ 后台 Bash（Ctrl+B 转后台，`/tasks` 管理）；**超时的命令转入后台而不是被杀**（`sleep` 开头除外）；Claude 可在命令运行时回应新输入 | ⚠️ `unified_exec` 后台终端，`/ps` 查看、`/stop` 停止；进程可比当轮存活更久 | ⚠️ `job_list` / `job_output` / `job_kill`；owner 范围的统一注册表；完成通知可唤醒空闲 owner 或进入忙碌 owner 的下一步 inbox |
| 子 agent 上下文 | 只有"新建"，只带任务字符串 | ✅ 默认 **fresh**（系统提示 + 任务消息 + CLAUDE.md + git 状态）；**fork 继承父对话和完全相同的工具池**，跳过工具过滤 | ⚠️ `spawn_agent` 的 `fork_turns: "none" \| "all" \| <整数>`；子 agent 继承父轮的实时沙箱/审批覆盖；`/fork` 不等于 worktree | ⚠️ 多种 provider（进程内 spawn/fork、ACP、Codex、Claude Code、SDK）；fork 语义未读到 |
| 子 agent 并行与回流 | 并发 2；单写者；深度 1 | ✅ 并发上限默认 20，嵌套深度默认 3；默认后台运行；结果以完成通知回流；`SendMessage` 可续接；转写存 jsonl | ⚠️ `send_message`（入队）、`followup_task`（入队并触发，可 `interrupt:true`） | ⚠️ 可续接的持久子会话 |
| 会话数据模型 | `messages[]`（OpenAI 风格 dict） | jsonl 转写 | — | ⚠️ **追加式事件日志**（`turn/start`、`step/start`、`user/message`、`tool/call`、`tool/result`、`turn/end` 等），视图由日志派生；压缩只改视图，原始事实保留 |
| 工具管线 | 策略判定 + 审批 + 账本 | hooks 可在调用前后拦截 | — | ⚠️ 8 步：call → `pre-execute` → guard → 执行包装（超时/重试）→ 校验 → `post-execute` → 规范化 → result；默认串行，工具声明 `isConcurrencySafe` 才并发 |

## 对我们的启发

1. **权限做成三层：** 规则（deny 任何模式都生效）→ OS 沙箱（不可用时 fail closed）→ 可选分类器（规则优先，分类器兜底，连续拦截退回人工，失败即拒绝）。
2. **长命令超时转后台，不杀进程。** 用户插话、对话轮次结束都不应终止有副作用的进程；停止必须是明确动作。
3. **插话有两种语义：** 注入当前轮 / 排队到下一轮，都要支持。
4. **Worker 上下文用 `fork_turns: none / N / all`**，比 fresh/summary/fork 三档更连续。fork 想便宜，需要与父 agent 共享相同的系统提示和工具列表（缓存前缀），这与"子 agent 工具更少"的权限设计冲突；Claude Code 的 fork 明确继承完全相同的工具池，印证了这一点。
5. **子 agent 继承父级的实时权限设置**，不能通过派生绕开限制。
6. **待决：** 内部以"消息"为主，还是像 dsh 那样以"事件日志"为主、消息只是投影。

## 来源

- Claude Code：[权限模式](https://code.claude.com/docs/en/permission-modes)、[沙箱](https://code.claude.com/docs/en/sandboxing)、[子 agent](https://code.claude.com/docs/en/sub-agents)、[交互模式](https://code.claude.com/docs/en/interactive-mode)
- DeepSeek Harness：[官方仓库](https://github.com/deepseek-ai/deepseek-harness/)、[inside-deepseek-harness（第三方解读）](https://github.com/RenatoMignone/inside-deepseek-harness)
- Codex（第三方）：[安全模型：两轴框架](https://codex.danielvaughan.com/2026/04/08/codex-cli-security-model-approval-sandbox-two-axis/)、[Steering 工作方式](https://x.com/nepinepimate3/article/2079569651856802017)、[多 agent 编排 v2](https://codex.danielvaughan.com/2026/04/11/codex-cli-multi-agent-orchestration-v2-complete-guide/)、[后台终端比轮次存活更久（vercel/ai issue）](https://github.com/vercel/ai/issues/24831)
- 官方入口（本次未读到有效内容）：Codex 仓库 `openai/codex` 与其文档站；DeepSeek Harness 的 `docs/architecture.md`、`SAFETY.md`

## 待办（核实）

- [ ] 读 Codex 官方文档与源码（`openai/codex`），把 ⚠️ 换成 ✅
- [ ] 读 dsh `docs/architecture.md` 与 `SAFETY.md`，确认 fork 语义、steering API、job 的沙箱约束
