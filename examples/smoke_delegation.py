"""Live provider delegation check. Running this script makes billed model calls.

Uses the CLI's local .env configuration, an isolated read-only fixture, and a
fresh SQLite session. Prints check results, never provider credentials/output.
"""

import argparse
from contextlib import redirect_stdout, redirect_stderr
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

from baseagent.main import main
from baseagent.session import SessionStore


def smoke(output_directory, *, preauthorize=False):
    identifier = "delegation-smoke-" + uuid4().hex
    directory = Path(output_directory).resolve() / identifier
    directory.mkdir(parents=True, exist_ok=False)
    workspace = directory / "workspace"
    workspace.mkdir()
    marker = "delegated-" + uuid4().hex
    content = ("This file is a controlled delegation fixture.\nmarker=" + marker + "\n").encode("utf-8")
    (workspace / "evidence.txt").write_bytes(content)
    configuration = directory / "subtasks.json"
    configuration.write_text(json.dumps({"max_depth": 2, "max_nodes": 2, "tasks": [{
        "name": "reader", "tools": ["read_file"],
        "system_prompt": "Use read_file to read evidence.txt. Return the exact marker value from the file. Do not guess it.",
        "max_steps": 4, "max_model_calls": 4, "max_tool_calls": 4,
        "max_total_tokens": 1200000 if preauthorize else 20000, "max_duration_seconds": 90,
    }]}), encoding="utf-8")
    database = directory / "sessions.sqlite3"
    options = ["--root", str(workspace), "--db", str(database), "--session", identifier,
               "--subtasks-config", str(configuration)]
    if preauthorize:
        options += ["--preauthorize-model", "--request-bound-profile", "deepseek-context-v1", "--max-completion-tokens", "4096"]
    prompt = "Call run_subtask exactly once with name reader to read evidence.txt and report its marker. Do not read the file yourself. Then return that exact marker as your final answer."
    command = [sys.executable, "-m", "baseagent", prompt, *options,
               "--max-model-calls", "8", "--max-tool-calls", "8",
               "--max-total-tokens", "1200000" if preauthorize else "50000", "--max-duration-seconds", "120"]
    process = subprocess.run(command, capture_output=True, timeout=180)
    store = SessionStore(database)
    state = store.load(identifier)
    report = {"format": "baseagent.live_delegation", "version": 1, "session_id": identifier,
              "command": command, "model_kind": "configured_live_provider", "usage_kind": "provider_reported",
              "cli_exit_code": process.returncode, "checks": {}, "database": str(database)}
    checks = report["checks"]
    checks["root_completed"] = state is not None and state.status == "completed" and process.returncode == 0
    if state is not None:
        tree = store.task_tree(identifier)
        nodes = tree["nodes"]
        calls = store.calls(identifier, state.turn_id)
        children = [node["state"] for node in nodes]
        reads = [record for node in nodes for record in node["tool_calls"]
                 if json.loads(record["request_json"])["function"]["name"] == "read_file"]
        checks["one_completed_reader_node"] = len(nodes) == 1 and nodes[0]["name"] == "reader" and children[0]["status"] == "completed"
        checks["root_only_delegated"] = len(calls) == 1 and json.loads(calls[0]["request_json"])["function"]["name"] == "run_subtask" and calls[0]["status"] == "completed" and calls[0]["attempts"] == 1
        checks["child_actual_read_matches_fixture"] = bool(reads) and any(
            record["status"] == "completed" and json.loads(record["result_json"])["data"].get("content") == content.decode("utf-8")
            for record in reads if record["result_json"] and json.loads(record["result_json"])["ok"])
        checks["final_answer_contains_observed_marker"] = marker in (state.final_answer or "")
        checks["provider_usage_known_for_whole_tree"] = state.total_tokens > 0 and state.unknown_usage_calls == 0 and all(child["unknown_usage_calls"] == 0 for child in children)
        values = [state.to_dict(), *children]
        checks["direct_usage_reconstructs_root_totals"] = all(
            sum(value["metadata"].get("direct_usage", {}).get(counter, -1) for value in values) == getattr(state, counter)
            for counter in ("model_calls", "tool_calls", "prompt_tokens", "completion_tokens", "total_tokens"))
        replies = [message for message in state.messages if message["role"] == "tool"]
        checks["single_paired_result_delivery"] = len(replies) == 1 and len(calls) == 1 and replies[0]["tool_call_id"] == calls[0]["call_id"]
        checks["fixture_bytes_unchanged"] = (workspace / "evidence.txt").read_bytes() == content
        if preauthorize:
            starts = [value for value in store.events(identifier) if value["type"] == "model_started"]
            checks["every_tree_request_has_capacity_reservation"] = len(starts) == state.model_calls and all(value["data"].get("reserved_tokens") == 1048576 for value in starts)
            checks["known_usage_releases_all_tree_reservations"] = not state.model_reservations and all(not child["model_reservations"] for child in children)
            checks["provider_usage_within_reserved_total_and_output_cap"] = all(not value["metadata"].get("request_bound_violation") for value in values) and all(value["total_tokens"] > 0 for value in values)
        before = state.to_dict()
        # A completed CLI resume must succeed even if both adapter construction
        # and any network connection are forbidden by the host process.
        checks["cached_cli_resume_without_adapter_or_network"] = False
        if state.status == "completed":
            saved_argv = sys.argv
            try:
                with patch("baseagent.main.Model", side_effect=AssertionError("unexpected adapter construction")), patch("socket.socket.connect", side_effect=AssertionError("unexpected network connection")), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    sys.argv = ["baseagent", "--resume", *options]
                    cached_exit = main()
                checks["cached_cli_resume_without_adapter_or_network"] = cached_exit == 0 and store.load(identifier).to_dict() == before
            finally:
                sys.argv = saved_argv
        report["metrics"] = {counter: getattr(state, counter) for counter in ("model_calls", "tool_calls", "total_tokens", "unknown_usage_calls")}
        report["nodes"] = [{"task_id": node["task_id"], "name": node["name"], "status": node["state"]["status"], "model_calls": node["state"]["model_calls"], "tool_calls": node["state"]["tool_calls"], "total_tokens": node["state"]["total_tokens"]} for node in nodes]
        report["runtime_contract"] = state.metadata.get("runtime_contract")
    report["fixture_sha256"] = sha256(content).hexdigest()
    report["passed"] = bool(checks) and all(checks.values())
    target = directory / "report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), "passed": report["passed"], "checks": checks, "metrics": report.get("metrics")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=Path(".baseagent"))
    parser.add_argument("--preauthorize", action="store_true", help="Use the official DeepSeek capacity bound and output cap")
    arguments = parser.parse_args()
    raise SystemExit(smoke(arguments.output_directory, preauthorize=arguments.preauthorize))
