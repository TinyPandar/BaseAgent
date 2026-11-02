import json

from scripts.compare_coding_m1 import comparison_summary, main


def test_dry_run_validates_report_without_model(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"runs": [{"external_acceptance_passed": True, "total_tokens": 10}]}))
    monkeypatch.setattr("scripts.compare_coding_m1.initialize_model",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("model initialization")))
    assert main(["--dry-run", "--baseline-report", str(baseline), "--budget-cny", "1.5"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["runtime_config"]["spill"]["threshold_tokens"] == 2000
    assert output["runtime_config"]["guards"] == {
        "wrapup_hint_steps": 3, "repeat_call_guard": True, "dedupe_reads": True}


def test_comparison_summary_metrics():
    before = [{"external_acceptance_passed": True, "total_tokens": 100, "prompt_tokens": 80,
               "completion_tokens": 20, "cost_cny": 1},
              {"external_acceptance_passed": False, "total_tokens": 200, "prompt_tokens": 150,
               "completion_tokens": 50, "cost_cny": 3}]
    after = [{"external_acceptance_passed": True, "total_tokens": 70, "prompt_tokens": 50,
              "completion_tokens": 20, "cost_cny": 0.5},
             {"external_acceptance_passed": True, "total_tokens": 90, "prompt_tokens": 60,
              "completion_tokens": 30, "cost_cny": 1.5}]
    summary = comparison_summary(before, after)
    assert summary["before"]["success_rate"] == 0.5
    assert summary["after"]["success_rate"] == 1
    assert summary["before"]["total_tokens"] == 150
    assert summary["after"]["total_tokens"] == 80
    assert summary["total_tokens_reduction"] == 70 / 150
    assert summary["passed"] == {"success_rate": True, "total_tokens_reduction": True}


def test_finalize_cassette_stamps_policy_v3(tmp_path):
    from scripts.compare_coding_m1 import finalize_cassette
    cassette = tmp_path / "run_1.json"
    cassette.write_text(json.dumps({"version": 1, "model_name": "m"}) + "\n" + json.dumps({"request": {}}) + "\n")
    finalize_cassette(cassette, {"extra_body": {"enable_thinking": False}})
    header = json.loads(cassette.read_text().splitlines()[0])
    assert header["scenario_policy_version"] == 3
    assert header["extra_body"] == {"enable_thinking": False}
    assert header["base_url"] == "<redacted-endpoint>"


def test_live_loop_continues_after_failed_run_and_writes_report(tmp_path, monkeypatch, capsys):
    import contextlib

    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"runs": [{"external_acceptance_passed": True, "total_tokens": 10}]}))

    class Client:
        model_name, generation_params, max_completion_tokens = "fake", {}, 1024

    class Cassette:
        def __init__(self, path, *a, **k):
            self.path = path

        def close(self):
            self.path.write_text(json.dumps({"version": 1}) + "\n")

    calls = []

    def run_scenario(*a, **k):
        calls.append(1)
        raise AssertionError("boom")

    monkeypatch.setattr("scripts.compare_coding_m1.model_configuration",
                        lambda **k: {"api_key": "", "base_url": ""})
    monkeypatch.setattr("scripts.compare_coding_m1.initialize_model",
                        lambda **k: contextlib.nullcontext(Client()))
    monkeypatch.setattr("scripts.compare_coding_m1.CassetteModel", Cassette)
    monkeypatch.setattr("scripts.compare_coding_m1.run_scenario", run_scenario)
    monkeypatch.chdir(tmp_path)
    assert main(["--runs", "2", "--budget-cny", "0.1", "--baseline-report", str(baseline),
                 "--output-dir", str(tmp_path / "out"), "--report-dir", str(tmp_path / "reports")]) == 1
    assert len(calls) == 2
    for number in (1, 2):
        header = json.loads((tmp_path / "out" / f"run_{number}.json").read_text().splitlines()[0])
        assert header["scenario_policy_version"] == 3
    report = json.loads(next((tmp_path / "reports").glob("coding-m1-*.json")).read_text())
    assert [run["reason"] for run in report["runs"]] == ["AssertionError", "AssertionError"]
    assert report["partial"] is False and report["error"] is None
