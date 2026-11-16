"""Harbor-shaped fake environments for offline tests (no Docker, no Harbor)."""

import asyncio
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace


class ShellEnvironment:
    """``async exec`` like Harbor's docker environment, backed by bash in a local directory.

    Like ``docker exec``: cancelling the call stops waiting for the client, but the process
    keeps running until it ends on its own or something inside the environment kills it.
    Commands get a minimal environment (PATH only), like a task container that never
    received host variables. Every call is recorded for assertions.
    """

    def __init__(self, workdir):
        self.workdir = str(Path(workdir).resolve())
        self.calls = []
        self.fail_with = None

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.calls.append({"command": command, "cwd": cwd, "env": env, "timeout_sec": timeout_sec, "user": user})
        if self.fail_with is not None:
            raise self.fail_with
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        process = subprocess.Popen(["bash", "-c", command], cwd=cwd or self.workdir, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True,
                                   env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})

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


class MappedShellEnvironment(ShellEnvironment):
    """Shows ``alias`` (for example /app) as the working directory, backed by a local directory.

    The agent only ever sees the alias. If anything did native host I/O on the alias path,
    it would hit a path that does not exist on this machine.
    """

    def __init__(self, workdir, alias="/app"):
        super().__init__(workdir)
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
