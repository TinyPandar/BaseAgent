"""Bounded root-to-directory AGENTS.md guidance within a workspace.

Each applicable ``AGENTS.md`` (workspace root first, then every directory down to the target) is
read in full for its SHA-256, so the instruction digest - and with it the ``expected_instructions``
write CAS and drift detection - always covers the whole file. Only the text shown to the model is
bounded: at most ``FILE_LIMIT`` bytes per file and ``TOTAL_LIMIT`` bytes for all of them together.
A cut entry says so (``truncated``, ``size_bytes``, ``shown_bytes``); an entry that fits is unchanged.
"""

from hashlib import sha256
import json

from .result import ErrorCode, ToolFailure

RULES_FILE = "AGENTS.md"
FILE_LIMIT = 20_000
TOTAL_LIMIT = 40_000
TRUNCATION_NOTE = (f"Some guidance was truncated (at most {FILE_LIMIT} bytes per AGENTS.md and {TOTAL_LIMIT} bytes "
                   "in total are shown). Read the file with read_file for the rest.")


def instruction_digest(values):
    contract = [{"path": value["path"], "sha256": value["sha256"]} for value in values]
    return sha256(json.dumps(contract, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _read(path, shown_limit):
    """(sha256 of the whole file, size in bytes, the first bytes up to shown_limit + 3)."""
    digest, size, head = sha256(), 0, bytearray()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
            size += len(chunk)
            if len(head) <= shown_limit:
                head += chunk[:shown_limit + 4 - len(head)]
    return digest.hexdigest(), size, bytes(head)


def _cut(head, limit):
    """Decode at most ``limit`` bytes, stepping back to a whole UTF-8 character."""
    for end in range(limit, max(limit - 4, -1), -1):
        try:
            return head[:end].decode("utf-8"), end
        except UnicodeDecodeError:
            continue
    return head[:limit].decode("utf-8"), limit  # raises like an undecodable file always did


HEAD_BYTES = FILE_LIMIT + 4   # enough bytes to cut at any shown limit on a whole UTF-8 character


def rules_entry(digest, size, head, shown_limit=FILE_LIMIT):
    """The instruction entry for one rules file from (whole-file SHA-256, size, first bytes).

    Shared by every backend so the cut, the fields and the digest are identical: LocalBackend reads the
    triple from the host file, DockerBackend from a snapshot inside the container. ``head`` holds at
    least ``min(size, shown_limit + 3)`` bytes.
    """
    if size <= shown_limit:
        return {"sha256": digest, "content": head.decode("utf-8")}
    content, shown = _cut(head, shown_limit)
    return {"sha256": digest, "content": content, "truncated": True, "size_bytes": size, "shown_bytes": shown}


def read_rules_file(path, shown_limit=FILE_LIMIT):
    """One rules file as an instruction entry: ``{path?, sha256, content}`` plus truncation fields when cut."""
    return rules_entry(*_read(path, shown_limit), shown_limit)


def assemble(scope, chain):
    """``get_instructions`` result from the root-to-directory chain ``[(relative path, read(shown_limit))]``.

    ``read(limit)`` returns the (sha256, size, head) triple. The shown text is bounded per file and in
    total; the digest always covers the whole files.
    """
    values, shown_total = [], 0
    for relative, read in chain:
        limit = min(FILE_LIMIT, TOTAL_LIMIT - shown_total)
        entry = rules_entry(*read(limit), limit)
        shown_total += len(entry["content"].encode("utf-8"))
        values.append({"path": relative, **entry})
    value = {"scope": scope, "instructions": values, "digest": instruction_digest(values)}
    if any(item.get("truncated") for item in values):
        value["note"] = TRUNCATION_NOTE
    return value


def get_local_instructions(workspace, path="."):
    target = workspace._path(path)
    directory = target if target.is_dir() else target.parent
    relative = directory.relative_to(workspace.root)
    directories = [workspace.root]
    for part in relative.parts:
        directories.append(directories[-1] / part)
    chain = []
    for directory in directories:
        candidate = directory / RULES_FILE
        if not candidate.exists():
            continue
        canonical = workspace._path(str(candidate.relative_to(workspace.root)))
        if canonical != candidate.absolute():
            raise ToolFailure(ErrorCode.PERMISSION_DENIED, "repository instructions cannot use symlinks")
        chain.append((str(candidate.relative_to(workspace.root)), lambda limit, file=candidate: _read(file, limit)))
    return assemble(str(directory.relative_to(workspace.root)), chain)


def rules_summary(workspace, *, enabled=True):
    """Metadata of the root project rules file for /status, json output and stderr (never its text).

    ``None`` when there is no root ``AGENTS.md`` (and the rules are on), so outputs stay unchanged.
    """
    from baseagent.backends import LocalBackend
    backend = getattr(workspace, "backend", None)
    if backend is not None and not isinstance(backend, LocalBackend) and callable(getattr(backend, "rules_files", None)):
        return _backend_rules_summary(workspace, backend, enabled)
    root = workspace.root
    candidate = root / RULES_FILE
    claude = (root / "CLAUDE.md").is_file()
    if not candidate.is_file():
        if not enabled:
            return {"enabled": False, "loaded": False, "path": None, "claude_md_ignored": claude}
        return None if not claude else {"enabled": True, "loaded": False, "path": None, "claude_md_ignored": True}
    try:
        entry = read_rules_file(candidate)
    except (OSError, UnicodeDecodeError) as exc:
        return {"enabled": enabled, "loaded": False, "path": RULES_FILE, "error": type(exc).__name__,
                "claude_md_ignored": claude}
    size = entry.get("size_bytes", len(entry["content"].encode("utf-8")))
    return {"enabled": enabled, "loaded": enabled, "path": RULES_FILE, "size_bytes": size,
            "shown_bytes": entry.get("shown_bytes", size) if enabled else 0, "truncated": bool(entry.get("truncated")),
            "sha256": entry["sha256"], "claude_md_ignored": claude}


def _backend_rules_summary(workspace, backend, enabled):
    """``rules_summary`` read through a container backend: the host never opens workspace paths itself.

    A container can turn ``AGENTS.md`` into a link to a host file; existence and type are decided inside
    the container (links are not followed out of the workspace) and size and hash come from
    ``backend.get_instructions``, so such a link is reported as unreadable instead of describing the
    host file.
    """
    try:
        kinds = backend.rules_files()
    except (OSError, ToolFailure, RuntimeError) as exc:     # DockerError: the container is gone
        return {"enabled": enabled, "loaded": False, "path": RULES_FILE, "error": type(exc).__name__,
                "claude_md_ignored": False}
    claude = kinds.get("CLAUDE.md") == "ok"
    state = kinds.get(RULES_FILE)
    if state in ("missing", "dir", "notfile"):
        if not enabled:
            return {"enabled": False, "loaded": False, "path": None, "claude_md_ignored": claude}
        return None if not claude else {"enabled": True, "loaded": False, "path": None, "claude_md_ignored": True}
    error = "symlink" if state in ("escape", "link") else None if state == "ok" else str(state)
    entry = None
    if error is None:
        try:
            entry = next(item for item in backend.get_instructions(".")["instructions"] if item["path"] == RULES_FILE)
        except (OSError, UnicodeDecodeError, ToolFailure, RuntimeError, StopIteration) as exc:
            error = type(exc).__name__
    if error is not None:
        return {"enabled": enabled, "loaded": False, "path": RULES_FILE, "error": error, "claude_md_ignored": claude}
    size = entry.get("size_bytes", len(entry["content"].encode("utf-8")))
    return {"enabled": enabled, "loaded": enabled, "path": RULES_FILE, "size_bytes": size,
            "shown_bytes": entry.get("shown_bytes", size) if enabled else 0, "truncated": bool(entry.get("truncated")),
            "sha256": entry["sha256"], "claude_md_ignored": claude}


def describe_rules(summary):
    """The /status line (also the stderr note when the rules file is cut)."""
    claude = "; CLAUDE.md is present but not read (only AGENTS.md is)" if summary and summary.get("claude_md_ignored") else ""
    if summary is None or summary.get("path") is None:
        state = "off (--no-project-rules)" if summary and not summary["enabled"] else "none (no AGENTS.md in the workspace root)"
        return f"Project rules: {state}{claude}"
    if summary.get("error"):
        return f"Project rules: AGENTS.md could not be read ({summary['error']}){claude}"
    if not summary["enabled"]:
        return (f"Project rules: off (--no-project-rules); AGENTS.md ({summary['size_bytes']} bytes) is not shown to "
                f"the model{claude}")
    if summary["truncated"]:
        return (f"Project rules: AGENTS.md loaded, truncated: the model sees the first {summary['shown_bytes']} of "
                f"{summary['size_bytes']} bytes (limit {FILE_LIMIT} bytes per file, {TOTAL_LIMIT} in total){claude}")
    return f"Project rules: AGENTS.md loaded ({summary['size_bytes']} bytes){claude}"


def get_instructions(workspace, path="."):
    return workspace.get_instructions(path)
