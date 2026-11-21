"""Built-in guard: refuse commands that recursively delete critical paths.

Always on, independent of ``--tool-policy``, ``--mode`` and approvals; applied by the
gateway to every tool call whose arguments carry an ``argv`` string list
(``run_command``, ``verify_command``, ``start_job``). It is a best-effort argv check,
not a sandbox: see docs/tool_annotations.md for what is and is not caught.

Critical paths: a filesystem or drive root, the workspace root or any ancestor of it,
the home directory or any ancestor of it, and any directory named ``.git`` or
``.baseagent``.
"""

from __future__ import annotations

import os
from pathlib import PureWindowsPath
import re
import shlex

PROTECTED_NAMES = frozenset({".git", ".baseagent"})
DELETE_PROGRAMS = frozenset({"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "\n", "(", ")"})
_CMD_SWITCH = re.compile(r"^/[a-zA-Z?]$")
_PS_PATH_PARAMS = ("-path", "-literalpath", "-lp", "-pspath")
_PS_VALUE_PARAMS = ("-filter", "-include", "-exclude", "-credential", "-stream")
_HOME_TOKENS = re.compile(r"^(~|\$home|\$\{home\}|%userprofile%|\$env:userprofile|\$env:home)(?=$|[\\/])", re.I)
_MAX_DEPTH = 3
_MAX_TOKENS = 4096


def _program(token: str) -> str:
    name = PureWindowsPath(token.strip("\"'")).name.lower()
    return re.sub(r"\.(exe|cmd|bat|com|ps1)$", "", name)


def _tokenize(text: str) -> list[list[str]]:
    """Command segments of a shell string, tokenized the POSIX and the Windows way."""
    variants = []
    for posix in (True, False):
        try:
            lexer = shlex.shlex(text, posix=posix, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)[:_MAX_TOKENS]
        except ValueError:
            tokens = text.split()[:_MAX_TOKENS]
        if not posix:
            tokens = [token[1:-1] if len(token) > 1 and token[0] == token[-1] and token[0] in "\"'" else token for token in tokens]
        segment = []
        for token in tokens:
            if token in _SEPARATORS or set(token) <= set(";&|()"):
                if segment:
                    variants.append(segment)
                segment = []
            else:
                segment.append(token)
        if segment:
            variants.append(segment)
    return variants


class _Paths:
    def __init__(self, root, base, home):
        self.root = self._norm(root) if root else None
        self.base = base
        self.home = self._norm(home) if home else None

    @staticmethod
    def _norm(path):
        return os.path.normcase(os.path.abspath(path))

    def resolve(self, token: str) -> str | None:
        token = token.strip().strip("\"'")
        if not token:
            return None
        match = _HOME_TOKENS.match(token)
        if match:
            if not self.home:
                return None
            token = self.home + token[match.end():]
        elif re.match(r"^(\$|%)", token):
            return None  # other variables are not expanded
        if os.name == "nt":
            if re.match(r"^/[a-zA-Z](/|$)", token):  # Git Bash style /c/Users
                token = token[1] + ":/" + token[3:]
            if re.match(r"^[a-zA-Z]:$", token):  # drive-relative: treat as the drive root
                token += "\\"
        head, tail = os.path.split(token.rstrip("/\\") or token)
        if tail in {"*", ".*", "*.*"}:
            token = head or "."  # emptying a directory counts as deleting it
        return self._norm(os.path.join(self.base, token))

    def critical(self, path: str) -> str | None:
        if os.path.dirname(path) == path:
            return "a filesystem or drive root"
        if os.path.basename(path) in PROTECTED_NAMES:
            return f"a {os.path.basename(path)} directory"
        for label, target in (("the workspace root or a directory containing it", self.root),
                              ("the home directory or a directory containing it", self.home)):
            if target and (path == target or _contains(path, target)):
                return label
        return None


def _contains(parent: str, child: str) -> bool:
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:  # different drives
        return False


def _delete_targets(words: list[str]) -> list[str] | None:
    """Targets of a recursive delete, or None when ``words`` is not one."""
    name = _program(words[0])
    recursive, targets, options_done, skip = False, [], False, None
    for word in words[1:]:
        lower = word.lower()
        if skip is not None:
            if skip == "path":
                targets.extend(part for part in word.split(",") if part)
            skip = None
            continue
        if options_done:
            targets.append(word)
        elif word == "--":
            options_done = True
        elif name != "rm" and _CMD_SWITCH.match(word):
            recursive = recursive or lower == "/s"
        elif lower.startswith("--"):
            recursive = recursive or lower == "--recursive"
        elif lower.startswith("-") and len(word) > 1:
            if any(param.startswith(lower) for param in _PS_PATH_PARAMS) and lower not in {"-p"}:
                skip = "path"
            elif any(param.startswith(lower) and len(lower) >= 3 for param in _PS_VALUE_PARAMS):
                skip = "value"
            elif "-recurse".startswith(lower) and len(lower) >= 2:
                recursive = True
            elif name == "rm" and ("r" in word[1:] or "R" in word[1:]):
                recursive = True
        elif name in {"remove-item", "ri"}:
            targets.extend(part for part in word.split(",") if part)  # PowerShell path arrays
        else:
            targets.append(word)
    return targets if recursive else None


def _git_clean_targets(words: list[str]) -> list[str] | None:
    """Pathspecs (default ".") of ``git clean`` that force-removes untracked and ignored directories."""
    index, base = 1, None
    while index < len(words) and words[index].startswith("-"):
        if words[index] in {"-C", "-c"} and index + 1 < len(words):
            if words[index] == "-C":
                base = words[index + 1]
            index += 2
        else:
            index += 1
    if index >= len(words) or words[index] != "clean":
        return None
    flags, paths, options_done = set(), [], False
    for word in words[index + 1:]:
        if options_done or not word.startswith("-"):
            paths.append(word)
        elif word == "--":
            options_done = True
        elif word.startswith("--"):
            flags.add({"--force": "f", "--dry-run": "n"}.get(word, ""))
        else:
            flags.update(word[1:])
    if "n" in flags or not ({"f", "d"} <= flags and flags & {"x", "X"}):
        return None
    paths = paths or ["."]
    return [os.path.join(base, path) for path in paths] if base else paths


def _find_targets(words: list[str]) -> list[str] | None:
    if "-delete" not in words:
        return None
    starts = []
    for word in words[1:]:
        if word.startswith(("-", "(", "!")):
            break
        starts.append(word)
    return starts or ["."]


def _check(words, paths, depth):
    for start in range(len(words)):
        word = words[start]
        if depth < _MAX_DEPTH and (" " in word or "\n" in word or ";" in word or "&" in word):
            for segment in _tokenize(word):
                reason = _check(segment, paths, depth + 1)
                if reason:
                    return reason
        program = _program(word)
        if program in DELETE_PROGRAMS:
            targets = _delete_targets(words[start:])
        elif program == "git":
            targets = _git_clean_targets(words[start:])
        elif program == "find":
            targets = _find_targets(words[start:])
        else:
            continue
        for target in targets or ():
            resolved = paths.resolve(target)
            label = paths.critical(resolved) if resolved else None
            if label:
                return f"{' '.join(words[start:start + 3])[:200]!s} would delete {label} ({target[:200]})"
    return None


def critical_path_violation(argv, *, root=None, cwd=None, home=None) -> str | None:
    """Reason text when ``argv`` recursively deletes a critical path, else None.

    ``root`` is the workspace root, ``cwd`` the command's working directory relative to it
    (``start_job``), ``home`` overrides the home directory (tests).
    """
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
        return None
    base = os.path.abspath(root) if root else os.getcwd()
    if isinstance(cwd, str) and cwd:
        base = os.path.join(base, cwd)
    paths = _Paths(root, base, home if home is not None else os.path.expanduser("~"))
    return _check(list(argv), paths, 0)
