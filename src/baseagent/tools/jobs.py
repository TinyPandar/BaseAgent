"""Opt-in background jobs: long-running argv commands that outlive a turn.

Three kinds of job share one registry and one record format:

* ``job``      -- ``start_job``: started without a shell in its own process group (POSIX session /
  Windows Job object); stdout+stderr go through a pipe to a size-capped log. Ends with the session.
* ``detached`` -- ``start_job(keep_running=true)``: a small runner process (``_RUNNER``) started outside
  the harness's process group / Job object owns the command, writes the capped log and records the
  exit status itself, so the job survives the harness and can be inspected after a restart.
* ``run_command`` -- a ``run_command`` that hit its timeout while background jobs are on: the same live
  process (not restarted) is taken over from ``run_process``; the output captured so far starts the log
  and later output is appended. Its pipes end in the harness, so it ends with the session.

Every job has a JSON record ``<root>/.baseagent/jobs/<id>.json`` (atomic replace) next to its log.
A new JobManager loads earlier records; a record whose process is gone is ``lost``; a live pid is only
trusted after its creation time matches the recorded one (pid reuse), otherwise it is ``unknown`` and
``job_kill`` refuses it.

Turn cancellation and turn end never touch a job. This is lifetime management, not a sandbox: a job runs
with the harness user's rights on the host. All process creation in this module goes through
``spawn_job_process`` so it can later be routed through an OS sandbox.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path

from baseagent.kernel.hooks import AgentMiddleware

from . import job_links
from .process import INTERJECTION, TIMEOUT, _kill_tree
from .result import ErrorCode, ToolFailure, ToolResult

RUNNING, EXITED, FAILED, KILLED = "running", "exited", "failed", "killed"
LOST, UNKNOWN = "lost", "unknown"
TERMINAL = (EXITED, FAILED, KILLED, LOST)
JOB_TOOLS = ("start_job", "job_list", "job_output", "job_kill", "job_wait", "job_settle")
DEFAULT_MAX_LOG_BYTES = 10_000_000
DEFAULT_MAX_RUNNING = 8
DEFAULT_MAX_JOBS = 100
DEFAULT_KEEP_FINISHED = 50      # finished records (and logs) kept on disk; older ones are deleted at load
DEFAULT_LIST_LIMIT = 30         # job_list shows the newest N (running/unknown jobs always)
MAX_OUTPUT_CHUNK = 20_000
NOTICE_TAIL_BYTES = 600
NOTICE_TAIL_LINES = 5
RECORD_VERSION = 1
STARTUP_TIMEOUT = 15.0
NOTICE_HEADER = "[后台任务通知 / background job notice]"
UNSUPPORTED_BACKEND = ("background jobs need the local backend; they are not available with --backend docker "
                       "because jobs would run on the host, outside the container. Use run_command inside the "
                       "container (bounded by its timeout), or run without --backend docker to use background jobs")
SLEEP_COMMANDS = {"sleep", "sleep.exe"}
_RECORD_NAME = re.compile(r"^(j[0-9a-f]{8})\.json$")

_managers: "weakref.WeakSet[JobManager]" = weakref.WeakSet()


@atexit.register
def _close_all() -> None:
    # Normal interpreter exit: stop every job this process still owns (keep_running jobs excepted).
    # On Windows the Job object handle closing at process death does the same even after a crash.
    for manager in list(_managers):
        try:
            manager.close()
        except Exception:  # noqa: BLE001 -- best effort at exit
            pass


# -- process identity (pid reuse guard) -------------------------------------------------------------
def _kernel32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetExitCodeProcess.restype = wintypes.BOOL
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k.GetProcessTimes.restype = wintypes.BOOL
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.TerminateProcess.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k


def _win_identity(k, handle):
    import ctypes
    from ctypes import wintypes
    code = wintypes.DWORD()
    if not k.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
        return False, None
    times = [wintypes.FILETIME() for _ in range(4)]
    if not k.GetProcessTimes(handle, *[ctypes.byref(item) for item in times]):
        return True, None
    return True, f"win:{(times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}"


def probe_process(pid) -> tuple[bool | None, str | None]:
    """(alive, identity): identity is an opaque creation-time token, None when it cannot be read.

    alive is None when it cannot be determined either way.
    """
    if type(pid) is not int or pid <= 0:
        return False, None
    if os.name == "nt":
        import ctypes
        k = _kernel32()
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            return (True, None) if error == 5 else (False, None)  # 5 = access denied: exists, not ours
        try:
            return _win_identity(k, handle)
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        pass
    except OSError:
        return None, None
    stat = Path(f"/proc/{pid}/stat")
    if stat.parent.parent.is_dir() and Path("/proc/self").exists():
        try:
            fields = stat.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
        except OSError:
            return False, None   # vanished in between
        if fields[0] in ("Z", "X"):
            return False, None
        return True, f"linux:{fields[19]}"   # field 22: start time in clock ticks since boot
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True, None
    text = out.stdout.strip()
    return (True, f"ps:{text}") if out.returncode == 0 and text else (True, None)


def _kill_verified(pid, identity) -> bool:
    """Kill pid's process tree only if it is still the process with the recorded identity."""
    if not identity:
        return False
    if os.name == "nt":
        k = _kernel32()
        handle = k.OpenProcess(0x1001, False, pid)  # PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            alive, current = _win_identity(k, handle)
            if not alive or current != identity:
                return False
            # A job's leader owns the Job object of its tree (KILL_ON_JOB_CLOSE): the tree ends with it.
            return bool(k.TerminateProcess(handle, 1))
        finally:
            k.CloseHandle(handle)
    alive, current = probe_process(pid)
    if not alive or current != identity:
        return False
    try:
        os.killpg(pid, signal.SIGKILL)   # the leader started its own session: pgid == pid
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return False
    return True


# -- the detached runner ------------------------------------------------------------------------------
# Runs as ``python -c _RUNNER <spec json>``, outside the harness's process group / Job object. It starts
# the command in its own process tree (POSIX: the runner's session; Windows: a KILL_ON_JOB_CLOSE Job
# object the runner holds), writes the size-capped log, then records the exit status atomically.
# Killing the runner (killpg on POSIX, TerminateProcess on Windows) ends the whole tree.
_RUNNER = r"""
import json, os, signal, subprocess, sys, threading, time
spec = json.loads(sys.argv[1])
base = spec["base"]
def put(suffix, value):
    tmp = base + suffix + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    for attempt in range(100):
        try:
            os.replace(tmp, base + suffix)
            return
        except PermissionError:
            time.sleep(0.02)
    os.replace(tmp, base + suffix)
wjob = None
kwargs = {}
if os.name == "nt":
    import importlib.util
    module_spec = importlib.util.spec_from_file_location("_baseagent_windows_job", spec["windows_job"])
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    wjob = module.WindowsJob()
    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
try:
    child = subprocess.Popen(spec["argv"], cwd=spec["cwd"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, shell=False, **kwargs)
except FileNotFoundError as exc:
    put(".run.json", {"error": "not_found", "message": str(exc)})
    sys.exit(127)
except OSError as exc:
    put(".run.json", {"error": "execution_failed", "message": f"{type(exc).__name__}: {exc}"})
    sys.exit(126)
if wjob is not None:
    try:
        wjob.assign(int(child._handle))
    except OSError as exc:
        child.kill()
        put(".run.json", {"error": "execution_failed", "message": f"job object: {exc}"})
        sys.exit(126)
put(".run.json", {"pid": child.pid})
limit = spec["max_log_bytes"]
counts = {"log": 0, "out": 0}
def drain():
    log = open(base + ".log", "ab")
    try:
        while chunk := child.stdout.read1(65536):
            counts["out"] += len(chunk)
            kept = chunk[:max(limit - counts["log"], 0)]
            if kept and log is not None:
                try:
                    log.write(kept)
                    log.flush()
                    counts["log"] += len(kept)
                except OSError:
                    log.close()
                    log = None
    finally:
        if log is not None:
            log.close()
reader = threading.Thread(target=drain, daemon=True)
reader.start()
code = child.wait()
if wjob is not None:
    wjob.close()           # leftover descendants end with the command
    reader.join(5)
else:
    reader.join(1)
put(".exit.json", {"exit_code": code, "ended_at": time.time(), "log_bytes": counts["log"], "output_bytes": counts["out"]})
if os.name != "nt":
    os.killpg(0, signal.SIGKILL)   # leftover descendants (and this runner, whose work is done)
"""


def spawn_job_process(argv, cwd, *, detached=False, sandbox=None):
    """The one place jobs.py creates processes. Returns (Popen, WindowsJob | None).

    Attached: ``argv`` is the command itself, stdout+stderr piped, in its own process group (POSIX
    session) or a KILL_ON_JOB_CLOSE Job object owned by the harness (Windows).
    Detached: ``argv`` is the runner command line; it gets no pipes, its own session (POSIX) or breaks
    away from any Job object the harness is in (Windows, when allowed), so it outlives the harness.
    ``sandbox`` (backends.sandbox.Sandbox) confines an attached command; detached runners are refused
    by the caller under a sandbox. SandboxUnavailable propagates: never fall back to running unconfined.
    """
    env = None
    if sandbox is not None:
        if detached:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "keep_running jobs are not supported with a command sandbox")
        from baseagent.backends import sandbox as os_sandbox
        try:
            argv, env = os_sandbox.wrap(argv, cwd, None, sandbox)
        except os_sandbox.SandboxUnavailable as exc:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, str(exc)) from None
    if detached:
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, shell=False,
                                           creationflags=flags | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
            except PermissionError:
                # The harness's own Job object forbids breakaway: the runner then ends with that Job.
                process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, shell=False, creationflags=flags)
        else:
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, shell=False, start_new_session=True)
        return process, None
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
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
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
    return process, windows_job


def _write_json(path: Path, value) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    for _ in range(100):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:   # Windows: a reader has the target open for a moment
            time.sleep(0.02)
    os.replace(tmp, path)


def _read_json(path: Path):
    for _ in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (PermissionError, json.JSONDecodeError):
            time.sleep(0.02)
        except (OSError, UnicodeError):
            return None
    return None


class _Job:
    def __init__(self, job_id, argv, cwd, log_path, display_log, *, kind="job", keep_running=False, owner=None):
        self.id = job_id
        self.argv = list(argv)
        self.cwd = cwd                  # Path for jobs of this manager, relative str for loaded records
        self.log_path = log_path
        self.display_log = display_log
        self.kind = kind
        self.keep_running = keep_running
        self.owner = owner
        self.status = RUNNING
        self.exit_code = None
        self.pid = None
        self.leader_pid = None          # process whose tree is the job (killed by job_kill)
        self.leader_identity = None
        self.started_at = time.time()
        self.ended_at = None
        self.error = None
        self.log_bytes = 0              # bytes written to the log file
        self.output_bytes = 0           # bytes the job produced (including dropped ones)
        self.kill_requested = False
        self.notified = False
        self.loaded = False             # record from an earlier JobManager / process
        self.process = None
        self.windows_job = None
        self.log = None
        self.write_lock = threading.Lock()
        self.done = threading.Event()
        self.stop_lock = threading.Lock()
        self.link = job_links.new_link(job_id)   # purpose, responsible agent, delivery (tools/job_links.py)

    def describe(self, root):
        if isinstance(self.cwd, Path):
            cwd = "." if self.cwd == root else self.cwd.relative_to(root).as_posix()
        else:
            cwd = self.cwd
        end = self.ended_at if self.ended_at is not None else (None if self.status in TERMINAL else time.time())
        value = {"job_id": self.id, "status": self.status, "argv": self.argv, "cwd": cwd, "pid": self.pid,
                 "exit_code": self.exit_code, "started_at": round(self.started_at, 3),
                 "ended_at": round(self.ended_at, 3) if self.ended_at is not None else None,
                 "duration_seconds": round(end - self.started_at, 3) if end is not None else None,
                 "log_path": self.display_log, "log_bytes": self.log_bytes, "output_bytes": self.output_bytes,
                 "log_truncated": self.output_bytes > self.log_bytes}
        if self.kind != "job":
            value["kind"] = self.kind
        if self.keep_running:
            value["keep_running"] = True
        if self.loaded:
            value["from_earlier_process"] = True
        if self.error:
            value["error"] = self.error
        return value

    def record(self, root):
        value = self.describe(root)
        value.pop("duration_seconds", None)
        value.pop("from_earlier_process", None)
        value.update({"version": RECORD_VERSION, "kind": self.kind, "keep_running": self.keep_running,
                      "owner_session": self.owner, "leader_pid": self.leader_pid,
                      "leader_identity": self.leader_identity, "notified": self.notified,
                      "kill_requested": self.kill_requested, "error": self.error})
        return value


class JobManager:
    """Thread-safe registry of background jobs for one workspace root, persisted under log_dir."""

    def __init__(self, root, *, log_dir=None, max_log_bytes=DEFAULT_MAX_LOG_BYTES,
                 max_running=DEFAULT_MAX_RUNNING, max_jobs=DEFAULT_MAX_JOBS,
                 keep_finished=DEFAULT_KEEP_FINISHED, owner=None, sandbox=None):
        if type(max_log_bytes) is not int or max_log_bytes < 1:
            raise ValueError("max_log_bytes must be a positive integer")
        self.root = Path(root).resolve()
        # backends.sandbox.Sandbox or None. When set, every job process is spawned through it (fail closed).
        self.sandbox = sandbox
        self.log_dir = Path(log_dir) if log_dir is not None else self.root / ".baseagent" / "jobs"
        self.max_log_bytes = max_log_bytes
        self.max_running = max_running
        self.max_jobs = max_jobs
        self.keep_finished = keep_finished
        self.owner = owner              # session id; completion notices only go to the owning session
        self._jobs: dict[str, _Job] = {}
        self._started = 0
        self._lock = threading.Lock()
        self._link_lock = threading.Lock()
        # Waits in progress (tools/job_wait.py), for status display: key -> {"job_id", "agent", "phase", "since"}.
        self._waits: dict[str, dict] = {}
        self.wait_observers: list = []   # called without arguments when a wait starts, changes phase or ends
        self._closed = False
        self._load(prune=True)
        _managers.add(self)

    # -- persistence -----------------------------------------------------------------------------
    def _display(self, path):
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def _save(self, job, *, strict=False) -> None:
        """Write the job record. Later updates tolerate write errors (the in-memory registry stays
        authoritative for this process); ``strict`` raises them (a job is only created with a record)."""
        with self._lock:
            value = job.record(self.root)
        try:
            _write_json(self.log_dir / f"{job.id}.json", value)
        except OSError:
            if strict:
                raise

    def _link_path(self, job_id):
        return self.log_dir / f"{job_id}{job_links.LINK_SUFFIX}"

    def _save_link(self, job, *, strict=False) -> None:
        with self._link_lock:
            value = json.loads(json.dumps(job.link, ensure_ascii=False))
            try:
                _write_json(self._link_path(job.id), value)
            except OSError:
                if strict:
                    raise

    def _load(self, *, prune=False) -> None:
        """Pick up records written by earlier (or other) processes."""
        if not self.log_dir.is_dir():
            return
        loaded = []
        for path in sorted(self.log_dir.iterdir()):
            match = _RECORD_NAME.match(path.name)
            if not match or match.group(1) in self._jobs:
                continue
            value = _read_json(path)
            if not isinstance(value, dict) or value.get("version") != RECORD_VERSION or value.get("job_id") != match.group(1):
                continue
            try:
                job = _Job(value["job_id"], value["argv"], str(value.get("cwd") or "."),
                           self.log_dir / f"{value['job_id']}.log", value.get("log_path") or "",
                           kind=value.get("kind", "job"), keep_running=bool(value.get("keep_running")),
                           owner=value.get("owner_session"))
                job.status = value["status"] if value.get("status") in (*TERMINAL, RUNNING) else RUNNING
                job.exit_code = value.get("exit_code")
                job.pid = value.get("pid")
                job.leader_pid = value.get("leader_pid")
                job.leader_identity = value.get("leader_identity")
                job.started_at = float(value["started_at"])
                job.ended_at = value.get("ended_at")
                job.error = value.get("error")
                job.log_bytes = int(value.get("log_bytes") or 0)
                job.output_bytes = int(value.get("output_bytes") or 0)
                job.notified = bool(value.get("notified"))
                job.kill_requested = bool(value.get("kill_requested"))
            except (KeyError, TypeError, ValueError):
                continue
            job.loaded = True
            job.link = job_links.read_link(_read_json(self._link_path(job.id)), job.id) or job_links.legacy_link(
                job.id, owner=job.owner, notified=job.notified)
            if job.status in TERMINAL:
                job.done.set()
            loaded.append(job)
        with self._lock:
            for job in loaded:
                self._jobs.setdefault(job.id, job)
        for job in loaded:
            self._refresh(job)
        if prune:
            self._prune()

    def _prune(self) -> None:
        with self._lock:
            finished = sorted((job for job in self._jobs.values() if job.loaded and job.status in TERMINAL),
                              key=lambda job: job.started_at, reverse=True)
            dropped = finished[self.keep_finished:]
            for job in dropped:
                del self._jobs[job.id]
        for job in dropped:
            for suffix in (".json", ".log", ".run.json", ".exit.json", job_links.LINK_SUFFIX):
                try:
                    (self.log_dir / f"{job.id}{suffix}").unlink(missing_ok=True)
                except OSError:
                    pass

    def _apply_exit(self, job) -> bool:
        value = _read_json(self.log_dir / f"{job.id}.exit.json")
        if not isinstance(value, dict):
            return False
        with self._lock:
            job.exit_code = value.get("exit_code")
            job.ended_at = value.get("ended_at") or time.time()
            job.log_bytes = int(value.get("log_bytes") or 0)
            job.output_bytes = int(value.get("output_bytes") or 0)
            if job.kill_requested:
                job.status = KILLED
            else:
                job.status = EXITED if job.exit_code == 0 else FAILED
        return True

    def _refresh(self, job) -> None:
        """Re-derive the state of a record loaded from disk (not a child of this process)."""
        if not job.loaded or job.status not in (RUNNING, UNKNOWN):
            return
        self._sync_log_size(job)
        if job.kind == "detached" and self._apply_exit(job):
            job.done.set()
            self._save(job)
            return
        alive, identity = probe_process(job.leader_pid)
        with self._lock:
            if alive is False or (alive and identity and job.leader_identity and identity != job.leader_identity):
                # Gone, or the pid now belongs to another process: the job ended without a recorded status.
                job.status = LOST
                job.error = job.error or "process ended while no harness was watching it; exit code unknown"
            elif alive and identity and identity == job.leader_identity:
                job.status = RUNNING
            else:
                job.status = UNKNOWN
            lost = job.status == LOST
        if lost:
            job.done.set()
            self._save(job)

    def _sync_log_size(self, job) -> None:
        if job.kind == "detached" or job.loaded:
            try:
                size = job.log_path.stat().st_size
            except OSError:
                return
            with self._lock:
                if job.status in (RUNNING, UNKNOWN) or size > job.log_bytes:
                    job.log_bytes = size
                    job.output_bytes = max(job.output_bytes, size)

    # -- lifecycle -------------------------------------------------------------------------
    def _reserve(self, argv, cwd, *, kind, keep_running) -> _Job:
        """Check limits and create the job's empty log (caller holds self._lock)."""
        if self._closed:
            raise ToolFailure(ErrorCode.EXECUTION_FAILED, "job manager is closed")
        running = sum(job.status in (RUNNING, UNKNOWN) for job in self._jobs.values())
        if running >= self.max_running:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, f"at most {self.max_running} background jobs may run at once")
        if self._started >= self.max_jobs:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, f"at most {self.max_jobs} background jobs per session")
        job_id = "j" + secrets.token_hex(4)
        while job_id in self._jobs or (self.log_dir / f"{job_id}.json").exists():
            job_id = "j" + secrets.token_hex(4)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{job_id}.log"
        job = _Job(job_id, argv, cwd, log_path, self._display(log_path), kind=kind,
                   keep_running=keep_running, owner=self.owner)
        job.log = open(log_path, "xb")  # never overwrite another run's log
        return job

    def _discard(self, job) -> None:
        if job.log is not None:
            job.log.close()
            job.log = None
        for suffix in (".log", ".json", ".run.json", ".exit.json", job_links.LINK_SUFFIX):
            (self.log_dir / f"{job.id}{suffix}").unlink(missing_ok=True)

    def start(self, argv, cwd=None, *, keep_running=False, link=None) -> dict:
        """``link``: who starts it and why (``job_links.job_context``); None leaves both unknown."""
        cwd = self.root if cwd is None else Path(cwd)
        if not cwd.is_dir():
            raise ToolFailure(ErrorCode.NOT_FOUND, "cwd is not an existing directory")
        with self._lock:
            job = self._reserve(argv, cwd, kind="detached" if keep_running else "job", keep_running=keep_running)
            try:
                if keep_running:
                    job.log.close()
                    job.log = None
                    self._launch_detached(job)
                else:
                    self._launch(job)
            except BaseException:
                self._discard(job)
                raise
            job.link = job_links.new_link(job.id, **(link or {}))
            self._jobs[job.id] = job
            self._started += 1
        self._save_link(job)
        self._save(job)
        if keep_running:
            threading.Thread(target=self._monitor_detached, args=(job,), name=f"job-{job.id}-wait", daemon=True).start()
        else:
            reader = threading.Thread(target=self._drain, args=(job,), name=f"job-{job.id}-log", daemon=True)
            reader.start()
            threading.Thread(target=self._monitor, args=(job, [reader]), name=f"job-{job.id}-wait", daemon=True).start()
        return self.describe(job.id)

    def _launch(self, job):
        process, windows_job = spawn_job_process(job.argv, job.cwd, sandbox=self.sandbox)
        job.process, job.windows_job, job.pid = process, windows_job, process.pid
        job.leader_pid = process.pid
        job.leader_identity = probe_process(process.pid)[1]

    def _launch_detached(self, job):
        base = str(self.log_dir / job.id)
        spec = {"argv": job.argv, "cwd": str(job.cwd), "base": base, "max_log_bytes": self.max_log_bytes,
                "windows_job": str(Path(__file__).with_name("_windows_job.py"))}
        # Under a sandbox this raises: the runner writes into .baseagent (read-only there) and outlives the
        # per-run sandbox temp dir, so keep_running is refused rather than run unconfined.
        runner, _ = spawn_job_process([sys.executable, "-c", _RUNNER, json.dumps(spec)], job.cwd, detached=True,
                                      sandbox=self.sandbox)
        job.process = runner
        job.leader_pid = runner.pid
        job.leader_identity = probe_process(runner.pid)[1]
        started = Path(base + ".run.json")
        deadline = time.monotonic() + STARTUP_TIMEOUT
        info = None
        while time.monotonic() < deadline:
            info = _read_json(started)
            if info is not None or runner.poll() is not None:
                info = info if info is not None else _read_json(started)
                break
            time.sleep(0.02)
        if not isinstance(info, dict) or "pid" not in info:
            if runner.poll() is None:
                _kill_tree(runner)
                runner.wait(timeout=5)
            if isinstance(info, dict) and info.get("error") == "not_found":
                raise ToolFailure(ErrorCode.NOT_FOUND, "file or executable not found")
            message = info.get("message") if isinstance(info, dict) else "job runner did not start"
            raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"could not start job: {message}")
        job.pid = info["pid"]

    def adopt(self, handover, argv, cwd, *, link=None) -> tuple[dict, tuple]:
        """Take over a still-running run_command process (tools/process.py ProcessHandover).

        Returns (job description, output captured before the handover). All or nothing: a job exists
        only with its record written and its monitor running. Any failure before that (limits, closed
        manager, record not writable, or an exception such as KeyboardInterrupt) removes the job again
        (registry, counter, files, output redirect) and leaves the process with the caller, which kills
        it (timeout) or keeps waiting (interjection). Expected failures are raised as ToolFailure.
        """
        with self._lock:
            try:
                job = self._reserve(argv, Path(cwd), kind="run_command", keep_running=False)
            except OSError as exc:
                raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"could not create the job log: {exc}") from exc
            job.process, job.windows_job = handover.process, handover.windows_job
            job.pid = job.leader_pid = handover.process.pid
            job.started_at = handover.started_at
            job.link = job_links.new_link(job.id, **(link or {}))
            self._jobs[job.id] = job
            self._started += 1
        redirected = False
        try:
            job.leader_identity = probe_process(job.pid)[1]
            self._save_link(job, strict=True)
            self._save(job, strict=True)
            # Atomic switch: everything captured so far starts the log, later chunks are appended.
            snapshot = handover.redirect(lambda chunk: self._write(job, chunk))
            redirected = True
            threading.Thread(target=self._monitor, args=(job, list(handover.readers)), name=f"job-{job.id}-wait",
                             daemon=True).start()
        except BaseException as exc:
            if redirected:
                handover.release()
            with self._lock:
                self._jobs.pop(job.id, None)
                self._started -= 1
            job.process = job.windows_job = None
            with job.write_lock:
                self._discard(job)
            if isinstance(exc, Exception):
                raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"could not record the job: {exc}") from exc
            raise
        return self.describe(job.id), snapshot

    def _write(self, job, chunk):
        if not chunk:
            return
        with job.write_lock:
            with self._lock:
                room = self.max_log_bytes - job.log_bytes
                job.output_bytes += len(chunk)
                kept = chunk[:max(room, 0)]
                job.log_bytes += len(kept)
            if kept and job.log is not None:
                try:
                    job.log.write(kept)
                    job.log.flush()
                except OSError:
                    # Disk full or similar: keep draining so the job never blocks on a full pipe.
                    job.log.close()
                    job.log = None

    def _drain(self, job):
        stream = job.process.stdout
        try:
            while chunk := stream.read1(65536):
                self._write(job, chunk)
        except (OSError, ValueError):
            pass
        finally:
            stream.close()

    def _monitor(self, job, readers):
        process = job.process
        error = None
        try:
            process.wait()
        except BaseException as exc:  # noqa: BLE001 -- recorded as a failed job
            error = f"monitor failed: {type(exc).__name__}"
        # The main process is gone: stop descendants it left behind (they would hold the log pipe).
        self._terminate(job)
        for reader in readers:
            reader.join(timeout=5)
        with job.write_lock:
            if job.log is not None:
                job.log.close()
                job.log = None
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
        self._save(job)
        job.done.set()

    def _monitor_detached(self, job):
        try:
            job.process.wait()
        except BaseException:  # noqa: BLE001
            pass
        if not self._apply_exit(job):
            with self._lock:
                job.ended_at = time.time()
                if job.kill_requested:
                    job.status = KILLED
                else:
                    job.status = FAILED
                    job.error = "job runner ended without recording an exit status"
        self._sync_log_size(job)
        self._save(job)
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
        self._refresh(job)
        if job.loaded:
            return self._kill_loaded(job, wait)
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

    def _kill_loaded(self, job, wait):
        if job.status == UNKNOWN:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED,
                              f"cannot verify that pid {job.leader_pid} is still this job's process (pid reuse "
                              "guard); not killing it. Check and stop it outside the harness if needed")
        if job.status != RUNNING:
            value = self.describe(job.id)
            value["killed"] = False
            return value
        with self._lock:
            job.kill_requested = True
        if not _kill_verified(job.leader_pid, job.leader_identity):
            with self._lock:
                job.kill_requested = False
            self._refresh(job)
            if job.status == RUNNING:
                raise ToolFailure(ErrorCode.EXECUTION_FAILED, f"could not stop pid {job.leader_pid}")
            value = self.describe(job.id)
            value["killed"] = False
            return value
        end = time.monotonic() + wait
        while time.monotonic() < end and probe_process(job.leader_pid)[0]:
            time.sleep(0.05)
        with self._lock:
            job.status = KILLED
            job.ended_at = time.time()
        self._sync_log_size(job)
        self._save(job)
        job.done.set()
        value = self.describe(job.id)
        value["killed"] = True
        return value

    def close(self, *, wait=5.0) -> None:
        """Kill every running job of this process except keep_running ones (session shutdown). Idempotent."""
        with self._lock:
            self._closed = True
            jobs = [job for job in self._jobs.values()
                    if job.status == RUNNING and not job.loaded and not job.keep_running]
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
            self._load()
            with self._lock:
                job = self._jobs.get(job_id)
        if job is None:
            raise ToolFailure(ErrorCode.NOT_FOUND, f"unknown job: {job_id}")
        return job

    def describe(self, job_id) -> dict:
        job = self._get(job_id)
        self._refresh(job)
        self._sync_log_size(job)
        with self._lock:
            return job.describe(self.root)

    def list(self, limit=DEFAULT_LIST_LIMIT) -> dict:
        self._load()
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            self._refresh(job)
            self._sync_log_size(job)
        jobs.sort(key=lambda job: job.started_at)
        active = [job for job in jobs if job.status in (RUNNING, UNKNOWN)]
        recent = jobs[-limit:]
        shown = [job for job in jobs if job in recent or job in active]
        with self._lock:
            described = [job.describe(self.root) for job in shown]
        value = {"jobs": described, "running": sum(item["status"] == RUNNING for item in described)}
        if len(shown) < len(jobs):
            value["omitted"] = len(jobs) - len(shown)
        return value

    def wait(self, job_id, timeout=None) -> dict:
        job = self._get(job_id)
        if job.loaded:
            end = None if timeout is None else time.monotonic() + timeout
            while job.status in (RUNNING, UNKNOWN) and (end is None or time.monotonic() < end):
                self._refresh(job)
                time.sleep(0.1)
        else:
            job.done.wait(timeout)
        return self.describe(job_id)

    def output(self, job_id, offset=0, limit=8000, tail=None) -> dict:
        job = self._get(job_id)
        if type(limit) is not int or not 1 <= limit <= MAX_OUTPUT_CHUNK:
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, f"limit must be 1-{MAX_OUTPUT_CHUNK}")
        if type(offset) is not int or offset < 0 or (tail is not None and (type(tail) is not int or tail < 1)):
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "offset must be >= 0 and tail >= 1")
        info = self.describe(job_id)
        size = info["log_bytes"]
        if tail is not None:
            offset = max(0, size - min(tail, limit))
        offset = min(offset, size)
        data = b""
        if size > offset:
            try:
                with open(job.log_path, "rb") as handle:
                    handle.seek(offset)
                    data = handle.read(min(limit, size - offset))
            except FileNotFoundError:
                data = b""
        next_offset = offset + len(data)
        return {**info, "offset": offset, "next_offset": next_offset,
                "content": data.decode("utf-8", errors="replace"),
                "more": next_offset < size,
                "complete": info["status"] in TERMINAL and next_offset >= size}

    def _tail(self, job) -> str:
        try:
            with open(job.log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - NOTICE_TAIL_BYTES))
                data = handle.read()
        except OSError:
            return ""
        lines = data.decode("utf-8", errors="replace").replace("\r\n", "\n").splitlines()
        if size > NOTICE_TAIL_BYTES and lines:
            lines = lines[1:]   # probably cut in the middle
        return "\n".join(line[:200] for line in lines[-NOTICE_TAIL_LINES:])

    def take_notices(self) -> list[dict]:
        """Finished jobs of this owner not yet announced to the agent (each is returned once, durably)."""
        self._load()
        with self._lock:
            jobs = [job for job in self._jobs.values() if not job.notified and job.owner == self.owner]
        for job in jobs:
            self._refresh(job)
            if job.status in TERMINAL:
                # Another manager (a resumed process) may already have announced it: the record decides.
                record = _read_json(self.log_dir / f"{job.id}.json")
                if isinstance(record, dict) and record.get("notified"):
                    job.notified = True
        with self._lock:
            finished = [job for job in jobs if job.status in TERMINAL and not job.notified]
            for job in finished:
                job.notified = True
        notices = []
        for job in finished:
            self._save(job)
            notices.append({**self.describe(job.id), "tail": self._tail(job)})
        return notices


    # -- responsibility and delivery (tools/job_links.py) -------------------------------------------
    def responsible(self, job, agent, *, active=None) -> bool:
        """Is ``agent`` (a session id) the one that handles this job's result now?

        The agent that started it, whichever agent happens to call a model first. When that agent is one
        of this session's workers and is no longer active (``active(session_id)`` is false), the owning
        session (the main agent) takes over. A job with no recorded agent belongs to its owner session.
        """
        started_by = job.link["agent_session"]
        if started_by is not None and started_by == agent:
            return True
        if job.owner != self.owner:
            return False
        if started_by is None:
            return self.owner is None or agent is None or agent == self.owner
        return agent is not None and agent == self.owner and active is not None and not active(started_by)

    def _of(self, agent, active):
        self._load()
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.started_at)
        chosen = [job for job in jobs if self.responsible(job, agent, active=active)]
        for job in chosen:
            self._refresh(job)
        return chosen

    def unreceived(self, agent, *, active=None) -> list[dict]:
        """Finished jobs ``agent`` is responsible for whose result its model has not received yet.

        Read from the records every time and not changed by reading, so a notice that was taken and then
        lost (a crash before the checkpoint) is found again. ``mark_taken`` / ``mark_received`` /
        ``settle`` record the three later steps separately.
        """
        found = []
        for job in self._of(agent, active):
            if job.status in TERMINAL and not job.link["delivery"]["settled"] \
                    and not job_links.received_by(job.link, agent):
                found.append({**self.describe(job.id), "tail": self._tail(job), "link": job.link,
                              "handed_over_by": job.link["agent_session"] if job.link["agent_session"] != agent else None})
        return found

    def unfinished(self, agent, *, active=None) -> list[_Job]:
        """Jobs ``agent`` still has to deal with: running or unknown, or finished and not settled."""
        return [job for job in self._of(agent, active)
                if job.status not in TERMINAL or not job.link["delivery"]["settled"]]

    def mark_taken(self, job_id) -> None:
        job = self._get(job_id)
        with self._lock:
            if job.notified:
                return
            job.notified = True
        self._save(job)

    def mark_received(self, job_id, agent, turn) -> None:
        job = self._get(job_id)
        with self._lock:
            if job_links.received_by(job.link, agent):
                return
            job.link["delivery"]["received"].append({"session": agent, "turn": turn, "at": time.time()})
        self._save_link(job)

    def settle(self, job_id, how, *, agent, turn, note=None) -> None:
        job = self._get(job_id)
        with self._lock:
            if job.link["delivery"]["settled"]:
                return
            job.link["delivery"]["settled"] = job_links.settlement(how, session=agent, turn=turn, note=note)
        self._save_link(job)

    def set_goal(self, job_id, standing, *, inputs, reason=None, declared=None) -> None:
        """Confirm or drop the job's goal after the agent has seen ``inputs`` user inputs."""
        job = self._get(job_id)
        with self._lock:
            job_links.fill_declared(job.link, **(declared or {}))
            job.link["goal"] = {"standing": standing, "confirmed_inputs": inputs,
                                "reason": job_links.clip(reason, job_links.MAX_REASON) if reason else None,
                                "changed_at": time.time()}
        self._save_link(job)

    def link_of(self, job_id) -> dict:
        """A copy of the job's link; changing it changes nothing."""
        job = self._get(job_id)
        with self._lock:
            return json.loads(json.dumps(job.link, ensure_ascii=False))

    def begin_wait(self, key, job_id, agent, phase) -> None:
        with self._lock:
            self._waits[key] = {"job_id": job_id, "agent": agent, "phase": phase, "since": time.time()}
        self._observe_waits()

    def wait_phase(self, key, phase) -> None:
        with self._lock:
            if key in self._waits:
                self._waits[key]["phase"] = phase
        self._observe_waits()

    def end_wait(self, key) -> None:
        with self._lock:
            self._waits.pop(key, None)
        self._observe_waits()

    def _observe_waits(self):
        for observer in list(self.wait_observers):
            try:
                observer()
            except Exception:  # noqa: BLE001 -- display only.
                pass

    def waits(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._waits.values()]

    @property
    def closing(self) -> bool:
        return self._closed

    def overview(self, limit=10) -> list[dict]:
        """This session's jobs for status displays: unsettled ones and the newest ``limit``. Changes nothing."""
        if self.owner is None:
            return []      # the session has not started: an unnamed session's old jobs are not this one's
        with self._lock:
            jobs = sorted((job for job in self._jobs.values() if job.owner == self.owner), key=lambda job: job.started_at)
            waits = {item["job_id"]: item["phase"] for item in self._waits.values()}
            recent = jobs[-limit:]
            return [{"job_id": job.id, "status": job.status, "exit_code": job.exit_code,
                     "agent": job.link["agent_session"], "purpose": job.link["purpose"],
                     "goal": job.link["goal"]["standing"], "waiting": waits.get(job.id),
                     "delivery": job_links.stage(job.link, terminal=job.status in TERMINAL, notified=job.notified)}
                    for job in jobs if job in recent or job.id in waits or not job.link["delivery"]["settled"]]


class JobNoticeMiddleware(AgentMiddleware):
    """Tell the agent, before its next model call, which background jobs finished since the last notice.

    Notices now travel through the agent inbox (``baseagent.agent.inbox``, source ``job``): the chat
    composition uses ``InboxMiddleware(inbox, jobs=manager)`` so a notice and a user's mid-turn line
    arrive in one message. This class is that middleware with a private inbox, for callers that only
    want job notices.
    """

    def __init__(self, manager: JobManager):
        from baseagent.agent.inbox import InboxMiddleware
        self.manager = manager
        self._delivery = InboxMiddleware(jobs=manager)

    def before_agent(self, state):
        self._delivery.before_agent(state)

    def before_model(self, state):
        self._delivery.before_model(state)

    def after_model(self, state, message):
        self._delivery.after_model(state, message)

    def after_agent(self, state):
        self._delivery.after_agent(state)


# -- run_command timeout / user interjection -> background job ----------------------------------------
def user_interjection(context):
    """Predicate: has the user sent this agent a mid-turn (steer) line that is not delivered yet?

    Only the agent's own inboxes count (the InboxMiddleware layers of the pipeline running this call),
    so a line for the main agent never moves a worker's command, and ``/queue`` lines (next turn),
    worker results, job notices and ``/tell`` text never move a command. None without an inbox.
    """
    from baseagent.agent.inbox import STEER, USER, InboxMiddleware
    inboxes = [layer.inbox for layer in (getattr(context, "middleware", None) or ())
               if isinstance(layer, InboxMiddleware)]
    if not inboxes:
        return None
    return lambda: any(inbox.pending(mode=STEER, source=USER) for inbox in inboxes)


class TimeoutHandover:
    """``on_timeout`` for run_command: move the still-running process into a job instead of killing it.

    Offered on timeout (refusal: the process is killed, as before) or, earlier, when the user sends
    this agent a line mid-turn (refusal: the command keeps running under its own timeout).
    """

    def __init__(self, manager, argv, cwd, link=None):
        self.manager = manager
        self.argv = list(argv)
        self.cwd = cwd
        self.link = link
        self.refused = None
        self.interjection_refused = None

    @classmethod
    def for_command(cls, manager, argv, cwd, context=None):
        # `sleep` is a deliberate wait: when it times out the model wanted it to end (Claude Code
        # exempts it too), so it keeps the kill-on-timeout behaviour.
        if Path(argv[0]).name.lower() in SLEEP_COMMANDS:
            return None
        return cls(manager, argv, cwd, job_links.job_context(context))

    def __call__(self, handover):
        interjection = getattr(handover, "reason", TIMEOUT) == INTERJECTION
        try:
            job, (stdout, stderr, stdout_truncated, stderr_truncated) = self.manager.adopt(handover, self.argv, self.cwd,
                                                                                               link=self.link)
        except ToolFailure as exc:
            if interjection:
                self.interjection_refused = str(exc)
            else:
                self.refused = str(exc)
            return None
        data = {"exit_code": None, "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "stdout_truncated": stdout_truncated, "stderr_truncated": stderr_truncated,
                "moved_to_background": True, "job_id": job["job_id"], "pid": job["pid"],
                "log_path": job["log_path"]}
        if interjection:
            elapsed = max(0.0, time.time() - handover.started_at)
            data["handover_reason"] = INTERJECTION
            data["message"] = (f"the user sent a message while this command was running ({elapsed:.1f} seconds in); "
                               f"it was handed over to background job {job['job_id']} (same process, not restarted, "
                               "not stopped) so the message can be read now. It has not necessarily finished: the "
                               "output so far is below and continues in the job log (job_output); job_list shows "
                               "its status. Use job_kill to stop it; it ends with the session.")
        else:
            data["message"] = (f"command still running after {handover.timeout} seconds; moved to background job "
                               f"{job['job_id']} (same process, not restarted). The output so far is below and "
                               "continues in the job log (job_output). Use job_kill to stop it; it ends with the session.")
        return ToolResult(data=data)

    def revoke(self, result):
        """Stop the job a handover just created because cancellation or Ctrl+C won the race.

        True only when the job is confirmed no longer running.
        """
        job_id = (result.data or {}).get("job_id")
        if not job_id:
            return False
        try:
            info = self.manager.kill(job_id)
        except ToolFailure:
            return False
        return info.get("status") not in (RUNNING, UNKNOWN)

    def annotate(self, result):
        if self.refused and result.error is not None and result.error.code == ErrorCode.TIMEOUT:
            note = self.refused
            if self.interjection_refused and self.interjection_refused != note:
                note = f"{note}; earlier, on the user's message: {self.interjection_refused}"
            return ToolResult.failure(result.error.code, f"{result.error.message} (not moved to background: {note})",
                                      data=result.data, retryable=result.error.retryable)
        if self.interjection_refused and not (result.data or {}).get("moved_to_background"):
            # The user's message did not move the command; say why (it ran on and ended on its own).
            data = {**(result.data or {}), "not_moved_on_interjection": self.interjection_refused}
            return ToolResult(data=data, error=result.error)
        return result


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
    """Register start_job / job_list / job_output / job_kill / job_wait / job_settle for ``workspace``."""
    from .job_wait import register_wait_tools

    def start_job(argv, cwd=".", keep_running=False, purpose=None, expect=None, next_step=None, *, context=None):
        _require_commands(workspace)
        # Same argv validation as run_command; the timeout argument does not apply to jobs.
        workspace.validate_command(argv, 1)
        manager = _manager(workspace)
        directory = workspace.root if cwd in (".", "") else workspace._path(cwd)
        if context is not None and context.cancellation is not None:
            context.cancellation.check()
        link = job_links.job_context(context, purpose=purpose, expect=expect, next_step=next_step)
        return manager.start(argv, directory, keep_running=keep_running, link=link)

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
    text = {"type": "string", "minLength": 1, "maxLength": job_links.MAX_DECLARED}
    declared = {key: dict(text) for key in job_links.DECLARED}
    from .annotations import COMMAND, READ, ToolAnnotations
    # Kills processes (destructive); a repeat has no further effect; stays in the workspace's jobs.
    kill_annotations = ToolAnnotations(read_only=False, destructive=True, idempotent=True, open_world=False)
    registry.register("start_job", "Start a long-running argv command (no shell) as a background job in the workspace and "
                      "return its job_id at once; it keeps running across turns and cancellation until job_kill and "
                      "ends with the session unless keep_running is true (then it also survives the harness exiting). "
                      "Output goes to a size-capped log; requires command permission. State purpose, expect and "
                      "next_step when you know them: they are kept with the job and shown with its result",
                      params({"argv": argv, "cwd": {"type": "string", "minLength": 1, "maxLength": 4000},
                              "keep_running": {"type": "boolean"}, **declared}, ["argv"]),
                      start_job, contextual=True, annotations=COMMAND)
    registry.register("job_list", "List recent background jobs, including ones from earlier sessions, with status "
                      "(running/exited/failed/killed/lost/unknown), exit code and times",
                      params({}, []), job_list, retry_safe=True, annotations=READ)
    registry.register("job_output", "Read a background job's combined stdout/stderr log by byte offset/limit, or the last "
                      "tail bytes; next_offset continues paging",
                      params({"job_id": job_id, "offset": {"type": "integer", "minimum": 0},
                              "limit": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_CHUNK},
                              "tail": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_CHUNK}}, ["job_id"]),
                      job_output, retry_safe=True, annotations=READ)
    registry.register("job_kill", "Stop a background job and its whole process tree; requires command permission",
                      params({"job_id": job_id}, ["job_id"]), job_kill, annotations=kill_annotations)
    register_wait_tools(registry, lambda: _manager(workspace), params, job_id, declared)
