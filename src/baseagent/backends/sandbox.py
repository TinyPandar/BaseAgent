"""OS sandbox for command processes started by LocalBackend (docs/sandbox.md).

Modes: ``off`` (default, unconfined, unchanged behaviour), ``read-only`` (commands may write only the
per-run temp directory) and ``workspace-write`` (also the workspace root, except protected directories
such as ``.git``, ``.venv`` and ``.baseagent``). Network is denied in confined modes unless explicitly
allowed.

Implementations: macOS ``sandbox-exec`` with a generated Seatbelt (SBPL) profile, Linux ``bwrap``
(bubblewrap). Native Windows has none. A confined mode without a usable implementation fails closed
with ``SandboxUnavailable`` (code ``SANDBOX_UNAVAILABLE``); commands never fall back to running
unconfined.

The sandbox only governs child processes. In-process file tools, MCP servers, hooks and the harness
itself are not inside it; child processes inherit the harness environment variables.

Other spawners (background jobs) call :func:`wrap` right before ``subprocess.Popen``::

    argv, env = sandbox.wrap(argv, cwd, env, backend.sandbox)   # unchanged when sandbox is None
    subprocess.Popen(argv, cwd=cwd, env=env, ...)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePath

MODES = ("off", "read-only", "workspace-write")
CONFINED_MODES = ("read-only", "workspace-write")
# Same directories the Docker backend keeps read-only (backends/docker.py PROTECTED_PARTS).
PROTECTED_PARTS = (".git", ".venv", ".baseagent")
UNAVAILABLE_CODE = "SANDBOX_UNAVAILABLE"
SEATBELT = "seatbelt"
BUBBLEWRAP = "bubblewrap"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PROFILE_VERSION = 1


class SandboxUnavailable(RuntimeError):
    """A confined mode was requested but no OS sandbox can enforce it; nothing is run."""

    code = UNAVAILABLE_CODE

    def __init__(self, mode, reason):
        self.mode = mode
        self.reason = reason
        super().__init__(f"{UNAVAILABLE_CODE}: --sandbox {mode} needs OS confinement, but {reason}. "
                         "Commands are refused, never run unconfined; use --sandbox off (no confinement) "
                         "or --backend docker (container isolation). See docs/sandbox.md")


@dataclass(frozen=True)
class SandboxPolicy:
    mode: str = "off"
    network: bool = False

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"unknown sandbox mode {self.mode!r}; expected one of {', '.join(MODES)}")
        if type(self.network) is not bool:
            raise ValueError("sandbox network must be boolean")

    @property
    def confined(self):
        return self.mode in CONFINED_MODES

    def contract(self):
        return {"version": PROFILE_VERSION, "mode": self.mode, "network": self.network,
                "protected_parts": list(PROTECTED_PARTS)}


def protected_dirs(root, protected_paths=()):
    """Paths inside ``root`` that stay read-only in workspace-write mode (absolute, sorted, unique)."""
    root = root if isinstance(root, PurePath) else Path(root)
    paths = {root / part for part in PROTECTED_PARTS}
    for path in protected_paths:
        path = path if isinstance(path, PurePath) else Path(path)
        if path == root:
            continue
        if path.is_relative_to(root):
            paths.add(path)
    return sorted(paths, key=str)


def _sbpl_string(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(policy, root, temp_dir, protected=()):
    """SBPL profile: everything allowed except writes outside the writable roots, protected dirs and network.

    In SBPL the last matching rule wins, so the deny rules for protected directories follow the allows.
    """
    writable = [temp_dir] + ([root] if policy.mode == "workspace-write" else [])
    lines = ["(version 1)", "(allow default)", "(deny file-write*)",
             "(allow file-write*",
             *[f"    (subpath {_sbpl_string(path)})" for path in writable],
             '    (literal "/dev/null") (literal "/dev/zero") (literal "/dev/dtracehelper")',
             '    (regex #"^/dev/tty") (regex #"^/dev/fd/"))']
    if policy.mode == "workspace-write" and protected:
        lines.append("(deny file-write*")
        lines.extend(f"    (subpath {_sbpl_string(path)})" for path in protected)
        lines[-1] += ")"
    if not policy.network:
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


def seatbelt_argv(policy, root, temp_dir, protected=(), executable=SANDBOX_EXEC):
    """Prefix that runs a command under ``sandbox-exec`` with the generated profile."""
    return [executable, "-p", seatbelt_profile(policy, root, temp_dir, protected), "--"]


def bwrap_argv(policy, root, cwd, temp_dir, protected=(), executable="bwrap"):
    """Prefix that runs a command under bubblewrap.

    The host root is bound read-only; /tmp is a fresh tmpfs; the workspace is re-bound (read-write in
    workspace-write, read-only otherwise, so a workspace below /tmp stays visible); the per-run temp
    directory is writable; protected directories are re-bound read-only on top.
    """
    argv = [executable, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    writable = policy.mode == "workspace-write"
    argv += ["--bind" if writable else "--ro-bind", str(root), str(root)]
    argv += ["--bind", str(temp_dir), str(temp_dir)]
    if writable:
        for path in protected:
            argv += ["--ro-bind-try", str(path), str(path)]
    argv += ["--unshare-pid"]
    if not policy.network:
        argv += ["--unshare-net"]
    argv += ["--die-with-parent", "--new-session", "--chdir", str(cwd), "--"]
    return argv


def detect(mode, *, platform=None, which=shutil.which, exists=os.path.exists):
    """(implementation, executable) usable on this platform, or raise SandboxUnavailable."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        executable = SANDBOX_EXEC if exists(SANDBOX_EXEC) else which("sandbox-exec")
        if not executable:
            raise SandboxUnavailable(mode, "sandbox-exec was not found on this macOS host")
        return SEATBELT, executable
    if platform.startswith("linux"):
        executable = which("bwrap")
        if not executable:
            raise SandboxUnavailable(mode, "bubblewrap (bwrap) is not installed; install it (e.g. apt install bubblewrap)")
        return BUBBLEWRAP, executable
    if platform in ("win32", "cygwin", "msys"):
        raise SandboxUnavailable(mode, "native Windows has no supported sandbox backend "
                                       "(Seatbelt is macOS-only, bubblewrap is Linux/WSL2-only)")
    raise SandboxUnavailable(mode, f"platform {platform!r} has no supported sandbox backend")


def _probe(argv, env):
    try:
        completed = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=15, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return completed.returncode, completed.stderr.decode("utf-8", errors="replace").strip()


class Sandbox:
    """A confined policy bound to one workspace, one implementation and one per-run temp directory.

    Construction detects the implementation and runs a trivial probe command inside it; any failure
    raises SandboxUnavailable (fail closed). ``platform``, ``which``, ``exists`` and ``probe`` are
    injectable for tests: ``probe`` is True (run the real probe), None (skip it) or a callable
    ``probe(argv, env) -> (exit_code, detail)``.
    """

    def __init__(self, policy, root, *, protected_paths=(), platform=None, which=shutil.which,
                 exists=os.path.exists, probe=True, temp_parent=None):
        if not isinstance(policy, SandboxPolicy):
            raise TypeError("policy must be a SandboxPolicy")
        if not policy.confined:
            raise ValueError("Sandbox needs a confined mode; use no sandbox for --sandbox off")
        self.policy = policy
        self.root = Path(root)
        self.protected = protected_dirs(self.root, protected_paths)
        self.implementation, self.executable = detect(policy.mode, platform=platform, which=which, exists=exists)
        self._lock = threading.Lock()
        self.closed = False
        # realpath: Seatbelt matches canonical paths (/private/var/..., not /var/...).
        self.temp_dir = Path(os.path.realpath(tempfile.mkdtemp(prefix="baseagent-sandbox-", dir=temp_parent)))
        if probe is True:
            probe = _probe
        if probe is not None:
            check = which("true") or "/usr/bin/true"
            argv, env = self.wrap([check], self.root, dict(os.environ))
            code, detail = probe(argv, env)
            if code != 0:
                self.close()
                reason = f"{self.implementation} could not start a confined process (exit {code}"
                raise SandboxUnavailable(policy.mode, reason + (f": {detail[:300]})" if detail else ")"))

    def prefix(self, cwd):
        if self.implementation == SEATBELT:
            return seatbelt_argv(self.policy, self.root, self.temp_dir, self.protected, self.executable)
        return bwrap_argv(self.policy, self.root, cwd, self.temp_dir, self.protected, self.executable)

    def wrap(self, argv, cwd=None, env=None):
        """(argv, env) that run ``argv`` confined; env defaults to os.environ, temp variables point inside."""
        with self._lock:
            if self.closed:
                raise SandboxUnavailable(self.policy.mode, "the sandbox of this run is already closed")
        cwd = self.root if cwd is None else Path(cwd)
        environment = dict(os.environ if env is None else env)
        for name in ("TMPDIR", "TMP", "TEMP"):
            environment[name] = str(self.temp_dir)
        return [*self.prefix(cwd), *argv], environment

    def contract(self):
        return self.policy.contract()

    def describe(self):
        """Non-secret facts for json output."""
        return {"kind": "local", "sandbox": self.policy.mode, "network": "enabled" if self.policy.network else "none",
                "implementation": self.implementation}

    def summary(self):
        network = "network enabled" if self.policy.network else "no network access"
        return f"local, sandbox {self.policy.mode} ({self.implementation}), {network}"

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
        shutil.rmtree(self.temp_dir, ignore_errors=True)


def wrap(argv, cwd, env, sandbox):
    """Single entry point for spawners: unchanged (argv, env) without a sandbox, else the confined pair."""
    if sandbox is None:
        return list(argv), env
    return sandbox.wrap(argv, cwd, env)


def create(policy, root, *, protected_paths=(), **options):
    """A Sandbox for a confined policy, None for ``off``; raises SandboxUnavailable (fail closed)."""
    if policy is None or not policy.confined:
        return None
    return Sandbox(policy, root, protected_paths=protected_paths, **options)
