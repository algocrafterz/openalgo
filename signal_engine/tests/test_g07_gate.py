"""G0.7 test quality: an attestation carrying coverage figures, enforced against the 80% floor."""

from __future__ import annotations

import copy

import pytest

from signal_engine.backtest import attestations as at
from signal_engine.backtest import evaluation as ev
from signal_engine.tests import test_g01_gate as g01
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row
from signal_engine.tests.test_g01_gate import COVERED, attest, git, verify

repo = g01.repo  # re-export the pytest fixture so tests in this module can request it


def _att(features=(900, 1000), core=(850, 1000), status="valid", **extra):
    packages = {
        "features": {"statements": features[1], "covered": features[0]},
        "strategy_core": {"statements": core[1], "covered": core[0]},
    }
    rec = {"id": "G0.7-001", "commit": "b" * 40, "reviewer": "rev", "date": "2026-10-10",
           "covered_paths": ["signal_engine/features", "signal_engine/strategy_core"], "checks": 3,
           "modes": None, "evidence": {"coverage": {"packages": packages},
                                       "process_rule": {"status": "tests-before-code is attested, not mechanically verified"}}}  # fmt: skip
    rec.update(extra)
    return {"gate": "G0.7", "status": status, "reasons": [] if status == "valid" else ["because"], "record": rec}


def _g07(att):
    return _row(_evaluate(_noisy_book(), attestation_g07=att), "G0.7")


class TestGate:
    def test_both_packages_over_the_floor_pass(self):
        row = _g07(_att())
        assert row["status"] == "pass" and row["hard"] is False and row["value"] == 85.0
        for part in ("features 90.0% (900 of 1000", "strategy_core 85.0%", "floor 80%", "G0.7-001", "not mechanically verified"):
            assert part in row["evidence"], part

    def test_exactly_eighty_percent_passes_and_just_below_fails(self):
        assert _g07(_att(features=(800, 1000), core=(800, 1000)))["status"] == "pass"
        row = _g07(_att(features=(799, 1000)))
        assert row["status"] == "fail" and row["value"] == 79.9

    def test_one_package_below_the_floor_fails_the_gate(self):
        assert _g07(_att(core=(500, 1000)))["status"] == "fail"

    def test_the_percentage_is_recomputed_from_the_counts(self):
        att = _att(features=(500, 1000))
        att["record"]["evidence"]["coverage"]["packages"]["features"]["percent"] = 99.0  # a claim, ignored
        assert _g07(att)["status"] == "fail"

    @pytest.mark.parametrize("bad", [(0, 0), (11, 10), (-1, 10)])
    def test_unusable_counts_are_not_evaluated(self, bad):
        assert _g07(_att(features=bad))["status"] == "not_evaluated"

    def test_missing_figures_for_a_package_are_not_evaluated(self):
        att = _att()
        del att["record"]["evidence"]["coverage"]["packages"]["strategy_core"]
        row = _g07(att)
        assert row["status"] == "not_evaluated" and "strategy_core" in row["evidence"]
        att = _att()
        att["record"]["evidence"] = {}
        assert _g07(att)["status"] == "not_evaluated"

    @pytest.mark.parametrize("status", ["missing", "stale", "invalid"])
    def test_an_attestation_that_is_not_valid_is_not_evaluated(self, status):
        row = _g07(_att(status=status))
        assert row["status"] == "not_evaluated" and "because" in row["evidence"] and status in row["evidence"]

    def test_no_lookup_is_not_evaluated(self):
        assert _g07(None)["status"] == "not_evaluated"

    def test_it_is_registered_and_not_evaluated_or_failed_rows_block_edge(self):
        from signal_engine.tests.test_verdict_mvp_policy import _derive, _evaluation, vd

        assert "G0.7" in ev._EVALUATORS
        for status in ("fail", "not_evaluated"):
            result = _derive(_evaluation("pass", G0_7=status))
            assert result["run_verdict"] == vd.MARGINAL and result["primary_failed_gate"] == "G0.7"


class TestWholeRepoRecord:
    def test_a_record_for_every_strategy_covers_any_strategy_class(self, repo):
        attest(repo, gate="G0.7", strategies=["*"])
        for s in ("VwapReversion", "Orb", None):
            assert at.verify("G0.7", s, git(repo, "rev-parse", "HEAD"), repo / "signal_engine", [])["status"] == "valid"

    def test_a_per_strategy_record_still_covers_only_its_strategies(self, repo):
        attest(repo, gate="G0.1", strategies=["VwapReversion"])
        assert verify(repo, strategy="Orb")["status"] == "missing"

    def test_the_wildcard_goes_stale_like_any_record(self, repo):
        attest(repo, gate="G0.7", strategies=["*"])
        p = repo / "signal_engine" / "strategy_core" / "core.py"
        p.write_text("RULE = 9\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "c2")
        assert at.verify("G0.7", "Orb", git(repo, "rev-parse", "HEAD"), repo / "signal_engine", [])["status"] == "stale"
