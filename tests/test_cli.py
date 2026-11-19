from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from wfo.cli import main


def test_generate_and_run_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WFO_SIM_LATENCY_SCALE", "0")
    monkeypatch.setenv("WFO_RATE_PER_S", "10000")
    monkeypatch.setenv("WFO_BURST", "10000")
    contacts = tmp_path / "c.csv"
    out = tmp_path / "e.csv"
    assert main(["generate", "--n", "60", "--out", str(contacts)]) == 0
    assert (
        main(
            [
                "run",
                "--input",
                str(contacts),
                "--output",
                str(out),
                "--db",
                str(tmp_path / "w.db"),
                "--strategy",
                "greedy",
                "--budget",
                "100",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert summary["contacts"] == 60
    assert summary["verified"] > 0
    assert summary["ledger_spent_usd"] == pytest.approx(summary["spend_usd"], abs=1e-6)
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 60
    assert {r["status"] for r in rows} == {"done"}


def test_simulate_and_report(tmp_path: Path) -> None:
    out = tmp_path / "res"
    assert (
        main(["simulate", "--contacts", "300", "--seeds", "2", "--workers", "1", "--out", str(out)])
        == 0
    )
    results = json.loads((out / "results.json").read_text())
    base = results["scenarios"]["baseline"]
    assert set(base) == {"static", "greedy", "thompson-flat", "thompson", "oracle"}
    assert "apollo_outage" in results["scenarios"]
    assert results["label"].startswith("SIMULATION")
    for name in (
        "cost_per_verified",
        "coverage_vs_cost",
        "cumulative_regret",
        "p95_latency",
        "learning_curve",
    ):
        assert (out / f"{name}.png").stat().st_size > 10_000
    report = (out / "report.md").read_text()
    assert "SIMULATION ONLY" in report
    (out / "report.md").unlink()
    assert main(["report", "--results", str(out / "results.json")]) == 0
    assert (out / "report.md").exists()
