"""The opt-in recording's caps/privacy are exercised with fake clients only."""

import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.testing import CassetteModel
from scripts.record_coding_scenario import (
    MAX_CALLS,
    BoundedModel,
    RecordingStopped,
    cassette_redactor,
    main,
    scan_cassette,
)

DEFAULT_USAGE = TokenUsage(10, 5, 15)


class FakeClient:
    def __init__(self, usage=DEFAULT_USAGE):
        self.calls = 0
        self.usage = usage

    def complete(self, messages, tools):
        self.calls += 1
        return ModelResponse({"content": "synthetic"}, self.usage)


def test_hard_limit_is_per_run():
    client = FakeClient()
    model = BoundedModel(client)
    for _ in range(MAX_CALLS):
        model.complete([], [])
    for _ in range(3):
        with pytest.raises(RecordingStopped):
            model.complete([], [])
    assert model.calls == client.calls == 20
    assert model.cost_cny == Decimal("0.00036")
    model.start_run()
    model.complete([], [])
    assert model.calls == 1 and client.calls == 21


def test_budget_charged_on_response_then_stops_immediately():
    client = FakeClient(TokenUsage(1_000_000, 1_000_000, 2_000_000))
    model = BoundedModel(client, budget_cny=2, input_rate=2, output_rate=3)
    model.complete([], [])
    assert model.cost_cny == 5 and client.calls == 1
    with pytest.raises(RecordingStopped):
        model.complete([], [])
    assert client.calls == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-1"])
def test_bad_budget_or_prices_refused(value):
    for field in ("budget_cny", "input_rate", "output_rate"):
        with pytest.raises(ValueError):
            BoundedModel(FakeClient(), **{field: value})


class FakeFailure:
    def complete(self, messages, tools):
        raise ConnectionError("private diagnostic")


def test_unknown_usage_and_transport_failure_fail_closed():
    for client in (FakeClient(None), FakeFailure()):
        model = BoundedModel(client)
        for _ in range(2):
            with pytest.raises(RecordingStopped, match="recording"):
                model.complete([], [])
        assert model.calls == 1


def test_cassette_redacts_and_scans_credentials(tmp_path):
    key = "synthetic-private-credential-123"
    path = tmp_path / "fixture.jsonl"
    client = FakeClient()
    with CassetteModel(path, client, mode="record", redactor=cassette_redactor(tmp_path, key)) as model:
        model.complete([{"role": "user", "content": key}], [])
    scan_cassette(path, key)
    assert key not in path.read_text()
    assert "<SECRET>" in path.read_text()
    for credential in (key, "sk-unsafe-example", "Bearer unsafe-token"):
        path.write_text(json.dumps({"content": credential}) + "\n")
        with pytest.raises(ValueError):
            scan_cassette(path, key)
    path.write_text(json.dumps({"content": key}, ensure_ascii=True).replace("s", "\\u0073") + "\n")
    with pytest.raises(ValueError):
        scan_cassette(path, key)


def test_missing_key_is_pending_without_client(monkeypatch, capsys):
    monkeypatch.delenv("BASEAGENT_LLM_API_KEY", raising=False)
    factory = Mock(side_effect=AssertionError("must not instantiate client"))
    monkeypatch.setattr("scripts.record_coding_scenario.initialize_model", factory)
    assert main([]) == 2
    assert "pending" in capsys.readouterr().out
    factory.assert_not_called()


@pytest.mark.parametrize("runs", [2, 6])
def test_runs_range(runs):
    with pytest.raises(SystemExit):
        main(["--runs", str(runs)])


def test_three_runs_and_replay_with_fake_client(tmp_path, monkeypatch, capsys):
    from applications.project_assistant.evaluation.coding import scripted_model

    class Client:
        model_name = "fake-coding"
        max_completion_tokens = 1024

        def __init__(self):
            self.generation_params = {}
            self.calls = 0
            self.script = None

        def complete(self, messages, tools):
            if self.calls % 7 == 0:
                self.script, _ = scripted_model()
            self.calls += 1
            response = self.script.complete(messages, tools)
            for call in response.message.get("tool_calls", []):
                if call["function"]["name"] == "edit_file":
                    args = json.loads(call["function"]["arguments"])
                    args["edits"] = [{"old_text": "    return a - b", "new_text": "    return a + b"}]
                    call["function"]["arguments"] = json.dumps(args)
            return response

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    client = Client()
    key = "fake-only-private-credential"
    set_generic(monkeypatch, key)
    monkeypatch.setattr("scripts.record_coding_scenario.initialize_model", lambda *a, **kw: client)
    out = tmp_path / "out"
    reports = tmp_path / "reports"
    assert main(["--runs", "3", "--output-dir", str(out), "--report-dir", str(reports)]) == 0
    assert client.calls == 21
    assert len(list(out.glob("run_[0-9].json"))) == 3
    assert json.loads((out / "run_1.json").read_text().splitlines()[0])["scenario_policy_version"] == 3
    report = json.loads(next(reports.glob("*.json")).read_text())
    assert all(r["external_acceptance_passed"] and r["replay_consistent"] for r in report["runs"])
    assert main(["--replay", "--output-dir", str(out)]) == 0
    # Alter the expected result to prove replay detects mismatches.
    sidecar = out / "run_1.result.json"
    metadata = json.loads(sidecar.read_text())
    metadata["outcome"]["final_state"]["status"] = "tampered"
    sidecar.write_text(json.dumps(metadata))
    assert main(["--replay-dir", str(out)]) == 1
    assert key not in capsys.readouterr().out


def set_generic(monkeypatch, key="synthetic-private-credential-123"):
    monkeypatch.setenv("BASEAGENT_LLM_API_KEY", key)
    monkeypatch.setenv("BASEAGENT_LLM_BASE_URL", "https://llm-private.example.invalid/v1")
    monkeypatch.setenv("BASEAGENT_LLM_MODEL", "qwen-plus")


def test_reasoning_price_and_serialization():
    response = ModelResponse({"content": "ok"}, TokenUsage(1_000_000, 1_000_000, 2_000_000), 250_000)
    response = ModelResponse.from_dict(response.to_dict())
    client = Mock(spec=["complete"], complete=Mock(return_value=response))
    model = BoundedModel(client, budget_cny=20)
    model.complete([], [])
    assert model.cost_cny == Decimal("4.3")
    assert model.reasoning_tokens == 250_000


@pytest.mark.parametrize("text", ["llm-private", "https://llm-private.example.invalid", "Authorization: Basic abc", "sk-private-test"])
def test_scan_refuses_private_data(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"value": text}))
    with pytest.raises(ValueError):
        scan_cassette(path, base_url="https://llm-private.example.invalid/v1")


def test_endpoint_is_placeholder(tmp_path):
    endpoint = "https://llm-private.example.invalid/v1"
    redactor = cassette_redactor(tmp_path, base_url=endpoint)
    assert redactor.text(endpoint) == "<redacted-endpoint>"


@pytest.mark.parametrize("message,expected", [
    ({"tool_calls": [{"id": "ping-1", "type": "function", "function": {"name": "ping", "arguments": "{}"}}]}, True),
    ({"content": "pong"}, False),
    ({"tool_calls": [{"id": "x", "type": "function", "function": {"name": "ping", "arguments": "bad"}}]}, False),
])
def test_smoke_parsing(message, expected, capsys):
    from scripts.record_coding_scenario import smoke
    client = Mock(spec=["complete"], complete=Mock(return_value=ModelResponse(message, DEFAULT_USAGE)))
    assert smoke(client) is expected
    assert client.complete.call_count == 1
    assert len(client.complete.call_args.args[1]) == 1
    assert json.loads(capsys.readouterr().out)["usage"]["prompt_tokens"] == 10


@pytest.mark.parametrize("mode", ["budget", "transport", "limit"])
def test_failed_runs_saved_and_replayable(tmp_path, monkeypatch, mode):
    set_generic(monkeypatch)
    client = FakeClient(TokenUsage(1_000_000, 1_000_000, 2_000_000)) if mode == "budget" else FakeFailure() if mode == "transport" else FakeClient()
    client.model_name, client.generation_params, client.max_completion_tokens = "fake", {}, 1024
    from contextlib import nullcontext
    monkeypatch.setattr("scripts.record_coding_scenario.initialize_model", lambda **kw: nullcontext(client))
    out = tmp_path / "out"
    assert main(["--output-dir", str(out), "--report-dir", str(tmp_path / "reports")]) == 1
    report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text())
    assert report["runs"][0]["reason"]
    assert all(r["replay_consistent"] for r in report["runs"])
    assert report["runs"][0]["model_attempts"] == (20 if mode == "limit" else 1)
    assert len(report["runs"]) == (3 if mode == "limit" else 1)
    assert main(["--replay", "--output-dir", str(out)]) == 0


def test_budget_shared_between_runs():
    model = BoundedModel(FakeClient(TokenUsage(1_000_000, 0, 1_000_000)), budget_cny="1.5")
    model.complete([], [])
    model.start_run()
    model.complete([], [])
    assert model.cost_cny == Decimal("1.6")
    assert model.run_cost_cny == Decimal("0.8")
    model.start_run()
    with pytest.raises(RecordingStopped):
        model.complete([], [])
    assert model.client.calls == 2


def test_smoke_cli_one_call_no_recording(tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext

    set_generic(monkeypatch)
    message = {"tool_calls": [{"id": "ping-1", "type": "function", "function": {"name": "ping", "arguments": "{}"}}]}
    client = Mock(spec=["complete", "model_name", "generation_params", "max_completion_tokens"],
                  model_name="fake", generation_params={}, max_completion_tokens=64,
                  complete=Mock(return_value=ModelResponse(message, DEFAULT_USAGE)))
    monkeypatch.setattr("scripts.record_coding_scenario.initialize_model", lambda **kw: nullcontext(client))
    assert main(["--smoke", "--output-dir", str(tmp_path / "out"),
                 "--report-dir", str(tmp_path / "reports")]) == 0
    assert client.complete.call_count == 1
    assert not list(tmp_path.iterdir())
    assert '"compliant_tool_calls": true' in capsys.readouterr().out


def test_all_real_recordings_replay_offline(capsys):
    from scripts.record_coding_scenario import disconnected

    with disconnected():
        assert main(["--replay"]) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["run"] for r in reports] == [f"run_{i}" for i in range(1, 6)]
    assert all(r["replay_consistent"] and all(r["checks"].values()) for r in reports)


def test_replay_error_reports_original_mismatch(tmp_path, capsys):
    path = tmp_path / "run_1.json"
    with CassetteModel(path, FakeClient(), mode="record") as model:
        model.complete([{"role": "user", "content": "different request"}], [])
    path.with_suffix(".result.json").write_text(json.dumps({"outcome": {}}))
    assert main(["--replay-dir", str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"] == {"replay_completed": False}
    assert "CassetteMismatch: operation 0:" in report["error"]
    assert "unconsumed" not in report["error"]


def test_workspace_report_and_timing_normalize_across_recording_and_replay(tmp_path):
    from applications.project_assistant.evaluation.coding import (
        run_scenario,
        scenario_workspace,
        scripted_model,
    )
    from scripts.record_coding_scenario import outcome, replay

    with scenario_workspace() as root:
        script, _ = scripted_model()

        class Client:
            def complete(self, messages, tools):
                response = script.complete(messages, tools)
                for call in response.message.get("tool_calls", []):
                    if call["function"]["name"] == "write_file":
                        args = json.loads(call["function"]["arguments"])
                        args["content"] = f'Report: {root}/check_addition.py\n'
                        call["function"]["arguments"] = json.dumps(args)
                return response

        path = tmp_path / "run_1.json"
        with CassetteModel(path, Client(), mode="record", redactor=cassette_redactor(root)) as model:
            result, state, ledger = run_scenario(model, root)
        expected = outcome(result, state, ledger, root)
        text = path.read_text()
        assert str(root) not in text and "<WORKSPACE>" in text
    assert replay(path, details=True) == expected
    redactor = cassette_redactor(tmp_path)
    def tool(duration, root):
        return {"role": "tool", "content": json.dumps({"data": {
            "verifications": [{"duration_seconds": duration}], "stderr": f"{root}/check.py"}})}
    assert redactor.value(tool(0.01, str(tmp_path))) == redactor.value(tool(0.02, "<SECRET>"))
    # Normalization preserves command arguments and report text containing timings.
    assert redactor.value({"duration_seconds": 4}) == {"duration_seconds": 4}
