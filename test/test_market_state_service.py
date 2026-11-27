"""Tests for services/market_state_service.py (NIFTY daily market state, read-only analytics)."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from services import market_state_service as ms


def _frame(closes, start=date(2021, 1, 4), volume=1000, spread=1.0, skip_weekends=True):
    days, d = [], start
    while len(days) < len(closes):
        if not skip_weekends or d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "date": days,
            "open": c,
            "high": c + spread,
            "low": c - spread,
            "close": c,
            "volume": volume,
        }
    )


def _random_walk(n, seed=1, start=10000.0):
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(0, 0.01, n)))


def _states(closes, **kw):
    return ms.MarketStates(_frame(closes, **kw))


# ---------------------------------------------------------------- trend


def test_rising_series_is_uptrend_and_falling_is_downtrend():
    up = _states(np.linspace(100, 300, 120))
    down = _states(np.linspace(300, 100, 120))
    assert up.trend[-1] == "Uptrend"
    assert down.trend[-1] == "Downtrend"


def test_flat_series_is_range_by_equality():
    flat = _states([100.0] * 120)
    assert flat.trend[-1] == "Range"  # close == SMA20 == SMA50: strict comparisons fail


def test_trend_needs_the_slope_condition():
    # close above both averages but SMA20 falling: Range, not Uptrend.
    closes = list(np.linspace(100, 200, 100)) + list(np.linspace(199, 170, 10)) + [190.0] * 3
    s = _states(closes)
    i = len(closes) - 1
    c = pd.Series(closes)
    s20, s50 = c.rolling(20).mean(), c.rolling(50).mean()
    assert c[i] > s20[i] and c[i] > s50[i] and s20[i] < s20[i - 5]
    assert s.trend[i] == "Range"


def test_trend_needs_min_bars():
    closes = np.linspace(100, 300, ms.MIN_BARS_TREND)
    s = _states(closes)
    assert s.trend[-2] is None and s.trend[0] is None
    assert s.trend[-1] == "Uptrend"
    assert all(t is None for t in _states(closes[:-1]).trend)


def test_volatility_needs_min_bars():
    n = ms.MIN_BARS_VOLATILITY
    s = _states(_random_walk(n))
    assert s.volatility[: n - 1] == [None] * (n - 1)
    assert s.volatility[-1] in ("Low", "Normal", "High")
    assert all(v is None for v in _states(_random_walk(n - 1)).volatility)


# ---------------------------------------------------------------- volatility


def test_atr_equals_signal_engine_atr():
    from signal_engine.features.price import atr as engine_atr

    frame = _frame(_random_walk(200, seed=7), spread=2.5)
    mine = ms.atr_series(frame["high"], frame["low"], frame["close"])
    theirs = engine_atr(
        pd.DataFrame({"High": frame["high"], "Low": frame["low"], "Close": frame["close"]}), 14
    )
    assert np.allclose(mine.to_numpy(), theirs.to_numpy(), rtol=0, atol=1e-12)


def test_volatility_percentile_definition_and_thresholds():
    n = ms.MIN_BARS_VOLATILITY + 10
    frame = _frame(_random_walk(n, seed=3), spread=1.0)
    pct = ms.volatility_percentiles(frame)
    ratio = (ms.atr_series(frame["high"], frame["low"], frame["close"]) / frame["close"]).to_numpy()
    for i in (ms.MIN_BARS_VOLATILITY - 1, n - 1, n - 5):
        window = ratio[i - ms.PERCENTILE_WINDOW + 1 : i + 1]
        assert pct[i] == pytest.approx(sum(1 for x in window if x <= window[-1]) / len(window))
    assert np.isnan(pct[ms.MIN_BARS_VOLATILITY - 2])
    assert ms.volatility_label(0.25) == "Low"
    assert ms.volatility_label(0.2501) == "Normal"
    assert ms.volatility_label(0.7499) == "Normal"
    assert ms.volatility_label(0.75) == "High"
    assert ms.volatility_label(float("nan")) is None


def test_a_volatility_spike_is_high_and_a_calm_stretch_is_low():
    n = ms.MIN_BARS_VOLATILITY + 30
    wide = np.full(n, 100.0)
    frame = _frame(wide, spread=1.0)
    frame.loc[n - 1, ["high", "low"]] = [130.0, 70.0]
    assert ms.MarketStates(frame).volatility[-1] == "High"
    frame = _frame(wide, spread=5.0)
    frame["high"] = frame["close"] + np.linspace(5, 0.5, n)  # ATR/close falling to its low
    frame["low"] = frame["close"] - np.linspace(5, 0.5, n)
    assert ms.MarketStates(frame).volatility[-1] == "Low"


# ---------------------------------------------------------------- cleaning


def test_provisional_zero_volume_last_bar_is_dropped():
    frame = _frame(np.linspace(100, 200, 80))
    frame.loc[len(frame) - 1, "volume"] = 0
    states = ms.MarketStates(frame)
    assert len(states.dates) == 79
    assert frame["date"].iloc[-1] not in states.dates
    assert states.dropped_bars == 1


def test_bad_ohlc_rows_are_dropped_not_filled():
    frame = _frame(np.linspace(100, 200, 80))
    frame.loc[10, "close"] = np.nan
    frame.loc[20, "low"] = 0
    frame.loc[30, "open"] = -5
    states = ms.MarketStates(frame)
    assert len(states.dates) == 77 and states.dropped_bars == 3


def test_duplicates_are_removed_and_order_is_ascending():
    frame = _frame(np.linspace(100, 200, 80))
    shuffled = pd.concat([frame, frame.iloc[[5, 6]]]).sample(frac=1, random_state=0)
    clean = ms.clean_bars(shuffled)
    assert list(clean["date"]) == list(frame["date"])
    assert ms.MarketStates(shuffled).trend == ms.MarketStates(frame).trend


def test_timestamp_column_is_read_as_the_ist_date():
    frame = _frame(np.linspace(100, 200, 80))
    stamped = frame.copy()
    # Historify stamps daily bars at 05:30 IST (00:00 UTC) of the bar date
    stamped["timestamp"] = [
        int(pd.Timestamp(d, tz="Asia/Kolkata").timestamp()) + 19800 for d in frame["date"]
    ]
    assert list(ms.clean_bars(stamped.drop(columns="date"))["date"]) == list(frame["date"])


def test_empty_or_missing_input_has_no_bars():
    assert ms.MarketStates(None).dates == []
    assert ms.MarketStates(pd.DataFrame()).status()["state"] == "no_data"


# ---------------------------------------------------------------- look-ahead


def test_label_for_a_bar_ignores_later_data():
    closes = _random_walk(420, seed=11)
    frame = _frame(closes, spread=3.0)
    full = ms.MarketStates(frame)
    rng = np.random.default_rng(5)
    for i in list(rng.choice(np.arange(60, 420), 40, replace=False)) + [419]:
        cut = ms.MarketStates(frame.iloc[: i + 1])
        assert cut.at_bar(i) == full.at_bar(i), f"look-ahead at bar {i}"


def test_attribution_label_does_not_change_when_the_day_itself_changes():
    frame = _frame(_random_walk(300, seed=2))
    t = frame["date"].iloc[-1]
    base = ms.MarketStates(frame).label_for(t)
    changed = frame.copy()
    changed.loc[len(changed) - 1, ["open", "high", "low", "close"]] = [1.0, 2.0, 0.5, 1.5]
    assert ms.MarketStates(changed).label_for(t) == base
    assert base["as_of"] == frame["date"].iloc[-2].isoformat()


# ---------------------------------------------------------------- attribution


def _classified_bars():
    return _frame(np.linspace(100, 400, 300))


def test_attribution_uses_the_strictly_previous_bar_across_weekends():
    frame = _classified_bars()
    last = frame["date"].iloc[-1]
    labels = ms.classify_dates([last + timedelta(days=1)], bar_reader=lambda: frame)
    # ISO key of the requested date; the bar used is the latest one before it
    ((key, label),) = labels.items()
    assert key == (last + timedelta(days=1)).isoformat()
    assert label["as_of"] == last.isoformat() and label["status"] == "classified"
    # Monday after a Friday bar
    fridays = [d for d in frame["date"] if d.weekday() == 4]
    friday = fridays[-3]
    monday = friday + timedelta(days=3)
    got = ms.classify_dates([monday], bar_reader=lambda: frame)[monday.isoformat()]
    assert got["as_of"] == friday.isoformat()


def test_a_trade_on_a_bar_date_uses_the_bar_before_not_itself():
    frame = _classified_bars()
    t = frame["date"].iloc[200]
    got = ms.classify_dates([t], bar_reader=lambda: frame)[t.isoformat()]
    assert got["as_of"] == frame["date"].iloc[199].isoformat()


def test_a_holiday_gap_within_the_limit_still_classifies():
    frame = _classified_bars()
    i = 280  # late enough for both labels
    gapped = frame.drop(index=[i, i + 1])  # two missing bars (for example a holiday run)
    t = frame["date"].iloc[i + 2]
    got = ms.classify_dates([t], bar_reader=lambda: gapped)[t.isoformat()]
    assert got["status"] == "classified"
    assert got["as_of"] == frame["date"].iloc[i - 1].isoformat()


def test_a_hole_beyond_the_limit_is_unclassified_never_a_class():
    frame = _classified_bars()
    holed = frame.drop(index=range(270, 285))
    t = frame["date"].iloc[284]
    got = ms.classify_dates([t], bar_reader=lambda: holed)[t.isoformat()]
    assert got["trend"] is None and got["volatility"] is None
    assert got["status"] == "unclassified" and got["reason"] == ms.REASON_STALE_BAR
    assert (t - date.fromisoformat(got["as_of"])).days > ms.MAX_BAR_AGE_DAYS


def test_stale_limit_boundary():
    frame = _classified_bars()
    last = frame["date"].iloc[-1]
    ok = last + timedelta(days=ms.MAX_BAR_AGE_DAYS)
    stale = last + timedelta(days=ms.MAX_BAR_AGE_DAYS + 1)
    out = ms.classify_dates([ok, stale], bar_reader=lambda: frame)
    assert out[ok.isoformat()]["status"] == "classified"
    assert out[stale.isoformat()]["reason"] == ms.REASON_STALE_BAR


def test_insufficient_history_and_no_prior_bar_and_no_bars_and_bad_date():
    frame = _frame(np.linspace(100, 200, 30))
    t = frame["date"].iloc[-1] + timedelta(days=1)
    out = ms.classify_dates([t], bar_reader=lambda: frame)[t.isoformat()]
    assert out["status"] == "unclassified" and out["reason"] == ms.REASON_INSUFFICIENT_HISTORY
    before = frame["date"].iloc[0]
    assert ms.classify_dates([before], bar_reader=lambda: frame)[before.isoformat()]["reason"] == (
        ms.REASON_NO_PRIOR_BAR
    )
    assert ms.classify_dates(["2026-11-02"], bar_reader=lambda: pd.DataFrame())["2026-11-02"][
        "reason"
    ] == (ms.REASON_NO_BARS)
    assert ms.classify_dates(["not-a-date"], bar_reader=lambda: frame)["not-a-date"]["reason"] == (
        ms.REASON_BAD_DATE
    )


def test_trend_only_when_history_is_between_the_two_minimums():
    frame = _frame(_random_walk(100, seed=4))
    t = frame["date"].iloc[-1] + timedelta(days=1)
    got = ms.classify_dates([t], bar_reader=lambda: frame)[t.isoformat()]
    assert got["trend"] is not None and got["volatility"] is None
    assert got["status"] == "partial"
    rows = ms.group_by_state(
        [{"trade_date": t.isoformat(), "realized_pnl": 5.0, "cost": 1.0}],
        {t.isoformat(): got},
        "volatility",
    )
    assert rows[-1]["name"] == "Unclassified" and rows[-1]["trades"] == 1


def test_reproducible_across_runs():
    frame = _frame(_random_walk(400, seed=9))
    dates = [frame["date"].iloc[i] + timedelta(days=1) for i in range(60, 390, 7)]
    first = ms.classify_dates(dates, bar_reader=lambda: frame)
    second = ms.classify_dates(dates, bar_reader=lambda: frame.copy())
    assert first == second


# ---------------------------------------------------------------- aggregation


def _trades():
    return [
        {"trade_date": "2026-01-01", "realized_pnl": 100.0, "cost": 10.0},
        {"trade_date": "2026-01-01", "realized_pnl": -50.0, "cost": 10.0},
        {"trade_date": "2026-01-02", "realized_pnl": 30.0, "cost": 10.0},
        {"trade_date": "2026-01-02", "realized_pnl": 20.0, "cost": None},
        {"trade_date": "2026-01-03", "realized_pnl": -10.0, "cost": 5.0},
        {"trade_date": "2026-01-04", "realized_pnl": 40.0, "cost": 5.0},
        {"trade_date": "2026-01-09", "realized_pnl": 7.0, "cost": 1.0},
    ]


def _labels():
    unc = {"trend": None, "volatility": None, "status": "unclassified", "reason": "stale_bar"}
    return {
        "2026-01-01": {"trend": "Uptrend", "volatility": "Low", "status": "classified"},
        "2026-01-02": {"trend": "Uptrend", "volatility": "High", "status": "classified"},
        "2026-01-03": {"trend": "Downtrend", "volatility": "Normal", "status": "classified"},
        "2026-01-04": {"trend": "Range", "volatility": "Normal", "status": "classified"},
        "2026-01-09": unc,
    }


def _by_name(rows):
    return {r["name"]: r for r in rows}


def test_group_by_trend_matches_an_independent_recompute():
    rows = _by_name(ms.group_by_state(_trades(), _labels(), "trend"))
    up = rows["Uptrend"]
    assert up["trades"] == 4 and up["wins"] == 3
    assert up["win_rate"] == 75.0
    assert up["gross_pnl"] == 100.0 - 50.0 + 30.0 + 20.0
    # one Uptrend trade has no cost: net values withheld, coverage stated
    assert up["net_pnl"] is None and up["avg_net_per_trade"] is None
    assert up["profit_factor"] is None
    assert up["net_coverage"] == {"costed": 3, "total": 4}
    assert up["charges_estimated"] == 30.0
    down = rows["Downtrend"]
    assert down["trades"] == 1 and down["gross_pnl"] == -10.0 and down["net_pnl"] == -15.0
    assert down["profit_factor"] is None  # no wins: not meaningful
    rng = rows["Range"]
    assert rng["net_pnl"] == 35.0 and rng["profit_factor"] is None  # no losses
    unc = rows["Unclassified"]
    assert unc["trades"] == 1 and unc["gross_pnl"] == 7.0 and unc["net_pnl"] == 6.0


def test_full_cost_coverage_gives_net_average_and_profit_factor():
    trades = [t for t in _trades() if t["trade_date"] == "2026-01-01"]
    row = _by_name(ms.group_by_state(trades, _labels(), "trend"))["Uptrend"]
    # net: 90 and -60
    assert row["net_pnl"] == 30.0 and row["avg_net_per_trade"] == 15.0
    assert row["profit_factor"] == pytest.approx(90 / 60, abs=0.01)
    assert row["win_rate"] == 50.0 and row["net_coverage"] == {"costed": 2, "total": 2}


def test_gross_win_rate_but_net_profit_factor_basis():
    # gross +5 is a win, net -5 is a loss: win rate is gross, profit factor is net
    trades = [
        {"trade_date": "2026-01-01", "realized_pnl": 5.0, "cost": 10.0},
        {"trade_date": "2026-01-01", "realized_pnl": 30.0, "cost": 10.0},
        {"trade_date": "2026-01-01", "realized_pnl": -20.0, "cost": 10.0},
    ]
    row = _by_name(ms.group_by_state(trades, _labels(), "trend"))["Uptrend"]
    assert row["wins"] == 2
    assert row["profit_factor"] == pytest.approx(20 / 35, abs=0.01)


def test_totals_are_conserved_and_every_class_is_listed():
    trades, labels = _trades(), _labels()
    for dimension, names in (
        ("trend", ["Uptrend", "Downtrend", "Range", "Unclassified"]),
        ("volatility", ["Low", "Normal", "High", "Unclassified"]),
    ):
        rows = ms.group_by_state(trades, labels, dimension)
        assert [r["name"] for r in rows] == names
        assert sum(r["trades"] for r in rows) == len(trades)
        assert sum(r["gross_pnl"] or 0 for r in rows) == pytest.approx(
            sum(t["realized_pnl"] for t in trades)
        )
        assert sum(r["charges_estimated"] or 0 for r in rows) == pytest.approx(
            sum(t["cost"] or 0 for t in trades)
        )
        assert sum(r["net_coverage"]["costed"] for r in rows) == 6
        assert sum(r["net_coverage"]["total"] for r in rows) == 7
        assert sum(r["wins"] for r in rows) == sum(1 for t in trades if t["realized_pnl"] > 0)


def test_net_sums_equal_input_when_every_trade_is_costed():
    trades = [t for t in _trades() if t["cost"] is not None]
    rows = ms.group_by_state(trades, _labels(), "volatility")
    assert sum(r["net_pnl"] or 0 for r in rows) == pytest.approx(
        sum(t["realized_pnl"] - t["cost"] for t in trades)
    )


def test_unclassified_row_is_present_even_when_empty_and_empty_rows_have_no_figures():
    rows = ms.group_by_state([], {}, "trend")
    assert [r["name"] for r in rows][-1] == "Unclassified"
    for row in rows:
        assert row["trades"] == 0 and row["gross_pnl"] is None and row["win_rate"] is None
        assert row["net_coverage"] == {"costed": 0, "total": 0}


def test_small_sample_flag_at_the_constant():
    base = {"trade_date": "2026-01-01", "realized_pnl": 1.0, "cost": 0.0}
    labels = {"2026-01-01": {"trend": "Range", "volatility": "Low"}}
    below = ms.group_by_state([base] * (ms.SMALL_SAMPLE_TRADES - 1), labels, "trend")
    at = ms.group_by_state([base] * ms.SMALL_SAMPLE_TRADES, labels, "trend")
    assert _by_name(below)["Range"]["small_sample"] is True
    assert _by_name(at)["Range"]["small_sample"] is False
    assert ms.SMALL_SAMPLE_TRADES == 30


def test_a_date_with_no_label_is_unclassified_not_dropped():
    rows = ms.group_by_state(_trades(), {}, "trend")
    assert _by_name(rows)["Unclassified"]["trades"] == len(_trades())


def test_group_by_state_rejects_other_dimensions():
    with pytest.raises(ValueError):
        ms.group_by_state([], {}, "both")


def test_unclassified_summary_counts_by_reason():
    summary = ms.unclassified_summary(_trades(), _labels())
    assert summary["trend"] == {"trades": 1, "reasons": {"stale_bar": 1}}
    assert summary["volatility"] == {"trades": 1, "reasons": {"stale_bar": 1}}


def test_methodology_states_the_constants_and_the_residual_range():
    text = ms.methodology()
    assert text["min_bars_trend"] == 55 and text["min_bars_volatility"] == 266
    assert "residual" in text["trend"] and text["small_sample_text"].startswith("Small sample")
