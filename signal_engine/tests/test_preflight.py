"""Data contract and pre-run readiness (plan Phase 4; owner refinements 2026-10-05)."""
from __future__ import annotations

import json
import os
import stat
from datetime import date
from types import SimpleNamespace

import pytest

from signal_engine.backtest.data_readiness import contract as dc
from signal_engine.backtest.data_readiness import preflight as pf
from signal_engine.backtest.data_readiness import snapshot
from signal_engine.backtest.data_readiness import state as st
from signal_engine.tests.dq_fixtures import weekdays
from signal_engine.tests.published_fixtures import calendar_for, lenient_policy, publish_fixture

DAYS = [date.fromisoformat(d) for d in weekdays("2024-01-01", "2025-12-31")]   # ~520 sessions
SYMS = ("AAA", "BBB", "CCC")


def _frozen(timeframe="1d", warmup=252, prior=20, params=None, indicators=None, universe=None):
    intent = {"timeframe": timeframe, "parameters": params or {"lookback_sessions": 252},
              "indicators": indicators or [{"name": "HIGHEST_CLOSE", "period": 252}]}
    if universe is not None:
        intent["universe"] = universe
    resolved = {"execution.warmup_bars": {"value": warmup},
                "execution.eligibility_min_prior_sessions": {"value": prior}}
    return SimpleNamespace(intent=intent, resolved=resolved)


class TestContract:
    def test_history_is_the_largest_requirement_with_provenance(self):
        c = dc.resolve_contract(_frozen(warmup=300, prior=20, params={"lookback_sessions": 252,
                                                                       "fast_period": 10}))
        assert c.history_sessions == 300
        assert c.history_requirements == {"execution.warmup_bars": 300,
                                          "execution.eligibility_min_prior_sessions": 20,
                                          "parameters.lookback_sessions": 252,
                                          "parameters.fast_period": 10,
                                          "indicators.HIGHEST_CLOSE.period": 252}

    def test_daily_contract_forbids_dropping_sessions(self):
        c = dc.resolve_contract(_frozen())
        assert c.frequency == "D" and c.session_exclusion_allowed is False

    def test_daily_loader_minimum_is_the_strategy_history_not_a_fixed_500(self):
        c = dc.resolve_contract(_frozen(warmup=252, prior=60))
        assert c.loader_min_sessions == c.history_sessions == 252
        c = dc.resolve_contract(_frozen(warmup=300, prior=60))
        assert c.loader_min_sessions == 300
        assert any("loader minimum" in n for n in c.notes)

    def test_intraday_contract(self):
        c = dc.resolve_contract(_frozen(timeframe="5m"))
        assert c.frequency == "5m" and c.session_exclusion_allowed is True

    def test_default_universe_is_the_platform_fno_list(self):
        c = dc.resolve_contract(_frozen())
        assert c.universe_name == dc.DEFAULT_UNIVERSE and len(c.universe_symbols) > 100
        assert not any(s.endswith(".NS") for s in c.universe_symbols)
        assert len(c.universe_version) == 64

    def test_a_declared_current_fno_universe_is_the_fno_list_with_its_declaration(self):
        declared = {"type": "current_fno", "exchange": "NSE", "membership_basis": "current",
                    "point_in_time": False, "survivorship_bias": True}
        c = dc.resolve_contract(_frozen(universe=declared))
        default = dc.resolve_contract(_frozen())
        assert c.universe_name == dc.DEFAULT_UNIVERSE
        assert c.universe_symbols == default.universe_symbols
        assert c.universe_version == default.universe_version
        assert "declared in the spec" in c.universe_source and "survivorship bias" in c.universe_source

    def test_an_unknown_universe_declaration_is_refused(self):
        with pytest.raises(dc.ContractError):
            dc.resolve_contract(_frozen(universe={"type": "nifty_50"}))

    def test_declared_universe_is_respected(self):
        c = dc.resolve_contract(_frozen(universe=["AAA", "BBB"]))
        assert c.universe_symbols == ("AAA", "BBB") and c.universe_name == "spec"


@pytest.fixture
def clean(tmp_path):
    return publish_fixture(tmp_path, SYMS, DAYS)


def _contract(symbols=SYMS, **kw):
    return dc.resolve_contract(_frozen(universe=list(symbols), **kw))


def _run(version, contract, policy=None, **kw):
    root, git_dir, vid, cal = version
    m = snapshot.resolve_version(vid, root=root, git_dir=git_dir)
    kw.setdefault("calendar", cal)
    return pf.run_preflight(manifest=m, root=root, git_dir=git_dir, contract=contract,
                            policy=policy or lenient_policy(), **kw)


def test_clean_dataset_is_ready(clean):
    r = _run(clean, _contract())
    assert r.state == st.READY, r.reasons
    assert r.universe["eligible"] == list(SYMS) and r.exclusions == []
    assert r.dataset["version_id"] == "nse-daily-test-001" and r.dataset["root_hash"]


def test_symbol_missing_a_session_is_excluded_under_the_daily_contract(tmp_path):
    v = publish_fixture(tmp_path, SYMS, DAYS, drop={("CCC", DAYS[300])}, accepted=("CCC",),
                        policy=lenient_policy())
    r = _run(v, _contract())
    assert r.state == st.READY_WITH_WARNINGS
    (ex,) = r.exclusions
    assert ex["symbol"] == "CCC" and ex["class"] == "defect" and "CMP-MISSING-FEW" in ex["rules"]
    assert r.universe["eligible"] == ["AAA", "BBB"]


def test_defect_exclusions_above_the_threshold_block(tmp_path):
    from signal_engine.backtest.data_readiness import policy as pol
    v = publish_fixture(tmp_path, SYMS, DAYS, drop={("CCC", DAYS[300])}, accepted=("CCC",),
                        policy=lenient_policy())
    r = _run(v, _contract(), policy=pol.load_policy())     # 1 of 3 symbols > 5%
    assert r.state == st.NOT_READY and "defect_exclusion_share" in r.reasons


def test_universe_symbol_missing_from_the_dataset_is_a_recorded_limitation(clean):
    r = _run(clean, _contract(symbols=(*SYMS, "M&M")))
    assert r.state == st.READY_WITH_WARNINGS
    assert r.universe["unavailable"] == ["M&M"] and "COV-UNAVAILABLE" in r.warnings
    assert r.universe["strategy_universe_count"] == 4 and r.universe["requested"] == list(SYMS)


def test_require_ready_turns_warnings_into_not_ready(clean):
    r = _run(clean, _contract(symbols=(*SYMS, "M&M")), require_ready=True)
    assert r.state == st.NOT_READY and "require_ready" in r.reasons


def test_short_history_is_an_eligibility_exclusion_not_a_defect(tmp_path):
    v = publish_fixture(tmp_path, SYMS, DAYS, drop={("CCC", d) for d in DAYS[:300]})
    r = _run(v, _contract())
    ex = {e["symbol"]: e for e in r.exclusions}
    assert ex["CCC"]["class"] == "eligibility" and r.state == st.READY_WITH_WARNINGS
    assert r.counts["defect_excluded"] == 0 and r.counts["eligibility_excluded"] == 1


def test_history_between_the_strategy_need_and_500_sessions_is_eligible(tmp_path):
    """A symbol with 300 sessions serves a 252-session strategy; the old fixed 500 dropped it."""
    v = publish_fixture(tmp_path, SYMS, DAYS, drop={("CCC", d) for d in DAYS[:-300]})
    r = _run(v, _contract())
    assert "CCC" in r.universe["eligible"] and r.counts["eligibility_excluded"] == 0


def test_calendar_correction_inside_the_range_is_not_ready(clean):
    changed = calendar_for([d for d in DAYS if d != DAYS[100]], version="cal-corrected")
    r = _run(clean, _contract(), calendar=changed)
    assert r.state == st.NOT_READY and "CAL-INCOMPATIBLE" in r.reasons
    assert r.calendar_check["dataset_only"] == [DAYS[100].isoformat()]


def test_calendar_update_after_the_range_does_not_invalidate(clean):
    later = [*DAYS, date(2026, 1, 2), date(2026, 1, 5)]
    r = _run(clean, _contract(), calendar=calendar_for(later, version="cal-extended"))
    assert r.state == st.READY and r.calendar_check["compatible"] is True


def test_intraday_contract_has_no_published_version(clean):
    r = _run(clean, _contract(timeframe="5m"))
    assert r.state == st.NOT_READY and "no_published_interval" in r.reasons


def test_history_longer_than_the_dataset_is_not_ready(clean):
    r = _run(clean, _contract(warmup=5000))
    assert r.state == st.NOT_READY and "insufficient_history" in r.reasons


def test_tampered_version_is_not_ready(clean):
    root, git_dir, vid, _ = clean
    obj = next((root / "objects").rglob("*.parquet"))
    os.chmod(obj, stat.S_IRUSR | stat.S_IWUSR)
    obj.write_bytes(b"x")
    r = _run(clean, _contract())
    assert r.state == st.NOT_READY and "dataset_integrity" in r.reasons


def test_record_is_written_once_and_hashes_its_content(clean, tmp_path):
    r = _run(clean, _contract(symbols=(*SYMS, "M&M")), require_ready=True)
    p1 = pf.write_record(r, tmp_path / "idea")
    p2 = pf.write_record(r, tmp_path / "idea")
    assert p1 != p2 and p1.parent.name == "preflight"
    doc = json.loads(p1.read_text())
    assert doc["state"] == st.NOT_READY and doc["sha256"] == r.sha256
    assert doc["dataset"]["version_id"] == "nse-daily-test-001"
