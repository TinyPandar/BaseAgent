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
