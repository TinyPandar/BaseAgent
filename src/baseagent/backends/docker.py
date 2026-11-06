"""One container per task. Network is off unless the caller opts in.

LocalBackend remains available. This backend does not implement a custom
OS sandbox; Docker is the isolation boundary for this stage.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path, PurePosixPath
import subprocess
import tempfile

from baseagent.tools.result import ErrorCode, ToolFailure, ToolResult


class DockerError(RuntimeError):
    """A readable Docker failure. The kernel should see this, not hang."""


class DockerCli:
    def run(self, argv, *, input_bytes=None, timeout=60):
        try:
            completed = subprocess.run(argv, input=input_bytes, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise DockerError("Docker is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError("Docker command timed out") from exc
        return completed.returncode, completed.stdout, completed.stderr


def explain_docker_failure(stderr, *, image):
    text = stderr.decode("utf-8", errors="replace")
    lowered = text.lower()
    if "cannot connect to the docker daemon" in lowered or "docker daemon" in lowered and "error" in lowered:
        return "Docker is not running"
    if "no such image" in lowered or "pull access denied" in lowered or "not found" in lowered and image.lower() in lowered:
        return f"Docker image is missing: {image}"
    if "oomkilled" in lowered or "out of memory" in lowered:
        return "container ran out of memory"
    return text.strip() or "Docker command failed"


class DockerBackend:
    def __init__(self, root, *, image="python:3.12-slim", network="none", cpus=1, memory="512m",
                 pids=128, protected_paths=(), runner=None, start=True, name=None):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace root must be a directory")
        if network not in {"none", "bridge"}:
            raise ValueError("docker network must be none or bridge")
        if type(cpus) not in (int, float) or cpus <= 0 or type(pids) is not int or pids < 1:
            raise ValueError("cpu and pid limits must be positive")
        if not isinstance(memory, str) or not memory:
            raise ValueError("memory limit must be a Docker size such as 512m")
        self.image = image
        self.network = network
        self.cpus = cpus
        self.memory = memory
        self.pids = pids
        self.protected_paths = tuple(Path(path).resolve() for path in protected_paths)
        self.runner = runner or DockerCli()
        self.name = name or "baseagent-" + sha256(str(self.root).encode()).hexdigest()[:12]
        self.container_id = None
        self.allow_write = True
        if start:
            self.start()

    def contract(self):
        return {"kind": "docker", "version": 1, "image": self.image, "network": self.network,
                "cpus": self.cpus, "memory": self.memory, "pids": self.pids,
                "mount": "/workspace", "isolation": "container", "container": self.container_id}

    def start(self):
        if self.container_id:
            return self.container_id
        code, stdout, stderr = self.runner.run([
            "docker", "run", "-d", "--name", self.name, "--network", self.network,
            "--cpus", str(self.cpus), "--memory", self.memory, "--pids-limit", str(self.pids),
            "--entrypoint", "sleep", "-v", f"{self.root}:/workspace:rw", "-w", "/workspace",
            self.image, "infinity",
        ])
        if code != 0:
            raise DockerError(explain_docker_failure(stderr, image=self.image))
        self.container_id = stdout.decode().strip()
        if not self.container_id:
            raise DockerError("Docker did not return a container id")
        return self.container_id

    def close(self):
        if not self.container_id and not self.name:
            return
        code, stdout, stderr = self.runner.run(["docker", "rm", "-f", self.name])
        if code != 0 and b"No such container" not in stderr and b"No such container" not in stdout:
            raise DockerError(explain_docker_failure(stderr, image=self.image))
        self.container_id = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def resolve_path(self, path):
        relative = self._relative(path)
        return (self.root / relative).resolve()

    def read_bytes(self, path, limit=200_000):
        relative = self._relative(path)
        code, stdout, stderr = self._exec(["sh", "-c", "cat \"$1\"", "sh", relative])
        if code != 0:
            message = stderr.decode("utf-8", errors="replace")
            if "No such file" in message or "can't open" in message:
                raise ToolFailure(ErrorCode.NOT_FOUND, "file is not in the workspace")
            raise DockerError(explain_docker_failure(stderr, image=self.image))
        if len(stdout) > limit:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "file exceeds read limit")
        return stdout

    def file_hash(self, path):
        try:
            return sha256(self.read_bytes(path)).hexdigest()
        except ToolFailure as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return "missing"
            raise

    def get_instructions(self, path="."):
        relative = self._relative(path or ".")
        parts = [] if relative == "." else relative.split("/")
        values = []
        total = 0
        prefixes = [""]
        built = []
        for part in parts:
            built.append(part)
            prefixes.append("/".join(built))
        for prefix in prefixes:
            candidate = "AGENTS.md" if not prefix else prefix + "/AGENTS.md"
            try:
                content = self.read_bytes(candidate, 20_000)
            except ToolFailure:
                continue
            total += len(content)
            if total > 40_000:
                raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "applicable instructions exceed 40 KB")
            values.append({"path": candidate, "sha256": sha256(content).hexdigest(), "content": content.decode("utf-8")})
        digest = sha256(str([(item["path"], item["sha256"]) for item in values]).encode()).hexdigest()
        return {"scope": relative, "instructions": values, "digest": digest}

    def search_files(self, query, glob="**/*"):
        if not query:
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "query must not be empty")
        matches = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.root).as_posix()
            if any(part.casefold() in {".git", ".venv", ".baseagent"} for part in PurePosixPath(relative).parts):
                continue
            if glob != "**/*" and not PurePosixPath(relative).match(glob):
                continue
            try:
                text = self.read_bytes(relative).decode("utf-8")
            except (UnicodeError, ToolFailure, DockerError):
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if query in line:
                    matches.append({"path": relative, "line": line_no, "text": line[:300]})
                    if len(matches) >= 100:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def write_file(self, path, content, expected_sha256, expected_instructions):
        relative = self._relative(path)
        current = self.file_hash(relative)
        if expected_sha256 not in {None, "missing", current}:
            raise ToolFailure(ErrorCode.CONFLICT, "file version changed; read the current file")
        if expected_sha256 == "missing" and current != "missing":
            raise ToolFailure(ErrorCode.CONFLICT, "file already exists")
        data = content.encode("utf-8")
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(data)
            temporary = handle.name
        try:
            code, stdout, stderr = self.runner.run(["docker", "cp", temporary, f"{self.name}:/workspace/{relative}"])
        finally:
            Path(temporary).unlink(missing_ok=True)
        if code != 0:
            raise DockerError(explain_docker_failure(stderr, image=self.image))
        return {"path": relative, "bytes": len(data), "sha256": sha256(data).hexdigest()}

    def execute(self, argv, *, timeout, output_limit=20_000, cancellation=None):
        if cancellation is not None and cancellation.cancelled():
            return ToolResult.failure(ErrorCode.CANCELLED, "command cancelled before Docker exec")
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "argv must be a nonempty string array")
        code, stdout, stderr = self._exec(list(argv), timeout=timeout)
        text = stderr.decode("utf-8", errors="replace")
        if "oomkilled" in text.lower():
            raise DockerError("container ran out of memory")
        data = {"exit_code": code, "stdout": stdout[:output_limit].decode("utf-8", errors="replace"),
                "stderr": stderr[:output_limit].decode("utf-8", errors="replace"),
                "stdout_truncated": len(stdout) > output_limit, "stderr_truncated": len(stderr) > output_limit}
        if code != 0:
            return ToolResult.failure(ErrorCode.EXECUTION_FAILED, f"command exited with code {code}", data=data)
        return ToolResult(data=data)

    def _exec(self, argv, timeout=60):
        if not self.container_id:
            raise DockerError("Docker container is not running")
        return self.runner.run(["docker", "exec", "-w", "/workspace", self.name, *argv], timeout=timeout)

    def _relative(self, path):
        if not isinstance(path, str) or not path or path.startswith(("/", "\\")) or "\\" in path:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
        if any(part.casefold() in {".git", ".venv", ".baseagent"} or part.casefold().startswith(".env") for part in relative.parts):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path is protected")
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
        return relative.as_posix()
