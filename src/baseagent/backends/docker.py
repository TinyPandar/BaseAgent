"""One container per task run. Network is off unless the caller opts in.

LocalBackend remains available. This backend does not implement a custom OS sandbox;
Docker is the isolation boundary. Commands *and* file operations run inside the
container (docker_fs.py), so symbolic links are only ever resolved in the container's
mount namespace and cannot reach host paths. The host keeps the session database,
checkpoints and the cooperating-writer locks under ``.baseagent/``, which the container
sees as an empty tmpfs. See docs/docker_backend.md.
"""

from __future__ import annotations

import io
import os
import secrets
import socket
import sys
import tarfile
import threading
from hashlib import sha256
from pathlib import Path, PurePosixPath

from baseagent.tools.result import ErrorCode, ToolFailure, ToolResult

from .docker_fs import EXIT_CODES, FS_SCRIPT

DEFAULT_IMAGE = "python:3.12-slim"
NETWORKS = ("none", "bridge")
DEFAULT_CPUS = 1
DEFAULT_MEMORY = "512m"
DEFAULT_PIDS = 128
MOUNT = "/workspace"
LABEL = "baseagent.workspace"
READ_LIMIT = 200_000
BATCH = 200                      # paths per batched read exec
PROTECTED_PARTS = {".git", ".venv", ".baseagent"}
CLI_MISSING = ("Docker is not installed: there is no `docker` command on PATH. Install Docker Desktop or "
               "Docker Engine, or run without --backend docker.")


class DockerError(RuntimeError):
    """A readable Docker failure. The kernel should see this, not hang."""


class DockerUnavailable(DockerError):
    """Docker cannot be used at all: ``kind`` is cli_missing, daemon_unreachable or image_missing."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class DockerCli:
    """Runs the real ``docker`` command."""

    def run(self, argv, *, input_bytes=None, timeout=60):
        import subprocess
        try:
            completed = subprocess.run(argv, input=input_bytes, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise DockerUnavailable("cli_missing", CLI_MISSING) from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"Docker command timed out after {timeout} seconds: {' '.join(argv[:3])}") from exc
        return completed.returncode, completed.stdout, completed.stderr

    def stream(self, argv, *, cwd, timeout, output_limit, cancellation=None):
        """A command with the same timeout, cancellation and output bounds as LocalBackend."""
        from baseagent.tools.process import run_process
        try:
            return run_process(argv, cwd, timeout=timeout, output_limit=output_limit, cancellation=cancellation)
        except FileNotFoundError as exc:
            raise DockerUnavailable("cli_missing", CLI_MISSING) from exc


def explain_docker_failure(stderr, *, image):
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr)
    lowered = text.lower()
    if "cannot connect to the docker daemon" in lowered or ("docker daemon" in lowered and "error" in lowered) \
            or "permission denied while trying to connect" in lowered:
        first = text.strip().splitlines()[0] if text.strip() else ""
        return ("Docker daemon is not running or not reachable" + (f" ({first})" if first else "")
                + ". Start Docker Desktop (or `sudo systemctl start docker`; on Linux your user may need the "
                "docker group), then retry.")
    if "no such image" in lowered or "pull access denied" in lowered or "unable to find image" in lowered \
            or ("not found" in lowered and image.lower() in lowered):
        return f"Docker image is missing locally: {image}. Pull it first: docker pull {image}"
    if "oomkilled" in lowered or "out of memory" in lowered:
        return "container ran out of memory"
    return text.strip() or "Docker command failed"


def preflight(runner, image):
    """Raise DockerUnavailable before any model call when Docker cannot run this image."""
    code, _, stderr = runner.run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=20)
    if code != 0:
        raise DockerUnavailable("daemon_unreachable",
                                explain_docker_failure(stderr or b"Cannot connect to the Docker daemon", image=image))
    code, _, stderr = runner.run(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=20)
    if code != 0:
        text = stderr.decode("utf-8", errors="replace").lower()
        if "daemon" in text and "connect" in text:
            raise DockerUnavailable("daemon_unreachable", explain_docker_failure(stderr, image=image))
        raise DockerUnavailable("image_missing", f"Docker image is missing locally: {image}. "
                                f"Pull it first: docker pull {image}")


def host_user():
    """uid:gid for file ownership on Linux; Docker Desktop (macOS/Windows) maps ownership itself."""
    if sys.platform.startswith("linux") and hasattr(os, "getuid"):
        return os.getuid(), os.getgid()
    return None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True              # exists, owned by someone else
    return True


def _docker_level(stderr):
    """True when stderr is the docker CLI/daemon talking, not the command inside the container."""
    text = (stderr or b"").decode("utf-8", errors="replace").lower()
    return (text.startswith(("error response from daemon", "error: no such container", "oci runtime exec failed"))
            or "no such container" in text or "cannot connect to the docker daemon" in text
            or ("container" in text and ("is not running" in text or "is restarting" in text or "is paused" in text)))


def _read_outcome(status, content, limit):
    if status == "ok":
        if content is None:
            return ToolFailure(ErrorCode.NOT_FOUND, "file is not in the workspace")
        if len(content) > limit:
            return ToolFailure(ErrorCode.LIMIT_EXCEEDED, "file exceeds read limit")
        return content
    if status == "missing":
        return ToolFailure(ErrorCode.NOT_FOUND, "file is not in the workspace")
    if status == "large":
        return ToolFailure(ErrorCode.LIMIT_EXCEEDED, "file exceeds read limit")
    if status in ("escape", "link"):
        return ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
    return ToolFailure(ErrorCode.INVALID_ARGUMENTS, "path is not a regular file")


class DockerBackend:
    def __init__(self, root, *, image=DEFAULT_IMAGE, network="none", cpus=DEFAULT_CPUS, memory=DEFAULT_MEMORY,
                 pids=DEFAULT_PIDS, protected_paths=(), runner=None, start=True, name=None, owner=None,
                 check=True):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace root must be a directory")
        if network not in NETWORKS:
            raise ValueError("docker network must be none or bridge")
        if type(cpus) not in (int, float) or cpus <= 0 or type(pids) is not int or pids < 1:
            raise ValueError("cpu and pid limits must be positive")
        if not isinstance(memory, str) or not memory:
            raise ValueError("memory limit must be a Docker size such as 512m")
        if not isinstance(image, str) or not image.strip() or image.startswith("-") or any(c.isspace() for c in image):
            raise ValueError("docker image must be a name such as python:3.12-slim")
        if "," in str(self.root):
            raise ValueError("--backend docker cannot mount a workspace path that contains a comma")
        self.image = image
        self.network = network
        self.cpus = cpus
        self.memory = memory
        self.pids = pids
        self.protected_paths = tuple(Path(path).resolve() for path in protected_paths)
        harness = self.root / ".baseagent"
        for path in self.protected_paths:
            if path.is_relative_to(self.root) and not path.is_relative_to(harness):
                raise ValueError(f"with --backend docker the session database must live outside the workspace or "
                                 f"under {harness}; the container could otherwise rewrite {path}")
        self.runner = runner or DockerCli()
        self.workspace_key = sha256(str(self.root).encode()).hexdigest()[:12]
        self.name = name or f"baseagent-{self.workspace_key}-{os.getpid()}-{secrets.token_hex(3)}"
        # (uid, gid) owning new files on a Linux host; False/None disables the hand-back.
        self.owner = host_user() if owner is None else (owner or None)
        self.check = check
        self.container_id = None
        self.allow_write = True
        self.read_roots = set()
        self.reaped = []
        self.git_mount = None
        self._lock = threading.Lock()
        if start:
            self.start()

    # ----- identity and lifecycle -------------------------------------------------------
    def contract(self):
        # No container id or name: a resumed turn runs in a new container with the same settings.
        return {"kind": "docker", "version": 2, "image": self.image, "network": self.network,
                "cpus": self.cpus, "memory": self.memory, "pids": self.pids,
                "mount": MOUNT, "isolation": "container", "git": "read-only", "harness_dir": "tmpfs"}

    def describe(self):
        """Non-secret facts for json output and /status."""
        return {"kind": "docker", "image": self.image, "network": self.network, "container": self.name,
                "cpus": self.cpus, "memory": self.memory, "pids": self.pids}

    def summary(self):
        access = "no network access" if self.network == "none" else "network enabled (bridge)"
        return f"docker, image {self.image}, {access}, container {self.name}"

    def start(self):
        with self._lock:
            if self.container_id:
                return self.container_id
            if self.check:
                preflight(self.runner, self.image)
            self.reap_stale()
            # The tmpfs mount point must exist on the host first, or Docker creates it owned by root.
            (self.root / ".baseagent").mkdir(exist_ok=True)
            argv = ["docker", "run", "-d", "--pull", "never", "--name", self.name,
                    "--label", f"{LABEL}={self.workspace_key}", "--label", f"baseagent.pid={os.getpid()}",
                    "--label", f"baseagent.host={socket.gethostname()}",
                    "--network", self.network, "--cpus", str(self.cpus), "--memory", self.memory,
                    "--pids-limit", str(self.pids), "--entrypoint", "sleep",
                    "--mount", f"type=bind,source={self.root},target={MOUNT}"]
            git = self.root / ".git"
            if git.is_dir() and not git.is_symlink():
                self.git_mount = "read-only"
                argv += ["--mount", f"type=bind,source={git},target={MOUNT}/.git,readonly"]
            argv += ["--mount", f"type=tmpfs,destination={MOUNT}/.baseagent", "-w", MOUNT, self.image, "infinity"]
            code, stdout, stderr = self.runner.run(argv, timeout=120)
            if code != 0:
                message = explain_docker_failure(stderr, image=self.image)
                if message.startswith("Docker image is missing"):
                    raise DockerUnavailable("image_missing", message)
                if message.startswith("Docker daemon"):
                    raise DockerUnavailable("daemon_unreachable", message)
                raise DockerError(message)
            self.container_id = stdout.decode().strip()
            if not self.container_id:
                raise DockerError("Docker did not return a container id")
            return self.container_id

    def close(self):
        """Remove this run's container (idempotent). Files are handed back to the host user first."""
        with self._lock:
            if not self.container_id:
                return
            try:
                self._after("-")
            except Exception:  # noqa: BLE001 -- ownership is best effort; removal must still happen.
                pass
            code, stdout, stderr = self.runner.run(["docker", "rm", "-f", self.name], timeout=60)
            if code != 0 and b"no such container" not in (stdout + stderr).lower():
                raise DockerError(explain_docker_failure(stderr, image=self.image))
            self.container_id = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def reap_stale(self):
        """Remove containers of this workspace left by a process on this host that no longer runs."""
        code, stdout, _ = self.runner.run(["docker", "ps", "-a", "--filter", f"label={LABEL}={self.workspace_key}",
                                           "--format", '{{.Names}}|{{.Label "baseagent.pid"}}|{{.Label "baseagent.host"}}'],
                                          timeout=30)
        if code != 0:
            return
        here = socket.gethostname()
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.strip().split("|")
            if len(parts) != 3 or parts[0] == self.name or parts[2] != here:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            if _alive(pid):
                continue                      # another live session on this workspace: never touch it
            if self.owner:
                # Hand files back to the host user before removal; a stopped container is started for that.
                self.runner.run(["docker", "start", parts[0]], timeout=30)
                self.runner.run(["docker", "exec", "-w", MOUNT, parts[0], "sh", "-c", FS_SCRIPT, "baseagent-fs",
                                 MOUNT, "after", "-", str(self.owner[0]), str(self.owner[1])], timeout=120)
            removed, _, _ = self.runner.run(["docker", "rm", "-f", parts[0]], timeout=60)
            if removed == 0:
                self.reaped.append(parts[0])

    def alive(self):
        """(running, oom_killed) of this run's container."""
        code, stdout, _ = self.runner.run(["docker", "inspect", "-f", "{{.State.Running}} {{.State.OOMKilled}}",
                                           self.name], timeout=20)
        if code != 0:
            return False, False
        words = stdout.decode().split()
        return words[:1] == ["true"], words[1:2] == ["true"]

    def _gone(self, detail=""):
        return DockerError(f"Docker container {self.name} is no longer running (out of memory, removed, or Docker "
                           f"restarted){': ' + detail if detail else ''}. The session is saved; fix Docker and continue "
                           f"with --resume.")

    # ----- exec plumbing ------------------------------------------------------------------
    def _exec(self, argv, *, input_bytes=None, timeout=60, user=False):
        if not self.container_id:
            raise DockerError("Docker container is not running")
        command = ["docker", "exec"] + (["-i"] if input_bytes is not None else [])
        if user and self.owner:
            command += ["-u", f"{self.owner[0]}:{self.owner[1]}"]
        command += ["-w", MOUNT, self.name, *argv]
        code, stdout, stderr = self.runner.run(command, input_bytes=input_bytes, timeout=timeout)
        if code in (1, 125, 126, 127) and _docker_level(stderr):
            running, oom = self.alive()
            if not running:
                raise self._gone("out of memory" if oom else "")
            raise DockerError(explain_docker_failure(stderr, image=self.image))
        return code, stdout, stderr

    def _fs(self, op, *args, input_bytes=None, timeout=60, accept=(0,)):
        code, stdout, stderr = self._exec(["sh", "-c", FS_SCRIPT, "baseagent-fs", MOUNT, op, *args],
                                          input_bytes=input_bytes, timeout=timeout, user=True)
        if code == 70:
            raise DockerError("the workspace mount is missing in the container")
        if code in (126, 127):
            raise DockerError("the container image needs /bin/sh plus coreutils or busybox for file operations")
        if code in EXIT_CODES:
            name, message = EXIT_CODES[code]
            text = stderr.decode("utf-8", errors="replace").strip().removeprefix("baseagent-fs: ")
            raise ToolFailure(getattr(ErrorCode, name), message or text or "file operation failed")
        if code not in accept:
            raise DockerError(stderr.decode("utf-8", errors="replace").strip() or f"file operation failed ({code})")
        return stdout, stderr

    def _after(self, marker):
        owner = self.owner or ("-", "-")
        self._exec(["sh", "-c", FS_SCRIPT, "baseagent-fs", MOUNT, "after", marker, str(owner[0]), str(owner[1])],
                   timeout=120)

    # ----- namespace --------------------------------------------------------------------
    def allow_read_root(self, root):
        """Grant reads of a harness-controlled host directory (session spill files)."""
        self.read_roots.add(Path(root).resolve())

    def resolve_path(self, path):
        relative = self._relative(path)
        return (self.root / relative).resolve()

    def _relative(self, path):
        if path == ".":
            return "."
        if not isinstance(path, str) or not path or path.startswith(("/", "\\")) or any(c in path for c in "\\\0\n"):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
        if any(part.casefold() in PROTECTED_PARTS or part.casefold().startswith((".env", ".baseagent-edit-"))
               for part in relative.parts):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path is protected")
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
        if any(resolved == protected or resolved.is_relative_to(protected) for protected in self.protected_paths):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "session storage is protected")
        return relative.as_posix()

    def _guidance_relative(self, path):
        """The path whose directories carry guidance: links resolved on the namespace, as LocalBackend does."""
        relative = self._relative(path or ".")
        if relative == ".":
            return relative
        resolved = (self.root / relative).resolve().relative_to(self.root).as_posix()
        return resolved or "."

    # ----- reads ------------------------------------------------------------------------
    def _probe(self, entries, limit):
        """[(status, size)] and {relative: bytes} for ``r:`` / ``i:`` / ``s:`` entries, one exec per batch."""
        statuses, contents = [], {}
        for start in range(0, len(entries), BATCH):
            chunk = entries[start:start + BATCH]
            # tar exits 1/2 when a file changes or vanishes mid-read; the archive still holds the rest.
            stdout, stderr = self._fs("probe", str(limit), *chunk, timeout=120, accept=(0, 1, 2))
            lines = [line[3:].split() for line in stderr.decode("utf-8", errors="replace").splitlines()
                     if line.startswith("@@ ")]
            if len(lines) != len(chunk):
                raise DockerError("file probe returned an unexpected answer: "
                                  + stderr.decode("utf-8", errors="replace").strip()[:300])
            statuses += [(words[0], int(words[1]) if len(words) > 1 else None) for words in lines]
            if stdout:
                try:
                    with tarfile.open(fileobj=io.BytesIO(stdout), mode="r:") as archive:
                        for member in archive:
                            if not member.isfile():
                                continue
                            name = member.name[2:] if member.name.startswith("./") else member.name
                            handle = archive.extractfile(member)
                            contents[name] = handle.read() if handle else b""
                except tarfile.TarError as exc:
                    raise DockerError(f"could not read the file archive from the container: {exc}") from None
        return statuses, contents

    def read_many(self, paths, limit=READ_LIMIT):
        """{path: bytes | ToolFailure} for many workspace files in few execs (grep, search_files)."""
        relatives = [self._relative(path) for path in paths]
        statuses, contents = self._probe([f"r:{relative}" for relative in relatives], limit)
        return {path: _read_outcome(status, contents.get(relative), limit)
                for path, relative, (status, _) in zip(paths, relatives, statuses)}

    def read_bytes(self, path, limit=READ_LIMIT):
        if isinstance(path, str) and Path(path).is_absolute():
            target = Path(path).resolve()
            if any(target == root or target.is_relative_to(root) for root in self.read_roots):
                from baseagent.tools.editing import read_bytes
                return read_bytes(target, limit)       # harness spill files live on the host
        value = self.read_many([path], limit)[path]
        if isinstance(value, ToolFailure):
            raise value
        return value

    def file_hash(self, path):
        try:
            return sha256(self.read_bytes(path)).hexdigest()
        except ToolFailure as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return "missing"
            raise

    def stat_files(self, paths):
        """{path: (sha256 | "missing", mode | None)} without a size limit (undo, rollback)."""
        relatives = [self._relative(path) for path in paths]
        stdout, _ = self._fs("stat", *relatives)
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        if len(lines) != len(relatives):
            raise DockerError("file status returned an unexpected answer")
        values = {}
        for path, line in zip(paths, lines):
            words = line.split()
            if words[0] == "ok":
                values[path] = (words[1], int(words[2], 8) if len(words) > 2 else None)
            elif words[0] == "missing":
                values[path] = ("missing", None)
            elif words[0] == "escape":
                raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
            else:
                raise ToolFailure(ErrorCode.PERMISSION_DENIED, "write target must be a file")
        return values

    def file_mode(self, path):
        return self.stat_files([path])[path][1]

    def get_instructions(self, path="."):
        relative = self._guidance_relative(path)
        parts = [] if relative == "." else relative.split("/")
        prefixes = [""] + ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
        candidates = ["AGENTS.md" if not prefix else prefix + "/AGENTS.md" for prefix in prefixes]
        entries = ([] if relative == "." else [f"s:{relative}"]) + [f"i:{item}" for item in candidates]
        statuses, contents = self._probe(entries, 20_000)
        if relative != ".":
            if statuses.pop(0)[0] != "dir":
                candidates.pop()            # a file (or a new path) has no AGENTS.md inside it
                statuses.pop()
                parts = parts[:-1]
        values, total = [], 0
        for candidate, (status, _) in zip(candidates, statuses):
            if status in ("missing", "dir", "notfile"):
                continue
            if status in ("escape", "link"):
                raise ToolFailure(ErrorCode.PERMISSION_DENIED, "repository instructions cannot use symlinks")
            if status == "large":
                raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "file exceeds read limit")
            content = contents.get(candidate)
            if content is None:
                continue
            total += len(content)
            if total > 40_000:
                raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "applicable instructions exceed 40 KB")
            values.append({"path": candidate, "sha256": sha256(content).hexdigest(), "content": content.decode("utf-8")})
        from baseagent.tools.instructions import instruction_digest
        return {"scope": "/".join(parts) or ".", "instructions": values, "digest": instruction_digest(values)}

    def observe_many(self, paths):
        """{path: (sha256 | "missing", instruction digest)} for drift checks, in one exec.

        Same answers as ``file_hash`` + ``get_instructions`` per path; a path whose hash or
        guidance cannot be read maps to ``("unavailable", "unavailable")``.
        """
        from baseagent.tools.instructions import instruction_digest
        relatives = {path: self._guidance_relative(path) for path in paths}
        chains, wanted = {}, []
        for path, relative in relatives.items():
            parts = [] if relative == "." else relative.split("/")
            chain = ["AGENTS.md" if not index else "/".join(parts[:index]) + "/AGENTS.md"
                     for index in range(len(parts) + 1)]
            chains[path] = chain
            wanted += [item for item in chain if item not in wanted]
        entries = [f"r:{relative}" for relative in relatives.values()] + [f"i:{item}" for item in wanted]
        statuses, contents = self._probe(entries, READ_LIMIT)
        files = dict(zip(relatives.values(), statuses[:len(relatives)]))
        guidance = dict(zip(wanted, statuses[len(relatives):]))
        values = {}
        for path, relative in relatives.items():
            status = files[relative][0]
            if status == "missing":
                current = "missing"
            elif status == "ok" and relative in contents and len(contents[relative]) <= READ_LIMIT:
                current = sha256(contents[relative]).hexdigest()
            else:
                values[path] = ("unavailable", "unavailable")
                continue
            chain = chains[path][:-1]          # a file (or a missing path) has no AGENTS.md inside it
            found, total, broken = [], 0, False
            for item in chain:
                kind = guidance[item][0]
                if kind in ("missing", "dir", "notfile"):
                    continue
                content = contents.get(item)
                if kind != "ok" or content is None or len(content) > 20_000:
                    broken = True
                    break
                total += len(content)
                found.append({"path": item, "sha256": sha256(content).hexdigest()})
            values[path] = ("unavailable", "unavailable") if broken or total > 40_000 else (current, instruction_digest(found))
        return values

    def list_files(self, path="."):
        """(files, directories) under ``path``: sorted workspace-relative POSIX paths, links excluded."""
        relative = self._relative(path or ".")
        stdout, _ = self._fs("list", relative, timeout=120)
        tokens = stdout.split(b"\0")
        split = tokens.index(b"") if b"" in tokens else len(tokens)

        def names(raw):
            values = []
            for item in raw:
                name = os.fsdecode(item)
                name = name[2:] if name.startswith("./") else name
                if name and name != ".":
                    values.append(name)
            return sorted(values)
        return names(tokens[:split]), names(tokens[split + 1:])

    def search_files(self, query, glob="**/*"):
        if not query:
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "query must not be empty")
        if Path(glob).is_absolute() or ".." in Path(glob).parts:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "glob must stay inside workspace")
        files, _ = self.list_files(".")
        candidates = []
        for relative in files:
            parts = PurePosixPath(relative).parts
            if any(part.casefold() in PROTECTED_PARTS | {"__pycache__"} or part.casefold().startswith(".env")
                   for part in parts):
                continue
            if glob != "**/*" and not PurePosixPath(relative).match(glob):
                continue
            candidates.append(relative)
        matches = []
        for start in range(0, len(candidates), BATCH):
            for relative, value in self.read_many(candidates[start:start + BATCH]).items():
                if isinstance(value, ToolFailure):
                    continue
                try:
                    text = value.decode("utf-8")
                except UnicodeError:
                    continue
                for line_no, line in enumerate(text.splitlines(), 1):
                    if query in line:
                        matches.append({"path": relative, "line": line_no, "text": line[:300]})
                        if len(matches) >= 100:
                            return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    # ----- writes -----------------------------------------------------------------------
    def _instructions_for_write(self, relative, expected_instructions):
        instructions = self.get_instructions(relative)
        if expected_instructions is not None and expected_instructions != instructions["digest"]:
            raise ToolFailure(ErrorCode.CONFLICT, "repository instructions changed; reload them before editing")
        return instructions

    def write_file(self, path, content, expected_sha256, expected_instructions, *, pre_edit_check=None,
                   post_edit_check=None):
        """Compare-and-swap write inside the container, under the host's cooperating-writer lock.

        ``pre_edit_check(original_bytes)`` and ``post_edit_check(original_bytes, mode)`` run under
        that lock (EditPolicy diff limit and post-edit check, as in ``editing.atomic_write``).
        """
        from baseagent.tools.editing import file_lock
        relative = self._relative(path)
        if expected_sha256 is not None and expected_sha256 != "missing" and \
                (len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256)):
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "expected_sha256 must be a digest or missing")
        data = content.encode("utf-8")
        if len(data) > READ_LIMIT:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "content exceeds 200 KB write limit")
        with file_lock(self, self.root / relative):
            original, mode = None, None
            if pre_edit_check is not None or post_edit_check is not None:
                current, mode = self.stat_files([relative])[relative]
                if current != (expected_sha256 or "missing"):
                    raise ToolFailure(ErrorCode.CONFLICT, "file version changed; read the current file before editing")
                original = None if current == "missing" else self.read_bytes(relative)
            if pre_edit_check is not None:
                pre_edit_check(original)
            instructions = self._instructions_for_write(relative, expected_instructions)
            final = instructions["digest"]
            if PurePosixPath(relative).name.casefold() == "agents.md":
                from baseagent.tools.instructions import instruction_digest
                values = [item for item in instructions["instructions"] if item["path"] != relative]
                if len(data) > 20_000 or sum(len(item["content"].encode("utf-8")) for item in values) + len(data) > 40_000:
                    raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "new repository instructions exceed the guidance limits")
                values.append({"path": relative, "sha256": sha256(data).hexdigest()})
                final = instruction_digest(values)
            stdout, _ = self._fs("put", relative, expected_sha256 or "missing", "-", input_bytes=data)
            if post_edit_check is not None:
                post_edit_check(original, mode)
        written = stdout.decode().split()
        return {"path": relative, "bytes": len(data),
                "sha256": written[1] if len(written) > 1 else sha256(data).hexdigest(), "instruction_digest": final}

    def delete_file(self, path, expected_sha256, expected_instructions, *, post_delete_check=None):
        """Compare-and-delete inside the container, under the same lock as writes.

        ``post_delete_check(original_bytes, mode)`` runs under the lock after the delete.
        """
        from baseagent.tools.editing import file_lock
        relative = self._relative(path)
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "expected_sha256 must be the current file digest")
        with file_lock(self, self.root / relative):
            instructions = self._instructions_for_write(relative, expected_instructions)
            original, mode = None, None
            if post_delete_check is not None:
                current, mode = self.stat_files([relative])[relative]
                if current != expected_sha256:
                    raise ToolFailure(ErrorCode.CONFLICT, "file version changed; read the current file before editing")
                original = self.read_bytes(relative)
            self._fs("del", relative, expected_sha256)
            if post_delete_check is not None:
                post_delete_check(original, mode)
        return {"path": relative, "bytes": 0, "sha256": "missing", "instruction_digest": instructions["digest"]}

    def restore_bytes(self, path, content, mode=None):
        """Write exact bytes back (or delete when ``content`` is None) without a hash check.

        Callers (undo, apply_patch rollback, post_edit_check rollback) hold the file lock and have
        compared hashes already. ``mode`` is the permission mode for a file that is re-created.
        """
        relative = self._relative(path)
        if content is None:
            self._fs("del", relative, "force")
            return
        self._fs("put", relative, "force", format(mode, "o") if mode is not None else "-", input_bytes=bytes(content))

    # ----- commands ---------------------------------------------------------------------
    def execute(self, argv, *, timeout, output_limit=20_000, cancellation=None):
        if cancellation is not None and cancellation.cancelled():
            return ToolResult.failure(ErrorCode.CANCELLED, "command cancelled before Docker exec")
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "argv must be a nonempty string array")
        if not self.container_id:
            raise DockerError("Docker container is not running")
        marker = secrets.token_hex(8)
        command = ["docker", "exec", "-e", f"BASEAGENT_EXEC={marker}", "-w", MOUNT, self.name, *argv]
        stream = getattr(self.runner, "stream", None)
        try:
            if stream is not None:
                result = stream(command, cwd=self.root, timeout=timeout, output_limit=output_limit,
                                cancellation=cancellation)
            else:
                code, stdout, stderr = self.runner.run(command, timeout=timeout)
                data = {"exit_code": code, "stdout": stdout[:output_limit].decode("utf-8", errors="replace"),
                        "stderr": stderr[:output_limit].decode("utf-8", errors="replace"),
                        "stdout_truncated": len(stdout) > output_limit, "stderr_truncated": len(stderr) > output_limit}
                result = (ToolResult(data=data) if code == 0 else
                          ToolResult.failure(ErrorCode.EXECUTION_FAILED, f"command exited with code {code}", data=data))
        finally:
            # Like LocalBackend killing the process group: no leftovers; files go back to the host user.
            try:
                self._after(marker)
            except DockerError:
                pass
        data = result.data or {}
        code = data.get("exit_code")
        if code == 137 or (code in (1, 125, 126, 127) and _docker_level(str(data.get("stderr") or "").encode())):
            running, oom = self.alive()
            if not running:
                return ToolResult.failure(ErrorCode.EXECUTION_FAILED, str(self._gone("out of memory" if oom else "")),
                                          data=data)
            if code == 137 and oom:
                return ToolResult.failure(ErrorCode.EXECUTION_FAILED, "command was killed: the container ran out of "
                                          f"memory (limit {self.memory})", data=data)
        return result
