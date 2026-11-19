"""Harbor-shaped fake environments for offline tests (no Docker, no Harbor)."""

import asyncio
import atexit
import ctypes
import ctypes.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from baseagent.backends import exec_backend

# The fake environments run a POSIX shell here and the stop script reads /proc (Linux) or a
# mirror built from libproc (macOS). Elsewhere (Windows) every importing module is skipped,
# under pytest and unittest alike, instead of failing collection.
if not os.path.exists("/proc/self/stat") and not ctypes.util.find_library("proc"):
    raise unittest.SkipTest("needs /proc (Linux) or libproc (macOS) and a POSIX shell")


class ShellEnvironment:
    """``async exec`` like Harbor's docker environment, backed by bash in a local directory.

    Like ``docker exec``: cancelling the call stops waiting for the client, but the process
    keeps running until it ends on its own or something inside the environment kills it.
    Commands get a minimal environment (PATH only), like a task container that never
    received host variables. Every call is recorded for assertions.
    """

    def __init__(self, workdir, *, shims=("setsid",)):
        self.workdir = str(Path(workdir).resolve())
        self.calls = []
        self.fail_with = None
        # macOS has neither /proc nor setsid. By default a setsid shim gives commands the Linux
        # behavior (a new session per launch); tests can drop it or add other shims.
        self.shim_dir = make_shims(Path(self.workdir).parent / f"shims-{os.urandom(4).hex()}", shims)

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.calls.append({"command": command, "cwd": cwd, "env": env, "timeout_sec": timeout_sec, "user": user})
        if self.fail_with is not None:
            raise self.fail_with
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        process = subprocess.Popen(["bash", "-c", command], cwd=cwd or self.workdir, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True,
                                   env={"PATH": os.pathsep.join(filter(None, [self.shim_dir, os.environ.get(
                                       "PATH", "/usr/bin:/bin")]))})

        def wait():
            out, err = process.communicate()
            result = SimpleNamespace(stdout=out.decode("utf-8", "replace"), stderr=err.decode("utf-8", "replace"),
                                     return_code=process.returncode)
            try:
                loop.call_soon_threadsafe(lambda: future.done() or future.set_result(result))
            except RuntimeError:
                pass        # the caller's loop is gone; the process still ran to completion

        threading.Thread(target=wait, daemon=True).start()
        return await future


_KILL_SHIM = """#!/bin/bash
if [ "$1" = -c ] && [ $# -ge 2 ]; then
  script=$2; shift 2
  name=${1:-sh}; [ $# -gt 0 ] && shift
  exec /bin/bash -c 'kill() { __KILL__; }
script=$1; shift; eval "$script"' "$name" "$script" "$@"
fi
exec /bin/bash "$@"
"""

# Shim name -> script; the file is named by the part before the first "-" (sh-noop-kill -> sh).
SHIMS = {
    # util-linux setsid: a new session for the command (forks first when already a group leader).
    "setsid": f"""#!/bin/sh
exec "{sys.executable}" -c 'import os, sys
if os.getpgrp() == os.getpid() and os.fork():
    os._exit(0)
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
""",
    # A /bin/sh whose kill builtin accepts nothing, like a shell with an incompatible kill syntax.
    # The original script is passed through as its own argument and run unchanged by eval, with
    # its $0 and positional parameters; nothing is spliced into a quoted string.
    "sh": _KILL_SHIM.replace("__KILL__", 'echo "kill: unsupported syntax" >&2; return 2'),
    # A /bin/sh whose kill reports success and does nothing.
    "sh-noop-kill": _KILL_SHIM.replace("__KILL__", "return 0"),
}


def make_shims(directory, names):
    if not names:
        return None
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = directory / name.split("-")[0]
        target.write_text(SHIMS[name])
        target.chmod(0o755)
    return str(directory)


class MappedShellEnvironment(ShellEnvironment):
    """Shows ``alias`` (for example /app) as the working directory, backed by a local directory.

    The agent only ever sees the alias. If anything did native host I/O on the alias path,
    it would hit a path that does not exist on this machine.
    """

    def __init__(self, workdir, alias="/app", **kwargs):
        super().__init__(workdir, **kwargs)
        self.alias = alias

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        command = command.replace(self.alias, self.workdir)
        cwd = None if cwd is None else cwd.replace(self.alias, self.workdir, 1)
        result = await super().exec(command, cwd=cwd, env=env, timeout_sec=timeout_sec, user=user)
        result.stdout = result.stdout.replace(self.workdir, self.alias)
        return result


class LoopThread:
    """An event loop running in a background thread, like Harbor's trial loop seen from the agent thread."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *exc):
        async def drain():
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            if pending:
                await asyncio.wait(pending, timeout=5)
        asyncio.run_coroutine_threadsafe(drain(), self.loop).result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def temporary_workdir(test):
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    workdir = Path(directory.name) / "app"
    workdir.mkdir()
    return workdir.resolve()


class _BSDInfo(ctypes.Structure):
    """``struct proc_bsdinfo`` from <sys/proc_info.h>."""
    _fields_ = [("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32), ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32), ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32), ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32), ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32), ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32), ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32), ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64)]


class ProcMirror:
    """A /proc view of this macOS machine for the stop script, which reads processes only from /proc.

    Every 20 ms a thread writes ``<root>/<pid>/stat`` (Linux layout up to the session field)
    for the user's processes started since the tests began, read from libproc, and removes
    entries of processes that ended.
    macOS does not show other processes' environments, so ``environ`` stays empty: the stop
    finds processes by recorded group and session here; matching by the environment tag is
    covered by a synthetic /proc test. On Linux the real /proc is used instead.
    """

    SZOMB = 5

    def __init__(self):
        self.started = int(time.time()) - 1
        self.root = tempfile.mkdtemp(prefix="baseagent-proc-")
        atexit.register(shutil.rmtree, self.root, True)
        self._libproc = ctypes.CDLL(ctypes.util.find_library("proc"))
        self._seen = {}
        Path(self.root, "self").mkdir()
        Path(self.root, "self", "stat").write_text("0 (self) S 0 0 0\n")
        self.refresh()
        threading.Thread(target=self._loop, name="proc-mirror", daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(0.02)
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 -- keep mirroring; a missed round is caught by the next
                pass

    def refresh(self):
        pids = (ctypes.c_int * 8192)()
        count = self._libproc.proc_listallpids(pids, ctypes.sizeof(pids))
        uid, current = os.getuid(), {}
        for pid in pids[:max(count, 0)]:
            if pid <= 0:
                continue
            info = _BSDInfo()
            try:
                if self._libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) <= 0:
                    raise OSError
                sid = os.getsid(pid)
            except OSError:
                # macOS still lists a zombie but gives no details for it; Linux /proc keeps it as "Z".
                if pid in self._seen:
                    head, _, rest = self._seen[pid].rpartition(") ")
                    current[pid] = f"{head}) Z {rest.split(' ', 1)[1]}"
                continue
            if info.pbi_uid != uid or info.pbi_start_tvsec < self.started:
                continue        # only processes this test run started
            state = "Z" if info.pbi_status == self.SZOMB else "S"
            comm = info.pbi_comm.decode("utf-8", "replace")
            current[pid] = f"{pid} ({comm}) {state} {info.pbi_ppid} {info.pbi_pgid} {sid} 0 0\n"
        for pid, line in current.items():
            if self._seen.get(pid) != line:
                directory = Path(self.root, str(pid))
                directory.mkdir(exist_ok=True)
                (directory / "environ").touch()
                (directory / "stat.tmp").write_text(line)
                os.replace(directory / "stat.tmp", directory / "stat")
        for pid in set(self._seen) - set(current):
            shutil.rmtree(Path(self.root, str(pid)), ignore_errors=True)
        self._seen = current


def _install_proc_view():
    """Point the stop script at a /proc view on machines that have none (macOS)."""
    if os.path.exists("/proc/self/stat"):
        return None
    mirror = ProcMirror()
    exec_backend.PROC_ROOT = mirror.root
    return mirror


PROC_MIRROR = _install_proc_view()
