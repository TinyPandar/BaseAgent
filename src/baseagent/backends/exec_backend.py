"""Workspace backend whose every operation is a command in another environment.

The backend never touches the host filesystem for workspace paths. ``root`` is the
working directory *inside* the environment (for Terminal-Bench 2.0 tasks ``/app``),
reported by the environment itself, and is a ``PurePosixPath``: native I/O on it is
impossible by construction. Reads, writes, deletes, instructions and commands all go
through one ``channel``::

    channel(command: str, *, cwd: str | None, timeout: float, cancellation) -> ExecOutput

The channel raises ``ExecTimeout`` when the host-side deadline passes and
``ExecCancelled`` when the cancellation token fires (both after stopping the command).
Any other exception means the environment itself failed; the backend then raises
``EnvironmentUnavailable`` (a ``BaseException``) so the agent loop stops instead of
showing the model an infrastructure error as an ordinary tool failure.

File transfer only needs POSIX ``sh`` plus ``base64``, ``wc``, ``mktemp``, ``mv``,
``ln``, ``cp`` and ``rm`` in the environment. Commands use ``timeout`` when the
environment has it; otherwise the host-side deadline stops them.
"""

from __future__ import annotations

import base64
import binascii
import math
import posixpath
import re
import shlex
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath

from baseagent.tools.instructions import FILE_LIMIT, RULES_FILE, TOTAL_LIMIT, TRUNCATION_NOTE, _cut, instruction_digest
from baseagent.tools.result import ErrorCode, ToolFailure, ToolResult

READ_LIMIT = 200_000
WRITE_LIMIT = 200_000
# One argument of the command string stays well below Linux's 128 KiB MAX_ARG_STRLEN.
CHUNK_CHARS = 60_000
FILE_OP_TIMEOUT = 30.0
# Kill grace given to the in-environment `timeout`, and slack before the host deadline fires.
KILL_GRACE = 5
HOST_SLACK = 5
BEGIN, END = "__BASEAGENT_BEGIN__", "__BASEAGENT_END__"
PROTECTED = {".git", ".venv", ".baseagent"}

# Exit codes of the helper scripts.
NOT_FOUND, NOT_FILE, TOO_LARGE, CONFLICT, SHORT_WRITE, ESCAPES = 66, 65, 67, 73, 74, 77


class ExecTimeout(Exception):
    """The channel stopped a command at its host-side deadline."""


class ExecCancelled(Exception):
    """The channel stopped a command because the cancellation token fired."""


class EnvironmentUnavailable(BaseException):
    """The execution environment failed; this is infrastructure, not a model or tool outcome.

    A ``BaseException`` like ``Cancelled``: tool error normalization must not turn it into
    a tool result the model keeps retrying against.
    """


@dataclass(frozen=True)
class ExecOutput:
    return_code: int
    stdout: str = ""
    stderr: str = ""


# Shared prelude: $1 is the real workspace root, $2 the absolute target path.
# Refuses a target whose existing ancestor resolves outside the root, or a symlinked target.
_GUARD = r'''
R=$1; T=$2
rr=$(cd -P -- "$R" 2>/dev/null && pwd -P) || exit 70
a=$T
while [ ! -d "$a" ]; do a=$(dirname -- "$a"); done
real=$(cd -P -- "$a" 2>/dev/null && pwd -P) || exit 70
case "$real/" in "$rr"/*) ;; *) exit 77;; esac
[ -L "$T" ] && exit 77
'''

_READ = _GUARD + r'''
[ -e "$T" ] || exit 66
[ -f "$T" ] || exit 65
n=$(wc -c < "$T" | tr -d ' ')
[ "$n" -gt "$3" ] && exit 67
printf '%s\n' __BASEAGENT_BEGIN__
base64 < "$T" || exit 71
printf '\n%s\n' __BASEAGENT_END__
'''

# $3 staging file outside the workspace that receives base64 chunks.
_STAGE = r'''
printf '%s' "$2" >> "$1"
'''

# $3 staging file, $4 expected byte count, $5 "missing" for a no-overwrite create.
_COMMIT = _GUARD + r'''
S=$3
[ -e "$T" ] && [ ! -f "$T" ] && { rm -f -- "$S"; exit 65; }
d=$(dirname -- "$T")
mkdir -p -- "$d" || { rm -f -- "$S"; exit 72; }
tmp=$(mktemp "$d/.baseagent-edit-XXXXXX") || { rm -f -- "$S"; exit 72; }
trap 'rm -f -- "$tmp" "$S"' EXIT
[ -f "$T" ] && cp -p -- "$T" "$tmp" 2>/dev/null
base64 -d < "$S" > "$tmp" || exit 72
n=$(wc -c < "$tmp" | tr -d ' ')
[ "$n" -eq "$4" ] || exit 74
if [ "$5" = missing ]; then
  ln -- "$tmp" "$T" 2>/dev/null || exit 73
else
  [ -f "$T" ] || exit 73
  mv -f -- "$tmp" "$T" || exit 72
fi
'''

_DELETE = _GUARD + r'''
[ -e "$T" ] || exit 66
[ -f "$T" ] || exit 65
rm -f -- "$T" || exit 72
'''

# $3.. candidate AGENTS.md paths; prints each existing regular, non-symlink file.
_INSTRUCTIONS = _GUARD + r'''
if [ -d "$T" ]; then printf '__BASEAGENT_DIR__ 1\n'; else printf '__BASEAGENT_DIR__ 0\n'; fi
shift 2
for f in "$@"; do
  if [ -L "$f" ]; then printf '__BASEAGENT_LINK__ %s\n' "$f"; continue; fi
  if [ -f "$f" ]; then
    printf '__BASEAGENT_FILE__ %s\n' "$f"
    base64 < "$f" || exit 71
    printf '\n__BASEAGENT_EOF__\n'
  fi
done
'''


def _script(body, *args):
    return " ".join(["sh", "-c", shlex.quote(body), "sh", *(shlex.quote(str(arg)) for arg in args)])


def _decode(text):
    try:
        return base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        raise EnvironmentUnavailable("environment returned malformed file data") from None


def _framed(output: ExecOutput):
    lines = output.stdout.splitlines()
    try:
        start, end = lines.index(BEGIN), len(lines) - 1 - lines[::-1].index(END)
    except ValueError:
        raise EnvironmentUnavailable("environment returned no file data") from None
    return _decode("\n".join(lines[start + 1:end]))


class ExecBackend:
    """Files and commands of one environment, addressed relative to its working directory."""

    def __init__(self, channel, root, *, cancellation=None, file_op_timeout=FILE_OP_TIMEOUT):
        if not isinstance(root, (str, PurePosixPath)) or not str(root).startswith("/"):
            raise ValueError("exec backend root must be an absolute POSIX path inside the environment")
        normalized = posixpath.normpath(str(root))
        if normalized == "/":
            raise ValueError("exec backend root cannot be the environment's filesystem root")
        self.channel = channel
        self.root = PurePosixPath(normalized)
        self.protected_paths = ()
        self.allow_write = True
        self.cancellation = cancellation
        self.file_op_timeout = file_op_timeout
        self._has_timeout = None
        self.commands = 0

    def contract(self):
        return {"kind": "exec", "version": 1, "root": str(self.root), "isolation": "task-environment"}

    # -- namespace ----------------------------------------------------------------------------------

    def _relative(self, path) -> str:
        if not isinstance(path, str) or not path or "\0" in path or "\\" in path:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
        joined = posixpath.normpath(path if path.startswith("/") else posixpath.join(str(self.root), path))
        candidate = PurePosixPath(joined)
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace")
        relative = candidate.relative_to(self.root)
        if any(part.casefold() in PROTECTED or part.casefold().startswith((".env", ".baseagent-edit-"))
               for part in relative.parts):
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path is protected")
        return relative.as_posix()

    def resolve_path(self, path):
        return self.root / self._relative(path)

    def _absolute(self, relative):
        return str(self.root) if relative == "." else f"{self.root}/{relative}"

    # -- transport ----------------------------------------------------------------------------------

    def _run(self, command, *, timeout, cancellation=None):
        token = cancellation if cancellation is not None else self.cancellation
        if token is not None:
            token.check()
        self.commands += 1
        try:
            return self.channel(command, cwd=str(self.root), timeout=timeout, cancellation=token)
        except (ExecTimeout, ExecCancelled):
            raise
        except Exception as exc:  # noqa: BLE001 -- any other channel failure is the environment's
            raise EnvironmentUnavailable(f"environment exec failed: {type(exc).__name__}: {exc}") from exc

    def _file_op(self, body, *args):
        try:
            output = self._run(_script(body, *args), timeout=self.file_op_timeout)
        except ExecTimeout:
            raise EnvironmentUnavailable("environment file operation timed out") from None
        except ExecCancelled:
            from baseagent.kernel.cancellation import Cancelled
            raise Cancelled() from None
        if output.return_code == ESCAPES:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "path escapes workspace through a link")
        if output.return_code == 70:
            raise EnvironmentUnavailable("environment working directory is not accessible")
        return output

    # -- files --------------------------------------------------------------------------------------

    def read_bytes(self, path, limit=READ_LIMIT):
        relative = self._relative(path)
        output = self._file_op(_READ, str(self.root), self._absolute(relative), int(limit))
        if output.return_code == NOT_FOUND:
            raise ToolFailure(ErrorCode.NOT_FOUND, "file is not in the workspace")
        if output.return_code == NOT_FILE:
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "path is not a regular file")
        if output.return_code == TOO_LARGE:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "file exceeds read limit")
        if output.return_code != 0:
            raise EnvironmentUnavailable(f"environment could not read a file (exit {output.return_code})")
        value = _framed(output)
        if len(value) > limit:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "file exceeds read limit")
        return value

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
        directories = [""]
        for index in range(len(parts)):
            directories.append("/".join(parts[:index + 1]))
        candidates = [self._absolute(f"{prefix}/{RULES_FILE}" if prefix else RULES_FILE) for prefix in directories]
        output = self._file_op(_INSTRUCTIONS, str(self.root), self._absolute(relative), *candidates)
        if output.return_code != 0:
            raise EnvironmentUnavailable(f"environment could not read guidance (exit {output.return_code})")
        lines = output.stdout.splitlines()
        is_dir = "__BASEAGENT_DIR__ 1" in lines
        if not is_dir:
            # Only the target directory itself may hold the deepest AGENTS.md.
            candidates = candidates[:-1] if parts else candidates
        found, index = {}, 0
        while index < len(lines):
            line = lines[index]
            if line.startswith("__BASEAGENT_LINK__ ") and line[19:] in candidates:
                raise ToolFailure(ErrorCode.PERMISSION_DENIED, "repository instructions cannot use symlinks")
            if line.startswith("__BASEAGENT_FILE__ "):
                name, body, index = line[19:], [], index + 1
                while index < len(lines) and lines[index] != "__BASEAGENT_EOF__":
                    body.append(lines[index])
                    index += 1
                found[name] = _decode("\n".join(body))
            index += 1
        values, shown_total = [], 0
        for candidate in candidates:
            if candidate not in found:
                continue
            data = found[candidate]
            limit = min(FILE_LIMIT, TOTAL_LIMIT - shown_total)
            entry = {"sha256": sha256(data).hexdigest()}
            if len(data) <= limit:
                entry["content"] = data.decode("utf-8")
            else:
                content, shown = _cut(data[:limit + 4], limit)
                entry.update(content=content, truncated=True, size_bytes=len(data), shown_bytes=shown)
            shown_total += len(entry["content"].encode("utf-8"))
            values.append({"path": PurePosixPath(candidate).relative_to(self.root).as_posix(), **entry})
        scope = relative if is_dir else (posixpath.dirname(relative) or ".")
        value = {"scope": scope, "instructions": values, "digest": instruction_digest(values)}
        if any(item.get("truncated") for item in values):
            value["note"] = TRUNCATION_NOTE
        return value

    def search_files(self, query, glob="**/*"):
        # Search tools stay local-only in this stage; the model can run grep through run_command.
        raise ToolFailure(ErrorCode.EXECUTION_FAILED,
                          "workspace search requires a local backend; use run_command with grep in this environment")

    def _check_expected(self, relative, expected_sha256, expected_instructions):
        current = self.file_hash(relative)
        expected = "missing" if expected_sha256 is None else expected_sha256
        if expected != "missing" and not re.fullmatch("[0-9a-f]{64}", str(expected)):
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "expected_sha256 must be a digest or missing")
        if current != expected:
            raise ToolFailure(ErrorCode.CONFLICT, "file already exists" if expected == "missing"
                              else "file version changed; read the current file before editing")
        instructions = self.get_instructions(relative)
        if expected_instructions is not None and expected_instructions != instructions["digest"]:
            raise ToolFailure(ErrorCode.CONFLICT, "repository instructions changed; reload them before editing")
        return current, instructions

    def write_file(self, path, content, expected_sha256, expected_instructions):
        relative = self._relative(path)
        if relative == ".":
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "write target must be a file")
        data = content.encode("utf-8")
        if len(data) > WRITE_LIMIT:
            raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "content exceeds 200 KB write limit")
        current, instructions = self._check_expected(relative, expected_sha256, expected_instructions)
        final_digest = instructions["digest"]
        if PurePosixPath(relative).name.casefold() == RULES_FILE.casefold():
            values = [item for item in instructions["instructions"] if item["path"] != relative]
            if len(data) > FILE_LIMIT or sum(len(item["content"].encode("utf-8")) for item in values) + len(data) > TOTAL_LIMIT:
                raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "new repository instructions exceed the guidance limits")
            values.append({"path": relative, "sha256": sha256(data).hexdigest()})
            final_digest = instruction_digest(values)
        # Base64 chunks are staged outside the workspace; only the final rename touches it.
        staging = self._stage(base64.b64encode(data).decode("ascii"))
        output = self._file_op(_COMMIT, str(self.root), self._absolute(relative), staging, len(data),
                               "missing" if current == "missing" else "replace")
        if output.return_code == NOT_FILE:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "write target must be a file")
        if output.return_code == CONFLICT:
            raise ToolFailure(ErrorCode.CONFLICT, "file was created or removed by another writer")
        if output.return_code != 0:
            raise EnvironmentUnavailable(f"environment could not write a file (exit {output.return_code})")
        return {"path": relative, "bytes": len(data), "sha256": sha256(data).hexdigest(),
                "instruction_digest": final_digest}

    def _stage(self, encoded):
        output = self._file_op('mktemp "${TMPDIR:-/tmp}/baseagent-stage-XXXXXX"')
        staging = output.stdout.strip().splitlines()[-1] if output.return_code == 0 and output.stdout.strip() else ""
        if not staging.startswith("/"):
            raise EnvironmentUnavailable("environment could not create a staging file")
        for start in range(0, len(encoded), CHUNK_CHARS):
            chunk = self._file_op(_STAGE, staging, encoded[start:start + CHUNK_CHARS])
            if chunk.return_code != 0:
                raise EnvironmentUnavailable("environment could not stage file data")
        return staging

    def delete_file(self, path, expected_sha256, expected_instructions):
        relative = self._relative(path)
        if not isinstance(expected_sha256, str) or not re.fullmatch("[0-9a-f]{64}", expected_sha256):
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "expected_sha256 must be the current file digest")
        if self.file_hash(relative) == "missing":
            raise ToolFailure(ErrorCode.CONFLICT, "file does not exist; read the current workspace state")
        self._check_expected(relative, expected_sha256, expected_instructions)
        output = self._file_op(_DELETE, str(self.root), self._absolute(relative))
        if output.return_code == NOT_FOUND:
            raise ToolFailure(ErrorCode.CONFLICT, "file does not exist; read the current workspace state")
        if output.return_code == NOT_FILE:
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "write target must be a file")
        if output.return_code != 0:
            raise EnvironmentUnavailable(f"environment could not delete a file (exit {output.return_code})")
        return {"path": relative, "bytes": 0, "sha256": "missing",
                "instruction_digest": self.get_instructions(relative)["digest"]}

    # -- commands -----------------------------------------------------------------------------------

    def _timeout_available(self):
        if self._has_timeout is None:
            output = self._file_op("command -v timeout >/dev/null 2>&1")
            self._has_timeout = output.return_code == 0
        return self._has_timeout

    def execute(self, argv, *, timeout, output_limit=20_000, cancellation=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if type(output_limit) is not int or output_limit < 1:
            raise ValueError("output_limit must be a positive integer")
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "argv must be a nonempty string array")
        token = cancellation if cancellation is not None else self.cancellation
        if token is not None:
            token.check()
        # argv is quoted word by word: the environment's shell rebuilds exactly these arguments.
        command = shlex.join(argv)
        limited = self._timeout_available()
        if limited:
            command = f"timeout -k {KILL_GRACE} {math.ceil(timeout)} {command}"
        started = time.monotonic()
        try:
            output = self._run(command, timeout=timeout + (KILL_GRACE + HOST_SLACK if limited else 0),
                               cancellation=token)
        except ExecTimeout:
            return ToolResult.failure(ErrorCode.TIMEOUT, f"command timed out after {timeout} seconds",
                                      data=self._data(None, "", ""))
        except ExecCancelled:
            return ToolResult.failure(ErrorCode.CANCELLED, "command cancelled; partial side effects may exist",
                                      data=self._data(None, "", ""))
        data = self._data(output.return_code, output.stdout or "", output.stderr or "", output_limit)
        elapsed = time.monotonic() - started
        if limited and output.return_code in (124, 137) and elapsed >= timeout * 0.9:
            return ToolResult.failure(ErrorCode.TIMEOUT, f"command timed out after {timeout} seconds", data=data)
        if output.return_code:
            return ToolResult.failure(ErrorCode.EXECUTION_FAILED, f"command exited with code {output.return_code}",
                                      data=data)
        return ToolResult(data=data)

    @staticmethod
    def _data(code, stdout, stderr, limit=20_000):
        out, err = stdout.encode("utf-8"), stderr.encode("utf-8")
        return {"exit_code": code, "stdout": out[:limit].decode("utf-8", errors="replace"),
                "stderr": err[:limit].decode("utf-8", errors="replace"),
                "stdout_truncated": len(out) > limit, "stderr_truncated": len(err) > limit}
