"""Evaluation sidecars beside a sealed run: corrections, portfolio and cited diagnostics."""

import json
from pathlib import Path

from services import backtest_results_service as svc

RUN = "2026-10-05-toy-v1"


def _write(folder: Path, name: str, doc: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(doc))


def _row(status, evidence):
    return {
        "gate": "G3.3",
        "status": status,
        "value": 0.0,
        "threshold": "t",
        "evidence": evidence,
        "hard": False,
        "title": "Walk-forward",
    }


def _idea(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "ideas"
    idea = base / "idea-001-toy"
    folder = idea / "evaluations" / RUN
    _write(
        folder,
        "20261005T144832-ist-g33.json",
        {
            "kind": "corrected_gate_evaluation",
            "run_id": RUN,
            "evaluated_ist": "2026-10-05T14:48",
            "reason": "unit bug",
            "G3.3": {
                "recorded": _row("fail", "0 of 19"),
                "corrected": {
                    "status": "pass",
                    "value": 0.89,
                    "threshold": "t",
                    "evidence": "17 of 19",
                },
            },
        },
    )
    points = [
        [f"2024-{m:02d}-{d:02d}", float(m * 31 + d)] for m in range(1, 13) for d in range(1, 29)
    ]
    _write(
        folder,
        "20261014T005227-ist-portfolio.json",
        {
            "kind": "reconstructed_portfolio",
            "run_id": RUN,
            "evaluated_ist": "2026-10-14T00:52",
            "session_counts": {"recorded_days": 114, "test_sessions": 2396},
            "portfolio": {
                "capital_rs": 3e6,
                "stats": {"max_drawdown_pct": -23.0},
                "series": {
                    "equity_pct": points,
                    "drawdown_pct": [[d, -v] for d, v in points],
                    "monthly_pct": [{"month": "2024-01", "return_pct": 1.0}],
                },
            },
        },
    )
    _write(
        folder,
        "20261014T005227-ist-session-gates.json",
        {
            "kind": "corrected_gate_evaluation",
            "run_id": RUN,
            "evaluated_ist": "2026-10-14T00:52",
            "gates": {
                "G3.2": {
                    "recorded": {**_row("fail", "vs +2109.3"), "gate": "G3.2"},
                    "corrected": {
                        "status": "fail",
                        "value": 476.9,
                        "threshold": "t",
                        "evidence": "vs +1333.2",
                    },
                }
            },
        },
    )
    _write(
        folder,
        "20261005-diag.json",
        {"kind": "cited_diagnostic", "run_id": RUN, "name": "equal_weight", "reproducible": False},
    )
    _write(folder, "other-run.json", {"kind": "cited_diagnostic", "run_id": "another-run"})
    return idea, base


def test_sidecars_are_read_without_replacing_the_record(tmp_path):
    idea, base = _idea(tmp_path)
    out = svc.read_evaluations(idea, RUN, base, {"run_id": RUN})
    gates = {c["gate"]: c for c in out["corrections"]}
    assert set(gates) == {"G3.2", "G3.3"}
    assert gates["G3.3"]["recorded"]["status"] == "fail"
    assert gates["G3.3"]["corrected"]["status"] == "pass"
    assert gates["G3.3"]["changed_status"] is True
    assert gates["G3.2"]["changed_status"] is False
    assert gates["G3.3"]["file"] == "20261005T144832-ist-g33.json"
    portfolio = out["portfolio"]
    assert portfolio["source"] == "reconstructed"
    assert portfolio["session_counts"]["recorded_days"] == 114
    assert len(portfolio["equity_pct"]) <= 500
    assert portfolio["equity_pct"][0] == ["2024-01-01", 32.0]
    assert portfolio["equity_pct"][-1] == ["2024-12-28", 400.0]
    # Thinning keeps each bucket's worst drawdown, so the deepest point survives.
    assert min(v for _d, v in portfolio["drawdown_pct"]) == -400.0
    assert [d["name"] for d in out["diagnostics"]] == ["equal_weight"]


def test_a_recorded_portfolio_wins_over_a_reconstruction(tmp_path):
    idea, base = _idea(tmp_path)
    record = {"run_id": RUN, "portfolio": {"capital_rs": 1.0, "stats": {}, "series": {}}}
    out = svc.read_evaluations(idea, RUN, base, record)
    assert out["portfolio"]["source"] == "recorded"


def test_an_unavailable_recorded_portfolio_says_why(tmp_path):
    idea, base = _idea(tmp_path)
    record = {"run_id": RUN, "portfolio": {"unavailable": "no max_positions"}}
    out = svc.read_evaluations(idea, RUN, base, record)
    assert out["portfolio"] == {
        "source": "recorded",
        "unavailable": "no max_positions",
        "file": None,
        "evaluated_ist": None,
        "reason": None,
    }


def test_no_evaluations_folder_gives_empty_lists(tmp_path):
    out = svc.read_evaluations(tmp_path / "idea", RUN, tmp_path)
    assert out == {"corrections": [], "portfolio": None, "diagnostics": []}
