"""Intraday validation report statistics (INTRADAY-5M-DATASET-PLAN.md section 10)."""

import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import intraday_convert as ic
from signal_engine.backtest.data_readiness import intraday_report as rp


def test_wilson_lower_bound_known_values():
    assert rp.wilson_lower(99, 100) == pytest.approx(0.9455, abs=1e-4)
    assert rp.wilson_lower(0, 10) == pytest.approx(0.0, abs=1e-12)
    assert rp.wilson_lower(5, 5) < 1.0


def _sessions(statuses, year=2023):
    return pd.DataFrame({"symbol": "AAA", "status": statuses,
                         "session": pd.date_range(f"{year}-01-02", periods=len(statuses))})


def test_conversion_table_keeps_the_outcomes_apart():
    s = _sessions([ic.CONVERTED] * 6 + [ic.CONVERTED_FALLBACK] * 2 + ["excluded:volume",
                                                                      ic.UNRESOLVED])
    t = rp.conversion_table(s, s.session.dt.year).loc[2023]
    assert t.sessions == 10 and t.primary == 60 and t.fallback == 20
    assert t["excluded:volume"] == 10 and t[ic.UNRESOLVED] == 10


def test_exclusion_rate_is_per_symbol_session_and_per_year():
    s = pd.concat([_sessions([ic.CONVERTED] * 99 + ["missing"], 2022),
                   _sessions([ic.CONVERTED] * 96 + ["excluded:close"] * 4, 2023)])
    table, overall = rp.exclusion_table(s)
    assert table.loc[2022, "rate"] == 0.01 and table.loc[2022, "within_ceiling"]
    assert table.loc[2023, "rate"] == 0.04 and not table.loc[2023, "within_ceiling"]
    assert overall == pytest.approx(5 / 200)


def test_event_sessions_tag_the_ex_date_and_the_session_before():
    s = _sessions([ic.CONVERTED] * 4)
    ev = pd.DataFrame({"symbol": ["AAA"], "ex_date": ["2023-01-04"]})
    assert list(rp.event_sessions(s, ev)) == ["no event", "session before", "ex-date",
                                              "no event"]


def test_basis_class():
    s = pd.DataFrame({"k": [1.0, 0.5, 0.3, None], "price_factor": [0.5, 0.5, 0.5, 0.5]})
    assert list(rp.basis_class(s)) == ["k = 1", "k = NSE factor", "other k", "no k"]
