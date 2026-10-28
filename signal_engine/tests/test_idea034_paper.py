"""IDEA-034 v3 forward paper recorder: ledger, refusals, data gaps, capacity and the verdict."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from signal_engine.analysis import idea034_paper as rec

IST = "Asia/Kolkata"
N_HIST = 26
START = "2026-10-01"


def _day5(date, wave=1.5, base=100.0):
    idx = pd.date_range(f"{date} 09:15", periods=75, freq="5min", tz=IST)  # 09:15 .. 15:25
    c = base + wave * np.sin(np.linspace(0, 6 * np.pi, 75))
    return pd.DataFrame(
        {"Open": c, "High": c + 0.3, "Low": c - 0.3, "Close": c, "Volume": 1000.0}, index=idx
    )


def _five(push=110.0):
    """26 balance sessions then a trade day that closes the 10:10 bar at `push`."""
    days = pd.bdate_range(START, periods=N_HIST + 1)
    parts = [_day5(d.date()) for d in days[:-1]]
    last = _day5(days[-1].date(), wave=0.0)
    if push is not None:
        ts = pd.Timestamp(f"{days[-1].date()} 10:10", tz=IST)
        last.loc[ts, ["Open", "High", "Low", "Close"]] = (push, push + 0.2, push - 0.2, push)
    return pd.concat(parts + [last]), days[-1].date()


def _one(day, rows, fill=110.0):
    idx = pd.date_range(f"{day} 09:15", f"{day} 15:29", freq="1min", tz=IST)
    df = pd.DataFrame(
        {"Open": fill, "High": fill, "Low": fill, "Close": fill, "Volume": 10.0}, index=idx
    )
    for hhmm, ohlc in rows.items():
        df.loc[pd.Timestamp(f"{day} {hhmm}", tz=IST), ["Open", "High", "Low", "Close"]] = ohlc
    return df


def _setup(symbols=("AAA",), rows=None, push=110.0, gap=()):
    five, day = _five(push)
    data = {s: five for s in symbols if s not in gap}

    def load5(syms, start, end):
        return {s: data[s] for s in syms if s in data}

    def load1(sym, d):
        return _one(d, rows or {})

    return day, load5, load1


def _record(tmp_path, day, load5, load1, symbols=("AAA",), **kw):
    return rec.record_day(
        day, ledger=tmp_path / "l.db", symbols=list(symbols), load5=load5, load1=load1, **kw
    )


def _rows(tmp_path, table="signals"):
    with sqlite3.connect(tmp_path / "l.db") as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(f"select * from {table}")]


def _edge(tmp_path):
    return _rows(tmp_path)[0]["edge"]


def test_days_before_the_holdout_are_refused(tmp_path):
    with pytest.raises(ValueError, match="hold-out"):
        rec.record_day(
            dt.date(2026, 10, 28),
            ledger=tmp_path / "l.db",
            symbols=["A"],
            load5=lambda *a: {},
            load1=lambda *a: None,
        )


def test_an_unfilled_signal_is_recorded_and_counts_zero(tmp_path):
    day, load5, load1 = _setup()
    res = _record(tmp_path, day, load5, load1)
    assert res.status == "OK" and res.signals == 1
    r = _rows(tmp_path)[0]
    assert r["status"] == "UNFILLED" and r["direction"] == 1
    assert r["net_bps"] is None and r["per_signal_bps"] == 0.0
    assert r["mkt_net_bps"] is not None  # the market counterfactual is always recorded


def test_a_filled_signal_carries_costs_on_the_market_exit_only(tmp_path):
    day, load5, _ = _setup()
    _record(tmp_path, day, load5, lambda s, d: _one(d, {}))
    edge = _edge(tmp_path)
    tmp2 = tmp_path / "b"
    tmp2.mkdir()
    lo = round(edge - 0.10, 2)
    rows = {"10:20": (edge + 0.5, edge + 0.5, lo, edge)}
    _record(tmp2, day, load5, lambda s, d: _one(d, rows, fill=edge))
    r = _rows(tmp2)[0]
    assert r["status"] == "FILLED" and r["fill_price"] == pytest.approx(
        rec.round_to_tick(edge, 0.05)
    )
    assert r["exit_reason"] == "TIME"  # flat at the edge after the fill
    assert r["net_bps"] == pytest.approx(r["gross_bps"] - r["cost_bps"])
    assert r["cost_bps"] == pytest.approx(r["stat_bps"] + 4.0)
    assert r["net_bps_stress"] == pytest.approx(r["gross_bps"] - r["stat_bps"] - 8.0)
    assert r["mkt_net_bps"] == pytest.approx(r["mkt_gross_bps"] - r["mkt_stat_bps"] - 8.0)
    assert r["per_signal_bps"] == r["net_bps"]
    assert r["stop"] < r["limit_price"] < r["target"]  # every filled trade has a stop


def test_a_day_already_recorded_is_not_overwritten_without_force(tmp_path):
    day, load5, load1 = _setup()
    _record(tmp_path, day, load5, load1)
    again = _record(tmp_path, day, load5, load1)
    assert again.status == "ALREADY_RECORDED"
    assert len(_rows(tmp_path)) == 1
    forced = _record(tmp_path, day, load5, load1, force=True)
    assert forced.status == "OK"
    assert len(_rows(tmp_path)) == 1
    assert _rows(tmp_path, "days")[0]["forced"] == 1


def test_thin_data_is_a_data_gap_row_not_silence(tmp_path):
    syms = [f"S{i}" for i in range(10)]
    day, load5, load1 = _setup(symbols=syms, gap=syms[:2])  # 80 percent coverage
    res = _record(tmp_path, day, load5, load1, symbols=syms)
    assert res.status == "DATA_GAP"
    days = _rows(tmp_path, "days")
    assert days[0]["status"] == "DATA_GAP" and days[0]["symbols_with_data"] == 8
    assert _rows(tmp_path) == []


def test_capacity_beyond_ten_open_positions_is_recorded_as_a_skip(tmp_path):
    syms = [f"S{i:02d}" for i in range(11)]
    day, load5, _ = _setup(symbols=syms)
    _record(tmp_path, day, load5, lambda s, d: _one(d, {}), symbols=syms[:1])
    edge = _edge(tmp_path)
    tmp2 = tmp_path / "c"
    tmp2.mkdir()
    rows = {"10:20": (edge + 0.5, edge + 0.5, round(edge - 0.10, 2), edge)}
    _record(tmp2, day, load5, lambda s, d: _one(d, rows, fill=edge), symbols=syms)
    got = sorted(r["status"] for r in _rows(tmp2))
    assert got.count("FILLED") == 10 and got.count("CAPACITY_SKIP") == 1


def test_the_signal_does_not_depend_on_bars_after_it(tmp_path):
    day, load5, load1 = _setup()
    _record(tmp_path, day, load5, load1)
    base = _rows(tmp_path)[0]
    five, _ = _five()
    alt = five.copy()  # rewrite every later bar and today's volume (the session must still exist)
    late = alt.index >= pd.Timestamp(f"{day} 10:15", tz=IST)
    alt.loc[late, ["Open", "High", "Low", "Close"]] = 50.0
    alt.loc[alt.index.date == day, "Volume"] = 1e9
    tmp2 = tmp_path / "d"
    tmp2.mkdir()
    rec.record_day(
        day, ledger=tmp2 / "l.db", symbols=["AAA"], load5=lambda *a: {"AAA": alt}, load1=load1
    )
    trunc = _rows(tmp2)[0]
    for k in ("signal_time", "direction", "edge", "h", "atr", "stop", "target", "limit_price"):
        assert trunc[k] == base[k], k


def _frozen(tmp_path, fingerprint):
    path = tmp_path / "FROZEN.json"
    path.write_text(json.dumps({"fingerprint": fingerprint}), encoding="utf-8")
    return path


def test_a_changed_fingerprint_refuses_to_record(tmp_path):
    with pytest.raises(rec.FrozenMismatch):
        rec.check_frozen(_frozen(tmp_path, "0" * 64))
    rec.check_frozen(_frozen(tmp_path, rec.fingerprint()))  # the current code passes


def test_the_fingerprint_covers_the_code_and_the_criteria(monkeypatch):
    base = rec.fingerprint()
    monkeypatch.setitem(rec.CRITERIA, "min_signals", 1)
    assert rec.fingerprint() != base


def test_dry_run_writes_nothing(tmp_path):
    day, load5, load1 = _setup()
    res = _record(tmp_path, day, load5, load1, dry_run=True)
    assert res.signals == 1
    assert not (tmp_path / "l.db").exists()


REF = {
    "bands": {
        "50": {"mean_p5": -11.0, "mean_p95": 19.0, "dd_p95": 700.0, "dd_p99": 900.0},
        "100": {"mean_p5": -7.5, "mean_p95": 14.0, "dd_p95": 1000.0, "dd_p99": 1300.0},
    }
}


def _m(**kw):
    m = {
        "sessions": 45,
        "data_gap_sessions": 0,
        "signals": 110,
        "fills": 45,
        "net_per_signal": 3.0,
        "net_per_fill": 7.0,
        "fill_rate": 0.41,
        "mkt_net_per_signal": -2.0,
        "max_drawdown": 500.0,
        "cum_net": 330.0,
    }
    m.update(kw)
    return m


@pytest.mark.parametrize(
    "kw,status",
    [
        ({}, "PASS"),
        ({"signals": 30, "sessions": 10, "fills": 12}, "INSUFFICIENT_SAMPLE"),
        ({"signals": 60, "fills": 25, "cum_net": 100.0}, "ON_TRACK"),
        ({"signals": 60, "fills": 25, "cum_net": -800.0}, "KILL"),
        ({"signals": 60, "fills": 25, "max_drawdown": 950.0}, "KILL"),
        ({"net_per_signal": -0.5, "cum_net": -55.0}, "FAIL"),
        ({"fill_rate": 0.60}, "FAIL"),
        ({"mkt_net_per_signal": 4.0}, "FAIL"),
        ({"data_gap_sessions": 5}, "PAUSED_DATA"),
    ],
)
def test_the_verdict_follows_the_frozen_criteria(kw, status):
    assert rec.evaluate(_m(**kw), REF)["status"] == status


def test_the_report_states_the_status_and_missed_sessions(tmp_path, monkeypatch):
    day, load5, load1 = _setup()
    _record(tmp_path, day, load5, load1)
    later = day + dt.timedelta(days=3)
    monkeypatch.setattr(rec, "_sessions_since", lambda a, b: [day, later])
    text = rec.report(ledger=tmp_path / "l.db", reference_path=rec.REFERENCE)
    assert "signals 1; filled 0" in text
    assert "STATUS INSUFFICIENT_SAMPLE" in text
    assert "IB% (information only" in text
    assert f"missed runs): ['{later}']" in text


def test_a_data_gap_day_can_be_recorded_again_without_force(tmp_path):
    syms = [f"S{i}" for i in range(10)]
    day, thin5, load1 = _setup(symbols=syms, gap=syms[:2])
    assert _record(tmp_path, day, thin5, load1, symbols=syms).status == "DATA_GAP"
    _, full5, _ = _setup(symbols=syms)
    again = _record(tmp_path, day, full5, load1, symbols=syms)
    assert again.status == "OK"
    days = _rows(tmp_path, "days")
    assert len(days) == 1 and days[0]["status"] == "OK" and days[0]["forced"] == 0


def test_the_cli_refuses_a_future_day():
    future = dt.datetime.now(rec.IST).date() + dt.timedelta(days=2)
    with pytest.raises(SystemExit):
        rec.main(["--day", str(future)])
