"""A fake ``docker`` command for offline tests: no daemon, no image, no network.

``FakeDocker`` understands the subcommands DockerBackend uses (version, image inspect, ps,
run, exec, start, inspect, rm). ``docker exec`` really runs the command, with the container's
``/workspace`` mapped to the bound host directory, so the file-operation script
(``backends/docker_fs.py``) is exercised for real. It does not isolate anything: commands
run on the host. With ``--network none`` it points proxy variables at a closed port so
HTTP clients fail the way they would without a network.

Every call is recorded in ``calls``; tests assert on the arguments (mounts, labels, network,
``rm -f``). ``hook(argv)`` may return a ``(code, stdout, stderr)`` tuple to inject a failure.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from baseagent.backends.docker import CLI_MISSING, MOUNT, DockerUnavailable
from baseagent.tools.result import ErrorCode, ToolResult

DAEMON_DOWN = (b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
               b"Is the docker daemon running?\n")
OFFLINE_PROXY = "http://127.0.0.1:9"


class FakeDocker:
    def __init__(self, *, installed=True, daemon=True, images=("python:3.12-slim",), hook=None):
        self.installed, self.daemon = installed, daemon
        self.images = set(images)
        self.hook = hook
        self.calls = []
        self.containers = {}          # name -> {"root", "labels", "network", "running", "mounts"}
        self._lock = threading.Lock()
        self._counter = 0

    # ----- bookkeeping ----------------------------------------------------------------------
    def removed(self):
        """Names passed to ``docker rm -f``."""
        return [argv[3] for argv in self.calls if argv[1:3] == ["rm", "-f"]]

    def started(self):
        return [argv[argv.index("--name") + 1] for argv in self.calls if argv[1] == "run"]

    def add_stale(self, name, root, *, pid, host=None):
        """A container left behind by an earlier process (``pid`` should not be running)."""
        import socket
        from hashlib import sha256
        key = sha256(str(Path(root).resolve()).encode()).hexdigest()[:12]
        self.containers[name] = {"root": Path(root).resolve(), "network": "none", "running": False, "mounts": [],
                                 "labels": {"baseagent.workspace": key, "baseagent.pid": str(pid),
                                            "baseagent.host": host or socket.gethostname()}}

    def kill(self, name, *, oom=False):
        """Simulate the container dying (OOM, ``docker rm`` from outside, daemon restart)."""
        container = self.containers.get(name)
        if container is not None:
            container["running"] = False
            container["oom"] = oom

    # ----- the docker CLI ---------------------------------------------------------------------
    def run(self, argv, *, input_bytes=None, timeout=60):
        if not self.installed:
            raise DockerUnavailable("cli_missing", CLI_MISSING)
        with self._lock:
            self.calls.append(list(argv))
        if self.hook is not None:
            injected = self.hook(list(argv))
            if injected is not None:
                return injected
        if not self.daemon:
            return 1, b"", DAEMON_DOWN
        command = argv[1]
        if command == "version":
            return 0, b"27.0.0\n", b""
        if command == "image":
            image = argv[-1]
            return (0, b"sha256:fake\n", b"") if image in self.images else \
                (1, b"", f"Error response from daemon: No such image: {image}\n".encode())
        if command == "ps":
            wanted = argv[argv.index("--filter") + 1].removeprefix("label=").split("=", 1)
            lines = [f"{name}|{c['labels'].get('baseagent.pid', '')}|{c['labels'].get('baseagent.host', '')}"
                     for name, c in self.containers.items() if c["labels"].get(wanted[0]) == wanted[1]]
            return 0, ("\n".join(lines) + ("\n" if lines else "")).encode(), b""
        if command == "run":
            return self._run_container(argv)
        if command == "rm":
            name = argv[-1]
            if self.containers.pop(name, None) is None:
                return 1, b"", f"Error response from daemon: No such container: {name}\n".encode()
            return 0, f"{name}\n".encode(), b""
        if command == "start":
            container = self.containers.get(argv[-1])
            if container is None:
                return 1, b"", b"Error response from daemon: No such container\n"
            container["running"] = True
            return 0, b"", b""
        if command == "inspect":
            container = self.containers.get(argv[-1])
            if container is None:
                return 1, b"", f"Error: No such object: {argv[-1]}\n".encode()
            return 0, f"{str(container['running']).lower()} {str(container.get('oom', False)).lower()}\n".encode(), b""
        if command == "exec":
            prepared = self._prepare_exec(argv)
            if isinstance(prepared, tuple) and len(prepared) == 3 and isinstance(prepared[0], int):
                return prepared
            command_argv, cwd, extra = prepared
            completed = subprocess.run(command_argv, cwd=cwd, env={**os.environ, **extra}, input=input_bytes,
                                       capture_output=True, timeout=timeout, check=False)
            return completed.returncode, completed.stdout, completed.stderr
        return 1, b"", f"fake docker: unsupported command {command}\n".encode()

    def stream(self, argv, *, cwd, timeout, output_limit, cancellation=None):
        if not self.installed:
            raise DockerUnavailable("cli_missing", CLI_MISSING)
        with self._lock:
            self.calls.append(list(argv))
        if self.hook is not None:
            injected = self.hook(list(argv))
            if injected is not None:
                return _result(*injected, output_limit)
        prepared = self._prepare_exec(argv)
        if isinstance(prepared, tuple) and len(prepared) == 3 and isinstance(prepared[0], int):
            return _result(*prepared, output_limit)
        command_argv, workdir, extra = prepared
        from baseagent.tools.process import run_process
        # run_process has no env parameter: `env K=V ... argv` sets the container variables for this child only.
        wrapped = ["env", *[f"{key}={value}" for key, value in extra.items()], *command_argv] if extra else command_argv
        return run_process(wrapped, workdir, timeout=timeout, output_limit=output_limit, cancellation=cancellation)

    # ----- helpers ------------------------------------------------------------------------------
    def _run_container(self, argv):
        name = argv[argv.index("--name") + 1]
        image = argv[-2]
        if image not in self.images:
            return 125, b"", f"Unable to find image '{image}' locally\n".encode()
        if name in self.containers:
            return 125, b"", (f'docker: Error response from daemon: Conflict. The container name "/{name}" '
                              f"is already in use.\n").encode()
        labels, mounts = {}, []
        for index, value in enumerate(argv):
            if value == "--label":
                key, _, label = argv[index + 1].partition("=")
                labels[key] = label
            if value == "--mount":
                mounts.append(argv[index + 1])
        bind = next(item for item in mounts if item.startswith("type=bind") and item.endswith(f"target={MOUNT}"))
        root = Path(bind.split("source=", 1)[1].split(",", 1)[0])
        self._counter += 1
        self.containers[name] = {"root": root, "labels": labels, "network": argv[argv.index("--network") + 1],
                                 "running": True, "mounts": mounts, "image": image}
        return 0, f"fakecontainer{self._counter:04d}\n".encode(), b""

    def _prepare_exec(self, argv):
        index, env, workdir = 2, {}, MOUNT
        while argv[index].startswith("-"):
            flag = argv[index]
            if flag in ("-i", "-t"):
                index += 1
                continue
            value = argv[index + 1]
            if flag == "-e":
                key, _, item = value.partition("=")
                env[key] = item
            elif flag == "-w":
                workdir = value
            index += 2
        name, command = argv[index], argv[index + 1:]
        container = self.containers.get(name)
        if container is None:
            return 1, b"", f"Error response from daemon: No such container: {name}\n".encode()
        if not container["running"]:
            return 1, b"", f"Error response from daemon: container {name} is not running\n".encode()
        root = container["root"]

        def host(value):
            if value == MOUNT:
                return str(root)
            if value.startswith(MOUNT + "/"):
                return str(root) + value[len(MOUNT):]
            return value
        extra = dict(env)
        if container["network"] == "none":
            for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
                extra[key] = OFFLINE_PROXY
            extra["NO_PROXY"] = extra["no_proxy"] = ""
        return [host(part) for part in command], host(workdir), extra


def _result(code, stdout, stderr, limit):
    data = {"exit_code": code, "stdout": stdout[:limit].decode("utf-8", errors="replace"),
            "stderr": stderr[:limit].decode("utf-8", errors="replace"),
            "stdout_truncated": len(stdout) > limit, "stderr_truncated": len(stderr) > limit}
    if code:
        return ToolResult.failure(ErrorCode.EXECUTION_FAILED, f"command exited with code {code}", data=data)
    return ToolResult(data=data)
