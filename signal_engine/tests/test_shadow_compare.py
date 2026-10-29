import pandas as pd

from signal_engine.shadow import compare, store
from signal_engine.shadow.tick_bars import Tick
from signal_engine.timeutils import IST

DAY = "2026-10-29"


def _ts(hms):
    return pd.Timestamp(f"{DAY} {hms}", tz=IST)


def _hist(closes):
    idx = pd.date_range(_ts("09:15"), periods=len(closes), freq="5min")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )


def _ticks_matching(hist, close_shift=0.0):
    """Three quotes per bar that reproduce the history bar exactly, plus an optional close shift."""
    out, cum = [], 0
    for start, row in hist.iterrows():
        for offset, price in (
            (1, row.open),
            (2, row.high),
            (3, row.low),
            (4, row.close + close_shift),
        ):
            cum += 250
            arrival = start + pd.Timedelta(seconds=offset * 50)
            out.append(Tick("SBIN", arrival, arrival, price, cum, None, None))
    return out


def test_exact_ticks_pass_every_criterion():
    hist = _hist([100.0, 101.0, 102.0, 101.5])
    frame = compare.compare_symbol(_ticks_matching(hist), hist, [], tol=0.05)
    lags = pd.Series([0.1, 0.2])
    criteria = compare.evaluate({"SBIN": frame}, lags)
    assert criteria and all(c.passed for c in criteria), [c for c in criteria if not c.passed]


def test_a_wrong_close_fails_the_close_criterion_only_for_that_measure():
    hist = _hist([100.0, 101.0, 102.0, 101.5])
    frame = compare.compare_symbol(_ticks_matching(hist, close_shift=0.5), hist, [], tol=0.05)
    names = {c.name for c in compare.evaluate({"SBIN": frame}, pd.Series([0.1])) if not c.passed}
    assert "close_match" in names


def test_a_missing_bar_lowers_coverage_and_a_gap_explains_it():
    hist = _hist([100.0, 101.0, 102.0, 101.5])
    ticks = [t for t in _ticks_matching(hist) if t.arrival < _ts("09:25")]
    plain = compare.compare_symbol(ticks, hist, [], tol=0.05)
    gapped = compare.compare_symbol(ticks, hist, [(_ts("09:24"), _ts("09:31"))], tol=0.05)
    cov = {c.name: c.value for c in compare.evaluate({"S": plain}, pd.Series([0.1]))}["coverage"]
    cov_gap = {c.name: c.value for c in compare.evaluate({"S": gapped}, pd.Series([0.1]))}[
        "coverage"
    ]
    assert cov == 0.5 and cov_gap == 1.0


def test_a_tick_bar_where_history_has_none_counts_as_extra():
    hist = _hist([100.0, 101.0])
    ticks = _ticks_matching(hist) + [Tick("SBIN", _ts("15:40"), None, 100.0, 99999, None, None)]
    # 15:40 is outside the session, so it must not create a bar at all
    assert compare.compare_symbol(ticks, hist, [], tol=0.05).attrs["extra_bars"] == 0
    shifted = _hist([100.0, 101.0]).iloc[:1]
    assert (
        compare.compare_symbol(_ticks_matching(hist), shifted, [], tol=0.05).attrs["extra_bars"]
        == 1
    )


def test_tick_row_and_store_round_trip(tmp_path):
    msg = {
        "type": "market_data",
        "symbol": "SBIN",
        "data": {
            "ltp": 100.5,
            "volume": 10,
            "high": 101.0,
            "low": 99.0,
            "last_trade_time": "09:15:03",
        },
    }
    row = store.tick_row(msg, int(_ts("09:15:04").timestamp() * 1000))
    assert row[0] == "SBIN" and row[3] == 100.5 and row[2] == "09:15:03"
    assert store.tick_row({"type": "subscribe"}, 1) is None
    assert (
        store.tick_row({"type": "market_data", "symbol": "X", "data": {"ltp": 0, "volume": 1}}, 1)
        is None
    )
    conn = store.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO ticks VALUES (?,?,?,?,?,?,?)", row)
    conn.execute("INSERT INTO gaps VALUES (?,?,?)", (1000, 2000, "disconnect"))
    conn.commit()
    ticks = store.load_ticks(conn)["SBIN"]
    assert ticks[0].ltp == 100.5 and ticks[0].exchange_time == _ts("09:15:03")
    assert len(store.load_gaps(conn)) == 1
    conn.close()


def test_a_truncated_feed_exchange_time_is_ignored():
    from signal_engine.shadow.tick_bars import parse_exchange_time

    arrival = _ts("09:20")
    assert parse_exchange_time("15:", arrival) is None
    assert parse_exchange_time("15", arrival) is None


def test_a_session_with_no_usable_ticks_compares_without_error():
    hist = _hist([100.0, 101.0])
    after_close = [Tick("SBIN", _ts("15:40"), None, 100.0, 5, None, None)]
    frame = compare.compare_symbol(after_close, hist, [], tol=0.05)
    cov = {c.name: c.value for c in compare.evaluate({"S": frame}, pd.Series(dtype="float64"))}
    assert cov["coverage"] == 0.0
