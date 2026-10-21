"""No providers or network: real tools, Mock embedding and scripted responses."""

import json
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from applications.project_assistant.evaluation.coding import (
    ARGV,
    FIXTURE,
    REPOSITORY,
    repository_snapshot,
    run_scenario,
    scenario_workspace,
    scripted_model,
)
from baseagent.evaluation import ScriptedModel, _call
from baseagent.testing import CassetteModel
from baseagent.tools.workspace import Workspace
from scripts.record_coding_scenario import (
    cassette_redactor,
    replay,
    signature,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")
    with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), \
         patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), \
         patch("applications.project_assistant.app.OpenAIEmbedding", side_effect=AssertionError("paid embedding")), \
         patch("scripts.build_knowledge_base.OpenAIEmbedding", side_effect=AssertionError("paid embedding")), \
         patch("baseagent.main.Model", side_effect=AssertionError("paid model")):
        yield


def test_coding_success_and_fresh_disconnected_replay(tmp_path):
    before = repository_snapshot()
    cassette = tmp_path / "coding.jsonl"
    with scenario_workspace() as root:
        assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        assert not root.is_relative_to(REPOSITORY)
        # The copied test really fails before any repair, in the temp cwd.
        workspace = Workspace(root)
        assert not workspace.allow_write and not workspace.allow_command
        assert not workspace.backend.execute(ARGV, timeout=20, output_limit=2000).ok
        model, _ = scripted_model()
        with CassetteModel(cassette, model, mode="record", redactor=cassette_redactor(root)) as recording:
            result, state, ledger = run_scenario(recording, root)
        names = [json.loads(r["request_json"])["function"]["name"] for r in ledger]
        assert names.index("knowledge_search") < names.index("edit_file")
        assert json.loads(ledger[0]["result_json"])["data"]["evidence"]
        assert result["external_acceptance_passed"] and result["exact_patch"]
        assert state.status == "completed" and state.metadata["completion_report"]["passed"]
        assert result["artifact_exists"] and (root / "REPORT.md").is_file()
        assert result["model_attempts"] == 7 and result["tool_attempts"] == 6
        assert result["total_tokens"] == 105
        assert not result["unauthorized_side_effects"] and not result["duplicate_side_effects"]
        assert (root / "check_addition.py").read_bytes() == (FIXTURE / "check_addition.py").read_bytes()
    assert signature(replay(cassette)) == signature(result)
    assert repository_snapshot() == before


@pytest.mark.parametrize("options,permissions", [
    ({}, {"allow_write": False}),
    ({}, {"allow_command": False}),
    ({"repair": False}, {}),
    ({"verify": False}, {}),
    ({"plan": False}, {}),
    ({"artifact": False}, {}),
    ({"search": False}, {}),
])
def test_coding_failure_never_completed(options, permissions):
    model, count = scripted_model(**options)
    with scenario_workspace() as root:
        result, state, ledger = run_scenario(model, root, max_model_calls=count, **permissions)
        assert state.status != "completed"
        assert not result["external_acceptance_passed"]
        assert not result["completion_report"]["passed"]
        assert not result["unauthorized_side_effects"]
        if permissions or options.get("search") is False:
            assert any(r["result_json"] and (json.loads(r["result_json"]).get("error") or {}).get("code")
                       == "permission_denied" for r in ledger)
        if options.get("repair") is False:
            assert state.verifications[-1]["exit_code"] != 0
        if options.get("verify") is False:
            assert not state.verifications


def test_arbitrary_commands_and_real_repository_writes_denied():
    model = ScriptedModel([
        {"tool_calls": [_call("write_file", {"path": str(REPOSITORY / "forbidden.txt"), "content": "bad",
                                               "expected_sha256": "missing", "expected_instructions": "0" * 64}, "escape")]},
        {"tool_calls": [_call("verify_command", {"argv": ["echo", "wrong check"], "paths": []}, "wrong")]},
        {"content": "done"},
    ])
    with scenario_workspace() as root:
        result, state, ledger = run_scenario(model, root, max_model_calls=3)
        assert state.status != "completed" and not result["external_acceptance_passed"]
        assert not (REPOSITORY / "forbidden.txt").exists()
        assert all(json.loads(r["result_json"])["error"]["code"] == "permission_denied" for r in ledger)
        assert not state.verifications


def test_real_repository_cannot_be_a_scenario_workspace():
    with pytest.raises(AssertionError):
        run_scenario(ScriptedModel([]), REPOSITORY)
