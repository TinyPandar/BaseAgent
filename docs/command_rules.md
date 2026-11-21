# 命令级规则(argv_prefix)

`--tool-policy` 的条件规则除了 `equals` / `path_within` / `argv_exact`,还可以按命令前缀匹配 `argv`,
从而表达"允许 `git status`、`git push` 先问、拒绝 `rm`"这类策略。完整示例见
[examples/tool-policy.commands.json](../examples/tool-policy.commands.json):

```json
{
  "default": "ask",
  "tools": {
    "run_command": {
      "default": "ask",
      "precedence": "strictest",
      "rules": [
        {"action": "allow", "match": {"argv_prefix": ["git", "status"]}},
        {"action": "allow", "match": {"argv_prefix": ["git", "diff"]}},
        {"action": "ask",   "match": {"argv_prefix": ["git", "push"]}},
        {"action": "deny",  "match": {"argv_prefix": ["rm"]}}
      ]
    }
  }
}
```

```powershell
.\.venv\Scripts\python.exe -m baseagent --mode full --tool-policy examples\tool-policy.commands.json "检查仓库状态"
```

## 匹配语义

- `argv_prefix`:调用参数里的 `argv` 必须是数组,且前 N 个元素与规则逐个**完全相等**(区分大小写,
  按整个元素比较,不是字符串前缀)。`["git", "status"]` 匹配 `git status`、`git status --porcelain`,
  不匹配 `git statusx`、`git -C . status`、`Git status`、`/usr/bin/git status`。
- 只比较前缀那几个元素;后面的参数任意。`argv` 缺失、不是数组、前缀位置不是字符串时视为不匹配。
- 不做通配、正则、路径归一化或可执行文件识别。`["rm"]` 不会匹配 `/bin/rm`、`rm.exe`、`busybox rm`。
  因此**不要把 deny 前缀当成可靠的黑名单**,应以 `default: "deny"` 或 `"ask"` 兜底,再显式 allow 少量命令。
- 匹配的是工具参数里的 `argv` 键,与工具名无关,所以对 `run_command`、`verify_command` 以及以后
  使用 `argv` 参数的工具(例如后台任务的 `start_job`)都适用。但规则仍按**工具名**配置:每个执行命令的
  工具都要单独列出,否则它走顶层 `default`。顶层 `default` 用 `ask` 或 `deny`,新加的命令工具才不会被
  放行。
- 限制与 `argv_exact` 相同:1–128 个元素,每个是 1–4000 字符的非空字符串;每个工具最多 50 条规则。
  一条 `match` 里的多个条件(如 `argv_prefix` + `equals`)必须同时成立。参数 JSON 非法、有重复键时一律 deny。

## 多条规则同时命中:优先级

规则原有的语义是**第一条命中的规则生效**(first match),已有策略和测试依赖这个行为,
而且审批与会话恢复绑定策略指纹。为保持兼容,默认仍是 first match。

需要"最严格者胜"时,在该工具的条件对象里加 `"precedence": "strictest"`:

| `precedence` | 行为 |
| --- | --- |
| 省略或 `"first_match"` | 按顺序取第一条命中的规则;都不命中用该工具的 `default` |
| `"strictest"` | 收集所有命中的规则,取最严格的动作:`deny` > `ask` > `allow`,与规则顺序无关;都不命中用 `default` |

例:`allow ["git"]` 与 `deny ["git", "push", "--force"]` 同时存在时,`strictest` 下
`git push --force` 一定是 deny,不论两条规则谁在前。first match 下则必须把 deny 写在前面。
新写命令规则时建议总是用 `strictest`。

`default` 只在没有任何规则命中时使用,不参与"取最严格"。

## deny 是绝对的

策略给出的 `deny` 不能被任何其它层改成 `allow`。逐层核对(见 `tests/test_command_rules.py` 的
`DenyIsAbsoluteTests`):

- **审批**:只有动作为 `ask` 时才读取审批记录;`deny` 在读审批之前就返回。即使伪造一条 turn、
  调用 id、参数摘要、策略指纹都对得上的 `allow` 审批,也不改变 deny。`--approve-tool` 也只能对处于
  `awaiting_approval` 的调用记录决定,被 deny 的调用从不暂停,无从批准。策略收紧后,旧审批因指纹变化失效。
- **GatewayPolicy**(MCP 默认 ask、hook 的 `restrict`):只取更严格的一方,`restrict` 只能是 ask/deny。
- **IntersectionPolicy**(子任务、主代理 worker 的 `_child_policy`):父或子任意一方 deny 即 deny;
  内层策略的审批不会被外层继承。
- **EditToolPolicy**(`max_changed_lines` 超限转 ask):父策略 deny 直接返回。
- **HarnessKnight**:父策略 deny 直接返回;`REVIEW` 只能把 allow 变成 ask;`fail_mode=open`
  在没有决策缓存时也不会放行父策略已 deny 的调用。
- **`--mode full`**:只打开工作区的写/命令能力,策略仍在每次调用前和实际派发时各判定一次,
  deny 的调用不进入处理函数,结果是 `permission_denied`。
- **后台 worker**(`WorkerManager`):worker 用同一个 `tool_policy` 调 `run_agent`,外层同样包
  `GatewayPolicy`。

本次核对没有发现可绕过 deny 的路径,因此只补了测试,没有改这些层的代码。

## 已知绕过:包装器与解释器

前缀只约束 argv 的开头,**不理解它后面要执行什么**。下列程序会执行参数里给出的另一条命令或一段代码,
对它们写 allow 前缀等于放行任意命令:

- shell:`bash -c`、`sh -c`、`zsh`、`cmd /c`、`powershell -Command`、`pwsh`、`wsl`
- 解释器:`python -c`、`node -e`、`perl -e`、`ruby -e`、`php -r`、`deno`、`bun`
- 转发器:`env`、`xargs`、`sudo`、`nohup`、`nice`、`timeout`、`time`、`find -exec`、`npx`、`uvx`

例如 allow `["python"]` 时 `python -c "import shutil; shutil.rmtree('.')"` 也会放行;
deny `["rm"]` 挡不住 `bash -c "rm -rf ."`、`env rm -rf .`、`git clean -fdx`。此外看似无害的命令
也可能借参数执行别的东西(`git -c alias...`、`git diff --ext-diff`、`git diff --output=FILE`
写文件等)。

处理方式(保持简单):

- 加载策略时,若某条 **allow** 规则的 `argv_prefix` 第一个元素是已知包装器/解释器(去掉路径、
  `.exe` 等后缀和版本号后比较,如 `C:\Python\python3.12.exe` → `python`),会发出
  `CommandRuleWarning`(CLI 中打印到 stderr),并记在 `ToolPolicy.warnings` 里。只是警告,不拒绝
  加载,因为像 `["python", "-m", "unittest"]` 这样的前缀可能是有意为之。`ask`/`deny` 规则不警告。
- 包装器需要放行时,用 `argv_exact` 固定完整命令,或用 `ask` 让人逐次审批。
- 真正需要隔离时用 `--backend docker` 或系统级沙箱;策略规则不是沙箱。

列表见 `baseagent.tools.policy.COMMAND_WRAPPERS`,判断函数是 `command_wrapper(program)`。

## 按注解的规则(annotations)

策略可以加一个可选的顶层 `annotations` 段,按工具注解(`read_only`、`destructive`、`idempotent`、
`open_world`,见 [tool_annotations.md](tool_annotations.md))给出下限,值只能是 `ask` 或 `deny`:

```json
{"default": "allow", "tools": {"write_file": "allow"}, "annotations": {"destructive": "ask", "open_world": "ask"}}
```

- 注解级 `deny` 绝对生效,逐工具 `allow` 和审批都不能放开。
- 注解级 `ask` 只让位于显式的逐工具决定:字符串动作,或条件规则中**命中**的那一条;条件规则的
  兜底 `default` 和顶层 `default` 都与注解下限取更严格者。上例中 `write_file` 直接放行,
  `run_command`、MCP 工具等需要审批,只读工具不受影响。
- MCP 服务端自报的注解不可信,不参与判定;生效注解只来自配置的 `readOnly`。
- 不写 `annotations` 的策略契约与指纹不变。

此外,删除关键路径(工作区根、`.git`、`.baseagent`、主目录、文件系统/盘符根)的命令由内置检查
**始终拒绝**,不受本策略影响,范围见 [tool_annotations.md](tool_annotations.md#关键路径删除内置始终拦截)。

## 兼容性

- 不使用 `argv_prefix` / `precedence` / `annotations` 的策略,`contract()` 输出与指纹逐字节不变,已有审批和会话恢复
  不受影响(有测试固定了旧实现下的指纹)。
- 使用新键后指纹会变化;对已有会话 `--resume` 时需要 `--accept-config-changes`,旧审批失效,需重新审批。
- 旧版本不认识 `argv_prefix` / `precedence` / `annotations`,加载这样的策略会报错(fail closed),不会静默放宽。
