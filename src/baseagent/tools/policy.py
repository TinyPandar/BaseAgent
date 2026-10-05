"""Exact-call authorization, independent of model and middleware decisions."""

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path

from .result import ToolFailure


_actions = {"allow", "deny", "ask"}


def _action(value):
    return isinstance(value, str) and value in _actions


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
    def __init__(self, rules=None, *, default="allow", workspace=None):
        self.rules = deepcopy(dict(rules or {}))
        self.default = default
        self.workspace = workspace
        self._scoped = False
        if not _action(default):
            raise ValueError("tool policy requires exact tool names and allow/deny/ask actions")
        for name, rule in self.rules.items():
            if not isinstance(name, str) or not name:
                raise ValueError("policy tool names must be nonempty strings")
            self._validate(rule)

    def _validate(self, rule):
        if _action(rule):
            return
        if not isinstance(rule, dict) or set(rule) != {"default", "rules"} or not _action(rule["default"]) or not isinstance(rule["rules"], list) or len(rule["rules"]) > 50:
            raise ValueError("conditional tool policy requires default and up to 50 rules")
        for condition in rule["rules"]:
            if not isinstance(condition, dict) or set(condition) != {"action", "match"} or not _action(condition["action"]):
                raise ValueError("conditional rule requires action and match")
            match = condition["match"]
            if not isinstance(match, dict) or not match or set(match) - {"equals", "path_within", "argv_exact"}:
                raise ValueError("match requires equals, path_within, or argv_exact")
            if "equals" in match:
                if not isinstance(match["equals"], dict) or not match["equals"] or any(not isinstance(key, str) or not key for key in match["equals"]):
                    raise ValueError("equals requires named argument values")
                try:
                    json.dumps(match["equals"], allow_nan=False)
                except (TypeError, ValueError) as exc:
                    raise ValueError("equals values must be finite JSON") from exc
            if "argv_exact" in match and (not isinstance(match["argv_exact"], list) or not 1 <= len(match["argv_exact"]) <= 128 or any(not isinstance(item, str) or not item or len(item) > 4000 for item in match["argv_exact"])):
                raise ValueError("argv_exact requires a bounded nonempty string list")
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
        if not isinstance(value, dict) or set(value) != {"default", "tools"} or not isinstance(value["tools"], dict):
            raise ValueError("policy JSON requires default and tools")
        return cls(value["tools"], default=value["default"], workspace=workspace)

    @classmethod
    def from_json(cls, value, *, workspace=None):
        return cls.from_dict(_strict_arguments(value), workspace=workspace)

    def contract(self):
        value = {"default": self.default, "tools": deepcopy(self.rules)}
        if self._scoped:
            value["workspace_root"] = str(self.workspace.root)
        return value

    def _matches(self, match, arguments):
        if any(key not in arguments or not _equal(arguments[key], value) for key, value in match.get("equals", {}).items()):
            return False
        if "argv_exact" in match and not _equal(arguments.get("argv"), match["argv_exact"]):
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
        if isinstance(action, dict):
            rule = action
            try:
                values = _strict_arguments(arguments)
                action = next((condition["action"] for condition in rule["rules"] if self._matches(condition["match"], values)), rule["default"])
            except (ValueError, TypeError, RecursionError):
                return "deny"
        if action != "ask":
            return action
        approval = state.metadata.get("tool_approvals", {}).get(call_id)
        if approval and approval["turn_id"] == state.turn_id and approval["policy"] == self.fingerprint and approval["request_digest"] == request_digest(name, arguments):
            return approval["decision"]
        return "ask"
