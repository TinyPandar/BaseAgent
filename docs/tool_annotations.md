# 工具注解(Tool annotations)与关键路径删除拦截

## 注解记录

每个工具在注册时带一条不可变的 `ToolAnnotations`(`baseagent.kernel.tools`),语义对齐 MCP 的
ToolAnnotations:

| 字段 | 含义 | 未注解时的默认值 |
| --- | --- | --- |
| `read_only` | 不修改任何环境 | `False` |
| `destructive` | 可能删除或覆盖已有数据(仅对非只读工具有意义) | `True` |
| `idempotent` | 同样参数重复调用不产生额外效果(仅对非只读工具有意义) | `False` |
| `open_world` | 可能接触工作区和会话之外的外部世界(网络、任意程序) | `True` |

默认值就是 MCP 的保守默认:未注解的工具视为"非只读、可能破坏、非幂等、开放世界"。
`read_only=True` 时 `destructive` 必须为 `False`,否则注册报错。

注解**只是宿主侧元数据**:不进入发给模型的工具 schema(`ToolRegistry.specs()`),也不进入
`ToolRegistry.contract()`,因此录制的 cassette、能力契约和指纹都不变(见
`tests/test_tool_annotations.py`)。

访问接口:

```python
registry.annotations("run_command")       # ToolAnnotations;未知名字返回保守默认
registry.annotation_hints("mcp__srv__x")  # MCP 服务端自报的提示(不可信,仅供展示/审计)
registry.annotation_table()               # {工具名: {read_only: ..., ...}}
```

`register(..., annotations=ToolAnnotations(...))` 传入注解;`subset()` 会保留注解和提示。

### 与 `retry_safe` 的关系

`retry_safe` 仍是唯一控制调度的属性(并行只读批次、重试、只读 worker 能拿到的工具子集、MCP 默认
ask)。规则:

- `retry_safe=True` **蕴含** `read_only` 且 `idempotent`:显式注解与之矛盾时注册报 `ValueError`。
- `retry_safe=True` 且没有显式注解时,默认记录为
  `read_only=True, destructive=False, idempotent=True, open_world=True`(`open_world` 无从得知,保守取 True)。
- 反向不成立:只读注解不会让工具变成 `retry_safe`。内置工具中两者恰好一致(有测试固定)。

### 内置工具注解表

`destructive` 在本项目的口径:可能删除或覆盖**会话自身状态之外**的数据(工作区文件、进程、工作区
共享记忆、外部系统)。会话内的计划、笔记、历史摘要不算 destructive。

| 工具 | read_only | destructive | idempotent | open_world |
| --- | --- | --- | --- | --- |
| `get_instructions` `read_file` `search_files` `grep` `glob` `git_diff` `git_status` `get_task_state` `recall_memory` `read_history` `search_shared_memory` `job_list` `job_output` `read_spill` `list_skills` `load_skill` `read_skill_resource` `git_log` `git_show` `git_pickaxe` | ✔ | – | ✔ | – |
| `write_file` `edit_file` `apply_patch`(需预期哈希,重复调用会失败) | – | ✔ | ✔ | – |
| `publish_memory` `withdraw_memory`(工作区共享记忆,带版本号) | – | ✔ | ✔ | – |
| `update_plan` `remember` `forget_memory` `set_history_summary`(会话内状态,带版本号) | – | – | ✔ | – |
| `run_command` `verify_command` `start_job` | – | ✔ | – | ✔ |
| `job_kill` | – | ✔ | ✔ | – |
| `job_wait` `job_settle`(写任务关联:确认、结算;不碰进程,也不 `retry_safe`) | – | – | – | – |
| `delegate_task` `message_worker` `run_subtask`(子代理的每次工具调用另行授权) | – | – | – | – |
| 未注解的工具(如评测用的 `effect`)、未声明只读的 MCP 工具 | – | ✔ | – | ✔ |
| 配置为 `readOnly` 的 MCP 工具;`knowledge_search`(retry_safe 未显式注解,所引代码是评测题的证据,故不改动) | ✔ | – | ✔ | ✔ |

## MCP:服务端注解是不可信提示

`tools/list` 返回的 `annotations`(`readOnlyHint`、`destructiveHint`、`idempotentHint`、
`openWorldHint`,只取布尔值)会被读取并保存为 `annotation_hints`,但**不参与任何授权**:

- 生效注解只由配置决定:`read_only_tools`(配置里的 `readOnly: true`)中的工具取只读记录,其余一律
  保守默认。服务端声称 `readOnlyHint: true` 得不到任何额外权限:不会变成 `retry_safe`,只读模式下
  仍隐藏,网关仍默认 ask,注解级策略(如 `destructive: deny`)仍然命中它。
- 配置为只读、但服务端自报 `readOnlyHint: false` 或 `destructiveHint: true` 时,启动时打印一条
  `MCP warning`,提示检查配置;生效注解仍以配置为准(与 `retry_safe` 保持一致)。
- 提示不进入能力契约;服务端改变提示不会使会话契约变化。

## 按注解写策略

`--tool-policy` 可选的顶层 `annotations` 段,见 [command_rules.md](command_rules.md#按注解的规则annotations)。

```json
{"default": "allow", "tools": {"run_command": {"default": "ask", "precedence": "strictest",
  "rules": [{"action": "allow", "match": {"argv_prefix": ["git", "status"]}}]}},
 "annotations": {"destructive": "ask", "open_world": "ask"}}
```

- 键为四个注解名之一,值只能是 `ask` 或 `deny`;注解为 True 的工具受这条"下限"约束。
- 键也可以用 `+` 连接几个注解名(如 `"destructive+open_world": "ask"`),表示"同时为 True 才生效"(AND);
  每个名字必须是四个注解名之一且不能重复。单个名字的写法和含义不变。
- 组合规则:
  - 注解级 `deny` 是**绝对的**,任何逐工具规则和审批都不能放开。
  - 注解级 `ask` 只让位于**显式的逐工具决定**:该工具的字符串动作(如 `"write_file": "allow"`),
    或条件规则里**确实命中**的那一条。条件规则的兜底 `default` 不算显式,此时取两者中更严格的。
  - 没有逐工具条目的工具,取顶层 `default` 与注解下限中更严格的。
  - 注解不能放宽任何决定(没有注解级 `allow`)。
- 网关在判定每次调用时把该工具的注解交给策略(`baseagent.tools.policy.call_annotations`);
  直接调用 `ToolPolicy.action` 而不经网关时,一律按保守默认注解判定(失败关闭)。
- 不写 `annotations` 的策略 `contract()` 与指纹逐字节不变;写了以后指纹变化,已有会话恢复需要
  `--accept-config-changes`。

## `--mode full` 的默认命令审批

`--mode full` 不带 `--auto-approve` 时,宿主在用户策略(没有 `--tool-policy` 时是空策略)上加一条注解级下限:

```json
{"annotations": {"destructive+open_world": "ask"}}
```

即**同时**是 destructive 和 open_world 的工具每次调用都要审批。按上面的注解表,目前就是 `run_command`、
`verify_command`、`start_job`(`--background-jobs`)和未配置 `readOnly` 的 MCP 工具(它们在网关本来就默认 ask)。
`write_file` / `edit_file` / `apply_patch`(open_world 为假,`/undo` 能撤回)、`job_kill`(不是 open_world)、
记忆和计划工具、`knowledge_search` 这类只读工具都不受影响,照旧自动执行。

为什么用注解下限而不是在 `--mode full` 里另写一套判断:它复用已有的组合规则,和用户策略的关系不用另外定义——

- 用户策略对该工具的**显式** `allow`(字符串动作,或条件规则里命中的 `allow`,如 `argv_prefix: ["git", "status"]`)
  能放开这条下限,与其它注解级 `ask` 一致;条件规则的兜底 `default` 不算显式,没命中的命令仍然要审批。
- 用户策略的 `deny`(逐工具或注解级)仍然绝对生效;用户自己写了 `"destructive+open_world": "deny"` 时保留 deny。
- 全部 ask 的策略(如 Chainlit UI 生成的策略)行为不变。
- 审批与其它 ask 完全相同:绑定 call id、轮次、工具名和参数、策略指纹;Worker 继承同一策略,Worker 的命令在主对话里
  逐条询问,与 `--tool-policy` 的 ask 走同一条路径。

**契约**:只有下限生效时它才进入策略 `contract()`(`annotations` 段多出这一键);`--mode read-only` / `edit`、
`--auto-approve`、以及直接使用 `--allow-write --allow-command` 的运行,策略契约和指纹与以前逐字节相同。因此在这次
改动之前用 `--mode full` 开始、尚未结束的回合,用 `--mode full --resume` 继续时会报告运行配置变化
(`session runtime configuration changed`,并提示后续做法):加 `--auto-approve` 按原样继续,或加
`--accept-config-changes` 接受变化(此后命令要审批,旧审批失效)。已完成的会话开新一轮不受影响。

**关闭**:`--auto-approve`(或配置文件 `permissions.auto_approve: true`)恢复以前的行为:命令直接执行,
`--tool-policy` 仍然生效。只有 `--mode full` 接受命令行上的 `--auto-approve`,其它模式下给出会报参数错误
(配置文件里的 `auto_approve` 在其它模式下不起作用,不报错)。宿主侧的 `test_fix` 检查命令由宿主直接执行,不经过
模型工具调用,不受这条下限影响。

## 关键路径删除:内置、始终拦截

网关(`GatewayPolicy`)对**任何参数里带 `argv` 字符串数组的调用**(`run_command`、`verify_command`、
启用 `--background-jobs` 时的 `start_job`,以及将来同样使用 `argv` 的工具)先做一次检查;命中即
**拒绝**(deny),不进入审批:与 `--tool-policy`、`--mode`、hook、审批记录都无关,也无法被批准。
选择拒绝而非 ask:无人值守运行时没有人能可靠地确认,而这些操作没有合理的代理用途。
被拒绝的调用返回 `permission_denied`,消息以 `refused by the built-in critical-path guard:` 开头并
说明原因。该检查是代码而非配置,不进入任何契约。实现见 `baseagent/tools/critical_paths.py`。

**关键路径**:文件系统根或盘符根(`/`、`C:\`、`D:`)、工作区根目录及其任何上级目录、用户主目录及其
任何上级目录、任何名为 `.git` 或 `.baseagent` 的目录。相对路径按工作区根解析(`start_job` 的
`cwd` 会计入);`~`、`$HOME`、`%USERPROFILE%`、`$env:USERPROFILE` 视为主目录;末段为 `*`、`.*`
的通配视为清空其所在目录。

**能识别的删除形式**(argv 中任意位置出现,因此 `sudo`、`env`、`xargs` 等前缀也覆盖):

- `rm` 带递归选项:`-r`、`-R`、`-rf`、`-fr`、组合短选项、`--recursive`;`--` 之后都是目标。
- `rmdir /s`、`rd /s`、`del /s`、`erase /s`(cmd,开关不区分大小写)。
- PowerShell `Remove-Item` / `ri` / 别名,带 `-Recurse`(及其缩写 `-r`、`-rec`…),目标来自位置参数
  或 `-Path`/`-LiteralPath`,逗号分隔的路径数组会拆开。
- `git clean` 同时带强制(`-f`)、`-d` 和 `-x`/`-X` 且非 `-n`:没有路径时目标是当前目录(工作区根),
  会清掉被忽略的 `.baseagent`;只给非关键子路径(如 `git clean -fdx build`)不拦截。支持 `git -C 目录`。
- `find <起点> ... -delete`。
- argv 中含空白或 `;&` 的元素(`bash -c "..."`、`cmd /c "..."`、`powershell -Command "..."`)会按
  shell 词法拆成命令段后递归检查(最多 3 层)。

**不能识别**(这是尽力而为的 argv 检查,不是沙箱):

- 变量和命令替换(`rm -rf "$DIR"`、`$(pwd)`),除上面列出的主目录写法外不展开。
- 切换目录后再删(`cd / && rm -rf .`):`.` 仍按工作区根解析——这个例子恰好会被拦,但
  `cd .. && rm -rf proj` 之类不会。
- 解释器代码(`python -c "shutil.rmtree('.')"`、`node -e`)、`mv` 到别处、`rsync --delete`、
  `find -exec rm`、管道(`Get-ChildItem | Remove-Item`)、符号链接/联接点指向的真实目标。
- Docker 后端里容器内的绝对路径(如 `/workspace`)不等于宿主工作区路径;相对路径和根目录仍可识别。
- 不经网关、直接调用 `ToolRegistry.execute` 的库代码。

为求稳妥,检查偏保守:例如提交信息里写着 `rm -rf .` 的 `git commit -m` 也会被拒绝。真正的隔离请用
`--backend docker` 或系统级沙箱。

## HarnessKnight

`infer_impacts` 继续按工具名推断影响:影响的目标取决于参数形状(`path`、`argv`、`operations`),
注解不描述这些;改为按注解推断会改变现有工具(例如 `search_files`、MCP 工具)发出的事件。
因此只加了一条测试,固定名字分组与注解一致(读组只读、写组 destructive、执行组 destructive 且
open_world)。
