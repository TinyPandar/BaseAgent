"""Bounded local workspace searches with optional ripgrep support.

Regexes use Python's per-line semantics. Ripgrep results are normalized against
those semantics (or replaced when rg cannot represent a Python expression).
The fallback supports basic, nested .gitignore rules, including negation.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from baseagent.backends import LocalBackend

from .result import ErrorCode, ToolFailure

MAX_FILE_BYTES = 200_000
MAX_LINE_LENGTH = 300
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}


def _limit(value, name):
    if type(value) is not int or not 1 <= value <= 2000:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, f"{name} must be between 1 and 2000")


def _pattern(pattern):
    if not isinstance(pattern, str) or not pattern:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "glob pattern must not be empty")
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "glob must stay inside workspace")


def _matches(path, pattern):
    """Slashless patterns match basenames; **/ also matches zero directories."""
    if "/" not in pattern:
        return fnmatch.fnmatchcase(path.name, pattern)
    parts = Path(pattern).parts

    def match(names, patterns):
        if not patterns:
            return not names
        if patterns[0] == "**":
            return match(names, patterns[1:]) or bool(names and match(names[1:], patterns))
        return bool(names and fnmatch.fnmatchcase(names[0], patterns[0]) and match(names[1:], patterns[1:]))

    return match(path.parts, parts)


def _run_rg(rg, args, root, *, content=None):
    try:
        return subprocess.run([rg, "--no-config", *args], cwd=root, input=content,
                              capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _ignored(relative, is_dir, rules):
    ignored = False
    for scope, pattern, negate, directory_only, anchored in rules:
        local = relative.relative_to(scope) if relative.is_relative_to(scope) else None
        if local is None or (directory_only and not is_dir):
            continue
        if (_matches(local, pattern) if "/" in pattern or not anchored
                else fnmatch.fnmatchcase(local.as_posix(), pattern)):
            ignored = not negate
    return ignored


def _files(workspace, path, rg):
    # Non-local backends must never accidentally search the host namespace.
    if not isinstance(workspace.backend, LocalBackend):
        raise ToolFailure(ErrorCode.EXECUTION_FAILED, "workspace search requires a local backend")
    if not isinstance(path, str) or "\0" in path:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "path must be a string without NUL bytes")
    target = workspace._path(path)
    if not target.exists():
        raise ToolFailure(ErrorCode.NOT_FOUND, "search path does not exist")
    native = None
    if rg:
        args = ["--files", "--null", "--hidden", "--no-require-git",
                "--no-ignore-parent", "--no-ignore-global"]
        for name in sorted(_SKIP_DIRS | {".baseagent"}):
            args.extend(["--iglob", f"!**/{name}/**"])
        result = _run_rg(rg, args, workspace.root)
        if result is not None and result.returncode in (0, 1):
            native = {os.fsdecode(name) for name in result.stdout.split(b"\0") if name}

    def walk(directory, inherited):
        relative_dir = directory.relative_to(workspace.root)
        rules = list(inherited)
        ignore = directory / ".gitignore"
        if not ignore.is_symlink() and ignore.is_file():
            try:
                text = workspace.backend.read_bytes(str(ignore.relative_to(workspace.root)), MAX_FILE_BYTES).decode("utf-8")
                for line in text.splitlines():
                    line = line.rstrip()
                    if not line or line.startswith("#"):
                        continue
                    negate = line.startswith("!")
                    pattern = line[1:] if negate else line
                    rules.append((relative_dir, pattern.strip("/"), negate,
                                  pattern.endswith("/"), pattern.startswith("/")))
            except (OSError, UnicodeError, ToolFailure):
                pass
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            relative = entry.relative_to(workspace.root)
            if entry.is_symlink() or entry.name.casefold() in _SKIP_DIRS:
                continue
            try:
                workspace._path(str(relative))
                is_dir = entry.is_dir()
                if _ignored(relative, is_dir, rules):
                    continue
                if is_dir:
                    if target.is_relative_to(entry) or entry.is_relative_to(target):
                        yield from walk(entry, rules)
                elif entry.is_file() and (entry == target or entry.is_relative_to(target)):
                    if native is None or str(relative) in native:
                        yield entry
            except (OSError, ToolFailure):
                continue

    return sorted(walk(workspace.root, []), key=lambda file: str(file.relative_to(workspace.root)))


def _matching_lines(text, expression, rg, root):
    lines = text.splitlines()
    expected = [index for index, line in enumerate(lines, 1) if expression.search(line)]
    if rg:
        args = ["--text", "--line-number", "--color", "never", "--regexp", expression.pattern]
        if expression.flags & re.IGNORECASE:
            args.append("--ignore-case")
        result = _run_rg(rg, args, root, content=text.encode("utf-8"))
        if result is not None and result.returncode in (0, 1):
            actual = [int(line.split(b":", 1)[0]) for line in result.stdout.split(b"\n") if line]
            # Python supports regex constructs and Unicode folding that rg may
            # interpret differently. Keep the public semantics backend-independent.
            if actual == expected:
                return [(index, lines[index - 1]) for index in actual]
    return [(index, lines[index - 1]) for index in expected]


def grep(workspace, pattern, path=".", glob=None, ignore_case=False, max_matches=100):
    """Return matching UTF-8 lines, in workspace-relative path/line order."""
    _limit(max_matches, "max_matches")
    if not isinstance(pattern, str) or type(ignore_case) is not bool:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "invalid regex arguments")
    try:
        expression = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except (re.error, OverflowError) as error:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, f"invalid regex: {error}") from error
    if glob is not None:
        _pattern(glob)
    rg = shutil.which("rg")
    files = _files(workspace, path, rg)
    scope = workspace._path(path)
    matches, truncated = [], False
    for file in files:
        if glob is not None and not _matches(file.relative_to(scope if scope.is_dir() else scope.parent), glob):
            continue
        try:
            value = workspace.backend.read_bytes(str(file.relative_to(workspace.root)), MAX_FILE_BYTES)
            if b"\0" in value:
                continue
            text = value.decode("utf-8")
        except (OSError, UnicodeError, ToolFailure):
            continue
        for number, line in _matching_lines(text, expression, rg, workspace.root):
            if len(matches) == max_matches:
                return {"matches": matches, "truncated": True}
            truncated |= len(line) > MAX_LINE_LENGTH
            matches.append({"path": str(file.relative_to(workspace.root)), "line": number,
                            "text": line[:MAX_LINE_LENGTH]})
    return {"matches": matches, "truncated": truncated}


def glob(workspace, pattern, path=".", max_results=200):
    """List matching files, sorted by workspace-relative path."""
    _limit(max_results, "max_results")
    _pattern(pattern)
    files = _files(workspace, path, shutil.which("rg"))
    scope = workspace._path(path)
    matches = [str(file.relative_to(workspace.root)) for file in files
               if _matches(file.relative_to(scope if scope.is_dir() else scope.parent), pattern)]
    return {"matches": matches[:max_results], "truncated": len(matches) > max_results}
