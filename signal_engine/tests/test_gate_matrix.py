"""The gate matrix document is generated from the code and cannot drift from it."""

from __future__ import annotations

from pathlib import Path

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import gate_matrix as gm
from signal_engine.backtest.promotion import gate_order

DOC = Path(gm.__file__).resolve().parent.parent / "docs" / "GATE-MATRIX.md"


def test_document_is_current():
    assert DOC.read_text(encoding="utf-8") == gm.render(), (
        "docs/GATE-MATRIX.md is stale: run "
        "`uv run python -m signal_engine.backtest.gate_matrix --write`"
    )


def test_every_gate_has_a_row_and_its_effects():
    rows = gm.rows()
    assert [r["gate"] for r in rows] == [
        g[0] for g in sorted(ev._GATES, key=lambda g: gate_order(g[0]))
    ]
    for r in rows:
        assert r["verdict_effect"] and r["promotion_effect"] and r["class"]


class TestEffects:
    def test_a_failed_integrity_gate_makes_the_verdict_inconclusive(self):
        assert "INCONCLUSIVE" in gm.effect_for("G0.5")["verdict_effect"]

    def test_a_hard_strategy_gate_failure_is_no_edge(self):
        assert "NO_EDGE" in gm.effect_for("G1.2")["verdict_effect"]

    def test_too_few_trades_is_inconclusive(self):
        assert "INCONCLUSIVE" in gm.effect_for("G2.1")["verdict_effect"]

    def test_paper_stage_gates_do_not_decide_the_backtest_verdict(self):
        assert gm.effect_for("G4.8")["verdict_effect"].startswith("none")
        assert "paper" in gm.effect_for("G4.8")["promotion_effect"]

    def test_diagnostic_rows_block_nothing(self):
        for gate in ("G2.7", "X.2"):
            assert gm.effect_for(gate)["verdict_effect"].startswith("none")
            assert gm.effect_for(gate)["promotion_effect"].startswith("none")

    def test_tests_are_found_by_gate_id(self):
        assert any("test_g51_gate" in t for t in gm.effect_for("G5.1")["tests"])


def test_every_named_test_file_exists():
    names = {t for r in gm.rows() for t in r["tests"]} | set(gm.ALL_GATES_TESTS)
    assert not [n for n in sorted(names) if not (gm.TESTS_DIR / n).is_file()]


def test_report_only_gates_are_real_gates():
    ids = {g[0] for g in ev._GATES}
    assert gm.REPORT_ONLY <= ids


class TestRequiredFor:
    """Each gate says what it is needed for, so a live-only check cannot be mistaken for evidence
    of edge and an optional diagnostic cannot be mistaken for a blocker."""

    def test_backtest_gates_are_needed_for_paper_entry(self):
        for gate in ("G1.2", "G2.2", "G0.5", "G5.1", "G6.4", "G6.9"):
            assert gm.effect_for(gate)["required_for"].startswith("paper entry"), gate

    def test_live_only_gates_are_marked_as_pre_live_requirements(self):
        for gate in ("G0.7", "G2.4"):
            assert gm.effect_for(gate)["required_for"].startswith("live only"), gate

    def test_paper_and_live_stage_gates(self):
        assert gm.effect_for("G7.1")["required_for"].startswith("paper stage")
        assert gm.effect_for("G6.6")["required_for"].startswith("paper stage")
        assert gm.effect_for("G9.1")["required_for"].startswith("live stage")

    def test_diagnostics_are_optional_and_block_nothing(self):
        for gate in ("G2.7", "X.1", "X.2", "X.3", "X.4"):
            assert gm.effect_for(gate)["required_for"].startswith("optional"), gate

    def test_every_gate_has_a_required_for(self):
        assert all(r["required_for"] for r in gm.rows())
