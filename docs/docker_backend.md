# DockerBackend

一个任务一个容器。默认 `--network none`，任务结束销毁容器。只挂载该任务的工作区，所以容器看不到宿主持仓的其它路径。

LocalBackend 仍然可用。它不是操作系统沙箱。Docker 是这一阶段的隔离方案，没有自研 Seatbelt 或 Landlock。

## 和 LocalBackend 的差别

| | LocalBackend | DockerBackend |
| --- | --- | --- |
| 隔离 | 没有 | 容器 |
| 网络 | 跟随宿主 | 默认断开 |
| 资源 | 不限制 | CPU、内存、进程数有默认值 |
| 失败 | 宿主进程错误 | 镜像缺失、Docker 未启动、OOM 都是明确错误 |

默认断网是为了让题目不能在评测时自己下载答案或依赖。需要联网时必须显式把 `network` 设为 `bridge`。

## 切换

```python
from baseagent.backends import DockerBackend
from baseagent.tools.workspace import Workspace

with DockerBackend(root, image="python:3.12-slim") as backend:
    workspace = Workspace(backend=backend, allow_command=True)
```

没有 Docker 时，协议和配置测试仍然运行；真实容器用例会跳过。

## 演示

```bash
python -B scripts/demo_docker.py
```

它用本机已有的 `nginx:alpine` 跑一条命令，再尝试读工作区外的宿主文件和访问外网。容器结束后会被清掉。费用 0。
