"""Offline Terminal-Bench style scoring. Real tasks are not downloaded here."""

from __future__ import annotations

import json
from pathlib import Path

EVALUATION_DIR = Path(__file__).parent
SUBSET_FILE = EVALUATION_DIR / "terminal_bench_subset.json"
FIXTURE = EVALUATION_DIR / "fixtures" / "terminal_bench" / "local-echo"

FAILURES = {"sandbox_blocked", "steps_exhausted", "wrong_answer", "infrastructure"}


def load_subset(path=None):
    data = json.loads(Path(path or SUBSET_FILE).read_text(encoding="utf-8"))
    if data.get("dataset") != "terminal-bench@2.0" or not data.get("task_ids"):
        raise ValueError("subset file must name terminal-bench@2.0 task ids")
    return data


def classify(record):
    """Infrastructure failures are not model-quality failures."""
    if record.get("infrastructure"):
        return "infrastructure"
    if record.get("sandbox_blocked"):
        return "sandbox_blocked"
    if record.get("steps_exhausted"):
        return "steps_exhausted"
    if record.get("passed"):
        return "passed"
    return "wrong_answer"


def score_records(records):
    rows = []
    for record in records:
        kind = classify(record)
        if kind not in FAILURES and kind != "passed":
            raise ValueError("unknown outcome")
        rows.append({**record, "outcome": kind, "counts_as_model": kind not in {"infrastructure"}})
    model_rows = [row for row in rows if row["counts_as_model"]]
    passed = sum(row["outcome"] == "passed" for row in model_rows)
    return {"tasks": rows, "model_tasks": len(model_rows), "passed": passed,
            "success_rate": passed / len(model_rows) if model_rows else None,
            "infrastructure": sum(row["outcome"] == "infrastructure" for row in rows),
            "tokens": sum(row.get("tokens") or 0 for row in rows),
            "cost_cny": sum(row.get("cost_cny") or 0 for row in rows)}


def run_fixture(task_dir=None, *, executor):
    """Score one local fake task. executor(argv) returns a record without a model."""
    task_dir = Path(task_dir or FIXTURE)
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    if "ok" not in instruction.lower():
        raise ValueError("fixture instruction changed")
    record = executor(["sh", str(task_dir / "tests" / "test.sh")])
    record.setdefault("id", "local-echo")
    record.setdefault("tokens", 0)
    record.setdefault("cost_cny", 0)
    record.setdefault("steps", 1)
    return score_records([record])


def planned_live(budget_cny="15"):
    return {"executed": False, "provider_calls": 0, "cost_cny": 0, "status": "authorization_required",
            "budget_cny": str(budget_cny),
            "command": f"python scripts/evaluate_terminal_bench.py --live --budget-cny {budget_cny}",
            "note": "15-20 task subset is about 10-15 CNY. Full Terminal-Bench is a separate authorization. Do not run this."}
