# BaseAgent

A small coding-agent harness built around a provider-independent loop, a tool registry, middleware hooks, and a workspace boundary. The default CLI uses an OpenAI-compatible chat completion endpoint configured through a local `.env` file.

## Run

Set `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, and optionally `DEEPSEEK_MODEL` in `.env` (the file is ignored by Git). Then:

```powershell
.\.venv\Scripts\python.exe -m baseagent "Explain the project structure" --root .
.\.venv\Scripts\python.exe -m baseagent "Fix the failing test and verify it" --root . --allow-write --allow-command
```

The agent can read files, search text, and inspect `git diff` by default. File writes require `--allow-write`; subprocesses require `--allow-command`. These flags give the model those capabilities for the whole run. `--allow-command` is **not a sandbox**: the command may access anything the current OS account can access. Only enable it for tasks and workspaces you trust.

`--max-steps` limits logical agent rounds (default 8). `--max-model-calls` limits actual adapter call attempts, including middleware retries; it defaults to `--max-steps`. `--max-tool-calls` limits actual dispatch attempts, including invalid arguments and middleware retries (default 32). Attempts are counted before execution, so failures consume budget too. Middleware that returns a cached response or denies a tool without calling its handler consumes no execution budget. The OpenAI SDK's implicit retries are disabled and its request timeout is 30 seconds.
Library calls require integer step/model/tool attempt limits; booleans, fractions and non-finite values are rejected before dispatch. Invalid resume limits leave the saved checkpoint and events unchanged.
Each tool result is limited to 256 KB in the transcript. A non-completed run exits with code 1 (130 for a handled interruption) and prints its status and attempt counts. Serialized input byte limits, reported token budgets, and turn deadlines are enforced.
Use `--trace` to print model steps and tool names without dumping file contents or tool arguments.

Host JSON files (`--tool-policy`, `--completion-policy`, `--subtasks-config`, `--result-file`, `--resolve-usage`) are read with actual byte limits and must be UTF-8 objects; UTF-8 BOM is accepted. Duplicate fields at any nesting level, non-finite numbers including exponent overflow, invalid Unicode and unsupported nesting are refused. Rejected reconciliation files leave the session, tool ledger and events unchanged. Limits are respectively 20 KB, 20 KB, 32 KB, 256 KB and 4 KB.

## Optional browser UI (Chainlit)

Chainlit supplies the chat interface, expandable execution steps and approval buttons. No frontend build is required:

```powershell
uv sync --extra ui
uv run --extra ui baseagent-ui
# Enable capabilities when needed; sensitive calls still require approval.
uv run --extra ui baseagent-ui --allow-write --allow-command
```

Open `http://127.0.0.1:8000`. The default workspace is the current directory; use `--root PATH`, `--db PATH`, `--port 8001` or `--model NAME` as needed. Model credentials still come from the local `.env`. The UI server listens on the local loopback interface.

Send a task and follow-up messages in the chat. `/status` shows the current session, `/sessions` lists up to 100 saved sessions for this workspace, `/load SESSION_ID` displays a saved conversation, and `/resume` continues its unfinished turn. Chainlit's new-chat button starts a separate agent session. Session storage uses the existing SQLite ledger; the Chainlit history sidebar is not used.

Write, edit and command requests display their exact tool arguments with allow/deny buttons. Approval applies only to that session, turn and request. Startup capability flags are still required. The stop button requests cancellation; an in-flight provider call may take until its timeout to return. Paused tasks can be resumed. Changes to configuration or workspace require an explicit acceptance action.

The UI runs the existing CLI in a bounded subprocess and reads its committed checkpoints/events. It displays execution metadata as calls progress and the final answer after completion; individual answer tokens are not streamed. Advanced recovery (uncertain side effects, token reconciliation and subtask management) continues to use the CLI commands below. Generated framework settings live under the ignored `.baseagent/ui-runtime` directory. Install the `ui` extra only when using the browser interface.

The CLI projects task and capability guidance into each model request. General questions should be answered directly; local searches are not internet research. Tools disabled by startup capabilities are omitted from model-visible specifications, and the UI automatically denies those tools without presenting an approval button. Runtime permission checks remain in place.

Two consecutive tool rounds with identical arguments and outcomes trigger a model warning; a third identical round stops with `tool_loop_detected` before another model request. IDs and JSON key ordering do not count as progress; changed arguments or results do. This narrow guard does not detect every form of task drift. The existing step/model/tool budgets remain the final limits. UI resume does not relaunch exhausted or uncertain sessions; use a new chat for a revised task or the CLI for explicit recovery and budget changes.

## Durable sessions and recovery

Every CLI run uses a SQLite session. The default database is `.baseagent/sessions.sqlite3` under the current directory, ignored by Git. An omitted session ID is generated and printed on stderr. Use the same `--db` when running from another directory. Sessions bind to their original workspace; an explicitly different `--root` is rejected.

```powershell
# First turn and follow-up: prior user, assistant, and tool messages are retained.
.\.venv\Scripts\python.exe -m baseagent "Inspect the project" --session demo
.\.venv\Scripts\python.exe -m baseagent "Explain the entry point" --session demo

# Continue the current unfinished turn; a completed turn returns its saved answer.
.\.venv\Scripts\python.exe -m baseagent --session demo --resume
.\.venv\Scripts\python.exe -m baseagent --session demo --inspect-session

# Extend absolute turn limits when necessary; consumed attempts stay counted.
.\.venv\Scripts\python.exe -m baseagent --session demo --resume --max-steps 16 --max-model-calls 16
```

A new prompt starts a new turn only after the previous one completes. Turn counters reset for a new prompt; recovery preserves counters and unspecified limits. Write/command permissions must be granted explicitly on each invocation, including recovery. Tools and middleware are supplied again by the caller; the database stores state, not Python objects or provider credentials. Full archived history currently grows without compaction; the model receives a separate bounded projection.

The checkpoint includes the transcript, outcome, workspace, turn ID, and budgets. The tool ledger records `pending → running → completed`, original requests, actual dispatch attempts, the latest dispatched request and terminal result, and the final middleware result. The assistant message and its pending calls commit together; a completed tool result and its transcript reply also commit together. A per-session OS file lock rejects concurrent writers and is released when the process exits.

On recovery, completed tools are reused and pending tools continue. Any `running` tool blocks recovery with `needs_recovery` before further model/tool calls: it may have changed external state before the process died. Inspect its request and recorded evidence, verify the actual file/process/external outcome, then supply a verified result:

```json
{"ok": true, "data": {"verified": "the intended file content exists"}, "error": null}
```

```powershell
.\.venv\Scripts\python.exe -m baseagent --session demo --resolve-tool CALL_ID --result-file verified-result.json
.\.venv\Scripts\python.exe -m baseagent --session demo --resume
```

Reconciliation records the supplied outcome without executing the tool. You may supply a structured failure after verification as well. Inspection, reconciliation, and returning an already completed answer need no model credentials. Inspection shows current-turn ledger details, including arguments and saved results; session contents are local plaintext. `.baseagent` and the configured database/sidecar/lock paths are excluded from file tools. Enabled commands still have the current OS account's privileges.

SQLite cannot transact atomically with arbitrary file writes or external services. This is conservative recovery, not a general exactly-once guarantee. Middleware retries can still repeat side effects within a run and must be designed for idempotency. Lifecycle hooks run again on an active recovery invocation and should be safe to repeat; middleware is trusted code and must keep transcript/tool IDs consistent.

## Session maintenance

These commands need no model credentials. Listing returns metadata without transcripts; sessions are ordered by ID with a `next_after` cursor:

```powershell
.\.venv\Scripts\python.exe -m baseagent --list-sessions --session-limit 100
.\.venv\Scripts\python.exe -m baseagent --list-sessions --after-session demo
.\.venv\Scripts\python.exe -m baseagent --session demo --export-session .baseagent/demo.json
.\.venv\Scripts\python.exe -m baseagent --backup-db .baseagent/backup.sqlite3
```

Inspection and export use a single SQLite read snapshot, so a concurrent writer cannot mix an old checkpoint with newer tool records. JSON export streams the checkpoint, all turns' tool records, and all session events. It is a plaintext archive for inspection, not an import format. Backup uses SQLite's online backup API, includes committed WAL data, validates integrity and foreign keys, and produces a database you can reopen with `--db`. Do not copy only a live `.sqlite3` file as a backup. Restore by choosing the backup database; incomplete tool outcomes still require reconciliation.

Exports and backups never overwrite an existing destination or protected database/lock paths. They are built in temporary files and published only when complete. Publication requires a filesystem supporting hard links (supported on the tested Windows NTFS workspace); otherwise the operation fails without replacing an existing artifact. Prefer `.baseagent/` so archives stay ignored and excluded from coding file tools. Backup timeout defaults to 30 seconds, checked during backup progress; it does not modify source sessions.

```powershell
# Delete one completed idle session and its tool/event records.
.\.venv\Scripts\python.exe -m baseagent --session demo --delete-session
# Explicitly discard recovery data for an unfinished idle session.
.\.venv\Scripts\python.exe -m baseagent --session demo --delete-session --discard-unfinished
# Preview, then optionally apply cleanup of completed sessions inactive for 30 days.
.\.venv\Scripts\python.exe -m baseagent --cleanup-days 30
.\.venv\Scripts\python.exe -m baseagent --cleanup-days 30 --apply-cleanup
```

Deleting an unfinished session requires the explicit discard flag. A held session lock blocks deletion even with discard. Cleanup never selects unfinished sessions or unresolved tool records; it locks and rechecks every candidate before deleting, skips busy/changed candidates, and commits one session at a time. Its output reports eligible/deleted/skipped IDs and supports `--after-session` / `--session-limit` pagination. Default cleanup is a read-only preview. Deletion removes database records, not workspace files. Small lock files are retained to avoid creating two lock owners for the same ID; SQLite may reuse freed pages, so deletion is not secure erasure or immediate file shrinking.

## Model context limits

`--max-context-bytes` limits serialized UTF-8 input (`messages` and tool schemas, default 96000 bytes). This is a byte budget, not a token count or a guarantee about any provider's context window. `--max-tool-context-bytes` sets a tool result data excerpt target (default 4000 bytes); error metadata is preserved. Limits persist and are kept on recovery unless explicitly changed.

The input builder preserves system instructions and the current user prompt. It clips large tool data with an explicit `context_truncated` marker, removes completed old turns as units, and then removes old exchanges within the current turn if needed. Assistant tool calls and their replies stay paired. The latest exchange is retained and its data previews can shrink further to fit the total limit. The model is told when history or data is incomplete; it may use tools to inspect missing details. This currently uses deterministic trimming rather than a model-generated summary or long-term memory.

The original messages and results remain unchanged in SQLite. The input sent to the provider is a copy, and `--inspect-session` shows the last input size, number of removed messages, and number of clipped results. If required instructions, prompt, schemas, and latest exchange cannot fit, execution stops with `context_limit_exceeded` before an API call. Increase the limit or start a shorter task:

```powershell
.\.venv\Scripts\python.exe -m baseagent --session demo --resume --max-context-bytes 128000
```

## Runtime compatibility

An unfinished turn records a contract fingerprint. The library automatically compares tool schemas and descriptions. Callers may pass `runtime_config={...}` to version their model, middleware configuration, and tool implementations; only its digest is persisted. The CLI supplies a fingerprint of harness Python sources, model name, endpoint, relevant dependency versions, and context policy version. This detects implementation/configuration drift, not changes to workspace file contents or remote services.

On a mismatch, recovery is refused before tools or model calls and the saved checkpoint stays unchanged. Inspect the new behavior and explicitly opt in if continuation is appropriate:

```powershell
.\.venv\Scripts\python.exe -m baseagent --session demo --resume --accept-config-changes
```

Unfinished legacy sessions without a fingerprint also need explicit acceptance. The flag does not grant file/command permissions and does not bypass unresolved `running` tools. A new turn after a completed turn may use updated tool/runtime configuration; the session's original system prompt remains fixed. Returning a completed answer requires no compatibility override because nothing executes.

## Usage budgets, deadlines, and retries

The provider adapter returns `ModelResponse(message, TokenUsage(...))`. Actual reported input/output/total tokens accumulate per turn and are checkpointed before outer middleware returns. A custom model may still return a plain message, but its usage is then unknown. Each dispatch is conservatively recorded as unknown before the request; interruptions and failures are not assumed free. New turns reset counts; recovery keeps them. Legacy sessions without usage fields count previous model attempts as unknown.

```powershell
.\.venv\Scripts\python.exe -m baseagent "Inspect the entry point" --session budget-demo --max-total-tokens 12000 --max-duration-seconds 120
.\.venv\Scripts\python.exe -m baseagent --session budget-demo --resume --max-total-tokens 24000 --max-duration-seconds 300
```

By default the token limit is an **accounting limit**: a single request may overshoot it because input/output usage is only reported afterward. The harness then stops before further work and preserves the response for recovery. An exact-limit final answer can complete; another API dispatch cannot start. Missing usage stops a budgeted turn with `usage_unavailable`. Verify provider request/billing records and record the aggregate for *all* unknown attempts:

```json
{"prompt_tokens": 400, "completion_tokens": 20, "total_tokens": 420, "unknown_calls": 1}
```

```powershell
.\.venv\Scripts\python.exe -m baseagent --session budget-demo --resolve-usage verified-usage.json
.\.venv\Scripts\python.exe -m baseagent --session budget-demo --resume
```

Never supply zero unless you have verified those attempts consumed no tokens. Reconciliation records checked counts and never sends a model request. Unknown usage remains visible even when no token limit is set. Token/duration limit settings are inherited by subsequent turns unless overridden; usage and start time reset for each new turn.

### Model request reservation

Library callers can enable `run_agent(..., preauthorize_model=True, max_total_tokens=...)`. The adapter must implement `reserve_request(messages, tools)` returning a `TokenReservation` and `complete_reserved(..., reservation=..., cancellation=..., timeout=...)`. The quote binds a total token upper bound and output cap to a digest of the final projected messages and tool schemas. Core admission refuses requests whose bound exceeds the remaining allowance; each admitted reservation and unknown attempt are checkpointed before dispatch. Reported usage releases the reservation and charges actual usage. Exceptions, crashes and responses without usage retain the reservation and block further budgeted execution pending reconciliation.

The built-in `Model` accepts `token_counter=` (a trusted local callable returning a provider-specific prompt token upper bound) and `max_completion_tokens=`. Its reserved dispatch sends the output cap as `max_tokens`. The counter must account for provider message formatting and tool overhead, and the provider must honor that cap. Character counts or a generic tokenizer estimate do not establish this guarantee. Include the counter version and output cap in the caller's `runtime_config` so recovery detects configuration changes. This is a token allowance, not a currency budget.

Without an explicit bound profile, `--preauthorize-model --max-total-tokens N` stops with `reservation_unavailable` before provider client initialization or any provider request. The CLI now supports an explicit conservative capacity profile for the official DeepSeek endpoint:

```powershell
.\.venv\Scripts\python.exe -m baseagent "Read README.md and report its purpose" --session bounded-demo --model deepseek-flash --preauthorize-model --request-bound-profile deepseek-context-v1 --max-completion-tokens 4096 --max-total-tokens 1200000
```

`deepseek-context-v1` reserves **1,048,576 tokens for each request**, the provider's documented total context capacity including input and output; the output cap is already included, not added again. This is a worst-case capacity bound, not a prompt-token estimate. Actual reported usage is charged and the remaining reservation is released. Smaller remaining budgets refuse before adapter creation/network access. Every ancestor and child budget must admit this same bound, so the small token limit in `examples/subtasks.json` must be explicitly enlarged to use this profile. A tighter provider-specific prompt upper-bound counter remains unavailable; the [official tokenizer guidance](https://api-docs.deepseek.com/quick_start/token_usage/) describes local counting as an estimate.

Only canonical `deepseek-flash`/`deepseek-v4-pro` on `https://api.deepseek.com` (optionally `/v1`) are accepted. The profile pins the documented display names, 1,048,576 context capacity and 393,216 maximum output capacity from the [official model metadata API](https://api-docs.deepseek.com/api/list-models/). Before its first generation, each adapter queries `/models` with at most five seconds and the remaining deadline; missing/changed/ambiguous metadata refuses generation. Bound-mode HTTP disables redirects and environment proxies. Metadata checking consumes time but is not an extra generation; because dispatch admission is already recorded before this check, failures conservatively retain the unknown attempt and reservation for reconciliation. Cancellation/deadline are checked again after metadata verification. Other providers/proxies/aliases require their own independently justified library counter; no character/tokenizer heuristic is silently promoted to a bound.

The profile and output cap enter the CLI runtime fingerprint and must be repeated on recovery. `Model(..., request_bound=...)` also accepts the same profile through the library; it cannot be combined with `token_counter`. The reserved path verifies the exact pinned quote and sends `max_tokens`. Ordinary library `complete()` does not enable preauthorization by itself; use `run_agent(..., preauthorize_model=True, max_total_tokens=...)`.

If reported total usage exceeds the declared bound, or reported completion tokens exceed the output cap, `request_bound_violated` preserves the response and pauses before pending tools execute or an answer completes. Repair and verify the adapter's bounds, then explicitly resume with `--accept-request-bound-violation` (library: `accept_request_bound_violation=True`). Acknowledgment retains charged usage and does not increase the budget. Unknown usage reconciliation compares aggregate total usage against all outstanding total reservations; it cannot establish individual attempt bounds or output caps. New turns reset reservations; recovery retains them. Preauthorization is inherited by subsequent turns. Deadlines and cancellation are rechecked after local counting and before recording a dispatched attempt; a counter that expires the deadline or observes cancellation does not create unknown provider usage.

`--max-duration-seconds` is wall time from the original turn start, including time between recovery invocations. Increasing it extends that original deadline. Checks run at model/tool dispatch and loop boundaries. The CLI adapter bounds its HTTP timeout by remaining time (and its usual 30 seconds). Workspace commands, verification commands and `git_diff` use the smaller of their own timeout and the remaining turn time; an expired deadline prevents process launch. Process cleanup and checkpoint commits may finish after the deadline. This is cooperative stopping: arbitrary in-process Python handlers and HTTP transport phases are not forcefully terminated by this turn budget. Contextual library handlers can query `context.remaining_seconds()` for the current remaining time (`None` for no deadline). Old sessions without a start timestamp establish a migration origin on first active upgrade.

`--model-retries N` and `--tool-retries N` opt into up to N retries (0-4, default 0), with bounded exponential delays. Every dispatch still consumes core attempt budgets; deadlines are rechecked. Model retries cover transport failures and status 408/409/429/500/502/503/504, not authentication or arbitrary programming failures. With a token budget, an unknown failed attempt blocks the next request until usage is verified.

Tools additionally require `register(..., retry_safe=True)` and an explicitly retryable failure. Actual terminal calls are checked, so inner middleware redirects to an unsafe write cannot inherit a read tool's retry permission. Writes and commands are not declared retry-safe. A cached failure or denial with no dispatch is not retried. Tool retry failures/interruption may still leave a running ledger entry requiring conservative reconciliation; the retry declaration is a developer promise of idempotency, not an OS guarantee. Retry options participate in the CLI runtime fingerprint and must be supplied consistently on resume.

### Task-node persistence infrastructure

Schema v6 stores child task snapshots with stable task IDs, parent IDs, originating tool call IDs, and distinct node turn IDs. Under `store.exclusive(session_id)`, internal orchestration can call `store.create_task(root_state, call_id=..., name=..., state=child_state)` and `store.task_node(root_state, task_id)` to access a node checkpoint/ledger view. Creation requires a running parent tool call and enforces depth/quantity limits. Repeating the same parent call returns the existing node; a changed initial task request is refused. Nested creation supplies `parent_task_id`.

The node view's checkpoint methods commit node state, ancestor snapshots, root state, tool ledger changes and events in one transaction. Tool call IDs are isolated by node turn ID. Views reject writes outside the root lock and reject stale snapshots; reopen a view after another writer changes the node or an ancestor. Node events retain root-turn association and include task/parent IDs without task text.

The internal core loop accepts an `ExecutionScope`; `node_view.execution_scope()` binds its state to the exact ancestor snapshots committed by that node view. Actual model/tool dispatches check every ancestor limit and charge each ancestor once. State counters represent subtree usage; `metadata.direct_usage` separately records this node's own calls and tokens. Root preauthorization applies even when a child disables its own flag: request admission, durable reservations, actual usage refunds and bound violations propagate to all ancestors. Effective deadline is the earliest original deadline in the chain. Root persistent cancellation and a supplied ancestor cancellation token reach model/tool control boundaries and bounded commands. Recovery retains existing counters; it never reconstructs charges by summing overlapping subtree totals.

```powershell
.\.venv\Scripts\python.exe -m baseagent --session demo --task-tree
```

`--task-tree` reads current-turn child states and ledgers from one database snapshot without provider credentials. Full session export includes nodes from all root turns and their existing tool ledgers; SQLite backup preserves the tree. Deletion and age cleanup refuse unfinished nodes unless deletion explicitly discards the session. Without its delegation runtime, root `run_agent` pauses with `needs_recovery` when the current turn has unfinished children. Root aggregate usage reconciliation refuses unresolved child usage to avoid leaving node and ancestor ledgers inconsistent. Raw snapshots contain task/conversation data. See `SUBTASK_DESIGN.md` for the complete contract and outstanding validation.

#### Sequential durable delegation

The library accepts `run_agent(..., subtasks=SubtaskRuntime([...]))`. Define tasks using `SubtaskDefinition(name, tool_names, model=..., policy=..., ...)` from `baseagent.agent.subtasks`. Child models default to the active parent adapter; CLI tasks always inherit it. Caller runtime configuration must identify custom model/middleware behavior and its version. Definitions and their limits enter recovery fingerprints.

```powershell
.\.venv\Scripts\python.exe -m baseagent "Delegate reading README.md to reader, then summarize the findings" --session delegation-demo --subtasks-config examples/subtasks.json --max-model-calls 12 --max-total-tokens 50000
.\.venv\Scripts\python.exe -m baseagent --session delegation-demo --resume --subtasks-config examples/subtasks.json
```

Supplying `--subtasks-config` explicitly enables the `run_subtask(name, prompt)` tool. JSON accepts `tasks`, `max_depth`, `max_nodes`; each task requires `name` and `tools`, and can specify `system_prompt`, a normal tool `policy`, and local limit fields. Duplicate keys, nonfinite numbers, unknown fields and tools absent from the root registry are refused. The example authorizes only file-reading tools. Workspace write/command/publication permissions still come from the original handlers and capability switches.

Child tool registries reuse authorized parent handlers and select only available parent tool names. Nested tasks cannot gain a tool absent from their parent. Effective policy takes the stricter parent/child action, and approvals bind that combined policy and the node turn. Parent middleware is inherited; extra library task middleware can be configured explicitly. Cycles are refused, and depth/quantity limits apply before node creation.

A paused child leaves its parent delegation call running and preserves its node. After node approval or reconciliation, resume the root with the same configuration to continue that child and deliver one paired result. Managed delegation recovery does not charge the parent tool attempt again. Uncertain ordinary child tools still require manual verification. A completed child's result is cached: interruption or hard exit before parent delivery does not reexecute the child model or tools. Results contain task ID, status and up to 8000 UTF-8 bytes of answer; the full answer stays in its node snapshot. Plans, private notes and verification records stay in that node. File observations transfer for subsequent drift checks, while parent completion policy still requires its own actual verification records.

To extend a child's configured limits, edit the configuration and explicitly use `--accept-config-changes`; existing counters, original start time and system prompt remain. Root limits may also need extension. Policy changes invalidate prior node approvals. The `delegation` offline evaluation scenario exercises the combined workflow. Current source has passed a live-provider preauthorized read/delegate/cache check and the ten delegation contracts have been audited; see `HARNESS_AUDIT.md` for evidence and limits. Overall completion still requires a trusted tight provider input counter.

`examples/smoke_delegation.py` reproduces the live check using the CLI's local `.env` configuration and **makes billed provider requests**. It creates a fresh isolated read-only workspace/session under `.baseagent`, delegates a random file marker to a reader, validates raw file evidence and provider usage accounting, then prohibits adapter creation and network connections during a completed CLI resume. It prints only check results and writes a report alongside the database and fixture. Run it from the repository root:

```powershell
.\.venv\Scripts\python.exe examples/smoke_delegation.py
.\.venv\Scripts\python.exe examples/smoke_delegation.py --preauthorize
```

This verifies one actual provider workflow, not the quality of arbitrary model decisions or every failure boundary. The detailed offline/fault-injection tests cover those mechanics separately.

### Delegation without database files

`run_agent(..., subtasks=runtime, store=None)` automatically creates an in-memory SQLite ledger. The same node identities, ancestor budgets, policy intersection, cancellation, tool pairing and transaction rules apply. It creates no database or lock files. The returned state includes `metadata.execution_storage = {"durable": false, "resumable": false}` and the node snapshots in `metadata.subtask_tree`. The generated session ID identifies this ephemeral tree; it is not a durable CLI resume ID. The internal store closes when the call returns, including when a child pauses. Snapshots are inspection data, not an import/recovery format, and may contain private child transcripts and notes; handle them like session exports.

To approve or reconcile and resume within the same host process, retain an explicit `MemorySessionStore`:

```python
from baseagent.session import MemorySessionStore

with MemorySessionStore() as store:
    state = run_agent(model, "task", tools=tools, subtasks=runtime,
                      store=store, session_id="demo")
    nodes = store.task_tree("demo")["nodes"]
    # If a node awaits approval, inspect its exact request and use
    # store.decide_task_tool(...); then resume with the same runtime:
    # state = run_agent(model, tools=tools, subtasks=runtime,
    #                   store=store, session_id="demo")
```

This backend serializes database operations, enforces process-local session locks and supports the existing node recovery APIs. Closing the store destroys its contents; process exit cannot be recovered. Explicit export/backup writes only the requested artifacts and can preserve a snapshot before closing. Shared memory tools require durable storage and are denied for both ephemeral roots and their children. The CLI continues to use file-backed `SessionStore`.

Children can explicitly publish/withdraw when their selected parent handlers have the workspace publication capability and their effective policy permits it. Node publication requires its current root turn, the correct node store and the held root lock. Published source metadata includes `source_task_id`; retrieval checks that exact node's note and raw message digest, rather than root private notes. The original node remains a valid reference source after the root starts another turn. Deleted node sources report `source_missing` and never fall back to a root note with the same key. Private notes stay in the node, and publication does not make their content instructions or verification evidence. Root publications retain the legacy source layout; node identity is an optional addition to the existing provenance JSON, so no schema migration is needed. Exports and SQLite backups preserve that identity.

#### Node-targeted recovery

Use task IDs and exact approval digests from `--task-tree`. These operations take the root lock and never call the model or tool:

```powershell
.\.venv\Scripts\python.exe -m baseagent --session demo --task-id TASK_ID --approve-tool CALL_ID --approval-digest DIGEST
.\.venv\Scripts\python.exe -m baseagent --session demo --task-id TASK_ID --deny-tool CALL_ID --approval-digest DIGEST
.\.venv\Scripts\python.exe -m baseagent --session demo --task-id TASK_ID --resolve-tool CALL_ID --result-file verified-result.json
.\.venv\Scripts\python.exe -m baseagent --session demo --task-id TASK_ID --resolve-usage verified-usage.json
.\.venv\Scripts\python.exe -m baseagent --session demo --task-id TASK_ID --acknowledge-task-bound
.\.venv\Scripts\python.exe -m baseagent --session demo --task-id TASK_ID --cancel-session
.\.venv\Scripts\python.exe -m baseagent --session demo --task-id TASK_ID --clear-cancel REQUEST_ID
```

Library equivalents are `decide_task_tool`, `resolve_task_call`, `resolve_task_usage` and `acknowledge_task_bound` on the root `SessionStore`. Approval binds the node turn, exact unexecuted call and policy fingerprint; a changed policy must request approval again. Tool reconciliation commits the supplied checked result and its paired node transcript entry, leaving completed effects unreplayed.

For usage, the JSON `unknown_calls` must match **all attempts made directly by the target node** in `metadata.direct_usage.unknown_usage_calls`, not its subtree total. The verified aggregate is charged once to the node and every ancestor, and only reservations owned by that node are released. Sibling and descendant unknown usage/reservations remain intact. Repeating a receipt after clearing those unknown attempts is refused. Aggregate receipts cannot prove per-attempt/output caps; total-bound excess is detected only when every reconciled attempt had a reservation. Bound acknowledgment must target the originating node and clears matching propagated violations without reducing charged usage or extending limits. Nodes from other root turns and stale/mismatched approval digests are refused. These recovery commands alone do not execute anything; resume with the delegation runtime/configuration afterward.

## Structured events

The ordered event log was introduced in SQLite schema v2; current schema v6 migrates v1/v2/v3/v4/v5 stores while retaining checkpoints and tool records. Events cover run start/stop, model dispatch/result/failure, assistant checkpoints, tool start/attempt/result/completion, recovery blocks, usage reconciliation, approval decisions, cancellation requests, reference publication/withdrawal, and task-node checkpoints. They include measured call durations, counts, outcome codes, and whether usage was reported. State changes and corresponding start/completion events commit together.

```powershell
.\.venv\Scripts\python.exe -m baseagent --session demo --events
.\.venv\Scripts\python.exe -m baseagent --session demo --events --after-event 20 --event-limit 100
```

The sequence is a database cursor; pages are ordered and contain up to 1000 events. Querying needs no model credentials. Events intentionally exclude prompts, argument values, result bodies, provider configuration, and exception text; raw checkpoints and tool ledger still contain conversation data. Nonpersistent library runs retain their last 100 metadata events in `State.events`. Completed-session cleanup deletes their events together.

### Following and retaining events

```powershell
.\.venv\Scripts\python.exe -m baseagent --session demo --events --event-page --after-event 20
.\.venv\Scripts\python.exe -m baseagent --session demo --follow-events 30 --after-event 20
.\.venv\Scripts\python.exe -m baseagent --session demo --prune-events 500 --keep-events 100
# Review delete_count/prune_through, then apply the unchanged preview:
.\.venv\Scripts\python.exe -m baseagent --session demo --prune-events 500 --keep-events 100 --event-prune-digest REVIEWED_DIGEST
```

`--event-page` adds `next_after`, `has_more`, `pruned_through`, and `history_lost` to the existing event query. Use these metadata pages for clients that need to detect retention. Sequence numbers are global across sessions; gaps alone do not imply lost history. Only the recorded per-session pruning boundary determines `history_lost`.

`--follow-events` emits and flushes JSON page envelopes as individual lines, polling every 250 ms by default. Each invocation lasts at most 60 seconds; reconnect using its last `next_after` cursor. Following does not acquire the executor lock, reset budgets, or infer that an executor has died when no event arrives. This streams committed metadata events; token-by-token model output is not part of the interface. Ctrl+C stops the observer with exit code 130. Library consumers use `event_page` and `follow_events`; following also accepts `poll_interval` and an optional cancellation token.

Event pruning defaults to a read-only preview. Apply requires the matching preview digest and session lock; a changed candidate set is rejected for fresh review. It removes an ordered prefix no later than the requested sequence and keeps at least the configured latest event count (1-100000). Full session state, tool ledger, cancellation requests, budgets, and verification evidence remain unchanged. The pruning boundary and cumulative deleted count commit atomically with deletion. Export and backup preserve this metadata, so consumers can detect missing history after recovery. SQLite may reuse deleted pages; pruning does not immediately shrink the database file or securely erase old bytes.

## Tool execution contract

Tool schemas are checked on registration. The registry validates required fields, types, nested values, array limits, and unknown fields before invoking a handler, using JSON Schema Draft 2020-12. JSON arguments containing duplicate keys or non-finite numbers are rejected.

Direct library calls to `ToolRegistry.execute` also reject non-finite or non-serializable argument values before invoking the handler. Tool arguments exceeding the JSON parser's nesting capacity produce `invalid_arguments` without executing the tool. Non-serializable, non-finite or excessively nested return values produce `invalid_result`. Direct `run_process` calls require a positive finite numeric timeout and a positive integer output byte limit; invalid limits raise `ValueError` before creating a process or Windows Job Object.

All tool messages and `wrap_tool_call` results use `ToolResult`:

```json
{"ok": true, "data": {"path": "README.md", "content": "..."}, "error": null}
```

```json
{"ok": false, "data": null, "error": {"code": "permission_denied", "message": "file writes disabled", "retryable": false}}
```

A plain JSON-serializable handler return value is wrapped as successful data. Expected failures should raise `ToolFailure` or return `ToolResult.failure(...)`; an ordinary dictionary containing an `error` key is still data. Interceptors must return `ToolResult` explicitly. Errors distinguish invalid arguments, unknown tools, permission denial, missing files, size limits, timeouts, execution failures, and invalid return values. They are nonretryable by default; a tool must explicitly identify a safe transient failure to opt into retry. Automatic retries are opt-in and also require a registered retry-safe tool.

Commands continuously drain stdout/stderr while retaining at most 20 KB of each stream. Results report truncation flags; nonzero exit codes are failures and retain captured output. Windows uses a kill-on-close Job Object to manage the launched process and its associated descendants; POSIX uses a process group. Command completion, timeout, and interruption clean up these processes, so detached background jobs are not supported. This manages process lifetimes; it does not restrict filesystem or network access. The Windows job is attached immediately after launch, so it is not an adversarial process-containment guarantee.

## Coding workflow

The CLI includes `RepositoryMiddleware`. It loads root `AGENTS.md` as reference data and checks previously observed file hashes and scoped instruction digests before agent, model, and tool execution. `before_tool` is a local preflight hook executed before marking the ledger entry running; the model/tool wrapper chains retain their existing ordering. Library callers must include this middleware to enable guidance projection and drift checks.

`get_instructions(path)` reads root-to-directory `AGENTS.md` files within the selected workspace. Each file is limited to 20 KB, with 40 KB combined. Guidance is projected as assistant reference data, without changing the saved transcript; user instructions and capability gates still apply. Nested guidance is returned by file reads. Instruction links and write aliases through symlinks/junctions are rejected.

`read_file` returns the raw whole-file SHA256, applicable instruction digest, and a line range (first 200 lines by default). Files are bounded to 200 KB; UTF-8 and original line endings are preserved. `write_file` requires `expected_sha256` and `expected_instructions`; use `missing` for creation. Existing files require their current hash. `edit_file` applies up to 20 exact, unique text replacements in memory and publishes only when every replacement matches and both digests remain current. Unrelated bytes remain unchanged.

Writes use cooperative per-file locks, sibling temporary files, flush/fsync, and atomic publication. Creation uses a non-overwriting hard link and therefore requires filesystem hard-link support. These guarantees cover one file at a time. Arbitrary external editors can still race the final hash check and replacement; this is optimistic conflict detection, not a filesystem compare-and-swap or an OS sandbox.

Observed files are limited to 500 per turn. Drift pauses execution with `workspace_changed`. Inspect the workspace before resuming with `--accept-workspace-changes`; this acknowledgment neither enables capabilities nor bypasses a pending write's expected hashes. Authorized commands refresh observations, while changed tracked files invalidate prior verification records. New turns reset observations but retain plans and verification history. Runtime contract changes separately require `--accept-config-changes` for unfinished sessions.

`update_plan` uses an expected revision, unique step IDs, up to 50 steps, and at most one step in progress. Plans record intent, not proof. `get_task_state` returns the plan and recent verification summaries. `verify_command` requires command permission, runs the actual command, records its exit code and tracked file hashes, and persists start/completion checkpoints. A successful command that changes tracked files is marked stale and must be rerun. Failures and interruptions remain visible. The latest 200 checks are retained in state; bounded command output remains in tool results rather than being duplicated in events.

The core supplies `ToolContext` to contextual tools per invocation; model arguments cannot provide it. Plan changes and terminal tool results commit together, including when an outer wrapper is interrupted. As with other commands, uncertain execution after a process crash requires ledger reconciliation.

## Tool policy and durable approval

An optional `--tool-policy policy.json` adds core authorization to every tool call:

```json
{"default": "allow", "tools": {"write_file": "ask", "edit_file": "ask", "run_command": "ask", "verify_command": "ask"}}
```

Actions are `allow`, `deny`, or `ask`, matched by exact tool name; no wildcard matching is implied. Without a policy, existing capability gates apply. For restricted registries use `default: deny` and explicitly allow required tools. The policy must have only `default` and `tools` fields and is bounded to 20 KB by the CLI.

A tool entry may also have ordered parameter rules:

```json
{
  "default": "deny",
  "tools": {
    "read_file": {
      "default": "deny",
      "rules": [{"action": "allow", "match": {"path_within": {"path": ["src", "tests"]}}}]
    },
    "edit_file": {
      "default": "deny",
      "rules": [{"action": "ask", "match": {"path_within": {"path": ["src"]}}}]
    },
    "run_command": {
      "default": "deny",
      "rules": [{"action": "ask", "match": {"argv_exact": ["python", "-B", "-m", "unittest", "discover", "-s", "tests"]}}]
    }
  }
}
```

The first matching rule determines the action; unmatched calls use that tool's default. All conditions in one match must hold. `equals` matches named top-level arguments against exact JSON values, including types; `argv_exact` matches the full ordered argv, with no extra arguments. `path_within` requires each named path argument to resolve to one of its configured directory/file roots inside the workspace. Scope roots may be prospective paths for new files. Protected paths, absolute arguments, `..`, colon/drive/stream syntax, and links/junction aliases do not match a path scope. Windows directory names follow the host path comparison behavior. Use a denying fallback for scope restrictions.

Each tool permits up to 50 ordered rules, and each path argument has up to 20 roots. Conditions do not implement regexes, shell interpretation, wildcard roots, executable identity checks, or command-content analysis. Conditional calls require strict finite JSON objects bounded to 256 KB; malformed or duplicate argument keys fail closed. Duplicate keys in the policy configuration are also rejected. `ToolPolicy.from_dict(..., workspace=workspace)` supports library configuration; path conditions require a workspace, whose identity participates in the policy fingerprint. Legacy string-action policies retain their original contract shape.

Parameter rules restrict authorization at dispatch, including middleware redirects, and complement existing workspace/hash/capability checks. Commands still have the host process's filesystem and network access. An exact argv does not contain the behavior of a modified executable or script; select trusted command implementations and use OS isolation when that is required.

An `ask` call stops with `awaiting_approval` before entering its tool wrapper or marking the ledger running. Inspect the pending ledger's full request and `metadata.approval_request.request_digest`, then record a decision:

```powershell
.\.venv\Scripts\python.exe -m baseagent --session demo --inspect-session
.\.venv\Scripts\python.exe -m baseagent --session demo --approve-tool CALL_ID --approval-digest REVIEWED_DIGEST
# Or: --deny-tool CALL_ID --approval-digest REVIEWED_DIGEST
.\.venv\Scripts\python.exe -m baseagent --session demo --resume --tool-policy policy.json --allow-write --allow-command
```

Only enable the capabilities required by your task. A decision does not execute the tool; resume is a separate operation. The decision transaction holds the session lock, requires the current pending request, and records an argument-free event. Inspection and decisions require no provider credentials. Approvals bind to the call ID, turn, exact tool name/argument string, and policy fingerprint. Each pending call is approved separately; new turns discard approvals. Changed policy invalidates old approvals and requires explicit runtime configuration acceptance. Denials produce paired `permission_denied` tool results without handler dispatch.

Core authorization runs before wrappers and again at actual terminal dispatch. Middleware redirects cannot inherit another request's approval; an unauthorized redirect receives a denial rather than creating a new approval during a running call. Middleware is trusted application code, not an adversarial sandbox. Approvals authorize a specific invocation, not unlimited tool access. They do not override file/command capabilities, content hashes, budgets, or recovery of uncertain prior side effects. Retry-safe calls may still retry within their authorized invocation under the existing bounded retry policy. Library callers pass `tool_policy=ToolPolicy(...)` to `run_agent`; use a `SessionStore` for resumable approval workflows.

Coding registries now include write/command capabilities and protected paths in their runtime contract. Enabling capabilities on an unfinished session requires `--accept-config-changes`; this is separate from approving the call. After this upgrade, old unfinished coding sessions also require that explicit compatibility acknowledgment.

## Cancellation

`--task-id TASK_ID` also targets cancellation and acknowledgment at a child in the current root turn. A node request is observed by that node and all descendants, while siblings' cancellation records remain separate. The active delegate pauses its ancestors when the cancelled child returns; it is not converted to a successful tool answer. Requesting cancellation works while the root executor owns its lock; acknowledging requires the root lock and the exact request ID. Completed/historical nodes refuse new cancellation requests. Root cancellation and node cancellation must be acknowledged independently. `SessionStore.request_cancellation(..., task_id=...)` and `clear_cancellation(..., task_id=...)` expose the same behavior in the library. Events carry the node/parent IDs; exports and backups retain the request under the node turn ID. Clearing a request alone does not execute anything or reset budget/ledger state.

SQLite schema v3 introduced durable cancellation requests, retained by the current v6 schema. A separate process can request cancellation even while an executor owns the session lock:

```powershell
.\.venv\Scripts\python.exe -m baseagent --session demo --cancel-session
.\.venv\Scripts\python.exe -m baseagent --session demo --inspect-session
# Review the ledger, then acknowledge the exact printed cancellation request ID:
.\.venv\Scripts\python.exe -m baseagent --session demo --clear-cancel REQUEST_ID
.\.venv\Scripts\python.exe -m baseagent --session demo --resume
```

Repeat cancellation requests return the same outstanding ID. The executor's checkpoints cannot erase the request because it lives in a separate table. Clearing requires the session lock and matching current turn/request ID, so it cannot acknowledge a newer request accidentally. Clearing only acknowledges cancellation; it does not resolve uncertain tool outcomes, reset budgets, or execute anything. These CLI operations require no model credentials. Exports include cancellation requests in the same SQLite snapshot; online backups preserve them, and session deletion removes them.

Core dispatch and loop boundaries check cancellation. Retry backoff waits are interruptible. Commands poll approximately every 100 ms, stop their managed process tree, and return a nonretryable `cancelled` result with bounded captured output. The result and transcript commit before the agent stops with status `cancelled`, so a normally recorded cancelled command is not replayed on resume. Partial side effects may already exist. Verification records are marked cancelled rather than passed.

Library callers can import `CancellationToken` from `baseagent.agent`, pass `cancellation=token` to `run_agent`, and call `token.cancel()` from another thread. Contextual handlers receive `context.cancellation`; call `check()` at safe boundaries or use `wait(seconds)` for interruptible waits. `Cancelled` derives from `BaseException` so registry error normalization does not swallow it. If a custom handler raises it without a recorded terminal result, its running ledger remains uncertain and requires reconciliation. Tokens supplied by a library caller are invocation-local; persistent CLI requests require explicit acknowledgment before resuming.

Custom model adapters may implement `complete_with_control(messages, tools, *, cancellation, timeout)`. Existing synchronous adapters remain compatible. The CLI's synchronous HTTP call cannot be forcibly cancelled midway: it is bounded by its existing transport timeout, records a returned response and usage, and stops before further dispatch. Arbitrary in-process handlers likewise require cooperation. Cancellation is not rollback, an OS sandbox, or proof that an external operation never happened. A model attempt cancelled without reported usage retains its unknown-use record for the existing reconciliation flow.

## Completion requirements

Use `--completion-policy completion.json` to enforce requirements before accepting a final answer:

```json
{
  "require_plan": true,
  "checks": [{"name": "unit tests", "argv": ["python", "-B", "-m", "unittest", "discover", "-s", "tests"], "paths": ["src/app.py", "tests/test_app.py"]}],
  "artifacts": ["src/app.py"]
}
```

Use the executable path appropriate to your environment. Each named check requires an actual `verify_command` record for that exact argv in the current turn. Its latest matching execution must pass with exit code zero, track every configured path, and retain matching before/after/current hashes. Older turns, legacy checks without a turn ID, fake successful wrapper results, changed files, and later failing executions cannot satisfy the requirement. Configure all source/test dependencies you need to track: the harness does not infer dependencies or prove that the chosen command tests the user's entire objective. Empty tracked paths provide no file freshness guarantee.

`require_plan` requires a nonempty plan with every step completed; it is an intent check alongside command evidence, not proof of correctness. Required artifacts must exist as readable workspace files; their SHA256 values are stored in the completion report. Artifact hashes use the existing 200 KB file bound. Existence is not a content-quality assessment, so use command assertions for content requirements. Up to 20 checks and 20 artifacts are supported; the CLI configuration is bounded to 20 KB.

Requirements are trusted harness configuration, projected into model input as a system instruction within the context budget. Rejected completion proposals append a durable harness notice and continue the same task within existing model/step/tool budgets. There is no automatic budget reset. `metadata.completion_report` records issues or accepted check IDs and artifact hashes; ordered events store only outcome/count metadata. A rejected proposal is not exposed as `final_answer`. Resume after a budget stop can perform missing work with explicitly increased limits. Accepted completed-session cache reads retain the historical completion result; they do not revalidate a changed workspace.

Library callers pass `completion_policy=CompletionPolicy(workspace, requirements)` to `run_agent`. Configuration participates in the runtime contract; changing or removing it on an unfinished session requires explicit configuration acceptance. Without a completion policy, the existing final-answer behavior remains available.

## Session memory and history summaries

Coding tools now include `remember`, `recall_memory`, `forget_memory`, `read_history`, and `set_history_summary`. They receive the active session context from the core. Notes and summaries persist with the existing SQLite state and tool-result transactions, survive new turns, and remain scoped to that session. Explicit publication uses the workspace reference API described below.

`remember` stores a named reference note with existing conversation message indices and a source digest. Updates and deletions require the current `memory_revision`, returned by `recall_memory`; this prevents silently overwriting a newer update. Notes are limited to 4 KB each, 100 entries, and 64 KB combined. Recall supports case-insensitive substring terms, ordered key pagination, and up to 20 results. Notes are retrieved explicitly rather than being injected as instructions. Their contents are agent-authored reference data, so source links do not prove the notes' semantic accuracy or current file freshness.

`read_history` reads an original JSON-encoded conversation message by its zero-based `message_index` (indices start at 1, excluding the initial system prompt), with character offsets and up to 2000 characters per page. It returns `next_offset`, total length, and a source digest. Full tool exchanges and all original message bodies remain saved even when model context is clipped or summarized. Smaller pages may be needed when a tool result is clipped by the model-input budget.

`set_history_summary` accepts bounded agent-authored text, an exclusive `through_message` boundary, and the expected memory revision. It may cover only whole completed turns before the current turn; it cannot summarize away pending calls or the active user task. An empty content string clears it. Summaries are limited to 8 KB of UTF-8 and bind to the digest of the exact original prefix. Before each model call, the core validates that source digest, replaces the covered history only in its model-input projection, and inserts the summary as assistant reference data. A changed source prefix causes fallback to the original history. The context byte budget still applies to the summary and all remaining input.

Summaries should retain goals, constraints, decisions and remaining work. Their text can be incomplete or incorrect: use `read_history` and current tools to verify details. Summary and note text is not promoted to system instructions, cannot satisfy completion-policy checks, and is omitted from metadata events. Raw session state and tool results still contain this private reference data. Do not store credentials. `forget_memory` removes the current note entry; earlier transcript/tool records can still contain its old text. Inspection/export/backup include the memory fields. These tools do not make a separate, unaccounted model request; authoring a summary uses the normal model/tool loop and its budgets.

## Workspace references across sessions

Sessions sharing the same SQLite database and canonical workspace root can use explicitly published reference notes. Session notes remain private until `publish_memory` is called. Publication and withdrawal require the separate `--allow-memory-publish` capability; file-write permission does not enable it. This capability participates in the runtime contract, so enabling it on an unfinished session requires explicit configuration acceptance. Tool policy can additionally require approval of `publish_memory` and `withdraw_memory`.

Publish an existing `remember` key with its current shared revision (`0` for a new key). The store checks the source session/workspace, persisted note snapshot, and source-message digest, then commits the shared reference and publication event together. Two concurrent publishers using the same expected revision cannot both overwrite a key. Publishing is not declared retry-safe. If the executor crashes between publication and its recorded terminal tool result, the running ledger requires conservative reconciliation; use the inspection CLI to check the shared reference before resolving it.

```powershell
.\.venv\Scripts\python.exe -m baseagent "Publish the agreed project notes" --session demo --allow-memory-publish
.\.venv\Scripts\python.exe -m baseagent --session demo --shared-memory --memory-query python
```

`search_shared_memory` and the inspection CLI return content, revision, source session/turn, message indices, digest, and source status. They support key pagination and substring terms, with at most 20 results per page. Retrieval does not automatically copy notes into the reader's private memory or promote them to instructions. `source_matches` means that the saved note and linked transcript still match; it does not establish semantic correctness or current file freshness. Changed/deleted notes or transcript sources are marked accordingly. Deleting a source session leaves the published reference with `source_missing`, so callers can see that the original history is no longer available.

`withdraw_memory` requires the current shared revision and increments it while removing the published text. A tombstone remains visible so stale revision-0 requests cannot recreate a withdrawn key. A deliberate republication uses the tombstone's current revision. Each workspace has at most 100 keys including tombstones, and 64 KB of live reference text. Old content may still remain in session transcripts, tool records, or backups. Full database backups preserve all workspace references; a session export includes current references attributed to that source session in its consistent snapshot. Workspaces/databases relocated independently do not automatically merge their knowledge namespaces.

## Offline acceptance evaluation

```powershell
.\.venv\Scripts\python.exe -m baseagent --eval-harness
.\.venv\Scripts\python.exe -m baseagent --eval-harness --eval-report .baseagent\eval-report.json
.\.venv\Scripts\python.exe -m baseagent --eval-harness --eval-case approval --eval-case recovery
```

This entry runs six repeatable end-to-end scenarios in isolated temporary workspaces:

| Scenario | Acceptance evidence |
|---|---|
| `coding` | Premature completion rejected; exact local repair preserves CRLF/unrelated bytes; actual configured Python check passes; completed cache does not dispatch |
| `approval` | No preapproval effect; approved invocation executes once across resume/cache |
| `recovery` | Interruption after a real file effect blocks resume; independently checked fixture evidence is reconciled without replay |
| `cancellation` | A real command is cancelled; verification does not pass; completed cancelled ledger result is not replayed |
| `memory` | Explicit workspace publication can be read by another session; private memory remains separate; source deletion is reported |
| `delegation` | Three-level execution reads a real file, pauses for node approval, reconciles an interrupted effect without replay, obeys the root budget, and executes root verification; direct/subtree accounting, cache/export and ephemeral delegation are checked |

Model responses and usage are scripted/synthetic; these cases evaluate harness mechanics rather than model intelligence. File operations, commands, SQLite transactions, policies, and recovery paths use the actual implementation. No provider credentials or network model calls are required, and the selected user workspace/database is not used. Temporary fixtures are removed after the run. The report records named checks, pass counts, bounded metrics, timing, Python version, and a digest of the current harness Python sources. Arbitrary exception text is omitted; a failed named check is identified. Exit code 0 means every selected case passed; 1 means a case failed or report publication failed.

Use a fresh output filename: report publication is atomic and refuses overwrites. The JSON report also prints to stdout and can be integrated into CI. Run unit/fault-injection tests separately for detailed boundary coverage; the six evaluation cases are a focused acceptance suite, and their success alone does not establish completion of every capability in `HARNESS_PLAN.md`. The delegation case reopens its SQLite store within one process; process-exit durability is verified by separate fault-injection tests. Library callers can use `baseagent.evaluation.evaluate(cases=..., report_path=...)`.

## Architecture

- `agent/agent.py`: model/tool cycle and stopping conditions; takes model, registry, and middleware as inputs.
- `agent/state.py`: transcript and explicit outcome.
- `agent/context.py`: bounded model context projection and tool-pair validation.
- `session/store.py`: SQLite checkpoints, tool ledger, session locks, and manual reconciliation.
- `session/maintenance.py`: consistent inspection/export, online backup, metadata listing, guarded deletion and cleanup.
- `session/compatibility.py`: runtime/tool contract digests and recovery compatibility checks.
- `middleware/middleware.py`: `MiddlewarePipeline` composes lifecycle hooks and `wrap_model_call` / `wrap_tool_call` interceptors. Before hooks run in registration order, after hooks run in reverse order, and the first registered wrapper is outermost. A wrapper may call its handler, return an alternate result, or retry. Avoid retries around side-effectful tools unless duplicate execution is safe.
- `tools/registry.py`: tool schemas and execution boundary.
- `middleware/retry.py`: bounded, opt-in model and safe tool retries.
- `tools/result.py`: stable result/error contract for handlers, middleware, and model tool messages.
- `tools/process.py`: bounded subprocess capture and timeout/interruption cleanup.
- `tools/workspace.py`: coding tools scoped to a workspace.
- `llm/model.py`: OpenAI-compatible model adapter.
- `llm/response.py`: model response envelope and validated token usage.
- `agent/events.py`: metadata event construction; SQLite stores complete persistent logs.
- `main.py`: CLI configuration and dependency composition.

The old customer-service examples remain in `tools/tools.py` as example functions; the coding CLI does not register them.

See `HARNESS_PLAN.md` for remaining harness capabilities and acceptance criteria.

Run local tests without a model key:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 分词器估算预算

用户允许最后一次模型请求超额时，使用 `--estimate-model`。先对最终消息和工具定义的完整 JSON 分词，再加默认 20% 和 256 tokens 余量及输出上限；该余量是工程估算，不是 DeepSeek 保证。预算不足时不发请求，返回后按实际 `usage` 结算。实际总消耗超过预算时停止后续模型和工具调用；返回的消息仍保存，增加预算后恢复不会重复这次请求。未知用量保留预留并暂停，需用现有用量核验入口恢复。根估算模式作用于整个子任务树，与严格预授权不能混用。

```powershell
uv run python examples/download_tokenizer.py
uv run baseagent "你的任务" --estimate-model --tokenizer-file .baseagent/tokenizers/deepseek-v4.json --max-total-tokens 20000 --max-completion-tokens 2048
```

本地已准备 `.baseagent/tokenizers/deepseek-v4.json`，下载脚本固定官方 ZIP 校验和，只读取 tokenizer JSON，不执行归档代码。自行提供其他 tokenizer JSON 时，需确认模型匹配；本实现对完整请求 JSON 分词，不复现服务端隐藏的聊天模板。文件内容摘要、tokenizers 版本、余量和输出上限纳入 CLI 恢复契约；恢复未完成会话时再次提供同一 `--tokenizer-file` 和参数。读取已完成的缓存答案及管理会话无需分词器或模型凭据。

可用 `--estimate-margin-percent` 和 `--estimate-fixed-margin` 调整余量。最后一次估算与实际总量差值保存在 `metadata.last_token_estimate`，其中误差包括输出预留与实际输出之间的差异，不是单独的输入计数误差。库入口使用 `Model(..., token_estimator=TokenizerEstimator(path), max_completion_tokens=...)` 和 `run_agent(..., estimate_model=True, max_total_tokens=...)`；`TokenizerEstimator` 位于 `baseagent.llm.estimation`。原有 `--preauthorize-model` 仍要求可靠上界，估算不能冒充它。

## Workspace backend 接口

文件读写、搜索、AGENTS.md 指令、文件哈希和命令执行统一由 `WorkspaceBackend` 实现。`Workspace` 保留工具权限、观察记录、编辑流程和验证记录；`LocalBackend` 承接原本机文件和子进程实现。默认 CLI 仍使用本机 backend，不提供操作系统沙箱。

```python
from baseagent.backends import LocalBackend, WorkspaceBackend
from baseagent.tools.workspace import Workspace, coding_tools

backend = LocalBackend("项目目录")
workspace = Workspace(backend=backend, allow_write=True, allow_command=True)
tools = coding_tools(workspace)
```

接口位于 `src/baseagent/backends/protocol.py`：`resolve_path`、`read_bytes`、`file_hash`、`get_instructions`、`search_files`、`write_file`、`execute` 和 `contract`。操作参数使用相对项目路径；`root`/`resolve_path` 返回稳定的项目命名空间标识，用于权限匹配和会话归属，工具层不能对这些 Path 执行宿主文件 I/O。未来远端实现需自行映射到环境内路径，并在该环境检查链接和目录边界。

backend 实现是可信扩展，负责字节读取限额、受保护路径/搜索过滤、指令范围及限额、原子文件版本/指令摘要检查、命令期限/取消/输出限额与后代清理。`write_file` 必须在目标环境内完成 CAS、锁和原子发布；不能先在宿主检查再向远端无条件写入。`execute` 接收 argv，不自行转成 shell 字符串。其 `contract()` 应返回不含凭据的实现版本、环境身份与配置，工具恢复契约会绑定此信息，环境变化时先拒绝恢复。新增 backend 的生命周期和远端环境连接管理将在接入实际执行环境时实现；本轮仅提供统一接口和本机实现。

所有指令读取、漂移检查、verify_command 和完成门槛也通过同一 backend。旧会话的工具契约因此发生变化，需要检查后显式接受配置变化才能继续。测试 `test_backends.py` 用完全不落盘的 backend 验证这些操作，没有宿主目录仍可读写和验收，同时宿主文件/搜索/进程调用被断言禁止。
