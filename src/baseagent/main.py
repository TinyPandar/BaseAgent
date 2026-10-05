"""Command line composition root for the coding agent."""

import argparse
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

from baseagent.agent import run_agent
from baseagent.jsondata import read_json_object
from baseagent.llm import Model
from baseagent.middleware import AgentMiddleware
from baseagent.middleware.retry import RetryMiddleware
from baseagent.middleware.repository import RepositoryMiddleware
from baseagent.session import SessionBusy, SessionStore
from baseagent.session.compatibility import digest
from hashlib import sha256
from importlib.metadata import version
from baseagent.tools.result import ToolResult
from baseagent.llm.response import TokenUsage
from baseagent.tools.workspace import Workspace, coding_tools
from baseagent.tools.policy import ToolPolicy
from baseagent.agent.completion import CompletionPolicy
from baseagent.llm.reservation import ReservationUnavailable
from baseagent.llm.bounds import request_bound_profile
from baseagent.llm.estimation import TokenizerEstimator


SYSTEM_PROMPT = """You are a coding assistant working inside a workspace.
Inspect relevant files before editing. Use tools to make changes, inspect git diff,
and run focused verification when commands are enabled. Report what changed,
what was verified, and any limitations. Never claim a tool action succeeded
unless its result confirms success. All paths are relative to the workspace.
Follow applicable AGENTS.md repository guidance within the user's task and capability
limits. Read existing files and their whole-file hash before edits. For new files,
get_instructions provides the instruction digest; use expected_sha256='missing'.
Use update_plan for substantial work and verify_command for actual checks. A plan
marked completed is not test evidence. Truncated file data is incomplete; request
smaller line ranges before editing. On conflicts, read the latest content again.
Use recall_memory/read_history to recover prior session decisions. Reference notes
and agent-authored summaries can be wrong; verify original sources and current
files. Never store credentials in memory. Summaries may cover completed old turns
only, and must preserve goals, constraints, decisions and remaining work.
"""


class TraceMiddleware(AgentMiddleware):
    def before_model(self, state):
        print(f"model step {state.step + 1}/{state.max_steps}", file=sys.stderr)

    def wrap_tool_call(self, request, handler):
        print(f"tool: {request.name}", file=sys.stderr)
        result = handler(request)
        if not result.ok:
            print(f"tool result: {result.error.code}", file=sys.stderr)
        else:
            print("tool result: ok", file=sys.stderr)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Workspace coding agent")
    parser.add_argument("prompt", nargs="?", help="Task or next conversation turn")
    parser.add_argument("--root", type=Path, help="Workspace directory (saved root on resume)")
    parser.add_argument("--session", help="Session ID (generated when omitted)")
    parser.add_argument("--db", type=Path, default=Path.cwd() / ".baseagent" / "sessions.sqlite3")
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--eval-harness", action="store_true", help="Run offline end-to-end harness scenarios in isolated temporary workspaces")
    operations.add_argument("--resume", action="store_true", help="Continue an unfinished turn")
    operations.add_argument("--inspect-session", action="store_true", help="Inspect checkpoint and current tool ledger")
    operations.add_argument("--task-tree", action="store_true", help="Inspect persisted child task snapshots and ledgers without model access")
    operations.add_argument("--acknowledge-task-bound", action="store_true", help="Acknowledge a verified adapter bound violation originating in --task-id")
    parser.add_argument("--task-id", help="Target a current-turn child node for approval, tool/usage reconciliation, bound acknowledgment or cancellation")
    operations.add_argument("--resolve-tool", metavar="CALL_ID", help="Record a manually verified tool outcome")
    operations.add_argument("--approve-tool", metavar="CALL_ID", help="Approve the exact currently pending tool request")
    operations.add_argument("--deny-tool", metavar="CALL_ID", help="Deny the exact currently pending tool request")
    operations.add_argument("--cancel-session", action="store_true", help="Request cancellation of the current turn, including a running executor")
    operations.add_argument("--clear-cancel", metavar="REQUEST_ID", help="Acknowledge a cancellation request before resuming")
    operations.add_argument("--events", action="store_true", help="Query ordered metadata events without model access")
    operations.add_argument("--follow-events", type=float, metavar="SECONDS", help="Stream event pages as flushed JSON lines for up to 60 seconds")
    operations.add_argument("--prune-events", type=int, metavar="THROUGH_SEQUENCE", help="Preview removal of an event prefix while keeping recent events")
    operations.add_argument("--shared-memory", action="store_true", help="Inspect published references for the session workspace without model access")
    operations.add_argument("--resolve-usage", type=Path, metavar="JSON_FILE", help="Record verified aggregate usage for unknown model attempts")
    operations.add_argument("--list-sessions", action="store_true", help="List session summaries")
    operations.add_argument("--export-session", type=Path, metavar="JSON_FILE", help="Archive a consistent session snapshot")
    operations.add_argument("--backup-db", type=Path, metavar="SQLITE_FILE", help="Create a consistent, non-overwriting SQLite backup")
    operations.add_argument("--delete-session", action="store_true", help="Delete the named completed session")
    operations.add_argument("--cleanup-days", type=float, help="Preview completed sessions older than this age")
    parser.add_argument("--apply-cleanup", action="store_true", help="Apply --cleanup-days instead of previewing")
    parser.add_argument("--discard-unfinished", action="store_true", help="Explicitly discard an unfinished session with --delete-session")
    parser.add_argument("--after-session", help="Session ID cursor for listing/cleanup")
    parser.add_argument("--session-limit", type=int, default=100, help="Session page size (1-1000)")
    parser.add_argument("--after-event", type=int, default=0, help="Event sequence cursor")
    parser.add_argument("--event-limit", type=int, default=100, help="Event page size (1-1000)")
    parser.add_argument("--event-page", action="store_true", help="Include retention/cursor metadata with --events")
    parser.add_argument("--keep-events", type=int, default=100, help="Minimum recent events to keep with --prune-events")
    parser.add_argument("--event-prune-digest", help="Apply the exact reviewed event retention preview digest")
    parser.add_argument("--memory-query", default="", help="Search text with --shared-memory")
    parser.add_argument("--after-memory-key", default="", help="Shared memory key cursor")
    parser.add_argument("--memory-limit", type=int, default=10, help="Shared memory page size (1-20)")
    parser.add_argument("--eval-report", type=Path, help="Save an atomic non-overwriting JSON evaluation report")
    parser.add_argument("--eval-case", action="append", choices=["coding", "approval", "recovery", "cancellation", "memory", "delegation"], help="Select an offline scenario (repeat for multiple cases)")
    parser.add_argument("--result-file", type=Path, help="Verified ToolResult JSON for --resolve-tool")
    parser.add_argument("--approval-digest", help="Reviewed request digest from --inspect-session")
    parser.add_argument("--tool-policy", type=Path, help="JSON tool/parameter policy with allow/deny/ask actions")
    parser.add_argument("--subtasks-config", type=Path, help="Explicitly enable named sequential subtasks from a JSON configuration; requires durable sessions")
    parser.add_argument("--request-bound-profile", choices=["deepseek-context-v1"], help="Explicit conservative whole-context bound for official DeepSeek Flash/Pro; requires --preauthorize-model")
    parser.add_argument("--max-completion-tokens", type=int, default=4096, help="Output cap for strict or estimated reservations (default: 4096)")
    parser.add_argument("--completion-policy", type=Path, help="JSON requirements: require_plan, exact command checks, and artifacts")
    parser.add_argument("--accept-config-changes", action="store_true", help="Explicitly accept changed runtime/tools for an unfinished turn")
    parser.add_argument("--accept-workspace-changes", action="store_true", help="Acknowledge changed observed files/guidance; stale write hashes remain rejected")
    parser.add_argument("--model", default=None, help="Model name (default: DEEPSEEK_MODEL)")
    parser.add_argument("--max-steps", type=int, help="Logical rounds (new turn: 8; resume: preserve)")
    parser.add_argument("--max-model-calls", type=int, default=None, help="Actual model call attempts, including middleware retries (defaults to max-steps)")
    parser.add_argument("--max-tool-calls", type=int, help="Actual tool attempts (new turn: 32; resume: preserve)")
    parser.add_argument("--max-context-bytes", type=int, help="Serialized model input UTF-8 bytes (default 96000; resume: preserve)")
    parser.add_argument("--max-tool-context-bytes", type=int, help="Per-tool data excerpt limit (default 4000; resume: preserve)")
    parser.add_argument("--max-total-tokens", type=int, help="Reported token budget per turn; unknown usage blocks execution")
    parser.add_argument("--estimate-model", action="store_const", const=True, default=None, help="Estimate request tokens locally; actual usage can exceed budget")
    parser.add_argument("--tokenizer-file", help="Local tokenizer.json for estimated budgets; never executes remote tokenizer code")
    parser.add_argument("--estimate-margin-percent", type=int, default=20)
    parser.add_argument("--estimate-fixed-margin", type=int, default=256)
    parser.add_argument("--preauthorize-model", action="store_const", const=True, default=None, help="Require trusted adapter request reservations; default CLI adapter has no prompt counter and refuses before dispatch")
    parser.add_argument("--accept-request-bound-violation", action="store_true", help="Acknowledge a verified request bound violation after repairing the adapter counter; usage/budgets remain")
    parser.add_argument("--max-duration-seconds", type=float, help="Wall duration from original turn start, including time between resumes")
    parser.add_argument("--model-retries", type=int, default=0, help="Retries after transient model errors (0-4; default disabled)")
    parser.add_argument("--tool-retries", type=int, default=0, help="Retries of explicitly safe, retryable tool failures (0-4)")
    parser.add_argument("--allow-write", action="store_true", help="Allow file writes")
    parser.add_argument("--allow-command", action="store_true", help="Allow subprocess commands")
    parser.add_argument("--allow-memory-publish", action="store_true", help="Allow publishing/withdrawing workspace-shared reference notes")
    parser.add_argument("--trace", action="store_true", help="Show model steps and tool names on stderr")
    args = parser.parse_args()
    if (args.max_steps is not None and args.max_steps < 1) or (args.max_model_calls is not None and args.max_model_calls < 1) or (args.max_tool_calls is not None and args.max_tool_calls < 0):
        parser.error("step/model limits must be positive and tool limit nonnegative")
    session_operation = args.resume or args.inspect_session or args.task_tree or args.acknowledge_task_bound or args.resolve_tool is not None or args.approve_tool is not None or args.deny_tool is not None or args.cancel_session or args.clear_cancel is not None or args.events or args.follow_events is not None or args.prune_events is not None or args.shared_memory or args.resolve_usage is not None or args.export_session is not None or args.delete_session
    database_operation = args.list_sessions or args.backup_db is not None or args.cleanup_days is not None
    operation = session_operation or database_operation or args.eval_harness
    if (args.eval_report or args.eval_case) and not args.eval_harness:
        parser.error("evaluation report/cases require --eval-harness")
    if args.eval_harness and (args.prompt is not None or args.session or args.root):
        parser.error("evaluation uses isolated fixtures; omit prompt, session and root")
    if session_operation and (not args.session or args.prompt is not None):
        parser.error("resume/inspect/resolve require --session and no prompt")
    if database_operation and (args.session is not None or args.prompt is not None):
        parser.error("list/backup/cleanup operate on --db without --session or a prompt")
    if args.discard_unfinished and not args.delete_session:
        parser.error("--discard-unfinished requires --delete-session")
    if args.apply_cleanup and args.cleanup_days is None:
        parser.error("--apply-cleanup requires --cleanup-days")
    if args.cleanup_days is not None and (not math.isfinite(args.cleanup_days) or args.cleanup_days <= 0):
        parser.error("cleanup age must be positive and finite")
    if bool(args.resolve_tool) != bool(args.result_file):
        parser.error("--resolve-tool requires --result-file, and vice versa")
    if bool(args.approve_tool or args.deny_tool) != bool(args.approval_digest):
        parser.error("approve/deny requires --approval-digest, and vice versa")
    if args.task_id is not None and (not args.task_id.strip() or len(args.task_id) > 128):
        parser.error("--task-id must be 1-128 nonblank characters")
    if args.task_id and not (args.approve_tool or args.deny_tool or args.resolve_tool or args.resolve_usage or args.acknowledge_task_bound or args.cancel_session or args.clear_cancel):
        parser.error("--task-id requires approval, tool/usage reconciliation, bound acknowledgment or cancellation")
    if args.acknowledge_task_bound and not args.task_id:
        parser.error("--acknowledge-task-bound requires --task-id")
    if args.event_page and not args.events:
        parser.error("--event-page requires --events")
    if args.event_prune_digest and args.prune_events is None:
        parser.error("--event-prune-digest requires --prune-events")
    if not operation and args.prompt is None:
        parser.error("supply a prompt or a session operation")
    for value in (args.max_context_bytes, args.max_tool_context_bytes):
        if value is not None and value < 256:
            parser.error("context byte limits must be >= 256")
    if args.max_total_tokens is not None and args.max_total_tokens < 1:
        parser.error("token budget must be positive")
    if args.max_duration_seconds is not None and (not math.isfinite(args.max_duration_seconds) or args.max_duration_seconds <= 0):
        parser.error("duration budget must be positive and finite")
    if not 0 <= args.model_retries <= 4 or not 0 <= args.tool_retries <= 4:
        parser.error("retry counts must be 0-4")

    # Defer provider configuration: inspection, reconciliation, and cached completion
    # need no model key and must not make an API call.
    class LazyModel:
        adapter = None

        def bound(self):
            return request_bound_profile(args.request_bound_profile, args.model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat"), os.getenv("DEEPSEEK_BASE_URL"), args.max_completion_tokens)

        def estimate_request(self, messages, tools):
            return estimator.reserve_request(messages, tools, args.max_completion_tokens)

        def reserve_request(self, messages, tools):
            if args.request_bound_profile:
                return self.bound().reserve_request(messages, tools)
            raise ReservationUnavailable("CLI adapter has no trusted prompt token counter; configure Model(token_counter=...) through the library before using preauthorization")

        def complete_reserved(self, messages, tools, **kwargs):
            self.initialize()
            return self.adapter.complete_reserved(messages, tools, **kwargs)

        def complete(self, messages, tools):
            return self.complete_with_timeout(messages, tools)

        def complete_with_timeout(self, messages, tools, *, timeout=None):
            self.initialize()
            if timeout is not None:
                return self.adapter.complete_with_timeout(messages, tools, timeout=timeout)
            return self.adapter.complete(messages, tools)

        def initialize(self):
            if self.adapter is None:
                load_dotenv()
                key, url = os.getenv("DEEPSEEK_API_KEY"), os.getenv("DEEPSEEK_BASE_URL")
                if not key or not url:
                    raise ValueError("DEEPSEEK_API_KEY and DEEPSEEK_BASE_URL are required in local .env")
                self.adapter = Model(model_name=args.model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat"), api_key=key, base_url=url,
                                     **({"request_bound": self.bound()} if args.request_bound_profile else {}),
                                     **({"token_estimator": estimator, "max_completion_tokens": args.max_completion_tokens} if estimator else {}))

    try:
        if args.eval_harness:
            from baseagent.evaluation import evaluate
            report = evaluate(cases=args.eval_case, report_path=args.eval_report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["passed"] else 1
        store = SessionStore(args.db)
        if args.list_sessions:
            print(json.dumps(store.list_sessions(after_id=args.after_session, limit=args.session_limit), ensure_ascii=False, indent=2))
            return 0
        if args.backup_db:
            print(store.backup(args.backup_db))
            return 0
        if args.cleanup_days is not None:
            print(json.dumps(store.cleanup(args.cleanup_days * 86400, dry_run=not args.apply_cleanup, after_id=args.after_session, limit=args.session_limit), ensure_ascii=False, indent=2))
            return 0
        existing = store.load(args.session) if args.session else None
        if session_operation and existing is None:
            parser.error("session does not exist")
        if args.export_session:
            print(store.export_session(args.session, args.export_session))
            return 0
        if args.delete_session:
            store.delete_session(args.session, discard_unfinished=args.discard_unfinished)
            print(f"Deleted session: {args.session}")
            return 0
        if args.events:
            values = store.event_page(args.session, after=args.after_event, limit=args.event_limit) if args.event_page else store.events(args.session, after=args.after_event, limit=args.event_limit)
            print(json.dumps(values, ensure_ascii=False, indent=2))
            return 0
        if args.follow_events is not None:
            for page in store.follow_events(args.session, after=args.after_event, limit=args.event_limit, duration=args.follow_events):
                print(json.dumps(page, ensure_ascii=False), flush=True)
            return 0
        if args.prune_events is not None:
            print(json.dumps(store.prune_events(args.session, through=args.prune_events, keep_latest=args.keep_events,
                                               expected_digest=args.event_prune_digest), ensure_ascii=False, indent=2))
            return 0
        if args.shared_memory:
            if not existing.workspace_root:
                raise ValueError("session has no workspace binding")
            if args.root and args.root.resolve() != Path(existing.workspace_root).resolve():
                raise ValueError("session belongs to a different workspace")
            print(json.dumps(store.search_shared_memory(existing.workspace_root, query=args.memory_query,
                                                       after_key=args.after_memory_key, limit=args.memory_limit), ensure_ascii=False, indent=2))
            return 0
        if args.cancel_session:
            print("Cancellation request: " + store.request_cancellation(args.session, task_id=args.task_id))
            return 0
        if args.clear_cancel:
            store.clear_cancellation(args.session, args.clear_cancel, task_id=args.task_id)
            print("Cancellation acknowledged. Inspect the ledger before resuming.")
            return 0
        if args.approve_tool or args.deny_tool:
            if args.task_id:
                store.decide_task_tool(args.session, args.task_id, args.approve_tool or args.deny_tool,
                                       "allow" if args.approve_tool else "deny", args.approval_digest)
            else:
                store.decide_tool(args.session, args.approve_tool or args.deny_tool,
                                  "allow" if args.approve_tool else "deny", args.approval_digest)
            print("Recorded tool decision. Resume with the same policy and capability settings.")
            return 0
        if args.resolve_usage:
            values = read_json_object(args.resolve_usage, max_bytes=4096, label="usage reconciliation")
            if not isinstance(values, dict) or "unknown_calls" not in values:
                raise ValueError("usage reconciliation requires unknown_calls and token counts")
            unknown_calls = values.pop("unknown_calls")
            if args.task_id:
                store.resolve_task_usage(args.session, args.task_id, TokenUsage.from_dict(values), unknown_calls)
            else:
                store.resolve_usage(args.session, TokenUsage.from_dict(values), unknown_calls)
            print("Recorded verified node model usage." if args.task_id else "Recorded verified model usage. Resume the session to continue.")
            return 0
        if args.acknowledge_task_bound:
            store.acknowledge_task_bound(args.session, args.task_id)
            print("Node bound violation acknowledged. Charged usage and limits are preserved.")
            return 0
        if args.task_tree:
            print(json.dumps(store.task_tree(args.session), ensure_ascii=False, indent=2))
            return 0
        if args.inspect_session:
            existing, ledger = store.inspect_session(args.session)
            print(json.dumps({"session_id": args.session, "status": existing.status, "workspace_root": existing.workspace_root,
                              "turn_id": existing.turn_id, "steps": existing.step, "model_calls": existing.model_calls,
                              "tool_calls": existing.tool_calls, "error": existing.error, "metadata": existing.metadata,
                              "max_context_bytes": existing.max_context_bytes, "max_tool_context_bytes": existing.max_tool_context_bytes,
                              "usage": {"prompt_tokens": existing.prompt_tokens, "completion_tokens": existing.completion_tokens,
                                        "total_tokens": existing.total_tokens, "unknown_calls": existing.unknown_usage_calls},
                              "max_total_tokens": existing.max_total_tokens, "max_duration_seconds": existing.max_duration_seconds,
                              "estimate_model": existing.estimate_model, "preauthorize_model": existing.preauthorize_model, "model_reservations": existing.model_reservations,
                              "turn_started_at": existing.turn_started_at,
                              "plan": existing.plan, "plan_revision": existing.plan_revision, "verifications": existing.verifications,
                              "memory_revision": existing.memory_revision, "memory_notes": existing.memory_notes,
                              "history_summary": existing.history_summary,
                              "cancellation_request": store.cancellation(args.session, existing.turn_id),
                              "ledger": ledger}, ensure_ascii=False, indent=2))
            return 0
        if args.resolve_tool:
            result = ToolResult.from_dict(read_json_object(args.result_file, max_bytes=256_000, label="tool reconciliation"))
            if args.task_id:
                store.resolve_task_call(args.session, args.task_id, args.resolve_tool, result)
            else:
                store.resolve_call(args.session, args.resolve_tool, result)
            if args.task_id:
                print(f"Recorded verified node outcome for {args.resolve_tool}.")
            else:
                print(f"Recorded verified outcome for {args.resolve_tool}. Resume with --session {args.session} --resume.")
            return 0
        root = args.root or (Path(existing.workspace_root) if existing and existing.workspace_root else Path.cwd())
        protected = [store.path, Path(str(store.path) + "-wal"), Path(str(store.path) + "-shm"), Path(str(store.path) + "-journal"), store.lock_dir]
        workspace = Workspace(root, allow_write=args.allow_write, allow_command=args.allow_command, allow_memory_publish=args.allow_memory_publish, protected_paths=protected)
        policy = ToolPolicy()
        if args.tool_policy:
            policy = ToolPolicy.from_dict(read_json_object(args.tool_policy, max_bytes=20_000, label="tool policy"), workspace=workspace)
        completion = None
        subtasks = None
        if args.subtasks_config:
            from baseagent.agent.subtasks import SubtaskRuntime
            configuration = read_json_object(args.subtasks_config, max_bytes=32000, label="subtask configuration")
            subtasks = SubtaskRuntime.from_dict(configuration, workspace=workspace)
        if args.completion_policy:
            completion = CompletionPolicy(workspace, read_json_object(args.completion_policy, max_bytes=20_000, label="completion policy"))
        load_dotenv()
        if args.request_bound_profile and not args.preauthorize_model:
            raise ValueError("request bound profile requires --preauthorize-model")
        if args.request_bound_profile and not 1 <= args.max_completion_tokens <= 393216:
            raise ValueError("request bound output cap must be 1-393216 tokens")
        estimating = args.estimate_model or bool(existing and existing.estimate_model)
        if estimating and (args.preauthorize_model or args.request_bound_profile):
            raise ValueError("estimated and strict budgets cannot be combined")
        cached = bool(existing and existing.status == "completed" and args.prompt is None)
        if (estimating and not args.tokenizer_file and not cached) or (args.tokenizer_file and not estimating):
            raise ValueError("estimated budgets require --tokenizer-file; tokenizer-file requires estimated mode")
        if estimating and not 1 <= args.max_completion_tokens <= 393216:
            raise ValueError("estimated output cap must be 1-393216 tokens")
        estimator = TokenizerEstimator(args.tokenizer_file, margin_percent=args.estimate_margin_percent, fixed_margin=args.estimate_fixed_margin) if estimating and args.tokenizer_file else None
        source_root = Path(__file__).parent
        source_hashes = {str(path.relative_to(source_root)): sha256(path.read_bytes()).hexdigest() for path in sorted(source_root.rglob("*.py"))}
        config = {"harness_source": digest(source_hashes), "openai_version": version("openai"), "jsonschema_version": version("jsonschema"),
                  "model": args.model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat"), "endpoint": os.getenv("DEEPSEEK_BASE_URL"),
                  "context_policy_version": 1}
        config["retries"] = {"model": args.model_retries, "tool": args.tool_retries, "delay": 0.25}
        if args.request_bound_profile:
            config["request_bound"] = request_bound_profile(args.request_bound_profile, config["model"], config["endpoint"], args.max_completion_tokens).contract()
        if estimator:
            config["token_estimate"] = {**estimator.contract(), "max_completion_tokens": args.max_completion_tokens, "tokenizers_version": version("tokenizers")}
        layers = [RepositoryMiddleware(workspace, accept_changes=args.accept_workspace_changes)]
        if args.trace:
            layers.append(TraceMiddleware())
        if args.model_retries or args.tool_retries:
            layers.append(RetryMiddleware(model_retries=args.model_retries, tool_retries=args.tool_retries))
        state = run_agent(
            LazyModel(), args.prompt, tools=coding_tools(workspace),
            middleware=layers,
            system_prompt=SYSTEM_PROMPT, max_steps=args.max_steps,
            max_model_calls=args.max_model_calls, max_tool_calls=args.max_tool_calls,
            store=store, session_id=args.session, workspace_root=workspace.root,
            max_context_bytes=args.max_context_bytes, max_tool_context_bytes=args.max_tool_context_bytes,
            runtime_config=config, accept_config_changes=args.accept_config_changes,
            max_total_tokens=args.max_total_tokens, max_duration_seconds=args.max_duration_seconds,
            tool_policy=policy,
            completion_policy=completion,
            estimate_model=args.estimate_model, preauthorize_model=args.preauthorize_model, accept_request_bound_violation=args.accept_request_bound_violation,
            subtasks=subtasks,
        )
    except KeyboardInterrupt:
        return 130
    except (ValueError, SessionBusy, OSError, sqlite3.Error) as exc:
        print(f"Session error: {exc}", file=sys.stderr)
        return 1
    print(f"Session: {state.session_id}", file=sys.stderr)
    print(f"Reported tokens: {state.total_tokens}; unknown usage calls: {state.unknown_usage_calls}", file=sys.stderr)
    if state.final_answer:
        print(state.final_answer)
    if state.error:
        print(f"Agent error: {state.error}")
    if state.status != "completed":
        print(f"Agent stopped: {state.status} (steps: {state.step}, model attempts: {state.model_calls}, tool attempts: {state.tool_calls})")
        if state.blocked_tool_calls:
            print("Inspect with --inspect-session; verify external effects before --resolve-tool.", file=sys.stderr)
        return 130 if state.status == "interrupted" else 1
    return 0
