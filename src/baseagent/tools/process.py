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

# Why a running process is offered to ``on_timeout`` (ProcessHandover.reason).
TIMEOUT = "timeout"
INTERJECTION = "interjection"
# A command must have run this long before an interjection moves it: short commands just finish.
INTERJECTION_GRACE = 1.0


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
    """A still-running command offered to ``run_process(on_timeout=...)``.

    ``reason`` is TIMEOUT (its timeout expired) or INTERJECTION (the user sent a message while it ran).
    The receiver may take over the live process (same pid, not restarted): ``redirect`` atomically
    returns the output captured so far and sends every later chunk to ``sink``. Whoever takes it over
    owns the process, its Windows Job object (may be None) and the reader threads from then on.
    """

    def __init__(self, process, windows_job, readers, capture, *, started_at, timeout, reason=TIMEOUT):
        self.process = process
        self.windows_job = windows_job
        self.readers = readers
        self.started_at = started_at
        self.timeout = timeout
        self.reason = reason
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


def _asks(predicate) -> bool:
    try:
        return bool(predicate())
    except Exception:  # noqa: BLE001 -- a broken signal never moves (or kills) a command
        return False


def _offer(on_timeout, handover):
    try:
        adopted = on_timeout(handover)
    except Exception:  # noqa: BLE001 -- a failed handover leaves the process to the caller
        return None
    return adopted if isinstance(adopted, ToolResult) else None


def _revoke(on_timeout, adopted) -> bool:
    """Undo an interjection handover (stop the job). True only when the stop is confirmed."""
    revoke = getattr(on_timeout, "revoke", None)
    if revoke is None:
        return False
    try:
        return bool(revoke(adopted))
    except Exception:  # noqa: BLE001
        return False


def run_process(argv: list[str], cwd: Path, *, timeout: float, output_limit: int = 20_000, cancellation=None,
                env=None, on_timeout=None, interjection=None,
                interjection_grace: float = INTERJECTION_GRACE) -> ToolResult:
    """Keep at most output_limit bytes per stream while continuously draining pipes.

    ``env`` None inherits the harness environment (unchanged default); a sandbox passes its own.

    ``on_timeout(handover)`` (optional) is called when the timeout expires while the process still runs.
    If it returns a ToolResult it has taken the process over (see ProcessHandover) and that result is
    returned as is; if it returns None the process is killed as usual. Cancellation always kills.

    ``interjection`` (optional, used only with ``on_timeout``) is a predicate polled while the process
    runs. Once it is true and the process has run for ``interjection_grace`` seconds, the process is
    offered to ``on_timeout`` early, at most once, with ``handover.reason == INTERJECTION``. A refusal
    does not kill: the command keeps running under its own timeout and cancellation. Cancellation is
    checked first; if it lands while an early handover happens, the handover is revoked
    (``on_timeout.revoke(result)`` stops the job) and the command reports cancelled.
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
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, shell=False, **kwargs)
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
    early = None  # the result of an interjection handover, while it may still have to be revoked
    offered = interjection is None or on_timeout is None
    try:
        start = time.monotonic()
        deadline = start + timeout
        while process.poll() is None:
            if cancellation is not None and cancellation.cancelled():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if not offered and time.monotonic() - start >= interjection_grace and _asks(interjection):
                offered = True
                early = _offer(on_timeout, ProcessHandover(process, job, readers, capture, started_at=started_at,
                                                           timeout=timeout, reason=INTERJECTION))
                if early is not None:
                    adopted = early
                    break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                pass
    except subprocess.TimeoutExpired:
        timed_out = True
    except BaseException:
        # Ctrl+C in the turn's thread right after a handover: the turn stops, so must the command.
        if early is not None:
            _revoke(on_timeout, early)
        raise
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
    try:
        cancelled_after = early is not None and cancellation is not None and cancellation.cancelled()
    except BaseException:
        _revoke(on_timeout, early)  # Ctrl+C in the turn's thread right here: same as above
        raise
    if cancelled_after:
        # Cancellation raced the handover: it wins; a cancelled command must not live on as a job.
        data = {key: early.data.get(key) for key in ("exit_code", "stdout", "stderr", "stdout_truncated",
                                                     "stderr_truncated", "job_id", "pid")}
        if _revoke(on_timeout, early):
            return ToolResult.failure(ErrorCode.CANCELLED, "command cancelled; partial side effects may exist",
                                      data=data)
        return ToolResult.failure(ErrorCode.CANCELLED,
                                  f"command cancelled while it was being moved to background job {data['job_id']}; "
                                  "it could not be confirmed stopped (check job_list, stop it with job_kill)",
                                  data=data)
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
