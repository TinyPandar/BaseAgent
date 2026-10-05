"""Offline end-to-end regression scenarios using real harness execution layers."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time

from baseagent.agent import run_agent
from baseagent.agent.cancellation import CancellationToken
from baseagent.agent.completion import CompletionPolicy
from baseagent.agent.subtasks import SubtaskDefinition, SubtaskRuntime
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.middleware import AgentMiddleware
from baseagent.middleware.repository import RepositoryMiddleware
from baseagent.session import SessionStore
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.registry import ToolRegistry
from baseagent.tools.result import ToolResult
from baseagent.tools.workspace import Workspace, coding_tools


class EvaluationFailure(Exception):
    pass


def _check(evidence, name, condition):
    evidence[name] = bool(condition)
    if not condition:
        raise EvaluationFailure(name)


def _call(name, args, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedModel:
    """Synthetic usage is explicit; evaluation does not measure model quality."""
    def __init__(self, messages):
        self.messages = iter(messages)
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        response = next(self.messages)
        if callable(response):
            response = response(messages)
        return ModelResponse(response, TokenUsage(10, 5, 15))


def _coding(root, evidence):
    source = b"def add(a, b):\r\n    return a - b\r\n\r\nuntouched = 'sentinel'\r\n"
    (root / "sample.py").write_bytes(source)
    workspace = Workspace(root, allow_write=True, allow_command=True)
    store = SessionStore(root / ".baseagent/sessions.sqlite3")
    tools = coding_tools(workspace)
    argv = [sys.executable, "-B", "-c", "import sample; assert sample.add(2,3)==5; assert sample.untouched=='sentinel'"]
    completion = CompletionPolicy(workspace, {"require_plan": True, "checks": [{"name": "addition", "argv": argv, "paths": ["sample.py"]}], "artifacts": ["sample.py"]})
    def edit(messages):
        value = json.loads(messages[-1]["content"])["data"]
        return {"tool_calls": [_call("edit_file", {"path": "sample.py", "expected_sha256": value["sha256"], "expected_instructions": value["instruction_digest"], "edits": [{"old_text": "return a - b", "new_text": "return a + b"}]}, "edit")]}
    model = ScriptedModel([
        {"content": "premature completion"},
        {"tool_calls": [_call("read_file", {"path": "sample.py"}, "read")]}, edit,
        {"tool_calls": [_call("verify_command", {"argv": argv, "paths": ["sample.py"]}, "verify"),
                        _call("update_plan", {"steps": [{"id": "fix", "title": "fix and verify", "status": "completed"}], "expected_revision": 0}, "plan")]},
        {"content": "verified completion"},
    ])
    state = run_agent(model, "Fix addition and verify it", tools=tools, store=store, session_id="coding", workspace_root=root,
                      middleware=[RepositoryMiddleware(workspace)], completion_policy=completion)
    _check(evidence, "completed_after_real_check", state.status == "completed" and state.metadata.get("completion_report", {}).get("passed"))
    _check(evidence, "only_target_bytes_changed", (root / "sample.py").read_bytes() == source.replace(b"return a - b", b"return a + b"))
    _check(evidence, "premature_completion_rejected", any(item["type"] == "completion_rejected" for item in store.events("coding")))
    _check(evidence, "verification_exit_zero", state.verifications[-1]["exit_code"] == 0)
    cached = run_agent(ScriptedModel([]), tools=tools, store=store, session_id="coding", workspace_root=root, completion_policy=completion)
    _check(evidence, "cached_resume_does_not_dispatch", cached.tool_calls == state.tool_calls and cached.model_calls == state.model_calls)
    return {"model_calls": state.model_calls, "tool_calls": state.tool_calls, "source_sha256": sha256((root / "sample.py").read_bytes()).hexdigest()}


def _effect_tools(root):
    tools = ToolRegistry()
    def effect():
        with (root / "effects.txt").open("a", encoding="utf-8") as handle:
            handle.write("executed\n")
        return {"executed": True}
    tools.register("effect", "Controlled evaluation effect", {"type": "object"}, effect)
    return tools


def _approval(root, evidence):
    store, tools = SessionStore(root / "sessions.sqlite3"), _effect_tools(root)
    policy = ToolPolicy({"effect": "ask"})
    state = run_agent(ScriptedModel([{"tool_calls": [_call("effect", {}, "a")]}]), "effect", tools=tools, store=store, session_id="approval", tool_policy=policy)
    _check(evidence, "pending_before_approval", state.status == "awaiting_approval" and store.call(state, "a")["status"] == "pending")
    _check(evidence, "no_preapproval_side_effect", not (root / "effects.txt").exists())
    store.decide_tool("approval", "a", "allow", state.metadata["approval_request"]["request_digest"])
    state = run_agent(ScriptedModel([{"content": "done"}]), tools=tools, store=store, session_id="approval", tool_policy=policy)
    run_agent(ScriptedModel([]), tools=tools, store=store, session_id="approval", tool_policy=policy)
    _check(evidence, "approved_call_runs_exactly_once", (root / "effects.txt").read_text() == "executed\n" and state.tool_calls == 1)
    return {"model_calls": state.model_calls, "tool_calls": state.tool_calls}


def _recovery(root, evidence):
    class Interrupt(AgentMiddleware):
        def wrap_tool_call(self, request, handler):
            handler(request)
            raise KeyboardInterrupt()
    store, tools = SessionStore(root / "sessions.sqlite3"), _effect_tools(root)
    state = run_agent(ScriptedModel([{"tool_calls": [_call("effect", {}, "a")]}]), "effect", tools=tools, store=store, session_id="recovery", middleware=[Interrupt()])
    _check(evidence, "interrupted_after_effect", state.status == "interrupted" and (root / "effects.txt").read_text() == "executed\n")
    blocked = run_agent(ScriptedModel([]), tools=tools, store=store, session_id="recovery")
    _check(evidence, "uncertain_ledger_blocks_resume", blocked.status == "needs_recovery")
    record = store.call(state, "a")
    result = ToolResult.from_dict(json.loads(record["terminal_result_json"]))
    _check(evidence, "terminal_evidence_matches_fixture", result.data == {"executed": True} and result.ok)
    store.resolve_call("recovery", "a", result)
    state = run_agent(ScriptedModel([{"content": "reconciled"}]), tools=tools, store=store, session_id="recovery")
    _check(evidence, "reconcile_does_not_replay_effect", state.status == "completed" and state.tool_calls == 1 and (root / "effects.txt").read_text() == "executed\n")
    return {"model_calls": state.model_calls, "tool_calls": state.tool_calls}


def _cancellation(root, evidence):
    store = SessionStore(root / ".baseagent/sessions.sqlite3")
    tools = coding_tools(Workspace(root, allow_command=True))
    token = CancellationToken()
    timer = threading.Timer(0.5, token.cancel)
    argv = [sys.executable, "-c", "import time; print('started',flush=True); time.sleep(20)"]
    def launch(messages):
        timer.start()
        return {"tool_calls": [_call("verify_command", {"argv": argv, "paths": []}, "a")]}
    try:
        state = run_agent(ScriptedModel([launch]), "cancel controlled command", tools=tools, store=store, session_id="cancel", workspace_root=root, cancellation=token)
    finally:
        if timer.ident is not None:
            timer.join(timeout=2)
    _check(evidence, "run_cancelled", state.status == "cancelled")
    _check(evidence, "cancelled_check_not_passed", state.verifications[-1]["status"] == "cancelled")
    record = store.call(state, "a")
    _check(evidence, "cancel_result_durable", record["status"] == "completed" and json.loads(record["result_json"])["error"]["code"] == "cancelled")
    state = run_agent(ScriptedModel([{"content": "cancel acknowledged"}]), tools=tools, store=store, session_id="cancel", workspace_root=root)
    _check(evidence, "cancelled_command_not_replayed", state.status == "completed" and state.tool_calls == 1)
    return {"model_calls": state.model_calls, "tool_calls": state.tool_calls}


def _memory(root, evidence):
    store = SessionStore(root / ".baseagent/sessions.sqlite3")
    tools = coding_tools(Workspace(root, allow_memory_publish=True))
    state = run_agent(ScriptedModel([{"tool_calls": [
        _call("remember", {"key": "language", "content": "Use Python", "source_messages": [1], "expected_revision": 0}, "note"),
        _call("publish_memory", {"key": "language", "expected_revision": 0}, "publish")]}, {"content": "published"}]),
        "This workspace uses Python", tools=tools, store=store, session_id="publisher", workspace_root=root)
    _check(evidence, "explicit_publication_completed", state.status == "completed")
    reader = run_agent(ScriptedModel([{"tool_calls": [_call("search_shared_memory", {"query": "python"}, "read")]}, {"content": "read reference"}]),
                       "Read workspace reference", tools=tools, store=store, session_id="reader", workspace_root=root)
    data = json.loads(reader.messages[-2]["content"])["data"]
    _check(evidence, "reader_sees_source_status", data["notes"][0]["source_status"] == "source_matches")
    _check(evidence, "private_memory_not_implicitly_copied", reader.memory_notes == {})
    store.delete_session("publisher")
    _check(evidence, "deleted_source_is_visible", store.search_shared_memory(root)["notes"][0]["source_status"] == "source_missing")
    return {"model_calls": reader.model_calls, "tool_calls": reader.tool_calls}


def _delegation(root, evidence):
    """Nested read/effect workflow with node approval, recovery and root gates."""
    content = "delegation fixture\n"
    (root / "input.txt").write_bytes(content.encode("utf-8"))
    effect_path = root / "effects.txt"
    workspace = Workspace(root, allow_command=True)
    tools = coding_tools(workspace)
    def effect():
        with effect_path.open("a", encoding="utf-8") as output:
            output.write("executed\n")
        return {"executed": True}
    tools.register("effect", "Controlled delegation evaluation effect", {"type": "object"}, effect)
    class InterruptEffect(AgentMiddleware):
        def wrap_tool_call(self, request, handler):
            result = handler(request)
            if request.name == "effect":
                raise KeyboardInterrupt()
            return result
    def delegate(name, identifier):
        return _call("run_subtask", {"name": name, "prompt": "Read the fixture and perform the authorized task"}, identifier)
    worker = ScriptedModel([
        {"tool_calls": [_call("read_file", {"path": "input.txt"}, "read")]},
        {"tool_calls": [_call("effect", {}, "effect")]}, {"content": "worker complete"},
    ])
    middle = ScriptedModel([{"tool_calls": [delegate("worker", "delegate")]}, {"content": "middle complete"}])
    runtime = SubtaskRuntime([
        SubtaskDefinition("reader", ("read_file", "effect", "run_subtask"), model=middle),
        SubtaskDefinition("worker", ("read_file", "effect"), model=worker,
                          policy=ToolPolicy({"effect": "ask"}), middleware=(InterruptEffect(),)),
    ])
    argv = [sys.executable, "-B", "-c", "from pathlib import Path; assert Path('effects.txt').read_text()=='executed\\n'; assert Path('input.txt').read_text()=='delegation fixture\\n'"]
    completion = CompletionPolicy(workspace, {"require_plan": False, "checks": [{"name": "delegated_effect", "argv": argv, "paths": ["effects.txt", "input.txt"]}], "artifacts": ["effects.txt"]})
    database = root / "sessions.sqlite3"
    store = SessionStore(database)
    options = dict(tools=tools, session_id="delegation", workspace_root=root, subtasks=runtime, completion_policy=completion)
    state = run_agent(ScriptedModel([{"tool_calls": [delegate("reader", "delegate")]}]), "Complete the delegated task and verify its artifact",
                      store=store, max_model_calls=4, **options)
    nodes = store.task_tree("delegation")["nodes"]
    leaf = next(node for node in nodes if node["name"] == "worker")
    leaf_id = leaf["task_id"]
    _check(evidence, "nested_approval_pauses_root_before_effect", state.status == "awaiting_approval" and not effect_path.exists() and len(nodes) == 2)
    read = next(record for record in leaf["tool_calls"] if record["call_id"] == "read")
    _check(evidence, "child_read_matches_actual_file", json.loads(read["result_json"])["data"]["content"] == content)
    _check(evidence, "repeated_call_ids_are_node_isolated", len({state.turn_id, *(node["state"]["turn_id"] for node in nodes)}) == 3 and all(node["spawn_call_id"] == "delegate" for node in nodes))
    # Reopen the file-backed store at each recovery boundary. This scenario is
    # same-process orchestration; separate tests cover actual hard process exit.
    store = SessionStore(database)
    store.decide_task_tool("delegation", leaf_id, "effect", "allow", leaf["state"]["metadata"]["approval_request"]["request_digest"])
    state = run_agent(ScriptedModel([]), store=store, **options)
    _check(evidence, "node_effect_interrupt_retains_parent_calls", state.status == "interrupted" and effect_path.read_text() == "executed\n" and store.call(state, "delegate")["status"] == "running")
    store = SessionStore(database)
    blocked_model = ScriptedModel([])
    state = run_agent(blocked_model, store=store, **options)
    _check(evidence, "uncertain_child_blocks_all_new_dispatch", state.status == "needs_recovery" and blocked_model.calls == 0 and (middle.calls, worker.calls) == (1, 2))
    leaf = next(node for node in store.task_tree("delegation")["nodes"] if node["task_id"] == leaf_id)
    record = next(record for record in leaf["tool_calls"] if record["call_id"] == "effect")
    result = ToolResult.from_dict(json.loads(record["terminal_result_json"]))
    _check(evidence, "child_terminal_evidence_independently_verified", result.ok and result.data == {"executed": True} and effect_path.read_text() == "executed\n")
    store.resolve_task_call("delegation", leaf_id, "effect", result)
    state = run_agent(ScriptedModel([]), store=SessionStore(database), **options)
    _check(evidence, "root_budget_blocks_descendant_model", state.status == "max_model_calls_exceeded" and state.model_calls == 4 and worker.calls == 2)
    started_at = state.turn_started_at
    root_model = ScriptedModel([
        {"content": "premature delegated success"},
        {"tool_calls": [_call("verify_command", {"argv": argv, "paths": ["effects.txt", "input.txt"]}, "verify")]},
        {"content": "verified delegated completion"},
    ])
    store = SessionStore(database)
    state = run_agent(root_model, store=store, max_model_calls=12, **options)
    nodes = store.task_tree("delegation")["nodes"]
    by_name = {node["name"]: node for node in nodes}
    _check(evidence, "extension_preserves_origin_and_counts", state.turn_started_at == started_at and (state.model_calls, state.tool_calls, state.total_tokens) == (9, 5, 135))
    _check(evidence, "direct_usage_sums_without_subtree_double_count", sum(item["metadata"]["direct_usage"]["total_tokens"] for item in [state.to_dict(), *(node["state"] for node in nodes)]) == state.total_tokens and [by_name[name]["state"]["total_tokens"] for name in ("reader", "worker")] == [75, 45])
    _check(evidence, "artifact_verified_by_root_completion_gate", state.status == "completed" and state.metadata["completion_report"]["passed"] and state.verifications[-1]["exit_code"] == 0 and state.unknown_usage_calls == 0)
    _check(evidence, "child_answer_cannot_replace_root_verification", any(value["type"] == "completion_rejected" for value in store.events("delegation")))
    replies = [message for message in state.messages if message["role"] == "tool" and message["tool_call_id"] == "delegate"]
    _check(evidence, "delegate_delivered_once_and_effect_never_replayed", len(replies) == 1 and store.call(state, "delegate")["attempts"] == 1 and effect_path.read_text() == "executed\n" and (middle.calls, worker.calls) == (2, 3))
    store.export_session("delegation", root / "export.json")
    exported = json.loads((root / "export.json").read_text(encoding="utf-8"))
    _check(evidence, "export_contains_complete_execution_tree", len(exported["task_nodes"]) == 2 and all(record["status"] == "completed" for record in exported["tool_calls"]))
    cached = run_agent(ScriptedModel([]), store=SessionStore(database), **options)
    _check(evidence, "completed_tree_cache_does_not_dispatch", cached.to_dict() == state.to_dict() and (middle.calls, worker.calls) == (2, 3))
    transient = run_agent(ScriptedModel([{"tool_calls": [delegate("reader", "delegate")]}, {"content": "done"}]), "Read with an ephemeral tree",
                          tools=tools, workspace_root=root, subtasks=SubtaskRuntime([
                              SubtaskDefinition("reader", ("read_file",), model=ScriptedModel([
                                  {"tool_calls": [_call("read_file", {"path": "input.txt"}, "read")]}, {"content": "read"}]))]))
    _check(evidence, "ephemeral_delegation_preserves_tree_and_budgets", transient.status == "completed" and transient.metadata["execution_storage"]["durable"] is False and len(transient.metadata["subtask_tree"]) == 1 and (transient.model_calls, transient.tool_calls, transient.total_tokens) == (4, 2, 60))
    return {"model_calls": state.model_calls, "tool_calls": state.tool_calls, "total_tokens": state.total_tokens, "nodes": len(nodes), "effect_sha256": sha256(effect_path.read_bytes()).hexdigest()}


SCENARIOS = {"coding": _coding, "approval": _approval, "recovery": _recovery, "cancellation": _cancellation, "memory": _memory, "delegation": _delegation}


def _write_report(report, target):
    target = Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError("evaluation report already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".evaluation-", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def evaluate(*, cases=None, report_path=None):
    selected = list(SCENARIOS) if cases is None else list(cases)
    if not selected or len(selected) != len(set(selected)) or any(name not in SCENARIOS for name in selected):
        raise ValueError("evaluation cases must be unique known scenario names")
    if report_path and (Path(report_path).exists() or Path(report_path).is_symlink()):
        raise FileExistsError("evaluation report already exists")
    source_root = Path(__file__).parent
    source = {str(path.relative_to(source_root)): sha256(path.read_bytes()).hexdigest() for path in sorted(source_root.rglob("*.py"))}
    report = {"format": "baseagent.evaluation", "version": 1, "model_kind": "scripted_offline", "usage_kind": "synthetic",
              "started_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
              "harness_source_digest": sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(), "cases": []}
    with tempfile.TemporaryDirectory(prefix="baseagent-evaluation-") as directory:
        for name in selected:
            root = Path(directory) / name
            root.mkdir()
            evidence = {}
            started = time.monotonic()
            try:
                metrics = SCENARIOS[name](root, evidence)
                result = {"name": name, "passed": True, "checks": evidence, "metrics": metrics}
            except Exception as exc:
                result = {"name": name, "passed": False, "checks": evidence, "error_type": type(exc).__name__}
                if isinstance(exc, EvaluationFailure):
                    result["failed_check"] = str(exc)
            result["duration_seconds"] = time.monotonic() - started
            report["cases"].append(result)
    report["passed"] = all(case["passed"] for case in report["cases"])
    report["passed_count"] = sum(case["passed"] for case in report["cases"])
    report["case_count"] = len(selected)
    if report_path:
        _write_report(report, report_path)
    return report
