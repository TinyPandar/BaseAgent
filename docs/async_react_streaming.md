# Async ReAct + Streaming (Step 2)

White-box design for the event-driven loop and model streaming. Offline-first; no paid smoke in this milestone.

## Goals

- Model tokens can stream as `model_delta` events for live CLI/UI display.
- Tool execution becomes event-driven with optional parallel **read-only** tools.
- Existing ToolPolicy, EditPolicy, ledger idempotency, budget reserve, resume, and cassettes must not regress.
- Out of scope: Router/Worker, Watchdog, MCP, Docker sandbox, subscription passthrough.

## Event table

| Event | When | Data (metadata only) | Notes |
| --- | --- | --- | --- |
| `run_started` | Loop enters | model_calls, tool_calls | unchanged |
| `model_started` | Before model dispatch | attempt | unchanged |
| `model_delta` | Each streamed chunk | attempt, index, `delta_kind` (`content`\|`tool_call`), delta; for tools: tool_index, call_id?, tool_name? | **new**; `delta_kind` avoids clashing with event type; no prompt/body dumps |
| `model_returned` / `model_failed` | After assemble / error | attempt, duration… | unchanged |
| `assistant_checkpoint` | After assistant message commit | step, tool_count | unchanged |
| `tool_started` / `tool_call_started` | Tool admitted, about to run | call_id | emit **both** (alias) |
| `tool_returned` | Tool finished normalize | call_id, ok, error_code | unchanged |
| `tool_completed` / `tool_call_finished` | Ledger/commit done | call_id, ok, error_code | emit **both** (alias) |
| `turn_finished` | Turn status finalized | status, model_calls, tool_calls, total_tokens | **new**; before `run_stopped` |
| `run_stopped` | Cleanup finished | status, counters | unchanged |

Prompts, raw arguments, and tool bodies stay in state / ledger, not in event payloads.

## Old loop vs new loop

### Before (sync ReAct)

```
while True:
  if pending tool calls:
    for call in pending:          # always serial
      prepare → start → execute → commit → events
    continue
  if assistant without tools: complete; break
  model.complete(entire response) → append assistant → checkpoint
```

### After (event-driven, streaming opt-in)

```
while True:
  if pending tool calls:
    prepare ALL pending serially (policy/approval/ledger start)
    partition: cached ledger hits vs need_run
    if parallel_readonly and every need_run tool is retry_safe and count>1:
      execute need_run bodies in a thread pool
    else:
      execute serially (writes / mixed / single always serial)
    commit + emit finished events in **original call order**
    continue
  if assistant without tools: accept_completion → turn_finished; break
  model_started
  if stream enabled and adapter supports it:
    for delta in stream: emit model_delta; check cancel
    assemble final message (== non-stream shape)
  else:
    model.complete(...)   # existing path
  append assistant → model_returned → assistant_checkpoint
```

Default flags keep today's behavior: streaming off, parallel off.

## Streaming contract

- OpenAI-compatible adapter: `chat.completions.create(..., stream=True)`.
- Assemble incremental `tool_calls[i].function.arguments` by tool index; final message matches non-stream `ModelResponse`.
- Optional `complete_stream(...)` / internal helper; non-stream `complete*` APIs remain.
- Cancellation: cooperative check between chunks; on cancel → `Cancelled` / interrupted session; no tool side effects from an unfinished model turn (assistant not committed).
- Cassette: record optional `chunks` beside final response. Replay stream yields chunks then final; replay non-stream returns final only. Old cassettes without `chunks` keep working. Stream vs non-stream final tool sequence / answer / ledger must match.

## Parallel read-only preconditions

Parallelism is allowed **only** when all of:

1. Opt-in (`--parallel-readonly-tools` / `state.metadata["parallel_config"]["readonly"]=true`).
2. Every tool in the batch is `registry.retry_safe(name)` (read-only / idempotent reads).
3. No tool in the batch is awaiting approval after prepare (approval still stops the turn).
4. Prepare + ledger start + commit stay on the main thread; only handler bodies may run concurrently.
5. Harness uses `request.call_id` (not a shared `active_call_id`) inside tool execution so concurrent calls do not clobber each other.

Writes / commands / non-`retry_safe` tools always run serially through existing approval.

Resume: `prepare_tool` still returns ledger-completed results without re-executing; parallel path must not re-run completed call_ids.

## Cancel / resume sequence

```
running --Ctrl-C / cancel--> INTERRUPTED or CANCELLED
  • model mid-stream: no assistant commit → no new tool calls
  • tool mid-flight: ledger marks attempt; completed tools stay completed
resume (--resume / UI continue)
  • _pending_calls skips tools with tool role replies / completed ledger
  • unfinished tools re-enter prepare → execute (or recover path)
```

## Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--stream` | off | Enable model streaming + `model_delta` |
| `--parallel-readonly-tools` | off | Parallelize retry_safe tool batches |

## Demo (offline scripted model or after paid auth)

```bash
# Live stream tokens to stderr/stdout while following events
python -m baseagent --stream --root "$PWD" --session demo-stream \
  --follow-events 30 -- "Summarize README in one sentence"

# Parallel reads (retry_safe tools only)
python -m baseagent --parallel-readonly-tools --root "$PWD" --session demo-par \
  -- "Read README.md and pyproject.toml then compare their titles"

# Interrupt then resume
python -m baseagent --stream --root "$PWD" --session demo-resume --max-steps 8 -- "..."
# Ctrl-C, then:
python -m baseagent --root "$PWD" --session demo-resume --resume
```

Paid qwen smoke is **not** part of this offline delivery; estimate cost in the final report and do not run it.
