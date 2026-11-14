"""Command line composition root for the coding agent."""

import argparse
import json
import math
from decimal import Decimal
import os
import sqlite3
import sys
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from baseagent.agent import run_agent
from baseagent.agent.completion import CompletionPolicy
from baseagent.agent.router import MAIN_AGENT_MODE, MAIN_AGENT_PROMPT, SINGLE_AGENT_MODE, is_main_agent_session
from baseagent.agent.workers import SessionTagMiddleware, SharedBudgetMiddleware, TurnBudget, WorkerManager
from baseagent.backends import LocalBackend
from baseagent.capabilities import SkillCatalog, SkillMiddleware, assemble_capabilities
from baseagent.config import load_harness_config
from baseagent.jsondata import read_json_object
from baseagent.llm import Model
from baseagent.llm.bounds import request_bound_profile
from baseagent.llm.estimation import TokenizerEstimator
from baseagent.llm.reservation import ReservationUnavailable
from baseagent.llm.response import TokenUsage
from baseagent.mcp.manager import McpStartupError
from baseagent.middleware import AgentMiddleware
from baseagent.middleware.execution_guidance import ExecutionGuidanceMiddleware
from baseagent.middleware.repository import RepositoryMiddleware
from baseagent.middleware.retry import RetryMiddleware
from baseagent.output import MachineOutput, result as machine_result
from baseagent.session import SessionBusy, SessionStore
from baseagent.session.compatibility import digest
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.result import ToolResult
from baseagent.tools.workspace import Workspace

SYSTEM_PROMPT = """You are a helpful assistant with optional coding tools inside a workspace.
Answer general questions directly. Only inspect files, plan coding work or create
documents when the user's request calls for it. Local file search is not web research.
If external research is needed, disclose that no web search tool is available.
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
Answer greetings, small talk and general questions that do not depend on earlier
work directly, without calling get_task_state, recall_memory or read_history.
Use those tools only when the user refers to earlier tasks, plans, memory or
conversation, or to recover prior session decisions. Reference notes
and agent-authored summaries can be wrong; verify original sources and current
files. Never store credentials in memory. Summaries may cover completed old turns
only, and must preserve goals, constraints, decisions and remaining work.
Oversized tool results may be replaced by a preview with a spill path. Open that
path with read_file and search it with grep when you need the full result;
read_spill remains available for bounded line slices.
"""


def model_configuration(model_name=None, *, generic_only=False):
    """Resolve a complete provider configuration without mixing credentials."""
    generic = generic_only or any(os.getenv("BASEAGENT_LLM_" + name) for name in
                                  ("BASE_URL", "API_KEY", "MODEL", "PROVIDER", "EXTRA_BODY"))
    prefix = "BASEAGENT_LLM_" if generic else "DEEPSEEK_"
    key, url = os.getenv(prefix + "API_KEY"), os.getenv(prefix + "BASE_URL")
    name = model_name or os.getenv(prefix + "MODEL") or (None if generic else "deepseek-chat")
    if not key or not url or not name:
        raise ValueError(prefix + "API_KEY, BASE_URL and MODEL configuration required")
    extra = os.getenv("BASEAGENT_LLM_EXTRA_BODY") if generic else None
    try:
        body = json.loads(extra, parse_constant=_invalid_json_constant) if extra else None
        if extra and not isinstance(body, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("BASEAGENT_LLM_EXTRA_BODY must be a JSON object") from None
    return {"model_name": name, "api_key": key, "base_url": url,
            **({"extra_body": body} if body is not None else {})}


def _invalid_json_constant(value):
    raise ValueError("nonfinite JSON value")


def initialize_model(model_name=None, *, generic_only=False, **kwargs):
    """Shared CLI/application/recorder OpenAI-compatible model composition."""
    return Model(**model_configuration(model_name, generic_only=generic_only), **kwargs)


def _trace_preview(value, limit):
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


DEFAULT_MODEL_RETRIES = 2
# --mode presets -> (--allow-write, --allow-command). read-only is the default without flags.
MODES = {"read-only": (False, False), "edit": (True, False), "full": (True, True)}
OUTPUT_FORMATS = ("text", "json", "jsonl")
FULL_MODE_WARNING = ("Warning: --mode full runs commands directly on this machine; LocalBackend is not a sandbox. "
                     "/undo restores files written by write_file/edit_file/apply_patch, not changes made by commands. "
                     "For isolation use DockerBackend (docs/docker_backend.md).")
CHAT_MAX_STEPS = 16
CHAT_WRAPUP_HINT_STEPS = 2
CHAT_MAX_WORKERS = 2

# glob/grep previews (count, shown, omitted, tool-side truncation) live in the shared chat text so the
# CLI trace, the TUI and the Web page say the same thing; re-exported here under the old names.
from baseagent.chat.text import PREVIEW_ITEMS, RESULT_LIMITS, list_preview as _list_preview  # noqa: E402,F401


class TraceMiddleware(AgentMiddleware):
    def __init__(self, *, preview_chars=0, steps=True, emit=None):
        self.preview_chars = preview_chars
        self.steps = steps
        # Workers emit into the chat's notice queue instead of the live terminal.
        self.emit = emit or (lambda line: print(line, file=sys.stderr))

    def before_model(self, state):
        if self.steps:
            self.emit(f"model step {state.step + 1}/{state.max_steps}")

    def wrap_tool_call(self, request, handler):
        self.emit(f"tool: {request.name}")
        if self.preview_chars:
            self.emit(f"tool args: {_trace_preview(request.arguments, self.preview_chars)}")
        result = handler(request)
        if not result.ok:
            if self.preview_chars:
                detail = f"{result.error.code}: {result.error.message}"
                self.emit(f"tool result: {_trace_preview(detail, self.preview_chars)}")
            else:
                self.emit(f"tool result: {result.error.code}")
        elif self.preview_chars:
            listing = _list_preview(request.name, request.arguments, result.data, self.preview_chars)
            self.emit(listing or f"tool result: {_trace_preview(result.data, self.preview_chars)}")
        else:
            self.emit("tool result: ok")
        return result


def main(argv=None, *, configure_parser=None, capability_factory=None,
         model_factory=None, system_prompt=SYSTEM_PROMPT) -> int:
    """Run the shared CLI; applications may extend parsing and composition only."""
    parser = argparse.ArgumentParser(description="Workspace coding agent", allow_abbrev=False)
    parser.add_argument("--config", type=Path, help="Versioned JSON harness configuration; explicit CLI options override it")
    parser.add_argument("prompt", nargs="?", help="Task or next conversation turn")
    parser.add_argument("--root", type=Path, help="Workspace directory (saved root on resume)")
    parser.add_argument("--session", help="Session ID (generated when omitted)")
    parser.add_argument("--db", type=Path, default=Path.cwd() / ".baseagent" / "sessions.sqlite3")
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--eval-harness", action="store_true", help="Run offline end-to-end harness scenarios in isolated temporary workspaces")
    operations.add_argument("--list-tool-packages", action="store_true", help="List available built-in tool modules without model or database access")
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
    operations.add_argument("--checkpoints", action="store_true", help="List per-turn file checkpoints of --session as JSON (no model access)")
    operations.add_argument("--rewind", metavar="N|last", help="Restore files written by the agent in turn N and every later turn, or only the latest turn with 'last' (no model access)")
    parser.add_argument("--force", action="store_true", help="--rewind: restore even files that changed after the agent wrote them")
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
    parser.add_argument("--subtasks-config", type=Path, help="Debugging/library entry: named sequential subtasks from a JSON configuration (not used by the main agent)")
    parser.add_argument("--main-agent", action=argparse.BooleanOptionalAction, default=None, help="Interactive chat as a main agent that only talks and dispatches background workers (default on for new chat sessions; existing sessions keep their mode)")
    parser.add_argument("--max-workers", type=int, help=f"Main-agent chat: workers that may be active at once (default {CHAT_MAX_WORKERS}); extra delegations are rejected, and only one worker may write at a time")
    parser.add_argument("--budget-cny", help="Main-agent chat: CNY per main-agent turn shared by the main agent and its workers, charged from reported usage at qwen-plus rates (input 0.8, output 2 CNY per million tokens); off by default")
    parser.add_argument("--request-bound-profile", choices=["deepseek-context-v1"], help="Explicit conservative whole-context bound for official DeepSeek Flash/Pro; requires --preauthorize-model")
    parser.add_argument("--max-completion-tokens", type=int, default=4096, help="Output cap for strict or estimated reservations (default: 4096)")
    parser.add_argument("--completion-policy", type=Path, help="JSON requirements: require_plan, exact command checks, and artifacts")
    parser.add_argument("--test-command", type=json.loads, help='Test-and-fix command as a JSON argv array; requires --allow-command')
    parser.add_argument("--test-fix-max-iterations", type=int, default=None, help="Maximum failure feedback messages (default: 3)")
    parser.add_argument("--test-fix-output-max-chars", type=int, default=None, help="Failure output character limit (default: 4000)")
    parser.add_argument("--accept-config-changes", action="store_true", help="Explicitly accept changed runtime/tools for an unfinished turn")
    parser.add_argument("--accept-workspace-changes", action="store_true", help="Acknowledge changed observed files/guidance; stale write hashes remain rejected")
    parser.add_argument("--model", default=None, help="Model name (default: BASEAGENT_LLM_MODEL or DEEPSEEK_MODEL)")
    parser.add_argument("--max-steps", type=int, help="Logical rounds (new turn: 8, interactive chat: 16; resume: preserve)")
    parser.add_argument("--max-model-calls", type=int, default=None, help="Actual model call attempts, including middleware retries and failed attempts (defaults to max-steps)")
    parser.add_argument("--max-tool-calls", type=int, help="Actual tool attempts (new turn: 32; resume: preserve)")
    parser.add_argument("--compaction-max-chars", type=int, help="Legacy explicit JSON character trigger for summaries (0 disables compaction; resume: preserve)")
    parser.add_argument("--compaction-keep-turns", type=int, help="Recent user turns to preserve including the active turn (default 2)")
    parser.add_argument("--compaction-window-tokens", type=int, help="Estimated model window tokens; triggers near window-reserve-margin (default 24000)")
    parser.add_argument("--compaction-reserve-tokens", type=int, help="Tokens reserved for the summary (default 2000)")
    parser.add_argument("--compaction-safety-tokens", type=int, help="Safety margin tokens before the window limit (default 1000)")
    parser.add_argument("--compaction-tail-chars", type=int, help="Minimum characters of recent raw tail to keep (default 1000)")
    parser.add_argument("--compaction-min-tail-items", type=int, help="Minimum message items in the raw tail (default 2)")
    parser.add_argument("--compaction-max-tail-items", type=int, help="Maximum message items in the raw tail (default 20)")
    parser.add_argument("--compaction-min-prefix-chars", type=int, help="Skip when the summarizable prefix is shorter than this (default 500)")
    parser.add_argument("--compaction-max-input-chars", type=int, help="Drop older turn-groups above this summarizer input size (default 60000)")
    parser.add_argument("--spill", dest="spill_enabled", action=argparse.BooleanOptionalAction, default=None, help="Spill oversized tool results to session files (default on; --no-spill disables all budgets)")
    parser.add_argument("--spill-threshold-tokens", type=int, help="Legacy single-result trigger in estimated tokens; 0 disables only that trigger")
    parser.add_argument("--spill-single-chars", type=int, help="Spill one result above this many characters (default 50000)")
    parser.add_argument("--spill-total-chars", type=int, help="Spill largest results until remaining full results fit this many characters (default 200000)")
    parser.add_argument("--spill-max-age-turns", type=int, help="Cut previews for results older than this many turns (default 10)")
    parser.add_argument("--spill-old-preview-chars", type=int, help="Preview characters for aged spill stubs (default 200)")
    parser.add_argument("--spill-preview-chars", type=int, help="Preview characters in a fresh spill stub (default 2048)")
    parser.add_argument("--spill-head-lines", type=int, help="Lines to include at the start of spill previews (default 20)")
    parser.add_argument("--spill-tail-lines", type=int, help="Lines to include at the end of spill previews (default 10)")
    parser.add_argument("--wrapup-hint-steps", type=int, help="Inject a wrap-up hint at or below this many remaining steps (0 disables; default: 2 in interactive chat, 0 otherwise)")
    parser.add_argument("--repeat-call-guard", action=argparse.BooleanOptionalAction, default=None, help="Warn on repeated identical calls and block the third (resume: preserve)")
    parser.add_argument("--dedupe-reads", action=argparse.BooleanOptionalAction, default=None, help="Return a short reference for unchanged repeated file reads (resume: preserve)")
    parser.add_argument("--stream", action=argparse.BooleanOptionalAction, default=None, help="Stream model tokens as model_delta events (resume: preserve)")
    parser.add_argument("--parallel-readonly-tools", action=argparse.BooleanOptionalAction, default=None, help="Run retry_safe tool batches in parallel (resume: preserve)")
    parser.add_argument("--max-context-bytes", type=int, help="Serialized model input UTF-8 bytes (default 96000; resume: preserve)")
    parser.add_argument("--max-tool-context-bytes", type=int, help="Per-tool data excerpt limit (default 4000; resume: preserve)")
    parser.add_argument("--max-total-tokens", type=int, help="Reported token budget per turn; unknown usage blocks execution")
    parser.add_argument("--estimate-model", action=argparse.BooleanOptionalAction, default=None, help="Estimate request tokens locally; actual usage can exceed budget")
    parser.add_argument("--tokenizer-file", help="Local tokenizer.json for estimated budgets; never executes remote tokenizer code")
    parser.add_argument("--estimate-margin-percent", type=int, default=20)
    parser.add_argument("--estimate-fixed-margin", type=int, default=256)
    parser.add_argument("--preauthorize-model", action=argparse.BooleanOptionalAction, default=None, help="Require trusted adapter request reservations; default CLI adapter has no prompt counter and refuses before dispatch")
    parser.add_argument("--accept-request-bound-violation", action="store_true", help="Acknowledge a verified request bound violation after repairing the adapter counter; usage/budgets remain")
    parser.add_argument("--max-duration-seconds", type=float, help="Wall duration from original turn start, including time between resumes")
    parser.add_argument("--model-retries", type=int, default=DEFAULT_MODEL_RETRIES, help="Retries after transport timeouts and transient model errors; each retry is a billed model attempt (0-4; default 2)")
    parser.add_argument("--tool-retries", type=int, default=0, help="Retries of explicitly safe, retryable tool failures (0-4)")
    parser.add_argument("--allow-write", action=argparse.BooleanOptionalAction, default=False, help="Allow file writes")
    parser.add_argument("--allow-command", action=argparse.BooleanOptionalAction, default=False, help="Allow subprocess commands")
    parser.add_argument("--allow-memory-publish", action=argparse.BooleanOptionalAction, default=False, help="Allow publishing/withdrawing workspace-shared reference notes")
    parser.add_argument("--mode", choices=list(MODES), help="Permission preset: read-only (default: no writes or commands), edit (= --allow-write), full (= --allow-write --allow-command); replaces permission settings from --config; --tool-policy can only restrict it further")
    parser.add_argument("--output-format", choices=list(OUTPUT_FORMATS), default="text", help="One-shot prompt or --resume output: text (default, unchanged), json (one object on stdout) or jsonl (metadata events, then a result line); see docs/output.md")
    parser.add_argument("--harness-url", help="Enable optional HarnessKnight security evaluation")
    parser.add_argument("--harness-fail-mode", choices=["closed", "open"], default="closed")
    parser.add_argument("--harness-timeout", type=float, default=3.0)
    parser.add_argument("--project-rules", action=argparse.BooleanOptionalAction, default=None, help="Show the workspace AGENTS.md project rules to the model (default on; --no-project-rules hides the text, write digests still apply); see README 项目规则文件")
    parser.add_argument("--mcp-config", type=Path, help="MCP servers JSON (mcpServers: command/args/env per server, stdio only); default: .mcp.json in the workspace root; without either, MCP is off; see docs/mcp.md")
    parser.add_argument("--mcp-strict", action="store_true", help="Exit with code 2 when a configured MCP server or tool cannot be used (default: skip it with a warning on stderr)")
    parser.add_argument("--trace", action=argparse.BooleanOptionalAction, default=False, help="Show model steps and tool names on stderr")
    parser.add_argument("--tool-package", action="append", choices=["coding", "memory", "shared-memory"], help="Select built-in tool packages; repeat to combine (default: all)")
    parser.add_argument("--skills-dir", action="append", type=Path, help="Explicit local skill package directory; repeat to combine")
    parser.add_argument("--watchdog", action=argparse.BooleanOptionalAction, default=None, help="Enable task-level stall, repeat, budget and timeout inspection")
    parser.add_argument("--watchdog-stall-steps", type=int, help="Hint after this many steps without a write or verify (0 disables; suggested 3)")
    parser.add_argument("--watchdog-stall-abort-steps", type=int, help="Abort after this many stalled steps (0 disables; suggested 5)")
    parser.add_argument("--watchdog-repeat-abort", type=int, help="Abort after this many equivalent tool calls (0 disables; suggested 4)")
    parser.add_argument("--watchdog-max-steps", type=int, help="Task step budget enforced by Watchdog (0 disables)")
    parser.add_argument("--watchdog-max-tokens", type=int, help="Task token budget enforced by Watchdog (0 disables)")
    parser.add_argument("--watchdog-max-seconds", type=float, help="Task wall-clock budget enforced by Watchdog (0 disables)")
    parser.add_argument("--watchdog-tool-timeout", type=float, help="Single-tool timeout in seconds (0 disables)")
    parser.add_argument("--ui", choices=["cli", "tui", "web"], default="cli", help="Interactive chat frontend: cli (default, line-based), tui (Textual; extra 'tui') or web (local FastAPI + SSE page; extra 'web')")
    parser.add_argument("--host", help="--ui web: address to bind (default 127.0.0.1); any non-loopback address requires --token")
    parser.add_argument("--port", type=int, help="--ui web: port (default 8765)")
    parser.add_argument("--token", help="--ui web: access token required on every request (or BASEAGENT_WEB_TOKEN); mandatory off localhost")
    if configure_parser is not None:
        configure_parser(parser)
    bootstrap = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    bootstrap.add_argument("--config", type=Path)
    # Only explicit command-line permission flags count for --mode conflicts (not --config defaults).
    bootstrap.add_argument("--allow-write", action=argparse.BooleanOptionalAction, default=None)
    bootstrap.add_argument("--allow-command", action=argparse.BooleanOptionalAction, default=None)
    initial, _ = bootstrap.parse_known_args(argv)
    defaults = {}
    if initial.config is not None:
        try:
            defaults = load_harness_config(initial.config)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
    # Append actions must replace configured arrays, rather than concatenate them.
    packages_default = defaults.pop("tool_package", None)
    skills_default = defaults.pop("skills_dir", [])
    parser.set_defaults(**defaults)
    args = parser.parse_args(argv)
    if not math.isfinite(args.harness_timeout) or args.harness_timeout <= 0:
        parser.error("--harness-timeout must be positive and finite")
    if args.tool_package is None:
        args.tool_package = packages_default
    if args.skills_dir is None:
        args.skills_dir = skills_default
    if (args.max_steps is not None and args.max_steps < 1) or (args.max_model_calls is not None and args.max_model_calls < 1) or (args.max_tool_calls is not None and args.max_tool_calls < 0):
        parser.error("step/model limits must be positive and tool limit nonnegative")
    session_operation = args.resume or args.inspect_session or args.task_tree or args.acknowledge_task_bound or args.resolve_tool is not None or args.approve_tool is not None or args.deny_tool is not None or args.cancel_session or args.clear_cancel is not None or args.events or args.follow_events is not None or args.prune_events is not None or args.shared_memory or args.resolve_usage is not None or args.export_session is not None or args.delete_session or args.checkpoints or args.rewind is not None
    database_operation = args.list_sessions or args.backup_db is not None or args.cleanup_days is not None
    operation = session_operation or database_operation or args.eval_harness or args.list_tool_packages
    interactive = not operation and args.prompt is None
    if interactive:
        # Chat turns are open-ended; one-shot defaults stay unchanged for scripts.
        if args.max_steps is None:
            args.max_steps = CHAT_MAX_STEPS
            if args.max_model_calls is None:
                args.max_model_calls = CHAT_MAX_STEPS
        if args.wrapup_hint_steps is None:
            args.wrapup_hint_steps = CHAT_WRAPUP_HINT_STEPS
    if (args.eval_report or args.eval_case) and not args.eval_harness:
        parser.error("evaluation report/cases require --eval-harness")
    if args.main_agent and not interactive:
        parser.error("--main-agent applies to interactive chat; one-shot prompts stay single-agent")
    if args.main_agent and args.subtasks_config:
        parser.error("--subtasks-config is a named-subtask debugging entry; use it with --no-main-agent")
    if (args.max_workers is not None or args.budget_cny is not None) and args.prompt is not None:
        parser.error("--max-workers and --budget-cny apply to main-agent chat; one-shot prompts stay single-agent")
    if args.max_workers is not None and args.max_workers < 1:
        parser.error("--max-workers must be positive")
    if args.budget_cny is not None:
        try:
            if not Decimal(args.budget_cny) > 0:
                raise ValueError
        except (ArithmeticError, ValueError):
            parser.error("--budget-cny must be a positive number")
    if args.eval_harness and (args.prompt is not None or args.session or args.root):
        parser.error("evaluation uses isolated fixtures; omit prompt, session and root")
    if args.list_tool_packages and (args.prompt is not None or args.session):
        parser.error("package discovery requires no prompt or session")
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
    for value in (args.max_context_bytes, args.max_tool_context_bytes):
        if value is not None and value < 256:
            parser.error("context byte limits must be >= 256")
    if args.compaction_max_chars is not None and args.compaction_max_chars < 0:
        parser.error("compaction character budget must be nonnegative")
    if args.compaction_keep_turns is not None and args.compaction_keep_turns < 1:
        parser.error("compaction keep turns must be positive")
    for value in (args.compaction_window_tokens, args.compaction_reserve_tokens, args.compaction_safety_tokens,
                  args.compaction_tail_chars, args.compaction_min_prefix_chars, args.spill_threshold_tokens,
                  args.spill_single_chars, args.spill_total_chars, args.spill_max_age_turns,
                  args.spill_old_preview_chars, args.spill_preview_chars, args.spill_head_lines,
                  args.spill_tail_lines):
        if value is not None and value < 0:
            parser.error("compaction and spill budgets must be nonnegative")
    for value in (args.compaction_min_tail_items, args.compaction_max_tail_items,
                  args.compaction_max_input_chars):
        if value is not None and value < 1:
            parser.error("compaction counts and summarizer input limits must be positive")
    if args.max_total_tokens is not None and args.max_total_tokens < 1:
        parser.error("token budget must be positive")
    if args.max_duration_seconds is not None and (not math.isfinite(args.max_duration_seconds) or args.max_duration_seconds <= 0):
        parser.error("duration budget must be positive and finite")
    if not 0 <= args.model_retries <= 4 or not 0 <= args.tool_retries <= 4:
        parser.error("retry counts must be 0-4")
    if args.ui != "cli" and not interactive:
        parser.error("--ui tui/web applies to interactive chat; one-shot prompts and session operations stay on the CLI")
    if args.force and args.rewind is None:
        parser.error("--force applies to --rewind")
    if args.rewind is not None:
        from baseagent.session.checkpoints import parse_target
        try:
            rewind_target = parse_target(args.rewind)
        except ValueError as exc:
            parser.error(f"--rewind: {exc}")
    if args.mode is not None:
        if operation and not args.resume:
            parser.error("--mode applies to one-shot prompts, --resume and interactive chat")
        allow_write, allow_command = MODES[args.mode]
        for flag, explicit, wanted in (("allow-write", initial.allow_write, allow_write),
                                       ("allow-command", initial.allow_command, allow_command)):
            if explicit is not None and explicit != wanted:
                given = f"--{flag}" if explicit else f"--no-{flag}"
                parser.error(f"--mode {args.mode} conflicts with {given}: {args.mode} means "
                             f"{'--' if wanted else '--no-'}{flag}; drop one of them")
        # The preset replaces permission settings that came from --config.
        args.allow_write, args.allow_command = allow_write, allow_command
    if (args.mcp_config is not None or args.mcp_strict) and operation and not args.resume:
        parser.error("--mcp-config and --mcp-strict apply to one-shot prompts, --resume and interactive chat")
    if args.output_format != "text":
        if interactive:
            parser.error(f"--output-format {args.output_format} applies to one-shot prompts and --resume; "
                         "interactive chat writes to a terminal or UI")
        if operation and not args.resume:
            parser.error(f"--output-format {args.output_format} applies to one-shot prompts and --resume; "
                         "session and database operations already print JSON or fixed text")
    if args.ui != "web" and (args.host is not None or args.port is not None or args.token is not None):
        parser.error("--host, --port and --token apply to --ui web")
    if args.ui == "web":
        from baseagent.web.config import web_settings
        try:
            web = web_settings(args.host, args.port, args.token or os.getenv("BASEAGENT_WEB_TOKEN"))
        except ValueError as exc:
            print(f"--ui web: {exc}", file=sys.stderr)
            return 2
    if args.ui != "cli":
        missing = _missing_frontend(args.ui)
        if missing:
            print(missing, file=sys.stderr)
            return 2

    machine = MachineOutput(args.output_format)
    if machine.enabled and not args.session:
        # json/jsonl report the session id even when the run fails before the first checkpoint.
        args.session = uuid4().hex

    # Defer provider configuration: inspection, reconciliation, and cached completion
    # need no model key and must not make an API call.
    class LazyModel:
        adapter = None

        def bound(self):
            configuration = model_configuration(args.model)
            return request_bound_profile(args.request_bound_profile, configuration["model_name"], configuration["base_url"], args.max_completion_tokens)

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
                self.adapter = initialize_model(args.model,
                                     **({"request_bound": self.bound()} if args.request_bound_profile else {}),
                                     **({"token_estimator": estimator, "max_completion_tokens": args.max_completion_tokens} if estimator else {}))

    mcp = None
    try:
        if args.list_tool_packages:
            from baseagent.capabilities.assembly import BUILTIN_PACKAGES
            print(json.dumps({name: list(tools) for name, tools in BUILTIN_PACKAGES.items()}, ensure_ascii=False, indent=2))
            return 0
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
        if args.checkpoints:
            print(json.dumps(store.checkpoints(args.session), ensure_ascii=False, indent=2))
            return 0
        if args.rewind is not None:
            if not existing.workspace_root:
                raise ValueError("session has no workspace binding")
            if args.root and args.root.resolve() != Path(existing.workspace_root).resolve():
                raise ValueError("session belongs to a different workspace")
            protected = [store.path, Path(str(store.path) + "-wal"), Path(str(store.path) + "-shm"),
                         Path(str(store.path) + "-journal"), store.lock_dir]
            # Writing back is the user's own restore, so it needs no --allow-write; paths stay inside
            # the workspace boundary and outside protected paths.
            target = Workspace(existing.workspace_root, protected_paths=protected)
            print(json.dumps(store.rewind(target, args.session, rewind_target, force=args.force), ensure_ascii=False, indent=2))
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
            from baseagent.timestamps import TIME_ZONE, iso_utc
            tree = store.task_tree(args.session)
            # Numbers stay Unix seconds; the *_utc strings mark the time zone for people reading the output.
            tree["time_zone"] = TIME_ZONE
            tree["current_root_turn_started_at_utc"] = iso_utc(tree["current_root_state"].get("turn_started_at"))
            for node in tree["nodes"]:
                node["turn_started_at_utc"] = iso_utc((node.get("state") or {}).get("turn_started_at"))
            print(json.dumps(tree, ensure_ascii=False, indent=2))
            return 0
        if args.inspect_session:
            from baseagent.timestamps import TIME_ZONE, iso_utc
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
                              "turn_started_at_utc": iso_utc(existing.turn_started_at), "time_zone": TIME_ZONE,
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
        project_rules = args.project_rules is not False
        workspace = Workspace(root, allow_write=args.allow_write, allow_command=args.allow_command, allow_memory_publish=args.allow_memory_publish, protected_paths=protected,
                              project_rules=project_rules)
        from baseagent.tools.instructions import describe_rules, rules_summary
        rules = rules_summary(workspace, enabled=project_rules)
        if rules is not None and rules.get("truncated") and project_rules:
            print(describe_rules(rules), file=sys.stderr)
        if args.mode == "full" and isinstance(workspace.backend, LocalBackend):
            print(FULL_MODE_WARNING, file=sys.stderr)
        policy = ToolPolicy()
        if args.tool_policy:
            policy = ToolPolicy.from_dict(read_json_object(args.tool_policy, max_bytes=20_000, label="tool policy"), workspace=workspace)
        completion = None
        subtasks = None
        # Existing sessions keep the mode they started with; new chats default to the main agent.
        main_agent = is_main_agent_session(existing) if existing else (interactive and args.main_agent is not False and not args.subtasks_config)
        if interactive and existing and args.main_agent is not None and args.main_agent != main_agent:
            print("Note: this session was started " + ("with" if main_agent else "without")
                  + " the main agent and keeps that mode.", file=sys.stderr)
        if main_agent and args.subtasks_config:
            raise ValueError("--subtasks-config cannot be combined with a main-agent session")
        if main_agent and args.prompt is not None:
            # Results of background workers are pushed into a chat; a one-shot prompt has none.
            raise ValueError(f"session {args.session} is a main-agent chat session; a one-shot prompt runs a single "
                             f"agent and cannot continue it. Continue it in chat: baseagent --session {args.session}")
        if args.subtasks_config:
            from baseagent.agent.subtasks import SubtaskRuntime
            configuration = read_json_object(args.subtasks_config, max_bytes=32000, label="subtask configuration")
            subtasks = SubtaskRuntime.from_dict(configuration, workspace=workspace)
        requirements = (read_json_object(args.completion_policy, max_bytes=20_000, label="completion policy")
                        if args.completion_policy else {"require_plan": False, "checks": [], "artifacts": []})
        if args.test_command is not None:
            requirements["test_fix"] = {**requirements.get("test_fix", {}), "argv": args.test_command}
        for field, value in (("max_iterations", args.test_fix_max_iterations), ("output_max_chars", args.test_fix_output_max_chars)):
            if value is not None:
                if "test_fix" not in requirements:
                    raise ValueError("test-fix settings require --test-command or completion policy test_fix")
                requirements["test_fix"][field] = value
        if args.completion_policy or "test_fix" in requirements:
            completion = CompletionPolicy(workspace, requirements)
        load_dotenv()
        if args.request_bound_profile and not args.preauthorize_model:
            raise ValueError("request bound profile requires --preauthorize-model")
        if args.request_bound_profile and not 1 <= args.max_completion_tokens <= 393216:
            raise ValueError("request bound output cap must be 1-393216 tokens")
        estimating = args.estimate_model or bool(existing and existing.estimate_model)
        if estimating and (args.preauthorize_model or args.request_bound_profile):
            raise ValueError("estimated and strict budgets cannot be combined")
        cached = bool(existing and existing.status == "completed" and args.prompt is None and not interactive)
        if (estimating and not args.tokenizer_file and not cached) or (args.tokenizer_file and not estimating):
            raise ValueError("estimated budgets require --tokenizer-file; tokenizer-file requires estimated mode")
        if estimating and not 1 <= args.max_completion_tokens <= 393216:
            raise ValueError("estimated output cap must be 1-393216 tokens")
        estimator = TokenizerEstimator(args.tokenizer_file, margin_percent=args.estimate_margin_percent, fixed_margin=args.estimate_fixed_margin) if estimating and args.tokenizer_file else None
        source_root = Path(__file__).parent
        source_hashes = {str(path.relative_to(source_root)): sha256(path.read_bytes()).hexdigest() for path in sorted(source_root.rglob("*.py"))}
        config = {"harness_source": digest(source_hashes), "openai_version": version("openai"), "jsonschema_version": version("jsonschema"),
                  "model": args.model or os.getenv("BASEAGENT_LLM_MODEL") or os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
                  "endpoint": os.getenv("BASEAGENT_LLM_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL"),
                  "context_policy_version": 1}
        spill_settings = {
            "enabled": args.spill_enabled, "threshold_tokens": args.spill_threshold_tokens,
            "single_chars": args.spill_single_chars, "total_chars": args.spill_total_chars,
            "max_age_turns": args.spill_max_age_turns, "old_preview_chars": args.spill_old_preview_chars,
            "preview_chars": args.spill_preview_chars, "head_lines": args.spill_head_lines,
            "tail_lines": args.spill_tail_lines}
        if any(value is not None for value in spill_settings.values()):
            config["spill"] = {key: value for key, value in spill_settings.items() if value is not None}
        compaction_settings = {
            "window_tokens": args.compaction_window_tokens, "reserve_tokens": args.compaction_reserve_tokens,
            "safety_tokens": args.compaction_safety_tokens, "tail_chars": args.compaction_tail_chars,
            "min_tail_items": args.compaction_min_tail_items, "max_tail_items": args.compaction_max_tail_items,
            "min_prefix_chars": args.compaction_min_prefix_chars,
            "max_summary_input_chars": args.compaction_max_input_chars}
        if any(value is not None for value in compaction_settings.values()):
            config["compaction"] = {key: value for key, value in compaction_settings.items() if value is not None}
        guard_settings = {key: value for key, value in {"wrapup_hint_steps": args.wrapup_hint_steps, "repeat_call_guard": args.repeat_call_guard, "dedupe_reads": args.dedupe_reads}.items() if value is not None}
        if any(value is not None for value in guard_settings.values()):
            config["guards"] = guard_settings
        if args.stream is not None:
            config["stream"] = {"enabled": bool(args.stream)}
        if args.parallel_readonly_tools is not None:
            config["parallel"] = {"readonly": bool(args.parallel_readonly_tools)}
        watchdog_settings = {key: value for key, value in {
            "enabled": args.watchdog, "stall_steps": args.watchdog_stall_steps,
            "stall_abort_steps": args.watchdog_stall_abort_steps, "repeat_abort": args.watchdog_repeat_abort,
            "max_steps": args.watchdog_max_steps, "max_tokens": args.watchdog_max_tokens,
            "max_seconds": args.watchdog_max_seconds, "tool_timeout_seconds": args.watchdog_tool_timeout,
        }.items() if value is not None}
        if watchdog_settings:
            config["watchdog"] = watchdog_settings
        if os.getenv("BASEAGENT_LLM_PROVIDER"):
            config["provider"] = os.environ["BASEAGENT_LLM_PROVIDER"]
        if os.getenv("BASEAGENT_LLM_EXTRA_BODY"):
            config["extra_body"] = model_configuration(args.model).get("extra_body")
        config["retries"] = {"model": args.model_retries, "tool": args.tool_retries, "delay": 0.25}
        if not project_rules:
            # Recorded only when off, so the default runtime contract (and existing sessions) stay unchanged.
            config["project_rules"] = False
        if args.request_bound_profile:
            config["request_bound"] = request_bound_profile(args.request_bound_profile, config["model"], config["endpoint"], args.max_completion_tokens).contract()
        if estimator:
            config["token_estimate"] = {**estimator.contract(), "max_completion_tokens": args.max_completion_tokens, "tokenizers_version": version("tokenizers")}
        layers = [ExecutionGuidanceMiddleware(workspace), RepositoryMiddleware(workspace, accept_changes=args.accept_workspace_changes)]
        if args.harness_url:
            from baseagent.middleware.harnessknight import install_harnessknight
            policy, layers = install_harnessknight(policy, layers, url=args.harness_url,
                fail_mode=args.harness_fail_mode, timeout=args.harness_timeout, agent_version=version("baseagent"))
        skills = SkillCatalog(args.skills_dir) if args.skills_dir else None
        tools = (capability_factory(workspace, args, skills, policy) if capability_factory is not None
                 else assemble_capabilities(workspace, packages=args.tool_package, skills=skills))
        if skills is not None:
            layers.append(SkillMiddleware(skills))
        if not cached:
            # MCP tools join the same registry, so policy, approval, ledger and hooks apply unchanged.
            from baseagent.mcp import McpManager
            mcp = McpManager.from_arguments(args.mcp_config, workspace.root, strict=args.mcp_strict,
                                            client_version=version("baseagent"))
            if mcp is not None:
                mcp.start()
                mcp.register(tools, allow_write=workspace.allow_write)
        base_layers = list(layers)
        chat_events = None
        from baseagent.chat.middleware import PatchDisplayMiddleware
        if interactive:
            from baseagent.chat import EventBuffer, ToolEventMiddleware
            chat_events = EventBuffer()
            # Previews for chat frontends; observes only. The TUI/Web show these instead of stderr trace lines.
            layers.append(ToolEventMiddleware(chat_events))
            if args.ui == "cli":
                # Outside the trace layer, so the apply_patch diff prints under its "tool result" line.
                layers.append(PatchDisplayMiddleware(lambda text: print(text, file=sys.stderr)))
                layers.append(TraceMiddleware(preview_chars=200, steps=args.trace))
        else:
            if not machine.enabled:
                # Text mode shows the whole apply_patch diff on stderr; json/jsonl keep stderr unchanged.
                layers.append(PatchDisplayMiddleware(lambda text: print(text, file=sys.stderr)))
            if args.trace:
                layers.append(TraceMiddleware())
        retry_layers = []
        if args.model_retries or args.tool_retries:
            retry_layers.append(RetryMiddleware(model_retries=args.model_retries, tool_retries=args.tool_retries))
        layers.extend(retry_layers)
        model = model_factory(args) if model_factory is not None else LazyModel()
        session = {"id": args.session}
        prompt = system_prompt
        workers = None
        # Chat sessions record their mode; one-shot sessions keep their original metadata.
        if interactive:
            layers.insert(0, SessionTagMiddleware(session_mode=MAIN_AGENT_MODE if main_agent else SINGLE_AGENT_MODE))
        if main_agent:
            # Workers get exactly the entry's tools and prompt; the main agent only dispatches.
            # Streaming deltas would print from worker threads into the prompt line, so workers never stream.
            worker_config = {key: value for key, value in config.items() if key != "stream"}

            def run_worker(user_input, *, session_id, tools, system_prompt, max_steps, max_model_calls, max_tool_calls,
                           middleware, cancellation, emit, metadata):
                return run_agent(
                    model, user_input, tools=tools,
                    middleware=[SessionTagMiddleware(**metadata), *base_layers,
                                PatchDisplayMiddleware(emit),
                                TraceMiddleware(preview_chars=200, steps=args.trace, emit=emit),
                                *([ToolEventMiddleware(chat_events, worker=metadata.get("worker_id"))] if chat_events else []),
                                *retry_layers, *middleware],
                    system_prompt=system_prompt, max_steps=max_steps,
                    max_model_calls=max_model_calls, max_tool_calls=max_tool_calls,
                    store=store, session_id=session_id, workspace_root=workspace.root,
                    compaction_max_chars=args.compaction_max_chars, compaction_keep_turns=args.compaction_keep_turns,
                    max_context_bytes=args.max_context_bytes, max_tool_context_bytes=args.max_tool_context_bytes,
                    runtime_config=worker_config, accept_config_changes=args.accept_config_changes,
                    max_total_tokens=args.max_total_tokens, max_duration_seconds=args.max_duration_seconds,
                    tool_policy=policy, cancellation=cancellation,
                    estimate_model=args.estimate_model, preauthorize_model=args.preauthorize_model,
                )

            def turn_budget(resuming=False, limits=None):
                steps = args.max_steps
                calls = args.max_model_calls
                current = store.load(session["id"]) if resuming and session["id"] else None
                if current is not None:
                    # A resumed turn keeps limits raised earlier (for example by /continue); never lower them.
                    steps, calls = max(steps or 0, current.max_steps), max(calls or 0, current.max_model_calls)
                if limits is not None:
                    steps, calls = limits[0], limits[1]
                steps = steps or CHAT_MAX_STEPS
                budget = TurnBudget(calls or steps, steps, budget_cny=args.budget_cny)
                if current is not None:
                    # A resumed turn keeps what it already used.
                    budget.model_calls, budget.steps = current.model_calls, current.step
                return budget

            workers = WorkerManager(tools, system_prompt=system_prompt, store=store, runner=run_worker,
                                    max_workers=args.max_workers or CHAT_MAX_WORKERS, budget_factory=turn_budget)
            subtasks = workers
            prompt = MAIN_AGENT_PROMPT

        def turn_limits(user_input, limits):
            """Limits for this run_agent call. /continue passes raised ``limits`` for the current turn; any other
            chat resume keeps the stored limits when they are higher than the chat defaults."""
            if limits is not None:
                return limits
            values = (args.max_steps, args.max_model_calls, args.max_tool_calls)
            if user_input is None and interactive and session["id"]:
                current = store.load(session["id"])
                if current is not None and current.status != "completed":
                    values = tuple(None if value is None else max(value, stored) for value, stored in
                                   zip(values, (current.max_steps, current.max_model_calls, current.max_tool_calls)))
            return values

        def run_turn(user_input, limits=None):
            extra = []
            max_steps, max_model_calls, max_tool_calls = turn_limits(user_input, limits)
            if workers is not None:
                # One shared budget per main-agent turn; workers started in it draw from the same pool.
                # A resumed turn gets a fresh pool seeded with the main agent's own usage so far.
                workers.budget = turn_budget(resuming=user_input is None, limits=limits)
                extra.append(SharedBudgetMiddleware(workers.budget, worker=False))
            state = run_agent(
                model, user_input, tools=tools,
                middleware=[*layers, *extra],
                system_prompt=prompt, max_steps=max_steps,
                max_model_calls=max_model_calls, max_tool_calls=max_tool_calls,
                store=store, session_id=session["id"], workspace_root=workspace.root,
                compaction_max_chars=args.compaction_max_chars, compaction_keep_turns=args.compaction_keep_turns,
                max_context_bytes=args.max_context_bytes, max_tool_context_bytes=args.max_tool_context_bytes,
                runtime_config=config, accept_config_changes=args.accept_config_changes,
                max_total_tokens=args.max_total_tokens, max_duration_seconds=args.max_duration_seconds,
                tool_policy=policy,
                completion_policy=completion,
                estimate_model=args.estimate_model, preauthorize_model=args.preauthorize_model, accept_request_bound_violation=args.accept_request_bound_violation,
                subtasks=subtasks,
            )
            session["id"] = state.session_id or session["id"]
            return state

        if interactive:
            from baseagent.chat import ChatSession
            chat = ChatSession(run_turn=run_turn, store=store, workspace=workspace, session_id=args.session,
                               workers=workers, main_agent=main_agent, events=chat_events,
                               continue_steps=args.max_steps or CHAT_MAX_STEPS,
                               continue_tool_calls=args.max_tool_calls or 32)
            if args.ui == "tui":
                from baseagent.tui.app import run_tui
                return run_tui(chat)
            if args.ui == "web":
                from baseagent.web.server import serve
                return serve(chat, **web)
            from baseagent.cli_chat import run_chat
            return run_chat(session=chat)
        if workers is not None:
            workers.restore(existing)
        if machine.enabled:
            machine.follow(store, args.session)
        state = run_turn(args.prompt)
        if workers is not None:
            # --resume of a main-agent turn: there is no chat to push into, so wait and print the results.
            from baseagent.chat import ChatSession
            human = sys.stderr if machine.enabled else sys.stdout
            finishing = ChatSession(run_turn=run_turn, store=store, workspace=workspace, session_id=session["id"],
                                    workers=workers, main_agent=True, status_events=False,
                                    printer=lambda stream, text: print(text, file=human if stream == "out" else sys.stderr))
            finishing.finish_workers()
    except KeyboardInterrupt:
        if machine.enabled:
            machine.finish(machine_result(session_id=args.session, status="interrupted", code=130,
                                          error="interrupted before the turn finished"))
        return 130
    except McpStartupError as exc:
        print(f"MCP error: {exc}", file=sys.stderr)
        if machine.enabled:
            machine.finish(machine_result(session_id=args.session, status="error", code=2, error=f"MCP error: {exc}"))
        return 2
    except (ValueError, SessionBusy, OSError, sqlite3.Error) as exc:
        print(f"Session error: {exc}", file=sys.stderr)
        if machine.enabled:
            machine.finish(machine_result(session_id=args.session, status="error", code=1, error=f"Session error: {exc}"))
        return 1
    finally:
        machine.stop()
        if mcp is not None:
            # Session or run over (normal end, error, Ctrl+C): stop every server process group.
            mcp.close()
    print(f"Session: {state.session_id}", file=sys.stderr)
    print(f"Reported tokens: {state.total_tokens}; unknown usage calls: {state.unknown_usage_calls}", file=sys.stderr)
    # json/jsonl: stdout carries only JSON; the human lines below move to stderr.
    human = sys.stderr if machine.enabled else sys.stdout
    if state.final_answer and not machine.enabled:
        print(state.final_answer)
    if state.error:
        print(f"Agent error: {state.error}", file=human)
    if state.status != "completed":
        print(f"Agent stopped: {state.status} (steps: {state.step}, model attempts: {state.model_calls}, tool attempts: {state.tool_calls})", file=human)
        from baseagent.cli_chat import LIMITS, stop_explanation
        if state.status in LIMITS or state.status == "usage_unavailable":
            print(stop_explanation(state, interactive=False), file=sys.stderr)
        if state.blocked_tool_calls:
            print("Inspect with --inspect-session; verify external effects before --resolve-tool.", file=sys.stderr)
        request = state.metadata.get("approval_request") or {}
        if state.status == "awaiting_approval" and request.get("name") == "apply_patch" and not machine.enabled:
            from baseagent.chat.text import call_request_json, patch_approval_text
            text = patch_approval_text(workspace, call_request_json(store, state, request.get("call_id")))
            if text:
                print(text, file=sys.stderr)
                print(f"Approve with --approve-tool {request.get('call_id')} --approval-digest "
                      f"{request.get('request_digest')} --session {state.session_id}", file=sys.stderr)
    if machine.enabled:
        value = machine_result(state, model_name=config["model"], store=store, include_patches=machine.format == "json")
        rules = rules_summary(workspace, enabled=project_rules)
        if rules is not None and rules.get("path"):
            # Optional, metadata only (never the rules text); absent without a root AGENTS.md.
            value["project_rules"] = rules
        machine.finish(value)
    if state.status != "completed":
        return 130 if state.status == "interrupted" else 1
    return 0


# --ui frontend -> (install extra, modules it needs). Core installs stay unchanged.
FRONTEND_EXTRAS = {"tui": ("tui", ("textual",)), "web": ("web", ("fastapi", "uvicorn"))}


def _missing_frontend(ui):
    """One-line install hint when the optional frontend dependencies are missing."""
    from importlib.util import find_spec
    extra, modules = FRONTEND_EXTRAS[ui]
    absent = [name for name in modules if find_spec(name) is None]
    if not absent:
        return None
    return (f"--ui {ui} needs the optional '{extra}' extra ({', '.join(absent)} not installed): "
            f"uv sync --extra {extra}  (or: pip install 'baseagent[{extra}]')")
