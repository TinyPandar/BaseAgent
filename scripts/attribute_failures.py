#!/usr/bin/env python3
"""Offline, deterministic attribution for recorded coding scenario runs (stdlib only)."""
import argparse
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("coding_scenario", "coding_scenario_v2")
CATEGORIES = ("tool_output_context_blowup", "repeated_file_reads", "verify_never_called",
              "step_limit_exhausted", "other")
PRIORITY = CATEGORIES
OUTPUT_LIMIT = 8000
CHECK_ARGV = ["/workspace/BaseAgent/.venv/bin/python", "-I", "-B", "check_addition.py"]
REJECTION = "Harness completion requirements are unmet"


def _args(call):
    try:
        return json.loads(call.get("function", {}).get("arguments", "{}"))
    except (TypeError, ValueError):
        return {}


def _tool_output(msg):
    try:
        return json.loads(msg.get("content", "{}"))
    except (TypeError, ValueError):
        return {}


def parse_cassette(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    outputs = {}
    for row in rows:
        for msg in row.get("request", {}).get("messages", []):
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                outputs[msg["tool_call_id"]] = msg.get("content", "")
    steps, all_calls = [], {}
    final_messages = rows[-1].get("request", {}).get("messages", []) if rows else []
    for row in rows:
        req = row.get("request", {})
        usage = row.get("response", {}).get("value", {}).get("usage", {})
        if not usage:
            continue
        msgs = req.get("messages", [])
        step_calls = []
        for msg in msgs:
            for call in msg.get("tool_calls", []):
                cid = call.get("id")
                if cid and cid in all_calls:
                    continue
                fn = call.get("function", {})
                out = outputs.get(cid, "")
                item = {"name": fn.get("name", ""), "arguments": _args(call),
                        "output_chars": len(out), "output": out, "call_id": cid}
                step_calls.append(item)
                if cid:
                    all_calls[cid] = item
        # Count tool output text in the prompt snapshot; this is also the basis for the final-step estimate.
        output_chars = sum(len(m.get("content", "")) for m in msgs if m.get("role") == "tool")
        answer = row.get("response", {}).get("value", {}).get("message", {}) or {}
        steps.append({"prompt_tokens": usage.get("prompt_tokens", 0),
                      "completion_tokens": usage.get("completion_tokens", 0),
                      "total_tokens": usage.get("total_tokens", 0),
                      "tool_output_chars": output_chars,
                      "new_tool_output_chars": sum(c["output_chars"] for c in step_calls),
                      "tool_calls": step_calls, "has_tool_call": bool(answer.get("tool_calls")),
                      "has_rejection": any(m.get("role") == "system" and REJECTION in str(m.get("content", "")) for m in msgs)})
    return steps, list(all_calls.values()), final_messages


def classify_run(result, calls, steps, messages=None):
    signals, seen, repeated_reads = [], set(), False
    denied, denied_tools, edits_succeeded = 0, set(), False
    verify_steps, identical, seen_calls = [], 0, set()
    for call in calls:
        name, args = call["name"], call["arguments"]
        sig = (name, json.dumps(args, sort_keys=True, separators=(",", ":")))
        if sig in seen_calls:
            identical += 1
        seen_calls.add(sig)
        try:
            out = json.loads(call.get("output", "{}"))
        except (TypeError, ValueError):
            out = {}
        if (out.get("error") or {}).get("code") == "permission_denied":
            denied += 1
            denied_tools.add(name)
        if name in {"edit_file", "write_file"} and out.get("ok"):
            if args.get("path") == "sample.py":
                edits_succeeded = True
            seen.discard(args.get("path"))
        elif name == "read_file":
            path = args.get("path")
            if path in seen:
                repeated_reads = True
            if path:
                seen.add(path)
        if name == "verify_command" or (name == "run_command" and args.get("argv") == CHECK_ARGV):
            verify_steps.append(next((i + 1 for i, s in enumerate(steps) if call in s["tool_calls"]), None))
    verified = bool(verify_steps)
    rejection_count = sum(1 for m in (messages or []) if m.get("role") == "system" and REJECTION in str(m.get("content", "")))
    # The response in a row is the answer to that row's request snapshot.
    stalled = sum(1 for s in steps if s["has_rejection"] and not s["has_tool_call"])
    if not steps and messages:
        rejected = False
        for msg in messages:
            if msg.get("role") == "system" and REJECTION in str(msg.get("content", "")):
                rejected = True
            elif rejected and msg.get("role") == "assistant" and not msg.get("tool_calls"):
                stalled += 1
    if repeated_reads:
        signals.append("repeated_file_reads")
    if not verified:
        signals.append("verify_never_called")
    if result.get("status") == "max_steps_exceeded":
        signals.append("step_limit_exhausted")
    if any(c["output_chars"] > OUTPUT_LIMIT for c in calls):
        signals.append("tool_output_context_blowup")
    prior_chars = 0
    for i, step in enumerate(steps):
        if i and prior_chars and step["prompt_tokens"] - steps[i - 1]["prompt_tokens"] > prior_chars / 4:
            signals.append("tool_output_context_blowup")
            break
        prior_chars = step["tool_output_chars"]
    signals = list(dict.fromkeys(signals))
    failed = not result.get("external_acceptance_passed", False)
    # Churn signals only take primary when they plausibly consumed steps: a repeated read,
    # a verify omission on a run with tool activity, or a large/expanding tool output.
    plausible = set()
    if repeated_reads and calls:
        plausible.add("repeated_file_reads")
    if not verified and len(calls) >= 3:
        plausible.add("verify_never_called")
    if "tool_output_context_blowup" in signals and calls:
        plausible.add("tool_output_context_blowup")
    primary = next((x for x in PRIORITY if x in (set(signals) & plausible)), None) if failed else None
    if failed and primary is None:
        primary = "step_limit_exhausted" if "step_limit_exhausted" in signals else "other"
    first_verify = min((x for x in verify_steps if x is not None), default=None)
    return {"failed": failed, "primary_category": primary, "signals": signals,
            "permission_denied_count": denied, "permission_denied_tools": sorted(denied_tools),
            "completion_rejections": rejection_count, "stalled_final_answers": stalled,
            "repeated_identical_tool_calls": identical, "first_verify_step": first_verify,
            "verify_succeeded": any((_tool_output({"content": c.get("output", "")}).get("data") or {}).get("exit_code") == 0 or
                                     _tool_output({"content": c.get("output", "")}).get("ok") is True
                                     for c in calls if c["name"] in {"verify_command", "run_command"}) if verified else False,
            "required_edit_succeeded": edits_succeeded}


def load_runs(root=ROOT):
    runs = []
    for scenario in SCENARIOS:
        for cassette in sorted((root / "tests/cassettes" / scenario).glob("run_*.json")):
            if cassette.name.endswith(".result.json"):
                continue
            result = json.loads(cassette.with_name(cassette.stem + ".result.json").read_text())
            steps, calls, messages = parse_cassette(cassette)
            header = json.loads(cassette.read_text(encoding="utf-8").splitlines()[0])
            runs.append({"scenario": scenario, "run": result.get("run", cassette.stem),
                         "status": result.get("status"), "external_acceptance_passed": result.get("external_acceptance_passed", False),
                         "policy_version": header.get("scenario_policy_version", 1),
                         "steps": steps, "tool_calls": calls, **classify_run(result, calls, steps, messages)})
    return runs


def _percentile(values, p):
    if not values:
        return None
    vals = sorted(values)
    x = (len(vals) - 1) * p
    lo, hi = math.floor(x), math.ceil(x)
    return vals[lo] + (vals[hi] - vals[lo]) * (x - lo)


def summarize(runs):
    failed = [r for r in runs if r["failed"]]
    categories = {c: {"count": sum(r["primary_category"] == c for r in failed)} for c in CATEGORIES}
    for d in categories.values():
        d["failed_pct"] = 100 * d["count"] / len(failed) if failed else 0
        d["all_pct"] = 100 * d["count"] / len(runs) if runs else 0
    max_steps = max((len(r["steps"]) for r in runs), default=0)
    per_step = []
    for i in range(max_steps):
        rows = [r["steps"][i] for r in runs if len(r["steps"]) > i]
        per_step.append({"step": i + 1, **{k: sum(x[k] for x in rows) / len(rows) for k in ("prompt_tokens", "completion_tokens", "new_tool_output_chars")}})
    budgets = []
    for r in failed:
        prod = denied_steps = stalled_steps = other_steps = 0
        for s in r["steps"]:
            if s["has_rejection"] and not s["has_tool_call"]:
                stalled_steps += 1
            elif s["tool_calls"]:
                if any((json.loads(c.get("output", "{}") or "{}").get("error") or {}).get("code") == "permission_denied" for c in s["tool_calls"]):
                    denied_steps += 1
                else:
                    prod += 1
            else:
                other_steps += 1
        budgets.append({"scenario": r["scenario"], "run": r["run"], "productive": prod,
                        "denied": denied_steps, "stalled": stalled_steps, "other": other_steps,
                        "total": len(r["steps"])})
    total_steps = sum(x["total"] for x in budgets)
    breakdown = {k: sum(x[k] for x in budgets) for k in ("productive", "denied", "stalled", "other")}
    breakdown["shares"] = {k: 100 * v / total_steps if total_steps else 0 for k, v in breakdown.items()}
    final_prompt = []
    for r in failed:
        if r["steps"]:
            s = r["steps"][-1]
            # Estimate tool-output tokens as chars/4; remainder of prompt is other messages.
            final_prompt.append((s["tool_output_chars"] / 4, s["prompt_tokens"]))
    tool_share = sum(a for a, _ in final_prompt) / sum(b for _, b in final_prompt) if final_prompt and sum(b for _, b in final_prompt) else 0
    return {"total_runs": len(runs), "failed_runs": len(failed), "categories": categories,
            "per_step": per_step, "step_budgets": budgets, "step_breakdown": breakdown,
            "final_prompt_tool_share": tool_share,
            "denied_calls": sum(r["permission_denied_count"] for r in failed),
            "denied_runs": sum(bool(r["permission_denied_count"]) for r in failed),
            "stalled_answers": sum(r["stalled_final_answers"] for r in failed),
            "repeated_calls": sum(r["repeated_identical_tool_calls"] for r in failed),
            "repeated_read_runs": sum("repeated_file_reads" in r["signals"] for r in failed)}


def render(runs):
    s = summarize(runs)
    lines = ["# M1 coding scenario failure attribution", "", f"Analyzed {s['total_runs']} runs; {s['failed_runs']} failed.", "",
             "## Deterministic rules", "", "A run fails when `external_acceptance_passed` is false. Verification is `verify_command`, or `run_command` with the fixture argv `python -I -B check_addition.py`. Repeated reads require a second `read_file` for a path before a successful edit to that path. Context blowup is a tool reply over 8,000 characters or prompt-token growth greater than cumulative preceding tool-output characters / 4. Prompt attribution estimates output tokens as chars/4.", "",
             "Primary priority is `tool_output_context_blowup` > `repeated_file_reads` > `verify_never_called` > `step_limit_exhausted` > `other`. Repeated reads require an observed duplicate path; verify omission requires tool activity; blowup requires an observed output or prompt-growth signal. These three are primary only when their run-level evidence makes consumed steps plausible. Step exhaustion is a fallback cause because it describes the limit, not why the run reached it.", "",
             "## Primary category shares", "", "| Category | Failed runs | % failed | % all |", "|---|---:|---:|---:|"]
    for cat, d in s["categories"].items():
        lines.append(f"| {cat} | {d['count']} | {d['failed_pct']:.1f}% | {d['all_pct']:.1f}% |")
    lines += ["", "## Step budget breakdown", "", "| Failed run | Productive tool steps | Denied tool steps | Stalled final answers | Initial rejected / other no-tool steps | Total recorded steps |", "|---|---:|---:|---:|---:|---:|"]
    for b in s["step_budgets"]:
        lines.append(f"| {b['scenario']} / {b['run']} | {b['productive']} | {b['denied']} | {b['stalled']} | {b['other']} | {b['total']} |")
    bd = s["step_breakdown"]
    lines += [f"| **Aggregate** | **{bd['productive']} ({bd['shares']['productive']:.1f}%)** | **{bd['denied']} ({bd['shares']['denied']:.1f}%)** | **{bd['stalled']} ({bd['shares']['stalled']:.1f}%)** | **{bd['other']} ({bd['shares']['other']:.1f}%)** | **{sum(x['total'] for x in s['step_budgets'])}** |", "",
              "Shares use total recorded steps in failed runs. A productive step contains tool calls without a permission denial; a denied step contains at least one denied call; a stalled step is a no-tool assistant turn after a completion rejection. The residual counts no-tool steps preceding the first rejection, including the answer first rejected by the harness.", "",
              "## Per-step tokens and tool output", "", "| Step | Mean prompt tokens | Mean completion tokens | Mean new tool-output chars returned |", "|---:|---:|---:|---:|"]
    for x in s["per_step"]:
        lines.append(f"| {x['step']} | {x['prompt_tokens']:.1f} | {x['completion_tokens']:.1f} | {x['new_tool_output_chars']:.1f} |")
    lines += ["", f"At failed runs' final steps, estimated prompt tokens attributable to tool outputs: **{s['final_prompt_tool_share']*100:.1f}%**; estimated as tool-output chars / 4, with the remaining prompt tokens attributed to other messages.", "",
              "## Findings", "", f"- Stalled final answers account for {bd['stalled']} of {sum(x['total'] for x in s['step_budgets'])} failed-run steps ({bd['shares']['stalled']:.1f}%). A wrap-up hint at <=3 remaining steps targets this pattern.",
              f"- {s['denied_calls']} permission-denied calls occurred across {s['denied_runs']} of {s['failed_runs']} failed runs. Identical-call detection found {s['repeated_calls']} repeats in failed runs; unchanged-file read dedupe targets the {s['repeated_read_runs']} failed run with repeated reads.",
              f"- Tool outputs account for an estimated {s['final_prompt_tool_share']*100:.1f}% of final-step failed-run prompt tokens; spilling large outputs targets this context growth.",
              "- v1 cassettes have no explicit `scenario_policy_version` header (the recorder defaults these unversioned cassettes to policy v1); v2 headers explicitly record policy v2. The v1 runs therefore capture the older stricter policy.", "",
              "## Runs", "", "| Scenario | Run | Policy | Status | Pass | Primary | Signals | Denied calls (tools) | Rejections | Stalled | Repeated calls | Verify step / ok | Edit ok | Steps |", "|---|---:|---|---|:---:|---|---|---|---:|---:|---:|---|:---:|---:|"]
    for r in runs:
        tools = ", ".join(r["permission_denied_tools"]) or "—"
        lines.append(f"| {r['scenario']} | {r['run']} | {r.get('policy_version', '—')} | {r['status']} | {'yes' if r['external_acceptance_passed'] else 'no'} | {r['primary_category'] or '—'} | {', '.join(r['signals']) or '—'} | {r['permission_denied_count']} ({tools}) | {r['completion_rejections']} | {r['stalled_final_answers']} | {r['repeated_identical_tool_calls']} | {r['first_verify_step'] or 'never'} / {'yes' if r['verify_succeeded'] else 'no'} | {'yes' if r['required_edit_succeeded'] else 'no'} | {len(r['steps'])} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="print machine-readable summary and runs")
    args = parser.parse_args()
    runs = load_runs()
    if args.json:
        print(json.dumps({"summary": summarize(runs), "runs": runs}, indent=2))
    else:
        path = ROOT / "docs/reports/m1_failure_attribution.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(runs), encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
