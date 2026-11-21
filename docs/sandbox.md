# 命令沙箱（`--sandbox`）

`--backend local`（默认后端）原本直接在本机执行 `run_command` / `verify_command` / `git_status` / `git_diff` 的命令进程，没有任何操作系统级隔离。`--sandbox` 给这些**命令进程**加一层 OS 沙箱：限制它们能写哪里、能不能联网。实现在 `src/baseagent/backends/sandbox.py`，由 `LocalBackend.execute` 在启动进程前一刻套上。

默认 `off`，行为、输出与运行契约和以前完全一样。

## 模式

| 模式 | 命令可写 | 网络 |
| --- | --- | --- |
| `off`（默认） | 不限制（与以前相同） | 不限制 |
| `read-only` | 只有本次运行的临时目录 | 默认禁止 |
| `workspace-write` | 工作区根目录 + 本次运行的临时目录；但 `.git`、`.venv`、`.baseagent` 以及工作区内的会话库等受保护路径仍然只读 | 默认禁止 |

- 读：两种受限模式都不限制读（整个文件系统可读）。
- 临时目录：每次运行新建一个 `baseagent-sandbox-*` 临时目录，命令的 `TMPDIR` / `TMP` / `TEMP` 指向它，运行结束时删除。
- 受保护路径：与 Docker 后端的 `PROTECTED_PARTS` 一致（`.git`、`.venv`、`.baseagent`），再加上 `Workspace.protected_paths` 中落在工作区内的路径（会话库、`-wal`/`-shm`/`-journal`、锁目录）。思路同 Claude Code：可写目录里的配置/元数据仍然不让命令改。所以 `workspace-write` 下命令不能 `git commit`；`git status` 可以运行（刷新索引失败会被 git 忽略）。
- 网络：`--sandbox-network` 显式放开（受限模式下才有意义，`off` 时给出会报参数错误）。目前是全开或全关，没有域名白名单。

用法：

```bash
baseagent --mode full --sandbox workspace-write "跑一下测试并修复"
baseagent --mode full --sandbox workspace-write --sandbox-network "安装依赖后运行测试"
```

配置文件（`--config`）：

```json
{"version": 1, "sandbox": {"mode": "workspace-write", "network": false}}
```

`mode` 对应 `--sandbox`，`network` 对应 `--sandbox-network`；命令行参数优先。

## 各平台实现

| 平台 | 实现 | 验证状态 |
| --- | --- | --- |
| macOS | `/usr/bin/sandbox-exec -p <生成的 SBPL 配置> -- argv` | **未在真机验证**：只有单元测试检查生成的配置（工作区可写、受保护目录拒写、网络拒绝）和平台选择 |
| Linux / WSL2 | `bwrap`（bubblewrap） | **未在真机验证**：只有单元测试检查生成的 argv 和平台选择 |
| 原生 Windows | 无 | 已在 Windows 主机实测：受限模式报 `SANDBOX_UNAVAILABLE`，退出码 3 |
| 其它平台 | 无 | 同上，报不可用 |

macOS 的 SBPL 配置（后面的规则优先）：

```
(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "<工作区>") (subpath "<临时目录>") /dev/null、/dev/tty 等)
(deny file-write* (subpath "<工作区>/.git") (subpath "<工作区>/.venv") (subpath "<工作区>/.baseagent") ...)
(deny network*)            ; 没有 --sandbox-network 时
```

`read-only` 时允许写的只有临时目录。路径使用 `realpath`（Seatbelt 按规范路径匹配，如 `/private/var/...`）。`deny network*` 也会挡住 Unix 域套接字。

Linux 的 bwrap 参数（按顺序，后面的挂载覆盖前面的）：

```
bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp
      --bind <工作区> <工作区>          # read-only 模式用 --ro-bind（工作区在 /tmp 下也仍可见）
      --bind <临时目录> <临时目录>
      --ro-bind-try <受保护路径> <受保护路径> ...   # 仅 workspace-write
      --unshare-pid [--unshare-net] --die-with-parent --new-session --chdir <工作区> -- argv
```

选择规则：按 `sys.platform` 选实现，再查工具是否存在（macOS 优先 `/usr/bin/sandbox-exec`，Linux 在 `PATH` 中找 `bwrap`）。启动前还会在沙箱里跑一次 `true` 作为探测；例如 Linux 禁用了非特权 user namespace 时 bwrap 会失败，这时同样按不可用处理。

## 失败即关闭（fail closed）

请求了受限模式但没有可用的实现时：

- CLI：在调用模型之前退出，stderr 打印 `Sandbox error: SANDBOX_UNAVAILABLE: --sandbox workspace-write needs OS confinement, but ...`，退出码 `3`（与 `--backend docker` 起不来相同，费用 0）。`--output-format json` 的结果里带 `"backend": {"kind": "local", "sandbox": "...", "network": "none", "unavailable": "SANDBOX_UNAVAILABLE"}`。
- 库：`LocalBackend(root, sandbox=SandboxPolicy("workspace-write"))` 构造时抛 `SandboxUnavailable`；沙箱关闭后再调 `execute` 返回 `permission_denied` 失败，消息以 `SANDBOX_UNAVAILABLE` 开头。
- 绝不悄悄退回到不受限执行，也没有类似 Claude Code `dangerouslyDisableSandbox` 的“沙箱外重试”。

## 与其它选项的组合

- `--backend docker` + 受限 `--sandbox`：参数错误（退出码 2）。容器已经隔离了命令（和文件工具），再叠一层是多余的，也容易误解；容器的网络用 `--docker-network` 控制。`--sandbox off` 可以与 docker 同用（等于没给）。
- `--background-jobs` + 受限 `--sandbox`：可以同用。后台任务的进程经 `tools/jobs.py` 的 `spawn_job_process` 用同一个沙箱启动；`run_command` 超时转后台的进程本来就由 `LocalBackend.execute` 在沙箱里启动，接管后仍在沙箱里。`start_job(keep_running=true)` 在沙箱下被拒绝（`permission_denied`）：它的 runner 要写 `.baseagent/jobs/`（沙箱里只读），而且要比本次运行的沙箱临时目录活得久。沙箱已关闭或不可用时任务被拒绝，绝不在沙箱外运行。
- `--mode full` + 受限沙箱：不再打印“LocalBackend is not a sandbox”警告，改为打印沙箱摘要说明。

## 输出与运行契约

- 只在受限模式下，`LocalBackend.contract()` 才多一个 `"sandbox": {"version": 1, "mode": ..., "network": ..., "protected_parts": [...]}`。默认 `off` 时契约逐字节不变，已有会话、cassette 和指纹不受影响。模式或网络设置变了，未完成的回合在 `--resume` 时会因运行契约不一致被拒绝（`--accept-config-changes` 可覆盖）。
- `--output-format json` 的结果和 jsonl 的 `backend_ready` 事件在受限模式下带 `{"kind": "local", "sandbox": ..., "network": "none"|"enabled", "implementation": "seatbelt"|"bubblewrap"}`；默认时没有 `backend` 键（与以前相同）。
- 发给模型的工具 schema 不变。

## 什么在沙箱里，什么不在

在沙箱里：`LocalBackend.execute` 启动的命令进程及其子进程（`run_command`、`verify_command`、`git_status`、`git_diff`、completion policy / test-fix 经同一路径运行的命令），以及后台任务（`start_job` 和超时转后台的 `run_command`，`keep_running` 除外，见上）。

不在沙箱里：

- 进程内的文件工具（`read_file`、`write_file`、`edit_file`、`apply_patch`、`grep` 等）——它们靠 `Workspace` / `LocalBackend` 自己的路径检查和 `--mode` 权限；
- MCP 服务器进程、hooks、harness 自身。

命令继承 harness 的环境变量（只改写 `TMPDIR`/`TMP`/`TEMP`），包括其中的密钥；沙箱只限制写和网络，不限制读。

## 给其它进程启动方的接口

```python
from baseagent.backends import sandbox

argv, env = sandbox.wrap(argv, cwd, env, backend.sandbox)   # backend.sandbox 为 None 时原样返回
subprocess.Popen(argv, cwd=cwd, env=env, ...)
```

后台任务已按这个接口接入：`JobManager` 持有后端的沙箱，`spawn_job_process(argv, cwd, sandbox=...)` 在 `Popen` 前调用 `sandbox.wrap(argv, cwd, None, sandbox)`；任务记录里保存的是未包装的原始 argv（显示和策略匹配都用它）。bwrap 用 `--chdir` 设定工作目录，`cwd` 传任务实际的目录；`--die-with-parent` 会让任务随 harness 退出，与默认的“任务随会话结束”一致。

## 后续工作

- 原生 Windows：需要 AppContainer / 受限令牌 / Job 对象等原生实现，目前只报不可用（WSL2 可用 bubblewrap）。
- 网络白名单：现在只能全开或全关；可参照 Claude Code 用本地代理做域名白名单。
- 沙箱下的 `keep_running` 任务：需要让 runner 在沙箱外、只把命令放进沙箱，并给它一个不随本次运行删除的临时目录。
- MCP stdio 服务器进程也可以经 `sandbox.wrap` 启动。
- macOS / Linux 在真机上的端到端验证（含受保护目录、网络拒绝、工作区位于 `/tmp` 下等情况）。
- `workspace-write` 下受保护路径若在运行开始时不存在（例如还没创建的 `-wal` 文件），bwrap 的 `--ro-bind-try` 会跳过它；常见布局里它们都在 `.baseagent/` 下，已被整体只读覆盖。
