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


def test_hard_limit_is_shared_across_runs():
    client = FakeClient()
    model = BoundedModel(client)
    for _ in range(MAX_CALLS):
        model.complete([], [])
    for _ in range(3):
        with pytest.raises(RecordingStopped):
            model.complete([], [])
    assert model.calls == client.calls == 20
    assert model.cost_cny == Decimal("0.0007")


def test_budget_charged_on_response_then_stops_immediately():
    client = FakeClient(TokenUsage(1_000_000, 1_000_000, 2_000_000))
    model = BoundedModel(client, budget_cny=2, input_rate=2, output_rate=3)
    with pytest.raises(RecordingStopped):
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


def test_unknown_usage_and_transport_failure_fail_closed():
    for client in (FakeClient(None), Mock(complete=Mock(side_effect=ConnectionError("private diagnostic")))):
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
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    factory = Mock(side_effect=AssertionError("must not instantiate client"))
    monkeypatch.setattr("scripts.record_coding_scenario.OpenAICompatModel", factory)
    assert main([]) == 2
    assert "pending" in capsys.readouterr().out
    factory.assert_not_called()


@pytest.mark.parametrize("runs", [2, 6])
def test_runs_range(runs):
    with pytest.raises(SystemExit):
        main(["--runs", str(runs)])


def test_recording_cap_across_three_real_harness_runs_with_fake_client(tmp_path, monkeypatch, capsys):
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
            return self.script.complete(messages, tools)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    client = Client()
    key = "fake-only-private-credential"
    monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    monkeypatch.setattr("scripts.record_coding_scenario.OpenAICompatModel", lambda *a, **kw: client)
    # Two complete runs (with disconnected replay) consume 14 calls; the third
    # cannot exceed the invocation's shared 20-call cap. Nothing is published.
    assert main(["--runs", "3", "--output-dir", str(tmp_path / "out")]) == 1
    assert client.calls == 20
    assert not list(tmp_path.rglob("*.jsonl"))
    output = capsys.readouterr().out
    assert "pending" in output and key not in output
