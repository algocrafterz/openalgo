"""The readiness state machine: NOT_READY, READY_WITH_WARNINGS, READY (plan D15, D19)."""

from __future__ import annotations

from pathlib import Path

import pytest

from signal_engine.backtest.data_readiness import policy as pol
from signal_engine.backtest.data_readiness import state as st
from signal_engine.backtest.data_readiness.rules import Action

POLICY = pol.load_policy(Path(__file__).resolve().parents[1] / "research" / "data_policy.yaml")


def _defects(n: int) -> list[st.Exclusion]:
    return [st.Exclusion(f"S{i}", None, "VAL-NEG-VOL", "validity", st.DEFECT) for i in range(n)]


def _assess(**kw):
    base = {"requested": 197, "blocks": [], "warnings": [], "exclusions": [],
            "eligible": None, "min_symbols": 1, "policy": POLICY}
    base.update(kw)
    if base["eligible"] is None:
        base["eligible"] = base["requested"] - len(base["exclusions"])
    return st.assess(**base)


class TestStates:
    def test_clean_is_ready(self):
        assert _assess().state == st.READY

    def test_a_block_is_not_ready(self):
        r = _assess(blocks=["SNAP-HASH"], warnings=["SURV-BIAS"])
        assert r.state == st.NOT_READY
        assert r.reasons == ("SNAP-HASH",)

    def test_a_warning_is_ready_with_warnings(self):
        assert _assess(warnings=["SURV-BIAS"]).state == st.READY_WITH_WARNINGS

    def test_an_exclusion_alone_is_ready_with_warnings(self):
        assert _assess(exclusions=_defects(1)).state == st.READY_WITH_WARNINGS

    def test_require_ready_blocks_warnings(self):
        r = st.assess(requested=10, blocks=[], warnings=["SURV-BIAS"], exclusions=[],
                      eligible=10, min_symbols=1, policy=POLICY, require_ready=True)
        assert r.state == st.NOT_READY
        assert "require_ready" in r.reasons

    def test_too_few_eligible_symbols_blocks(self):
        r = _assess(requested=5, eligible=2, min_symbols=3)
        assert r.state == st.NOT_READY
        assert "COV-MIN-SYMBOLS" in r.reasons


class TestDefectThreshold:
    """Block above 10 defect exclusions OR above 5% of requested, whichever comes first."""

    def test_197_requested_nine_defects_runs(self):
        assert _assess(requested=197, exclusions=_defects(9)).state == st.READY_WITH_WARNINGS

    def test_197_requested_tenth_defect_blocks_on_share(self):
        r = _assess(requested=197, exclusions=_defects(10))
        assert r.state == st.NOT_READY
        assert "defect_exclusion_share" in r.reasons

    def test_400_requested_tenth_defect_runs(self):
        assert _assess(requested=400, exclusions=_defects(10)).state == st.READY_WITH_WARNINGS

    def test_400_requested_eleventh_defect_blocks_on_count(self):
        r = _assess(requested=400, exclusions=_defects(11))
        assert r.state == st.NOT_READY
        assert "defect_exclusion_count" in r.reasons

    def test_eligibility_exclusions_do_not_count(self):
        elig = [st.Exclusion(f"E{i}", None, "COV-HISTORY", "coverage", st.ELIGIBILITY)
                for i in range(50)]
        r = _assess(requested=197, exclusions=elig)
        assert r.state == st.READY_WITH_WARNINGS
        assert r.defect_excluded_symbols == 0

    def test_sessions_of_one_symbol_count_once(self):
        ex = [st.Exclusion("S1", f"2026-01-0{d}", "VAL-OHLC", "validity", st.DEFECT)
              for d in range(1, 6)]
        r = _assess(requested=197, exclusions=ex)
        assert r.defect_excluded_symbols == 0
        assert r.state == st.READY_WITH_WARNINGS


class TestEscalation:
    def test_session_defect_becomes_symbol_when_not_allowed(self):
        ex = [st.Exclusion("S1", "2026-01-02", "VAL-OHLC", "validity", st.DEFECT)]
        out = st.apply_session_policy(ex, session_exclusion_allowed=False)
        assert out == [st.Exclusion("S1", None, "VAL-OHLC", "validity", st.DEFECT)]

    def test_session_defect_stays_when_allowed(self):
        ex = [st.Exclusion("S1", "2026-01-02", "VAL-OHLC", "validity", st.DEFECT)]
        assert st.apply_session_policy(ex, session_exclusion_allowed=True) == ex

    def test_escalation_deduplicates_a_symbol(self):
        ex = [st.Exclusion("S1", f"2026-01-0{d}", "VAL-OHLC", "validity", st.DEFECT)
              for d in (2, 3)]
        out = st.apply_session_policy(ex, session_exclusion_allowed=False)
        assert out == [st.Exclusion("S1", None, "VAL-OHLC", "validity", st.DEFECT)]


def test_action_constants_cover_the_plan():
    assert {a.value for a in Action} == {
        "BLOCK", "EXCLUDE_SYMBOL", "EXCLUDE_SESSION", "WARNING", "INFO"}


@pytest.mark.parametrize("state", [st.NOT_READY, st.READY_WITH_WARNINGS, st.READY])
def test_states_are_exactly_three(state):
    assert state in st.STATES and len(st.STATES) == 3


class TestReviewFindings:
    def test_nothing_requested_is_not_ready(self):
        r = st.assess(requested=0, blocks=[], warnings=[], exclusions=[], eligible=0,
                      min_symbols=0, policy=POLICY)
        assert r.state == st.NOT_READY
        assert "no_requested_symbols" in r.reasons

    def test_too_many_excluded_sessions_exclude_the_symbol(self):
        ex = [st.Exclusion("S1", f"2026-01-{d:02d}", "VAL-NEG-VOL", "validity", st.DEFECT)
              for d in range(1, 4)]
        out = st.apply_session_policy(ex, session_exclusion_allowed=True,
                                      sessions_per_symbol={"S1": 100},
                                      max_session_share=0.02)
        assert out == [st.Exclusion("S1", None, "VAL-NEG-VOL", "validity", st.DEFECT)]

    def test_few_excluded_sessions_stay_sessions(self):
        ex = [st.Exclusion("S1", "2026-01-02", "VAL-NEG-VOL", "validity", st.DEFECT)]
        out = st.apply_session_policy(ex, session_exclusion_allowed=True,
                                      sessions_per_symbol={"S1": 100},
                                      max_session_share=0.02)
        assert out == ex
