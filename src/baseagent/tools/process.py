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


def run_process(argv: list[str], cwd: Path, *, timeout: float, output_limit: int = 20_000, cancellation=None) -> ToolResult:
    """Keep at most output_limit bytes per stream while continuously draining pipes."""
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
    buffers = [bytearray(), bytearray()]
    truncated = [False, False]

    def drain(stream: BinaryIO, index: int) -> None:
        try:
            while chunk := stream.read(4096):
                room = output_limit - len(buffers[index])
                buffers[index].extend(chunk[:max(room, 0)])
                truncated[index] |= len(chunk) > room
        finally:
            stream.close()

    readers = [threading.Thread(target=drain, args=(stream, index), daemon=True) for index, stream in enumerate((process.stdout, process.stderr))]
    for reader in readers:
        reader.start()
    timed_out = False
    cancelled = False
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
        # Closing the job (or killing the group) also stops background descendants.
        # This runs for timeout, interruption, errors, and normal command completion.
        if job:
            job.close()
        else:
            _kill_tree(process)
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=2)

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
