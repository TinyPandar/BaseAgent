"""First-use trust for a project's ``.mcp.json`` (docs/mcp.md「首次信任」).

A ``.mcp.json`` in the workspace root names programs that would run on this machine. Before any of
them starts for the first time, the user confirms the exact file; the answer is remembered per file
and becomes invalid as soon as one byte of the file changes (including ``env`` values).

Trust records live in the user's configuration directory, never in the workspace: a cloned
repository cannot bring its own "already trusted" record. ``--mcp-config PATH`` is the user's own
choice and is not gated (unchanged behaviour).

Store: ``<config home>/mcp-trust.json``::

    {"version": 1, "projects": {"/abs/real/path/.mcp.json": {
        "sha256": "<sha256 of the file bytes>", "trusted_at": "2026-11-13T08:30:00+00:00",
        "servers": ["notes"]}}}

``<config home>`` is ``$BASEAGENT_CONFIG_HOME``, else ``$XDG_CONFIG_HOME/baseagent`` or
``~/.config/baseagent`` (Windows: ``%APPDATA%\\baseagent``). Env values are never stored or printed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from baseagent.jsondata import object_from_json
from baseagent.timestamps import iso_utc

from .config import CONFIG_NAME, McpConfigError, parse_servers

TRUST_FILE = "mcp-trust.json"
HOME_VARIABLE = "BASEAGENT_CONFIG_HOME"
MAX_CONFIG_BYTES = 200_000
MAX_STORE_BYTES = 1_000_000
PROMPT = "Trust this .mcp.json and start these servers? [y/N] "


def config_home() -> Path:
    explicit = os.getenv(HOME_VARIABLE)
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / "baseagent"
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / "baseagent"


def stdin_is_terminal() -> bool:
    """Interactive trust questions need a person at a terminal (patched in tests and the demo)."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class TrustStore:
    """Per-file trust records outside any workspace."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else config_home() / TRUST_FILE

    def usable_for(self, root) -> bool:
        """A store inside the workspace could have come with the repository, so it never counts there."""
        return not _inside(self.path, Path(root))

    def _load(self) -> dict:
        try:
            with self.path.open("rb") as handle:
                data = handle.read(MAX_STORE_BYTES + 1)
        except FileNotFoundError:
            return {"version": 1, "projects": {}}
        if len(data) > MAX_STORE_BYTES:
            raise ValueError(f"MCP trust store {self.path} is too large")
        value = object_from_json(data.decode("utf-8"), max_bytes=MAX_STORE_BYTES, label="MCP trust store")
        if value.get("version") != 1 or not isinstance(value.get("projects"), dict):
            raise ValueError(f"MCP trust store {self.path} has an unknown format")
        return value

    def _save(self, value) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".mcp-trust-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def key(config_path) -> str:
        return str(Path(config_path).resolve())

    def record(self, config_path):
        return self._load()["projects"].get(self.key(config_path))

    def is_trusted(self, config_path, digest) -> bool:
        entry = self.record(config_path)
        return isinstance(entry, dict) and entry.get("sha256") == digest

    def trust(self, config_path, digest, servers) -> dict:
        value = self._load()
        entry = {"sha256": digest, "trusted_at": iso_utc(time.time()), "servers": sorted(servers)}
        value["projects"][self.key(config_path)] = entry
        self._save(value)
        return entry

    def revoke(self, config_path) -> bool:
        value = self._load()
        removed = value["projects"].pop(self.key(config_path), None) is not None
        if removed:
            self._save(value)
        return removed


@dataclass
class Snapshot:
    """The exact bytes that are shown, hashed and (once trusted) started."""

    path: Path
    digest: str
    servers: list
    problems: list


def snapshot(path) -> Snapshot:
    path = Path(path)
    with path.open("rb") as handle:
        data = handle.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise McpConfigError(f"MCP configuration {path} exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        value = object_from_json(data.decode("utf-8-sig"), max_bytes=MAX_CONFIG_BYTES, label="MCP configuration")
    except (ValueError, UnicodeError) as exc:
        raise McpConfigError(f"cannot read MCP configuration {path}: {exc}") from exc
    servers, problems = parse_servers(value, path)
    return Snapshot(path, sha256(data).hexdigest(), servers, problems)


def describe(snap: Snapshot) -> str:
    """What the user is asked to trust: every server with its command, arguments and env key names."""
    lines = [f"MCP: this project's {CONFIG_NAME} ({snap.path}) wants to start {len(snap.servers)} server(s):"]
    for server in snap.servers:
        lines.append(f"  - {server.name}: {json.dumps([server.command, *server.args], ensure_ascii=False)}")
        keys = ", ".join(sorted(server.env)) if server.env else "(none)"
        lines.append(f"      env keys: {keys} (values not shown)")
    if not snap.servers:
        lines.append("  (no usable server entries)")
    return "\n".join(lines)


def how_to_trust(root) -> str:
    return (f"to trust it, review {Path(root) / CONFIG_NAME} and run: baseagent --root {root} --mcp-trust "
            "(or start an interactive chat there and answer y)")


@dataclass
class Decision:
    trusted: bool
    asked: bool = False
    reason: str | None = None  # "untrusted" | "declined" | "store_in_workspace"
    snapshot: Snapshot | None = None

    def skipped(self):
        """The optional json/jsonl ``mcp`` field when servers were not started (no values, names only)."""
        if self.trusted:
            return None
        return {"skipped": self.reason, "config": CONFIG_NAME,
                "servers": [server.name for server in (self.snapshot.servers if self.snapshot else [])]}


def gate(path, root, *, interactive, ask=None, out=None, store=None) -> Decision:
    """Decide whether the project ``.mcp.json`` at ``path`` may start. Nothing is started here.

    Interactive (a terminal): show the servers and ask once; "y" is remembered for these exact bytes.
    Non-interactive: never ask and never start an untrusted file.
    """
    out = out or (lambda line: print(line, file=sys.stderr))
    ask = ask or (lambda prompt: input(prompt))  # looked up at call time, like the chat's own prompts
    store = store or TrustStore()
    snap = snapshot(path)
    if not store.usable_for(root):
        out(f"MCP: the trust store {store.path} is inside the workspace, so it cannot vouch for this project; "
            f"servers in {CONFIG_NAME} were not started. Set {HOME_VARIABLE} to a directory outside the workspace.")
        return Decision(False, reason="store_in_workspace", snapshot=snap)
    if store.is_trusted(path, snap.digest):
        return Decision(True, snapshot=snap)
    changed = store.record(path) is not None
    if not interactive:
        why = "changed since it was trusted" if changed else "not trusted yet"
        out(f"MCP: {snap.path} is {why}; its servers were not started (non-interactive run). {how_to_trust(root)}.")
        return Decision(False, reason="untrusted", snapshot=snap)
    if changed:
        out(f"MCP: {CONFIG_NAME} changed since you trusted it; confirm it again.")
    out(describe(snap))
    try:
        answer = ask(PROMPT)
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in {"y", "yes"}:
        out(f"MCP: not trusted; no MCP server was started and this session has no MCP tools. Later, {how_to_trust(root)}.")
        return Decision(False, asked=True, reason="declined", snapshot=snap)
    store.trust(path, snap.digest, [server.name for server in snap.servers])
    out(f"MCP: trusted {snap.path} (remembered in {store.path} until the file changes; "
        f"revoke with baseagent --root {root} --mcp-untrust).")
    return Decision(True, asked=True, snapshot=snap)


def trust_command(root, *, store=None, out=print) -> int:
    """``--mcp-trust``: show the project's servers and record trust for exactly these bytes."""
    store = store or TrustStore()
    path = Path(root) / CONFIG_NAME
    if not path.is_file():
        out(f"No {CONFIG_NAME} in {root}; nothing to trust.")
        return 1
    if not store.usable_for(root):
        out(f"The trust store {store.path} is inside the workspace; set {HOME_VARIABLE} to a directory outside it.")
        return 1
    snap = snapshot(path)
    out(describe(snap))
    store.trust(path, snap.digest, [server.name for server in snap.servers])
    out(f"Trusted {snap.path} (sha256 {snap.digest[:12]}…); recorded in {store.path}. "
        f"It stays trusted until the file changes; revoke with baseagent --root {root} --mcp-untrust.")
    return 0


def untrust_command(root, *, store=None, out=print) -> int:
    """``--mcp-untrust``: forget the trust record of the project's ``.mcp.json``."""
    store = store or TrustStore()
    path = Path(root) / CONFIG_NAME
    if store.revoke(path):
        out(f"Trust revoked for {store.key(path)}; its servers will ask again before starting.")
    else:
        out(f"No trust record for {store.key(path)}.")
    return 0
