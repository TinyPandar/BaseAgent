# DockerBackend：`--backend docker`

一次运行一个容器。命令和文件读写都在容器里做，默认断网，结束时删容器。LocalBackend 仍是默认，它不是沙箱。Docker 是这一阶段的隔离方案，没有自研 Seatbelt 或 Landlock。

```bash
docker pull python:3.12-slim                                   # 只需一次；CLI 不会偷偷拉镜像
baseagent --backend docker --mode full "跑一下测试并修好"         # 一次性任务
baseagent --backend docker --mode edit                          # 连续对话（--ui cli|tui|web 都行）
baseagent --backend docker --session S --resume                 # 续跑：新容器，同样的设置
baseagent --backend docker --docker-image node:22-slim --docker-network bridge "…"   # 换镜像、显式开网
```

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--backend local\|docker` | `local` | 不传时与以前逐字一致。`docker` 只用于一次性任务、`--resume` 和连续对话；配 `--checkpoints` / `--rewind` 等运维操作报错（退出码 2），那些操作不需要容器 |
| `--docker-image IMAGE` | `python:3.12-slim` | 必须已在本地（`docker run --pull never`）。镜像里要有 `/bin/sh` 和 coreutils 或 busybox（`cat`、`sha256sum`、`tar`、`find`、`mktemp`、`readlink -f`、`stat` 等），Debian/Ubuntu/Alpine 系都行 |
| `--docker-network none\|bridge` | `none` | `none` 没有任何网络；要联网必须显式写 `bridge` |

两个 `--docker-*` 参数配 `--backend local`（或不写 `--backend`）报错，退出码 2。资源上限固定，不开参数：CPU `1`、内存 `512m`、进程数 `128`（`--cpus`、`--memory`、`--pids-limit`）。

`--mode read-only|edit|full` 和 `--tool-policy` 的含义与 local 完全相同，docker 不放宽任何权限：没开 `--allow-command`，模型照样不能跑命令，只是命令开了以后跑在容器里。`--mode full` 在 docker 下不打印「LocalBackend 不是沙箱」的警告，改为：

```text
Backend: docker, image python:3.12-slim, no network access, container baseagent-3f2a…-4121-9c0e1d
Note: --mode full runs commands inside the Docker container (no network access). /undo restores files written by write_file/edit_file/apply_patch, not changes made by commands.
```

## 容器生命周期

- **一个 CLI 进程一个容器。** 一次性任务、一次 `--resume`、一整段连续对话（所有轮次，加上主 Agent 派出的全部 Worker）各用一个容器。Worker 和主 Agent 共用工作区挂载，所以共用容器；每条命令结束后只清理它自己留下的进程（见下），不会误杀并行 Worker 的命令。
- **起：** 解析参数、读完会话之后，调用模型之前。先预检（`docker version`、`docker image inspect`），再清理同一工作区的残留容器，再 `docker run -d … sleep infinity`。
- **停：** `finally` 里 `docker rm -f`：正常结束、Ctrl+C（130）、未捕获异常、对话 `/exit` 或 EOF、TUI 退出、Web 服务停止，都会删。关掉终端窗口（SIGHUP）和 `kill`（SIGTERM）在 docker 后端运行期间也转成正常退出（退出码 128+信号），同样会删。删之前把工作区文件属主交还宿主用户（Linux）。
- **续跑是新容器。** 会话状态在宿主的会话库里，工作区文件在宿主目录，都不丢；容器里工作区以外的东西（装的包、`/tmp`）不保留。`contract()` 不含容器 id，所以同样的镜像和网络设置能续跑；换后端、换镜像或换网络续跑一个未完成的轮次会被拒绝：

  ```text
  Session error: session S has an unfinished turn that ran with --backend docker --docker-image python:3.12-slim --docker-network none; continue it with the same settings (this run uses --backend local). …
  ```

  `--accept-config-changes` 可以覆盖（与其它运行时契约变化相同）。已完成的会话开新一轮可以换后端。
- **名字和残留：** `baseagent-<工作区哈希12>-<pid>-<随机6>`，带标签 `baseagent.workspace`、`baseagent.pid`、`baseagent.host`。同一工作区同时开两个 docker 会话不撞名，也不会互相删容器。启动时找同一工作区、同一主机上 pid 已经不在的容器（例如上次被 `kill -9`），交还属主后删掉，并在 stderr 说一句 `Removed leftover container … of an earlier run that did not exit cleanly.`；pid 还活着的、别的主机的，不碰。

## 挂载与文件边界

```text
--mount type=bind,source=<工作区>,target=/workspace
--mount type=bind,source=<工作区>/.git,target=/workspace/.git,readonly     # .git 是目录时
--mount type=tmpfs,destination=/workspace/.baseagent
```

- **只挂工作区。** 宿主上别的路径在容器里不存在。没有 `--privileged`、没有额外 `-v`。
- **文件读写在容器里做。** `read_file`、`write_file`、`edit_file`、`apply_patch`、`grep`、`glob`、`search_files`、`get_instructions`、撤销写回，都是 `docker exec` 里的一段固定 POSIX sh（`backends/docker_fs.py`）。宿主先做和 LocalBackend 相同的命名空间检查（`..`、绝对路径、解析后出界的链接、受保护路径都拒绝，不发任何 exec）；容器里再用 `readlink -f` 查一遍，写入路径上不允许任何符号链接。链接只在容器的挂载命名空间里解析，即使有并发的竞态，也碰不到宿主路径。
- **写入的原子性和 CAS 与 local 相同：** 宿主上拿同一把协作写锁（`.baseagent/file-locks`），容器里比哈希、写临时文件、重命名前再比一次哈希；新建用 `ln`（不覆盖已存在的文件），替换用 `mv -f` 并沿用原权限位。删除也在锁内、容器里比哈希再 `rm`。
- **受保护路径与 local 一致：** `.git`、`.env*`、`.venv`、`.baseagent`、会话库，`write_file` / `apply_patch` 都拒绝。
- **`.baseagent/` 在容器里是空的 tmpfs。** 会话库、检查点、文件锁、溢出文件都在宿主侧，容器里的命令看不到也改不了；写进去的东西随容器消失。会话库（`--db`）落在工作区里但不在 `.baseagent/` 下时拒绝启动（退出码 2），否则容器能改它。
- **`.git` 只读。** 镜像里有 `git` 时 `git status` / `git diff`（`git_status`、`git_diff` 工具）照常能用；容器不能改 hooks 和 config（否则宿主上下一次 `git` 会执行容器写进去的代码），也不能 commit。
- **溢出文件**（大工具结果）由 harness 写在宿主会话目录，`read_file` 读它们走宿主，和 local 相同。

## 文件属主

- **Linux：** 文件工具的 exec 带 `-u 宿主uid:gid`，新文件直接是宿主用户的。命令仍以镜像默认用户（通常 root）跑，以便 `apt-get` / `pip install`；每条命令结束后、删容器前，各跑一次 `find … ! -user uid -exec chown -h uid:gid`（跳过 `.git`、`.baseagent`），把 root 建的文件交还宿主用户。
- **macOS / Windows（Docker Desktop）：** 文件共享本来就映射成宿主用户，不做这一步。
- 进程被 `kill -9` 时来不及交还；下一次在同一工作区启动时清理残留容器前补做。

## 命令

`docker exec -e BASEAGENT_EXEC=<随机标记> -w /workspace <容器> argv…`。超时、取消、输出上限复用 LocalBackend 的 `run_process`（宿主侧的 docker CLI 进程）。命令结束（含超时、取消）后再 exec 一次，按这个环境变量杀掉这条命令留下的所有进程，和 LocalBackend「命令结束杀整个进程组」对齐。命令自己清空环境变量就能逃过这一步，和 local 用 `setsid` 逃出进程组一样。

## 检查点、撤销、原子补丁

- `/checkpoints`、`/undo`、`/rewind N`（含 `--force`、漂移检测、等待审批时拒绝）在 docker 下与 local 相同：哈希比对和写回都在容器里（`stat_files`、`restore_bytes`），恢复的字节与写入前逐字节相同，权限位也写回。
- `--checkpoints`、`--rewind last|N` 这两个运维操作不起容器，在宿主上按快照写回（容器已经删了，没有并发的容器进程），所以没有 Docker 也能撤销 docker 会话。
- `apply_patch` 的全有或全无成立：整批校验在容器里读完、算完才写；写到第 k 个失败时按快照倒序在锁内写回，检查点记录撤掉。删除有锁（与写入同一把）。
- `EditPolicy` 的 `max_changed_lines` 和 `post_edit_check` 在 docker 下正常工作：检查命令在容器里跑，失败时在锁内把原字节写回。不能写回的后端（第三方实现没有 `restore_bytes`）在构造 `Workspace` 时就报错，不会静默跳过。

## 失败与退出码

| 情况 | 什么时候发现 | 结果 |
| --- | --- | --- |
| 没装 Docker（`docker` 不在 PATH） | 启动，调用模型之前 | `Docker error (cli_missing): Docker is not installed …`，退出码 **3** |
| daemon 没起 / 没权限连 socket | 启动，调用模型之前 | `Docker error (daemon_unreachable): Docker daemon is not running or not reachable (…). Start Docker Desktop …`，退出码 **3** |
| 镜像不在本地 | 启动，调用模型之前 | `Docker error (image_missing): Docker image is missing locally: X. Pull it first: docker pull X`，退出码 **3** |
| 会话库在工作区里、不在 `.baseagent/` 下 | 启动 | `--backend docker: with --backend docker the session database must live …`，退出码 2 |
| 运行中容器没了（OOM、被外部删、Docker 重启） | 下一次工具调用或模型调用前 | 命令返回 `Docker container X is no longer running (out of memory, removed, or Docker restarted)… continue with --resume`；工作区检查随即让这一轮以 `failed` 落盘，退出码 1。修好 Docker 后 `--resume` 用新容器继续 |
| 命令被 OOM 杀掉但容器还在 | 命令结束 | `command was killed: the container ran out of memory (limit 512m)` |

绝不静默退回 local。退出码 3 只用于「Docker 起不来」，与 0 / 1 / 2 / 130 不重叠；json 下的形状见 [output.md](output.md)。

## 输出里怎么看出后端

- 文本模式：stderr 一行 `Backend: docker, image …, no network access, container …`（local 没有这一行）。
- `--output-format json`：多一个 `backend` 对象 `{"kind": "docker", "image", "network", "container", "cpus", "memory", "pids"}`；local 没有这个键，没有它就是 local。
- `--output-format jsonl`：`output_started` 之后一行 `backend_ready`，`data` 同上；最后的 `result` 行也带 `backend`。
- `/status`（CLI、TUI、Web）：`Workspace:` 下面一行 `Backend: docker, …`；TUI/Web 的状态事件多一个 `backend` 字段。
- 会话元数据 `execution_backend`（只在 docker 时）：`{"kind", "image", "network"}`，`--inspect-session` 可见，用来给出上面那条续跑报错。

## 已知限制

- 不是虚拟机：容器与宿主共享内核，隔离强度就是 Docker 的默认配置（没有 user namespace remap、没有 seccomp 定制）。
- 工作区文件就是宿主文件（bind mount）：命令能改、能删工作区里的任何东西，`/undo` 撤不回命令的副作用。
- MCP 服务器仍在宿主上跑，不进容器（启动时 stderr 有一行说明）。
- 默认镜像 `python:3.12-slim` 里没有 `git`：`git_status` / `git_diff` 会返回「executable file not found」的失败。需要 git 时换一个带 git 的镜像（例如在它基础上 `apt-get install git` 构建一个）。
- `.git` 是文件（git worktree）时不挂载，容器里 `git` 找不到仓库，`git_status` / `git_diff` 返回可读的错误。工作区本来不是 git 仓库时，容器里可以 `git init`（建出来的 `.git` 在宿主上可见）。
- 每次文件操作是一次 `docker exec`（Linux 上几十毫秒，Docker Desktop 上更慢）；`grep`、`search_files`、漂移检查都批量读，一次 exec 处理一批。Linux 上每条命令后的属主交还会遍历工作区。
- 工作区路径里不能有逗号（`--mount` 的分隔符）。
- 每次 `--resume` 是新容器，容器里工作区以外的状态不保留。
- `SIGKILL` / 断电时容器留下来，下次在同一工作区启动时清理；不在同一工作区启动就一直留着（`docker ps -a --filter label=baseagent.workspace`）。
- 不支持远程 Docker 主机、多容器编排、Kubernetes。

## 库用法

```python
from baseagent.backends import DockerBackend
from baseagent.tools.workspace import Workspace

with DockerBackend(root, image="python:3.12-slim") as backend:      # network="none" by default
    workspace = Workspace(backend=backend, allow_write=True, allow_command=True)
```

`runner=` 可以换成假 Docker（`baseagent.testing.docker.FakeDocker`）：它解释 `docker run/exec/rm/ps/...`，把 exec 的命令在本机真实跑一遍（`/workspace` 映射到绑定的目录），所以文件脚本是真的在跑，但不隔离任何东西，只用于离线测试。

## 演示与测试

```bash
python -B scripts/demo_docker.py                # 真 Docker：改文件 → 跑命令 → 外网失败 → /undo → 容器删掉
python -B scripts/demo_docker.py --fake-docker  # 同一流程走假 Docker（离线、不隔离）
```

没有 Docker 的机器上第一条打印 `Docker error (cli_missing): …` 并以 3 退出。离线测试在 `tests/test_docker_cli.py`、`tests/test_docker_backend.py`、`tests/test_demo_docker.py`；真实冒烟测试在 `tests/test_docker_smoke.py`（隔离、断网、属主、撤销、正常结束和 Ctrl+C 清理），没有 Docker / daemon / 镜像时带原因跳过；镜像用 `BASEAGENT_DOCKER_TEST_IMAGE` 指定，默认 `python:3.12-slim`，拉镜像以外不联网。
