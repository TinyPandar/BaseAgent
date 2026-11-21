"""Bounded subprocess capture with timeout cleanup."""

from __future__ import annotations

import os
import math
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO

from .result import ErrorCode, ToolResult


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        finally:
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


class ProcessHandover:
    """A command still running at its timeout, offered to ``run_process(on_timeout=...)``.

    The receiver may take over the live process (same pid, not restarted): ``redirect`` atomically
    returns the output captured so far and sends every later chunk to ``sink``. Whoever takes it over
    owns the process, its Windows Job object (may be None) and the reader threads from then on.
    """

    def __init__(self, process, windows_job, readers, capture, *, started_at, timeout):
        self.process = process
        self.windows_job = windows_job
        self.readers = readers
        self.started_at = started_at
        self.timeout = timeout
        self._capture = capture

    def redirect(self, sink) -> tuple[bytes, bytes, bool, bool]:
        """Return (stdout, stderr, stdout_truncated, stderr_truncated) so far; later output goes to sink.

        The captured stdout, then stderr, are passed to sink first, before any later chunk.
        """
        capture = self._capture
        with capture.lock:
            snapshot = (bytes(capture.buffers[0]), bytes(capture.buffers[1]), *capture.truncated)
            for data in snapshot[:2]:
                if data:
                    sink(data)
            capture.sink = sink
        return snapshot


class _Capture:
    def __init__(self, limit):
        self.limit = limit
        self.buffers = [bytearray(), bytearray()]
        self.truncated = [False, False]
        self.lock = threading.Lock()
        self.sink = None

    def drain(self, stream: BinaryIO, index: int) -> None:
        try:
            # read1: forward output as it arrives (matters once a handover redirects it to a job log).
            while chunk := stream.read1(4096):
                with self.lock:
                    sink = self.sink
                    if sink is None:
                        room = self.limit - len(self.buffers[index])
                        self.buffers[index].extend(chunk[:max(room, 0)])
                        self.truncated[index] |= len(chunk) > room
                if sink is not None:
                    try:
                        sink(chunk)
                    except Exception:  # noqa: BLE001 -- keep draining so the process never blocks
                        pass
        finally:
            stream.close()


def run_process(argv: list[str], cwd: Path, *, timeout: float, output_limit: int = 20_000, cancellation=None,
                on_timeout=None) -> ToolResult:
    """Keep at most output_limit bytes per stream while continuously draining pipes.

    ``on_timeout(handover)`` (optional) is called when the timeout expires while the process still runs.
    If it returns a ToolResult it has taken the process over (see ProcessHandover) and that result is
    returned as is; if it returns None the process is killed as usual. Cancellation always kills.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    if type(output_limit) is not int or output_limit < 1:
        raise ValueError("output_limit must be a positive integer")
    if cancellation is not None:
        cancellation.check()
    job = None
    if os.name == "nt":
        from ._windows_job import WindowsJob
        job = WindowsJob()
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, shell=False, **kwargs)
    except BaseException:
        if job:
            job.close()
        raise
    if job:
        try:
            # Popen owns this native handle for the lifetime of the child process.
            job.assign(int(process._handle))
        except BaseException:
            _kill_tree(process)
            process.wait(timeout=5)
            job.close()
            process.stdout.close()
            process.stderr.close()
            raise
    started_at = time.time()
    capture = _Capture(output_limit)
    buffers, truncated = capture.buffers, capture.truncated
    readers = [threading.Thread(target=capture.drain, args=(stream, index), daemon=True) for index, stream in enumerate((process.stdout, process.stderr))]
    for reader in readers:
        reader.start()
    timed_out = False
    cancelled = False
    adopted = None
    try:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancellation is not None and cancellation.cancelled():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                pass
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        if timed_out and on_timeout is not None and process.poll() is None:
            try:
                adopted = on_timeout(ProcessHandover(process, job, readers, capture, started_at=started_at, timeout=timeout))
            except Exception:  # noqa: BLE001 -- a failed handover falls back to the normal kill
                adopted = None
            if not isinstance(adopted, ToolResult):
                adopted = None
        if adopted is None:
            # Closing the job (or killing the group) also stops background descendants.
            # This runs for timeout, interruption, errors, and normal command completion.
            if job:
                job.close()
            else:
                _kill_tree(process)
            process.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=2)
    if adopted is not None:
        return adopted

    data = {
        "exit_code": process.returncode,
        "stdout": buffers[0].decode("utf-8", errors="replace"),
        "stderr": buffers[1].decode("utf-8", errors="replace"),
        "stdout_truncated": truncated[0], "stderr_truncated": truncated[1],
    }
    if cancelled:
        return ToolResult.failure(ErrorCode.CANCELLED, "command cancelled; partial side effects may exist", data=data)
    if timed_out:
        return ToolResult.failure(ErrorCode.TIMEOUT, f"command timed out after {timeout} seconds", data=data)
    if process.returncode:
        return ToolResult.failure(ErrorCode.EXECUTION_FAILED, f"command exited with code {process.returncode}", data=data)
    return ToolResult(data=data)
