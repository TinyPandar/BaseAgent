"""Exact-call authorization, independent of model and middleware decisions."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path, PureWindowsPath
import re
import warnings

from baseagent.kernel.tools import ANNOTATION_KEYS, ToolAnnotations

from .result import ToolFailure


_actions = {"allow", "deny", "ask"}
_RANK = {"allow": 0, "ask": 1, "deny": 2}
_MATCH_KEYS = {"equals", "path_within", "argv_exact", "argv_prefix"}
# Optional per-tool key; absent means the original first-match order (contract unchanged).
_PRECEDENCE = {"first_match", "strictest"}
# Optional top-level "annotations" section: annotation name -> floor action (ask or deny).
# A key may join several names with "+": the floor applies when all of them are true.
_FLOOR_ACTIONS = {"ask", "deny"}
# The --mode full default (docs/tool_annotations.md): ask before tools that are both destructive and
# open-world (run_command, verify_command, start_job, non-read-only MCP tools). --auto-approve leaves it out.
COMMAND_FLOOR = "destructive+open_world"
# (tool name, ToolAnnotations) of the call being authorized, set by the gateway (agent/gateway.py).
_CALL_ANNOTATIONS = ContextVar("baseagent_call_annotations", default=None)


@contextmanager
def call_annotations(name, annotations):
    """Make ``annotations`` the record ToolPolicy sees for ``name`` while authorizing one call."""
    token = _CALL_ANNOTATIONS.set((name, annotations))
    try:
        yield
    finally:
        _CALL_ANNOTATIONS.reset(token)


def _annotation_key(key):
    """The annotation names of one "annotations" key ("destructive" or "destructive+open_world"), else None."""
    if not isinstance(key, str):
        return None
    parts = key.split("+")
    if any(part not in ANNOTATION_KEYS for part in parts) or len(set(parts)) != len(parts):
        return None
    return parts


def with_command_floor(policy):
    """Add the --mode full floor (COMMAND_FLOOR: ask) to ``policy`` in place and return it.

    A stricter user entry for the same key (deny) is kept. Only an active floor changes the
    policy contract; without it contract() and fingerprint stay byte-identical.
    """
    current = dict(policy.annotations or {})
    if current.get(COMMAND_FLOOR) != "deny":
        current[COMMAND_FLOOR] = "ask"
    policy.annotations = current
    return policy


def _annotations_for(name):
    current = _CALL_ANNOTATIONS.get()
    if current is not None and current[0] == name and isinstance(current[1], ToolAnnotations):
        return current[1]
    # Unknown to the caller: the conservative MCP defaults (fail closed).
    return ToolAnnotations()

# Programs that run another program or inline code taken from their arguments. An argv
# prefix that only names one of them cannot constrain what finally runs.
COMMAND_WRAPPERS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "busybox",
    "cmd", "powershell", "pwsh", "wsl", "start", "runas",
    "python", "python3", "py", "pythonw", "node", "deno", "bun", "perl", "ruby", "php", "lua", "tclsh",
    "env", "xargs", "sudo", "doas", "su", "nohup", "nice", "ionice", "time", "timeout", "stdbuf",
    "setsid", "chroot", "strace", "watch", "exec", "command", "builtin", "eval", "find",
    "npx", "uvx", "pipx",
})


class CommandRuleWarning(UserWarning):
    """An allow rule whose argv prefix names a command wrapper or interpreter."""


def command_wrapper(program):
    """Return the normalized wrapper name when ``program`` is a known wrapper, else None."""
    if not isinstance(program, str) or not program:
        return None
    name = PureWindowsPath(program).name.lower()  # also splits POSIX separators
    name = re.sub(r"\.(exe|cmd|bat|com|ps1)$", "", name)
    name = re.sub(r"[0-9.]+$", "", name) or name  # python3.12, pwsh7
    if name in COMMAND_WRAPPERS:
        return name
    return None


def _action(value):
    return isinstance(value, str) and value in _actions


def _argv_list(value, label):
    if not isinstance(value, list) or not 1 <= len(value) <= 128 or any(not isinstance(item, str) or not item or len(item) > 4000 for item in value):
        raise ValueError(f"{label} requires a bounded nonempty string list")


def _strict_arguments(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > 256_000:
        raise ValueError("conditional arguments exceed limit")
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate argument key")
            result[key] = item
        return result
    def constant(value):
        raise ValueError("invalid JSON constant")
    result = json.loads(value or "{}", object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(result, dict):
        raise ValueError("arguments must be an object")
    json.dumps(result, allow_nan=False)
    return result


def _equal(left, right):
    # Exact JSON values, including types; True must not match an integer 1.
    return type(left) is type(right) and json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def request_digest(name, arguments):
    # Bind the exact argument string; normalization must not hide changed input.
    return sha256(json.dumps([name, arguments], ensure_ascii=False).encode("utf-8")).hexdigest()


class ToolPolicy:
    def __init__(self, rules=None, *, default="allow", workspace=None, annotations=None):
        self.rules = deepcopy(dict(rules or {}))
        self.default = default
        # None (absent) keeps contract() and fingerprint unchanged for existing policies.
        self.annotations = deepcopy(annotations) if annotations is not None else None
        if self.annotations is not None and (
                not isinstance(self.annotations, dict) or not self.annotations
                or any(_annotation_key(key) is None or not isinstance(value, str) or value not in _FLOOR_ACTIONS
                       for key, value in self.annotations.items())):
            raise ValueError("policy annotations map read_only/destructive/idempotent/open_world "
                             "(or several joined by '+', all required) to ask or deny")
        self.workspace = workspace
        self._scoped = False
        # Human-readable notes about allow rules that cannot constrain a command (see COMMAND_WRAPPERS).
        self.warnings = []
        if not _action(default):
            raise ValueError("tool policy requires exact tool names and allow/deny/ask actions")
        for name, rule in self.rules.items():
            if not isinstance(name, str) or not name:
                raise ValueError("policy tool names must be nonempty strings")
            self._validate(rule)
            self._warn_wrappers(name, rule)
        for message in self.warnings:
            warnings.warn(message, CommandRuleWarning, stacklevel=2)

    def _warn_wrappers(self, name, rule):
        if not isinstance(rule, dict):
            return
        for index, condition in enumerate(rule["rules"]):
            prefix = condition["match"].get("argv_prefix")
            wrapper = command_wrapper(prefix[0]) if prefix else None
            if condition["action"] == "allow" and wrapper:
                self.warnings.append(
                    f"tool policy {name} rule {index}: argv_prefix allows {prefix[0]!r} ({wrapper}), which runs "
                    "arbitrary commands or code from its remaining arguments; the prefix cannot constrain them. "
                    "Use argv_exact or ask instead.")

    def _validate(self, rule):
        if _action(rule):
            return
        if (not isinstance(rule, dict) or not {"default", "rules"} <= set(rule) <= {"default", "rules", "precedence"}
                or not _action(rule["default"]) or not isinstance(rule["rules"], list) or len(rule["rules"]) > 50):
            raise ValueError("conditional tool policy requires default and up to 50 rules")
        if "precedence" in rule and (not isinstance(rule["precedence"], str) or rule["precedence"] not in _PRECEDENCE):
            raise ValueError("precedence must be first_match or strictest")
        for condition in rule["rules"]:
            if not isinstance(condition, dict) or set(condition) != {"action", "match"} or not _action(condition["action"]):
                raise ValueError("conditional rule requires action and match")
            match = condition["match"]
            if not isinstance(match, dict) or not match or set(match) - _MATCH_KEYS:
                raise ValueError("match requires equals, path_within, argv_exact, or argv_prefix")
            if "equals" in match:
                if not isinstance(match["equals"], dict) or not match["equals"] or any(not isinstance(key, str) or not key for key in match["equals"]):
                    raise ValueError("equals requires named argument values")
                try:
                    json.dumps(match["equals"], allow_nan=False)
                except (TypeError, ValueError) as exc:
                    raise ValueError("equals values must be finite JSON") from exc
            for key in ("argv_exact", "argv_prefix"):
                if key in match:
                    _argv_list(match[key], key)
            if "path_within" in match:
                paths = match["path_within"]
                if self.workspace is None or not isinstance(paths, dict) or not paths:
                    raise ValueError("path_within requires a workspace and named argument scopes")
                self._scoped = True
                for key, roots in paths.items():
                    if not isinstance(key, str) or not key or not isinstance(roots, list) or not 1 <= len(roots) <= 20:
                        raise ValueError("path scopes require named arguments and 1-20 roots")
                    for root in roots:
                        if not isinstance(root, str) or not root or ":" in root or Path(root).is_absolute() or ".." in Path(root).parts:
                            raise ValueError("path scope roots must be relative workspace paths")
                        try:
                            target = self.workspace._path(root)
                            if Path(os.path.abspath(self.workspace.root / root)) != target:
                                raise ValueError("scope roots cannot use links or junctions")
                        except ToolFailure as exc:
                            raise ValueError("path scope root is outside permitted workspace") from exc

    @classmethod
    def from_dict(cls, value, *, workspace=None):
        if (not isinstance(value, dict) or not {"default", "tools"} <= set(value) <= {"default", "tools", "annotations"}
                or not isinstance(value["tools"], dict)):
            raise ValueError("policy JSON requires default and tools")
        return cls(value["tools"], default=value["default"], workspace=workspace, annotations=value.get("annotations"))

    @classmethod
    def from_json(cls, value, *, workspace=None):
        return cls.from_dict(_strict_arguments(value), workspace=workspace)

    def contract(self):
        value = {"default": self.default, "tools": deepcopy(self.rules)}
        if self.annotations is not None:
            value["annotations"] = dict(sorted(self.annotations.items()))
        if self._scoped:
            value["workspace_root"] = str(self.workspace.root)
        return value

    def _matches(self, match, arguments):
        if any(key not in arguments or not _equal(arguments[key], value) for key, value in match.get("equals", {}).items()):
            return False
        if "argv_exact" in match and not _equal(arguments.get("argv"), match["argv_exact"]):
            return False
        if "argv_prefix" in match:
            argv, prefix = arguments.get("argv"), match["argv_prefix"]
            # Element-wise exact strings; a non-string element never matches.
            if not isinstance(argv, list) or len(argv) < len(prefix) or any(not isinstance(item, str) or item != expected for item, expected in zip(argv, prefix)):
                return False
        for key, roots in match.get("path_within", {}).items():
            value = arguments.get(key)
            if not isinstance(value, str) or not value or ":" in value or Path(value).is_absolute() or ".." in Path(value).parts:
                return False
            try:
                target = self.workspace._path(value)
                # Reject aliases even if they resolve inside an allowed directory.
                if Path(os.path.abspath(self.workspace.root / value)) != target:
                    return False
                scopes = []
                for root in roots:
                    scope = self.workspace._path(root)
                    if Path(os.path.abspath(self.workspace.root / root)) != scope:
                        return False
                    scopes.append(scope)
                if not any(target == scope or target.is_relative_to(scope) for scope in scopes):
                    return False
            except (OSError, ToolFailure):
                return False
        return True

    @property
    def fingerprint(self):
        return sha256(json.dumps(self.contract(), sort_keys=True).encode("utf-8")).hexdigest()

    def action(self, state, call_id, name, arguments):
        action = self.rules.get(name, self.default)
        # Explicit: a string action naming this tool, or a condition rule that matched.
        explicit = name in self.rules and not isinstance(action, dict)
        if isinstance(action, dict):
            rule = action
            try:
                values = _strict_arguments(arguments)
                if rule.get("precedence") == "strictest":
                    # Every matching rule counts; deny > ask > allow regardless of order.
                    matched = [condition["action"] for condition in rule["rules"] if self._matches(condition["match"], values)]
                    action = max(matched, key=_RANK.__getitem__) if matched else rule["default"]
                else:
                    first = next((condition["action"] for condition in rule["rules"] if self._matches(condition["match"], values)), None)
                    matched = [] if first is None else [first]
                    action = rule["default"] if first is None else first
                explicit = bool(matched)
            except (ValueError, TypeError, RecursionError):
                return "deny"
        if self.annotations is not None:
            flags = _annotations_for(name)
            floors = [floor for key, floor in self.annotations.items()
                      if all(getattr(flags, part) for part in _annotation_key(key))]
            floor = max(floors, key=_RANK.__getitem__) if floors else "allow"
            # An annotation deny is absolute; an annotation ask yields only to an explicit per-tool decision.
            if floor == "deny" or (not explicit and _RANK[floor] > _RANK[action]):
                action = floor
        # A deny is final: no approval below can turn it into allow.
        if action != "ask":
            return action
        approval = state.metadata.get("tool_approvals", {}).get(call_id)
        if approval and approval["turn_id"] == state.turn_id and approval["policy"] == self.fingerprint and approval["request_digest"] == request_digest(name, arguments):
            return approval["decision"]
        return "ask"
