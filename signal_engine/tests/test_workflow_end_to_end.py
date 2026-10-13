"""The research workflow, end to end, on deterministic fixtures.

Idea folder -> frozen spec -> recorded run -> trial ledger -> verdict -> promotion -> assessment
-> registry. Everything lives under a pytest tmp root with synthetic trades, so it proves the
machinery without spending a real trial or touching a recorded run.

It is also the verification evidence for three readiness rows whose fixes cannot show up on an
old, immutable manifest: the calendar label (row 7), the timeframe-aware warm-up (row 9) and
strategy versions with branch labels and pointers (row 4).
"""

from __future__ import annotations

import json

import pytest
import yaml

from signal_engine.backtest import record_run as rr
from signal_engine.backtest.session import OFFICIAL_SOURCE, SOURCE, Calendar
from signal_engine.research import build_registry
from signal_engine.research.intake import trials, versions
from signal_engine.tests import test_run_artifact_write as base
from signal_engine.tests.intake_fixtures import make_spec
from signal_engine.tests.test_record_run import _execution
from signal_engine.tests.test_run_artifact_write import _folder, _manifest, _record

root = base.root  # the idea-folder fixture, shared with test_run_artifact_write


def _idea_folder(root_):
    return next((root_ / "research" / "ideas").glob("idea-009-*"))


def _with(**changes):
    return rr.Execution(**{**_execution().__dict__, **changes})


def _rewrite_spec(root_, **changes):
    folder = _idea_folder(root_)
    (folder / "spec.yaml").write_text(yaml.safe_dump(make_spec(**changes)), encoding="utf-8")


class TestCalendarLabel:
    """Readiness row 7: the manifest names the calendar the run actually used."""

    def test_official_calendar_is_named_in_the_manifest(self, root):
        calendar = Calendar((), (), OFFICIAL_SOURCE, "cal-test")
        path = _record(root, execute=lambda key: _with(calendar=calendar))
        assert "(official NSE calendar)" in _manifest(path)["session"]["exchange"]

    def test_derived_calendar_is_named_as_derived(self, root):
        calendar = Calendar((), (), SOURCE, "cal-test")
        path = _record(root, execute=lambda key: _with(calendar=calendar))
        label = _manifest(path)["session"]["exchange"]
        assert "(derived calendar)" in label
        assert "official" not in label


class TestWarmup:
    """Readiness row 9: a higher-timeframe indicator's period is converted to base bars."""

    def test_recorded_warmup_converts_a_15_minute_indicator_to_five_minute_bars(self, root):
        _rewrite_spec(
            root,
            timeframe="5m",
            indicators=[{"name": "EMA_15M", "period": 20, "source": "close"}],
        )
        path = _record(root)
        cfg = json.loads((_folder(path) / "execution" / "config.json").read_text())
        assert cfg["resolved"]["execution.warmup_bars"]["value"] == 60

    def test_same_period_on_the_base_timeframe_is_not_scaled(self, root):
        _rewrite_spec(
            root, timeframe="5m", indicators=[{"name": "EMA", "period": 20, "source": "close"}]
        )
        path = _record(root)
        cfg = json.loads((_folder(path) / "execution" / "config.json").read_text())
        assert cfg["resolved"]["execution.warmup_bars"]["value"] == 20


class TestVersionsAndTrials:
    """Readiness row 4 and the trial count: versions, labels, pointers, and honest counting."""

    def test_first_run_registers_v1_and_one_material_trial(self, root):
        _record(root)
        folder = _idea_folder(root)
        assert [v.version for v in versions.load_versions(folder)] == ["v1"]
        assert trials.count(folder).total == 1

    def test_rerunning_the_same_strategy_adds_no_version_and_no_trial(self, root):
        _record(root)
        _record(root)
        folder = _idea_folder(root)
        assert [v.version for v in versions.load_versions(folder)] == ["v1"]
        assert trials.count(folder).total == 1

    def test_a_changed_rule_is_a_new_version_a_new_trial_and_keeps_its_parent(self, root):
        _record(root)
        folder = _idea_folder(root)
        versions.set_pointer(folder, "current_version", "v1")
        _rewrite_spec(root, exit=["Close below VWAP", "Maximum hold 20 bars"])
        _record(root)
        found = versions.load_versions(folder)
        assert [(v.version, v.parent) for v in found] == [("v1", None), ("v2", "v1")]
        assert trials.count(folder).total == 2

    def test_a_branch_label_and_the_pointers_validate_in_the_registry(self, root):
        _record(root)
        folder = _idea_folder(root)
        _rewrite_spec(root, exit=["Close below VWAP", "Maximum hold 20 bars"])
        _record(root, version_label="v1b", parent_version="v1", note="tighter exit")
        assert [v.version for v in versions.load_versions(folder)] == ["v1", "v1b"]
        versions.set_pointer(folder, "current_version", "v1b")
        versions.set_pointer(folder, "promoted_version", "v1")
        idea = versions.read_pointers(folder)
        assert idea == {"current_version": "v1b", "promoted_version": "v1"}
        assert build_registry._validate_version_pointers(idea, folder) == []

    def test_a_pointer_to_an_unknown_version_is_rejected(self, root):
        _record(root)
        folder = _idea_folder(root)
        problems = build_registry._validate_version_pointers(
            {"current_version": "v9", "promoted_version": None}, folder
        )
        assert problems == ["current_version 'v9' is not a version in versions.yaml"]


class TestRunEvidenceAgrees:
    """The record, the report and the assessment tell one story."""

    def test_record_report_and_assessment_agree(self, root):
        path = _record(root)
        flat = json.loads(path.read_text(encoding="utf-8"))
        report = json.loads((_folder(path) / "report" / "report.json").read_text())
        assessment = report["assessment"]
        assert flat["run_verdict"] == report["verdict"]["run_verdict"]
        assert (
            assessment["promotion_readiness"]["backtest"]
            == report["promotion"]["stages"]["backtest"]["decision"]
            == flat["promotion_decision"]
        )
        assert assessment["research_validity"]["limitations"] == report["promotion"]["limitations"]
        # every gate the assessment calls open is a real row of the recorded evaluation
        rows = {r["gate"] for d in report["evaluation"]["dimensions"] for r in d["rows"]}
        assert {g["gate"] for g in assessment["gates"]["open"]} <= rows

    def test_a_synthetic_run_never_reaches_edge(self, root):
        """No attestation, perturbation, Monte Carlo or live-caps evidence exists for a fixture:
        those gates read as BLOCKED, never as pass, so the verdict stays below EDGE."""
        path = _record(root)
        report = json.loads((_folder(path) / "report" / "report.json").read_text())
        blocked = {
            g["gate"] for g in report["assessment"]["gates"]["open"] if g["outcome"] == "BLOCKED"
        }
        assert {"G0.1", "G0.7", "G5.1", "G5.2"} <= blocked
        assert report["verdict"]["run_verdict"] != "EDGE"
        assert report["assessment"]["strategy_evidence"]["status"] != "EDGE"


class TestRegimeUsesIndependentNiftyOnly:
    """The regime block comes from an independent NIFTY series when one is supplied. It is never
    built from the strategy's own stock basket, and its absence is a plain note, not an error."""

    @staticmethod
    def _inputs():
        import numpy as np
        import pandas as pd

        from signal_engine.backtest.regime_data import RegimeInputs

        rng = np.random.default_rng(2)
        idx = pd.bdate_range("2022-01-03", "2026-06-30")
        close = pd.Series(18000 * np.exp(np.cumsum(rng.normal(0.0003, 0.008, len(idx)))), index=idx)
        source = {
            "nifty_source": "historify NSE_INDEX NIFTY 1m",
            "external_sources": "none",
            "nifty_sha256": "a" * 64,
            "vix_source": "unavailable",
        }
        return RegimeInputs(close=close, vix=None, source=source)

    def test_an_injected_nifty_series_is_recorded_with_its_source(self, root):
        path = _record(root, regimes=self._inputs)
        flat = json.loads(path.read_text(encoding="utf-8"))
        assert flat["regimes"]["source"]["external_sources"] == "none"
        assert flat["regimes"]["source"]["vix_source"] == "unavailable"
        assert "universe" not in json.dumps(flat["regimes"]["source"]).lower()

    def test_without_a_loader_the_block_is_not_built_and_says_so(self, root):
        path = _record(root)
        flat = json.loads(path.read_text(encoding="utf-8"))
        assert "regimes" not in flat
        assert "Regime analysis not available" in flat["notes"]

    def test_a_loader_that_finds_no_nifty_is_unavailable_not_an_error(self, root):
        from signal_engine.backtest.regime_data import RegimeDataUnavailable

        def no_nifty():
            raise RegimeDataUnavailable("NIFTY has no complete sessions")

        path = _record(root, regimes=no_nifty)
        flat = json.loads(path.read_text(encoding="utf-8"))
        assert "regimes" not in flat
        assert "NIFTY has no complete sessions" in flat["notes"]

    def test_the_stock_basket_is_never_used_as_a_benchmark(self):
        import signal_engine.backtest.record_run as rr

        assert not hasattr(rr, "regime_universe")
        assert "regime_closes" not in rr.Execution.__dataclass_fields__


class TestInvalidDataIsNeverPromotable:
    """INVALID_DATA is a run status, not a verdict; whatever the gates say, it cannot advance."""

    BAD = {"status": "WARNING", "affects_validity": True, "items": ["missing sessions"]}

    def test_an_invalid_data_run_has_no_verdict_and_is_not_ready(self, root):
        path = _record(root, execute=lambda key: _with(data_quality=self.BAD))
        flat = json.loads(path.read_text(encoding="utf-8"))
        report = json.loads((_folder(path) / "report" / "report.json").read_text())
        assert report["run_status"] == "INVALID_DATA"
        assert report["verdict"] is None
        assert flat["promotion_decision"] != "READY_FOR_NEXT_STAGE"
        assert report["assessment"]["research_validity"]["status"] == "FAIL"
        assert report["assessment"]["strategy_evidence"]["status"] == "INCONCLUSIVE"


class TestPromotionWorkflowBothSides:
    """Idea -> record -> verdict -> promotion -> lifecycle, with the gate results chosen by the
    test. A promising candidate reaches paper eligibility despite live-only blockers; invalid,
    inconclusive, no-edge and integrity-blocked candidates never do."""

    @staticmethod
    def _record_with(root, monkeypatch, overrides, **kw):
        from signal_engine.backtest import evaluation
        from signal_engine.tests.test_verdict import make_eval

        monkeypatch.setattr(evaluation, "evaluate", lambda *a, **k: make_eval(**overrides))
        # a run that names its symbols has a dataset fingerprint, so it can be reproducible
        kw.setdefault("execute", lambda key: _with(symbols=["AAA.NS", "BBB.NS"]))
        return _record(root, **kw)

    @staticmethod
    def _promotable(path):
        from signal_engine.research.intake import lifecycle

        flat = json.loads(path.read_text(encoding="utf-8"))
        run, reasons = lifecycle.find_promotable_run([flat], flat["strategy_version"])
        return flat, run, reasons

    def test_a_promising_candidate_reaches_paper_despite_live_only_blockers(
        self, root, monkeypatch
    ):
        # live-only: the test-coverage attestation (G0.7) and PBO (G2.4) are not evaluated, and a
        # research limitation is open. Every strategy-quality and risk gate passes.
        path = self._record_with(
            root, monkeypatch, {"G0_7": "not_evaluated", "G2_4": "not_evaluated"}
        )
        flat, run, reasons = self._promotable(path)
        report = json.loads((_folder(path) / "report" / "report.json").read_text())
        assert flat["run_verdict"] == "MARGINAL"  # held below EDGE by the open items
        assert report["assessment"]["strategy_evidence"]["status"] == "PROMISING"
        assert flat["promotion_decision"] == "READY_FOR_NEXT_STAGE"
        assert run is not None and reasons == []
        live = report["promotion"]["stages"]["live"]
        assert live["decision"] != "READY_FOR_NEXT_STAGE"
        assert "gate G0.7 has not been run" in live["reasons"]
        assert report["promotion"]["limitations"]  # the limitation travels with the record

    def test_a_clean_run_on_a_survivor_biased_universe_is_a_qualified_edge(
        self, root, monkeypatch
    ):
        """Historical membership data is not a dependency: the survivor-biased universe
        qualifies the EDGE (visible in the record) and blocks no stage."""
        from signal_engine.research import readiness

        survivor = "Point-in-time universe membership and per-date eligibility"
        # the real readiness ledger: row 5 is its only unverified P1 row, a scoped limitation
        assert readiness.missing_p1() == [survivor] and readiness.critical_p1() == []
        path = self._record_with(
            root,
            monkeypatch,
            {},
            readiness_missing_fn=readiness.missing_p1,
            readiness_critical_fn=readiness.critical_p1,
        )
        flat, run, reasons = self._promotable(path)
        report = json.loads((_folder(path) / "report" / "report.json").read_text())
        assert flat["run_verdict"] == "EDGE"
        assert report["verdict"]["qualifications"] == [survivor]
        assert "tested universe" in report["verdict"]["reasons"][0]["text"]
        assert report["promotion"]["limitations"] == [survivor]
        assert report["assessment"]["research_validity"]["limitations"] == [survivor]
        assert flat["promotion_decision"] == "READY_FOR_NEXT_STAGE"
        assert run is not None and reasons == []

    @pytest.mark.parametrize(
        ("overrides", "verdict"),
        [
            ({"G1_2": "fail"}, "NO_EDGE"),
            ({"G4_4": "fail"}, "NO_EDGE"),
            ({"G2_1": "fail"}, "INCONCLUSIVE"),
            ({"G0_5": "fail"}, "INCONCLUSIVE"),
            ({"G6_9": "fail"}, "INCONCLUSIVE"),
            ({"G3_2": "not_evaluated"}, "MARGINAL"),
            ({"G5_1": "not_evaluated"}, "MARGINAL"),
            ({"G0_2": "not_evaluated"}, "MARGINAL"),
        ],
    )
    def test_a_disqualified_candidate_is_never_promotable(
        self, root, monkeypatch, overrides, verdict
    ):
        path = self._record_with(root, monkeypatch, overrides)
        flat, run, reasons = self._promotable(path)
        assert flat["run_verdict"] == verdict
        assert flat["promotion_decision"] != "READY_FOR_NEXT_STAGE"
        assert run is None and reasons

    def test_invalid_data_is_never_promotable(self, root, monkeypatch):
        path = self._record_with(
            root,
            monkeypatch,
            {},
            execute=lambda key: _with(
                symbols=["AAA.NS"], data_quality=TestInvalidDataIsNeverPromotable.BAD
            ),
        )
        flat, run, reasons = self._promotable(path)
        assert flat["run_status"] == "INVALID_DATA"
        assert run is None and reasons

    def test_a_dirty_tree_is_never_promotable(self, root, monkeypatch):
        dirty = {"code_commit": "a" * 40, "code_dirty": True, "dirty_paths": ["signal_engine/backtest/engine.py"]}
        path = self._record_with(root, monkeypatch, {}, git=dirty, allow_dirty=True)
        flat, run, reasons = self._promotable(path)
        assert flat["promotion_decision"] != "READY_FOR_NEXT_STAGE"
        assert run is None and reasons

    def test_an_unverified_critical_engine_capability_holds_paper(self, root, monkeypatch):
        path = self._record_with(
            root, monkeypatch, {}, readiness_critical_fn=lambda: ["Run manifest"]
        )
        flat, run, _ = self._promotable(path)
        report = json.loads((_folder(path) / "report" / "report.json").read_text())
        assert flat["promotion_decision"] == "NOT_READY"
        assert (
            "critical engine capability not verified: Run manifest"
            in (report["promotion"]["stages"]["backtest"]["reasons"])
        )
        assert run is None

    def test_an_unrelated_unfinished_row_does_not_hold_paper(self, root, monkeypatch):
        path = self._record_with(
            root, monkeypatch, {}, readiness_missing_fn=lambda: ["Regime table", "Sector exposure"]
        )
        _, run, reasons = self._promotable(path)
        assert run is not None and reasons == []
