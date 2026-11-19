"""Opt-in background jobs: long-running argv commands that outlive a turn.

A job is started without a shell in its own process group (POSIX session / Windows Job object),
its stdout and stderr go to one size-capped log file under ``<root>/.baseagent/jobs/<id>.log``,
and a monitor thread records the exit. Turn cancellation and turn end never touch a job; only
``job_kill``, ``JobManager.close()`` (session/process shutdown) or the end of the owning process
stops it. When the job's main process exits, any descendants it left behind are stopped too.

This is lifetime management, not a sandbox: a job runs with the harness user's rights on the host.
"""

from __future__ import annotations

import atexit
import os
import secrets
import signal
import subprocess
import threading
import time
import weakref
from pathlib import Path

from baseagent.kernel.hooks import AgentMiddleware

from .process import _kill_tree
from .result import ErrorCode, ToolFailure

RUNNING, EXITED, FAILED, KILLED = "running", "exited", "failed", "killed"
JOB_TOOLS = ("start_job", "job_list", "job_output", "job_kill")
DEFAULT_MAX_LOG_BYTES = 10_000_000
DEFAULT_MAX_RUNNING = 8
DEFAULT_MAX_JOBS = 100
MAX_OUTPUT_CHUNK = 20_000
NOTICE_HEADER = "[后台任务通知 / background job notice]"
UNSUPPORTED_BACKEND = ("background jobs need the local backend; they are not available with --backend docker "
                       "(use run_command inside the container instead)")

_managers: "weakref.WeakSet[JobManager]" = weakref.WeakSet()


@atexit.register
def _close_all() -> None:
    # Normal interpreter exit: stop every job this process still owns. On Windows the Job object
    # handle closing at process death does the same even after a crash.
    for manager in list(_managers):
        try:
            manager.close()
        except Exception:  # noqa: BLE001 -- best effort at exit
            pass


class _Job:
    def __init__(self, job_id, argv, cwd, log_path, display_log):
        self.id = job_id
        self.argv = list(argv)
        self.cwd = cwd
        self.log_path = log_path
        self.display_log = display_log
        self.status = RUNNING
        self.exit_code = None
        self.pid = None
        self.started_at = time.time()
        self.ended_at = None
        self.error = None
        self.log_bytes = 0          # bytes written to the log file
        self.output_bytes = 0       # bytes the job produced (including dropped ones)
        self.kill_requested = False
        self.notified = False
        self.process = None
        self.windows_job = None
        self.done = threading.Event()
        self.stop_lock = threading.Lock()

    def describe(self, root):
        cwd = "." if self.cwd == root else self.cwd.relative_to(root).as_posix()
        value = {"job_id": self.id, "status": self.status, "argv": self.argv, "cwd": cwd, "pid": self.pid,
                 "exit_code": self.exit_code, "started_at": round(self.started_at, 3),
                 "ended_at": round(self.ended_at, 3) if self.ended_at is not None else None,
                 "duration_seconds": round((self.ended_at or time.time()) - self.started_at, 3),
                 "log_path": self.display_log, "log_bytes": self.log_bytes, "output_bytes": self.output_bytes,
                 "log_truncated": self.output_bytes > self.log_bytes}
        if self.error:
            value["error"] = self.error
        return value


class JobManager:
    """Thread-safe registry of background jobs for one workspace root."""

    def __init__(self, root, *, log_dir=None, max_log_bytes=DEFAULT_MAX_LOG_BYTES,
                 max_running=DEFAULT_MAX_RUNNING, max_jobs=DEFAULT_MAX_JOBS):
        if type(max_log_bytes) is not int or max_log_bytes < 1:
            raise ValueError("max_log_bytes must be a positive integer")
        self.root = Path(root).resolve()
        self.log_dir = Path(log_dir) if log_dir is not None else self.root / ".baseagent" / "jobs"
        self.max_log_bytes = max_log_bytes
        self.max_running = max_running
        self.max_jobs = max_jobs
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._closed = False
        _managers.add(self)

    # -- lifecycle -------------------------------------------------------------------------
    def start(self, argv, cwd=None) -> dict:
        cwd = self.root if cwd is None else Path(cwd)
        if not cwd.is_dir():
            raise ToolFailure(ErrorCode.NOT_FOUND, "cwd is not an existing directory")
        with self._lock:
            if self._closed:
                raise ToolFailure(ErrorCode.EXECUTION_FAILED, "job manager is closed")
            if sum(job.status == RUNNING for job in self._jobs.values()) >= self.max_running:
                raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, f"at most {self.max_running} background jobs may run at once")
            if len(self._jobs) >= self.max_jobs:
                raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, f"at most {self.max_jobs} background jobs per session")
            job_id = "j" + secrets.token_hex(4)
            while job_id in self._jobs:
                job_id = "j" + secrets.token_hex(4)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / f"{job_id}.log"
            try:
                display = log_path.relative_to(self.root).as_posix()
            except ValueError:
                display = str(log_path)
            job = _Job(job_id, argv, cwd, log_path, display)
            log = open(log_path, "xb")  # never overwrite another run's log
            try:
                self._launch(job)
            except BaseException:
                log.close()
                log_path.unlink(missing_ok=True)
                raise
            self._jobs[job_id] = job
        reader = threading.Thread(target=self._drain, args=(job, log), name=f"job-{job_id}-log", daemon=True)
        reader.start()
        threading.Thread(target=self._monitor, args=(job, reader), name=f"job-{job_id}-wait", daemon=True).start()
        return job.describe(self.root)

    def _launch(self, job):
        windows_job = None
        if os.name == "nt":
            from ._windows_job import WindowsJob
            windows_job = WindowsJob()
            # No console: console Ctrl+C / Ctrl+Break never reach the job.
            kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        else:
            # Own session: terminal SIGINT/SIGHUP for the harness do not reach the job.
            kwargs = {"start_new_session": True}
        try:
            process = subprocess.Popen(job.argv, cwd=job.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, shell=False, **kwargs)
        except BaseException:
            if windows_job:
                windows_job.close()
            raise
        if windows_job:
            try:
                windows_job.assign(int(process._handle))
            except BaseException:
                _kill_tree(process)
                process.wait(timeout=5)
                windows_job.close()
                process.stdout.close()
                raise
        job.process, job.windows_job, job.pid = process, windows_job, process.pid

    def _drain(self, job, log):
        stream = job.process.stdout
        try:
            while chunk := stream.read1(65536):
                with self._lock:
                    room = self.max_log_bytes - job.log_bytes
                    job.output_bytes += len(chunk)
                    kept = chunk[:max(room, 0)]
                    job.log_bytes += len(kept)
                if kept and log is not None:
                    try:
                        log.write(kept)
                        log.flush()
                    except OSError:
                        # Disk full or similar: keep draining so the job never blocks on a full pipe.
                        log.close()
                        log = None
        except (OSError, ValueError):
            pass
        finally:
            if log is not None:
                log.close()
            stream.close()

    def _monitor(self, job, reader):
        process = job.process
        error = None
        try:
            process.wait()
        except BaseException as exc:  # noqa: BLE001 -- recorded as a failed job
            error = f"monitor failed: {type(exc).__name__}"
        # The main process is gone: stop descendants it left behind (they would hold the log pipe).
        self._terminate(job)
        reader.join(timeout=5)
        with self._lock:
            job.exit_code = process.returncode
            job.ended_at = time.time()
            if job.kill_requested:
                job.status = KILLED
            elif error is not None or process.returncode != 0:
                job.status = FAILED
                job.error = error
            else:
                job.status = EXITED
        job.done.set()

    @staticmethod
    def _terminate(job):
        """Stop the job's whole process tree. Safe to call from several threads."""
        with job.stop_lock:
            windows_job, job.windows_job = job.windows_job, None
        if windows_job is not None:
            try:
                windows_job.close()   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: every process in the job ends
            except OSError:
                pass
        elif os.name != "nt":
            try:
                os.killpg(job.process.pid, signal.SIGKILL)   # also after the leader exited
            except OSError:
                pass
        if job.process.poll() is None:
            _kill_tree(job.process)

    def kill(self, job_id, *, wait=5.0) -> dict:
        job = self._get(job_id)
        with self._lock:
            running = job.status == RUNNING
            if running:
                job.kill_requested = True
        if running:
            self._terminate(job)
            job.done.wait(wait)
        value = self.describe(job_id)
        value["killed"] = running
        return value

    def close(self, *, wait=5.0) -> None:
        """Kill every running job (session/process shutdown). Idempotent."""
        with self._lock:
            self._closed = True
            jobs = [job for job in self._jobs.values() if job.status == RUNNING]
        for job in jobs:
            try:
                self.kill(job.id, wait=wait)
            except ToolFailure:
                pass

    # -- queries ---------------------------------------------------------------------------
    def _get(self, job_id) -> _Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ToolFailure(ErrorCode.NOT_FOUND, f"unknown job: {job_id}")
        return job

    def describe(self, job_id) -> dict:
        job = self._get(job_id)
        with self._lock:
            return job.describe(self.root)

    def list(self) -> dict:
        with self._lock:
            jobs = [job.describe(self.root) for job in self._jobs.values()]
        return {"jobs": jobs, "running": sum(job["status"] == RUNNING for job in jobs)}

    def wait(self, job_id, timeout=None) -> dict:
        self._get(job_id).done.wait(timeout)
        return self.describe(job_id)

    def output(self, job_id, offset=0, limit=8000, tail=None) -> dict:
        job = self._get(job_id)
        if type(limit) is not int or not 1 <= limit <= MAX_OUTPUT_CHUNK:
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, f"limit must be 1-{MAX_OUTPUT_CHUNK}")
        if type(offset) is not int or offset < 0 or (tail is not None and (type(tail) is not int or tail < 1)):
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "offset must be >= 0 and tail >= 1")
        with self._lock:
            info = job.describe(self.root)
            size = job.log_bytes
        if tail is not None:
            offset = max(0, size - min(tail, limit))
        offset = min(offset, size)
        data = b""
        if size > offset:
            with open(job.log_path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(min(limit, size - offset))
        next_offset = offset + len(data)
        return {**info, "offset": offset, "next_offset": next_offset,
                "content": data.decode("utf-8", errors="replace"),
                "more": next_offset < size,
                "complete": info["status"] != RUNNING and next_offset >= size}

    def take_notices(self) -> list[dict]:
        """Finished jobs not yet announced to the agent (each is returned once)."""
        with self._lock:
            finished = [job for job in self._jobs.values() if job.status != RUNNING and not job.notified]
            for job in finished:
                job.notified = True
            return [job.describe(self.root) for job in finished]


class JobNoticeMiddleware(AgentMiddleware):
    """Tell the agent, before its next model call, which background jobs finished since the last notice."""

    def __init__(self, manager: JobManager):
        self.manager = manager

    def before_model(self, state):
        notices = self.manager.take_notices()
        if notices:
            lines = [f"- {item['job_id']} {item['status']} (exit code {item['exit_code']}, "
                     f"{item['log_bytes']} log bytes): {' '.join(item['argv'])[:200]}" for item in notices]
            state.messages.append({"role": "user", "content": NOTICE_HEADER + "\n" + "\n".join(lines)
                                   + "\nUse job_output to read the log."})


# -- tool handlers ----------------------------------------------------------------------------
def _manager(workspace) -> JobManager:
    manager = getattr(workspace, "jobs", None)
    if manager is None:
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, UNSUPPORTED_BACKEND)
    return manager


def _require_commands(workspace):
    if not workspace.allow_command:
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "background jobs run commands; start with --allow-command")


def register_job_tools(registry, workspace) -> None:
    """Register start_job / job_list / job_output / job_kill on ``registry`` for ``workspace``."""

    def start_job(argv, cwd=".", *, context=None):
        _require_commands(workspace)
        # Same argv validation as run_command; the timeout argument does not apply to jobs.
        workspace.validate_command(argv, 1)
        manager = _manager(workspace)
        directory = workspace.root if cwd in (".", "") else workspace._path(cwd)
        if context is not None and context.cancellation is not None:
            context.cancellation.check()
        return manager.start(argv, directory)

    def job_list():
        return _manager(workspace).list()

    def job_output(job_id, offset=0, limit=8000, tail=None):
        return _manager(workspace).output(job_id, offset=offset, limit=limit, tail=tail)

    def job_kill(job_id):
        _require_commands(workspace)
        return _manager(workspace).kill(job_id)

    def params(properties, required):
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    argv = {"type": "array", "minItems": 1, "maxItems": 128, "items": {"type": "string", "minLength": 1, "maxLength": 4000}}
    job_id = {"type": "string", "pattern": "^j[0-9a-f]{8}$"}
    registry.register("start_job", "Start a long-running argv command (no shell) as a background job in the workspace and "
                      "return its job_id at once; it keeps running across turns and cancellation until job_kill. "
                      "Output goes to a size-capped log; requires command permission",
                      params({"argv": argv, "cwd": {"type": "string", "minLength": 1, "maxLength": 4000}}, ["argv"]),
                      start_job, contextual=True)
    registry.register("job_list", "List background jobs with status (running/exited/failed/killed), exit code and times",
                      params({}, []), job_list, retry_safe=True)
    registry.register("job_output", "Read a background job's combined stdout/stderr log by byte offset/limit, or the last "
                      "tail bytes; next_offset continues paging",
                      params({"job_id": job_id, "offset": {"type": "integer", "minimum": 0},
                              "limit": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_CHUNK},
                              "tail": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_CHUNK}}, ["job_id"]),
                      job_output, retry_safe=True)
    registry.register("job_kill", "Stop a background job and its whole process tree; requires command permission",
                      params({"job_id": job_id}, ["job_id"]), job_kill)
