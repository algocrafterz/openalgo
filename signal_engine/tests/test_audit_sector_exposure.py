"""Sector and correlation exposure diagnostic: pure, report-only, deterministic."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import sector_exposure as se

SECTORS = {"AAA": "BANK", "BBB": "BANK", "CCC": "IT"}


def _frame(rows):
    return pd.DataFrame(
        rows, columns=["symbol", "entry_time", "exit_time", "r_net"]
    ).assign(
        entry_time=lambda d: pd.to_datetime(d.entry_time), exit_time=lambda d: pd.to_datetime(d.exit_time)
    )


def test_shares_concentration_and_unmapped_named():
    t = _frame(
        [
            ("AAA", "2025-01-01 09:20", "2025-01-01 10:20", 1.0),
            ("BBB", "2025-01-01 09:20", "2025-01-01 11:20", -0.5),
            ("CCC", "2025-01-01 09:20", "2025-01-01 09:50", 2.0),
            ("ZZZ", "2025-01-01 09:20", "2025-01-01 09:50", 9.0),
        ]
    )
    rep = se.build_report(t, SECTORS)
    bank = rep["by_sector"]["BANK"]
    assert bank["trades"] == 2 and bank["trade_share"] == pytest.approx(2 / 3)
    assert bank["net_r"] == pytest.approx(0.5)
    assert bank["notional_days"] == pytest.approx(3 / 24)
    assert rep["unmapped_symbols"] == ["ZZZ"]
    assert rep["unmapped_trades"] == 1
    assert rep["top_sector"] == {"sector": "BANK", "trade_share": pytest.approx(2 / 3)}
    assert rep["herfindahl"] == pytest.approx((2 / 3) ** 2 + (1 / 3) ** 2)


def test_peak_simultaneous_exit_frees_slot_at_same_instant():
    t = _frame(
        [
            ("AAA", "2025-01-01 09:20", "2025-01-01 10:00", 1.0),
            ("BBB", "2025-01-01 10:00", "2025-01-01 11:00", 1.0),
        ]
    )
    assert se.build_report(t, SECTORS)["by_sector"]["BANK"]["peak_simultaneous"] == 1
    t2 = _frame(
        [
            ("AAA", "2025-01-01 09:20", "2025-01-01 10:01", 1.0),
            ("BBB", "2025-01-01 10:00", "2025-01-01 11:00", 1.0),
        ]
    )
    assert se.build_report(t2, SECTORS)["by_sector"]["BANK"]["peak_simultaneous"] == 2


def _daily(n, seed=0):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2025-01-01", periods=n)
    a = rng.normal(size=n)
    rows = []
    for i, d in enumerate(days):
        s = d.strftime("%Y-%m-%d")
        rows.append(("AAA", f"{s} 09:20", f"{s} 10:20", a[i]))
        rows.append(("BBB", f"{s} 09:20", f"{s} 10:20", a[i] * 2))
        if i < 5:
            rows.append(("CCC", f"{s} 09:20", f"{s} 10:20", rng.normal()))
    return _frame(rows)


def test_correlation_measured_and_unmeasured_never_zero():
    rep = se.build_report(_daily(40), SECTORS, min_overlap=20)
    c = rep["correlation"]
    assert c["min_overlap"] == 20
    pairs = {p["pair"]: p for p in c["pairs"]}
    assert pairs["AAA|BBB"]["corr"] == pytest.approx(1.0)
    assert pairs["AAA|BBB"]["overlap_days"] == 40
    assert pairs["AAA|CCC"]["corr"] is None and pairs["AAA|CCC"]["status"] == "unmeasured"
    assert c["measured_pairs"] == 1 and c["unmeasured_pairs"] == 2
    assert c["average_pairwise"] == pytest.approx(1.0)


def test_average_is_none_when_nothing_measurable():
    rep = se.build_report(_daily(10), SECTORS, min_overlap=20)
    assert rep["correlation"]["average_pairwise"] is None


def test_fingerprint_deterministic_and_input_order_free():
    t = _daily(25)
    a = se.build_evidence(t, SECTORS, min_overlap=20)
    b = se.build_evidence(t.sample(frac=1.0, random_state=3), SECTORS, min_overlap=20)
    assert a["content_sha256"] == b["content_sha256"]
    assert se.validate_evidence(a) == []
    a["report"]["trades"] = 1
    assert se.validate_evidence(a)
    assert a["report_only"] is True and a["report"]["limitations"]


def test_fails_fast_on_bad_input():
    with pytest.raises(se.SectorExposureError):
        se.build_report(pd.DataFrame({"symbol": ["A"]}), SECTORS)
    with pytest.raises(se.SectorExposureError):
        se.build_report(_frame([]), SECTORS)
    bad = _frame([("AAA", "2025-01-01 09:20", None, 1.0)])
    with pytest.raises(se.SectorExposureError):
        se.build_report(bad, SECTORS)
    with pytest.raises(se.SectorExposureError):
        se.build_report(_daily(3), SECTORS, min_overlap=1)


def test_multi_mapped_symbol_first_sector_wins_and_is_listed(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("A:\n  - X\n  - Y\nB:\n  - Y\n  - Z\n")
    m, multi = se.load_sector_map(p)
    assert m == {"X": "A", "Y": "A", "Z": "B"} and multi == {"Y": ["A", "B"]}


def test_trades_to_frame_from_trade_objects():
    from signal_engine.backtest.types import Trade

    tr = Trade("AAA", "2025-01-01", 1, "t", pd.Timestamp("2025-01-01 09:20"), 100, 100, 99, 102, 1.0,
               exit_time=pd.Timestamp("2025-01-01 10:20"), r_net=0.7)
    f = se.trades_to_frame([tr])
    assert list(f.columns) == ["symbol", "entry_time", "exit_time", "r_net"]
    assert math.isclose(f.r_net[0], 0.7)
