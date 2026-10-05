"""Session plan and actual command verification records."""

from copy import deepcopy
import time
from uuid import uuid4

from .context import ToolContext
from .result import ErrorCode, ToolFailure, ToolResult


def _state(context):
    if not isinstance(context, ToolContext):
        raise ToolFailure(ErrorCode.EXECUTION_FAILED, "this tool requires an active agent session")
    return context.state


def update_plan(steps, expected_revision, *, context):
    state = _state(context)
    if state.plan_revision != expected_revision:
        raise ToolFailure(ErrorCode.CONFLICT, "plan revision changed; get_task_state before updating")
    if len({step["id"] for step in steps}) != len(steps) or sum(step["status"] == "in_progress" for step in steps) > 1:
        raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "step IDs must be unique; at most one step may be in progress")
    state.plan = deepcopy(steps)
    state.plan_revision += 1
    return {"revision": state.plan_revision, "steps": deepcopy(state.plan)}


def get_task_state(recent_checks=10, *, context):
    state = _state(context)
    checks = state.verifications[-recent_checks:] if recent_checks else []
    summaries = [{key: deepcopy(record.get(key)) for key in ("id", "status", "exit_code", "duration_seconds", "hashes_after")} for record in checks]
    return {"revision": state.plan_revision, "steps": deepcopy(state.plan), "verifications": summaries}


def invalidate_verifications(workspace, state):
    hashes = {}
    for record in state.verifications:
        if record["status"] != "passed":
            continue
        for path, expected in record["hashes_after"].items():
            try:
                if path not in hashes:
                    hashes[path] = workspace.file_hash(path)
                current = hashes[path]
            except (OSError, ToolFailure):
                current = "unavailable"
            if current != expected:
                record["status"] = "stale"
                break


def verify_command(workspace, argv, paths, timeout=20, *, context):
    state = _state(context)
    if not workspace.allow_command:
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "verification commands require --allow-command")
    workspace.validate_command(argv, timeout)
    hashes = {str(workspace._path(path).relative_to(workspace.root)): workspace.file_hash(path) for path in paths}
    for path, value in hashes.items():
        workspace._check_observation_capacity(path, context)
        workspace._observe({"path": path, "sha256": value, "instruction_digest": workspace.get_instructions(path)["digest"]}, context)
    record = {"id": uuid4().hex, "turn_id": state.turn_id, "status": "running", "argv": list(argv), "hashes_before": hashes,
              "hashes_after": {}, "started_at": time.time(), "exit_code": None}
    state.verifications.append(record)
    del state.verifications[:-200]
    context.checkpoint("verification_started", check_id=record["id"], tracked_files=len(hashes))
    started = time.monotonic()
    try:
        result = workspace.run_command(argv, timeout, context=context)
    except Exception:
        record.update(status="failed", duration_seconds=time.monotonic() - started)
        context.checkpoint("verification_completed", check_id=record["id"], status="failed", exit_code=None)
        raise
    except BaseException as exc:
        from baseagent.agent.cancellation import Cancelled
        record.update(status="cancelled" if isinstance(exc, Cancelled) else "interrupted", duration_seconds=time.monotonic() - started)
        context.checkpoint("verification_interrupted", check_id=record["id"])
        raise
    after = {}
    for path in hashes:
        try:
            after[path] = workspace.file_hash(path)
        except (OSError, ToolFailure):
            after[path] = "unavailable"
    record.update(hashes_after=after, duration_seconds=time.monotonic() - started,
                  exit_code=(result.data or {}).get("exit_code"), status="passed" if result.ok else "failed")
    if result.error and result.error.code == ErrorCode.CANCELLED:
        record["status"] = "cancelled"
    if result.ok and after != hashes:
        record["status"] = "stale"
        result = ToolResult.failure(ErrorCode.CONFLICT, "tracked files changed during verification; rerun the check", data=result.data)
    context.checkpoint("verification_completed", check_id=record["id"], status=record["status"], exit_code=record["exit_code"])
    return ToolResult(data={**(result.data or {}), "verification_id": record["id"], "verification_status": record["status"]}, error=result.error)
