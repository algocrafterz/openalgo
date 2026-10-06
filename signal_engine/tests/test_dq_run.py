"""Per-run data-quality report built from the frames a run used."""

from __future__ import annotations

import datetime as dt
import json
import time

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import dataquality
from signal_engine.backtest.data_collect import DataCollector
from signal_engine.backtest.dq_run import DEFAULT_THRESHOLDS, build_data_quality

IST = "Asia/Kolkata"


def _panel(n_days: int = 20, bars: int = 75, start: str = "2025-01-01") -> pd.DatetimeIndex:
    days = pd.bdate_range(start, periods=n_days)
    stamps = [d + pd.Timedelta(hours=9, minutes=15 + 5 * i) for d in days for i in range(bars)]
    return pd.DatetimeIndex(stamps).tz_localize(IST)


def _good(idx: pd.DatetimeIndex, seed: int = 0, volume: int = 100) -> pd.DataFrame:
    r = np.random.default_rng(seed)
    c = 100 + np.cumsum(r.normal(0, 0.01, len(idx)))
    return pd.DataFrame(
        {"Open": c, "High": c + 0.05, "Low": c - 0.05, "Close": c, "Volume": volume}, index=idx
    )


def _col(interval: str = "5m", n: int = 3) -> DataCollector:
    return DataCollector(interval=interval, symbols_loaded=n)


def _frames(n: int = 3) -> dict:
    idx = _panel()
    return {f"S{i}": _good(idx, i) for i in range(n)}


def _item(rep: dict, check: str) -> dict:
    return next(i for i in rep["items"] if i["check"] == check)


def test_pass_on_clean_frames():
    rep = build_data_quality(_frames(), _col())
    assert rep["schema"] == 1
    assert rep["status"] == "PASS"
    assert rep["affects_validity"] is False
    assert rep["items"] == []
    assert rep["summary"]["symbols"] == 3
    assert rep["summary"]["sessions_checked"] == 60
    assert rep["summary"]["complete_pct_overall"] == 100.0
    assert rep["summary"]["audited_symbols"] == ["S0", "S1", "S2"]
    assert rep["dropped"] == _col().to_dict()


def test_no_frames_is_not_run():
    rep = build_data_quality({}, _col(n=0))
    assert rep["status"] == "NOT_RUN"
    assert rep["affects_validity"] is False


def test_policy_texts_present():
    rep = build_data_quality(_frames(), _col())
    pol = rep["policy"]
    assert "0.75 to 1.33" in pol["corporate_actions"]
    assert "NOT detected" in pol["corporate_actions"]
    assert "09:15-15:29" in pol["closing_auction"]
    assert pol["timestamps"] == "stored as epoch seconds, converted to IST at load"


def test_thresholds_recorded():
    rep = build_data_quality(_frames(), _col())
    assert rep["thresholds"]["bad_bar_share"] == 0.001
    assert rep["thresholds"]["incomplete_symbol_session_share"] == 0.02
    assert rep["thresholds"]["min_session_completeness"] == dataquality.MIN_SESSION_COMPLETENESS
    assert rep["thresholds"] == DEFAULT_THRESHOLDS


def test_impossible_bars_above_threshold_affect_validity():
    frames = _frames(1)
    df = frames["S0"].copy()
    df.iloc[:5, df.columns.get_loc("High")] = 1.0  # 5 of 1500 bars > 0.1%
    frames["S0"] = df
    rep = build_data_quality(frames, _col(n=1))
    assert rep["status"] == "WARNING" and rep["affects_validity"] is True
    it = _item(rep, "impossible_bars")
    assert it["severity"] == "warning" and it["count"] == 5 and it["symbols"] == ["S0"]


def test_dropped_duplicates_and_outside_count_toward_validity():
    col = _col()
    col.record_clean("S1", duplicate_ts=3)
    col.record_clean("S2", outside_hours=3)
    rep = build_data_quality(_frames(), col)  # 6 of 4506 bars = 0.133%
    assert rep["status"] == "WARNING" and rep["affects_validity"] is True
    assert _item(rep, "duplicate_timestamps")["count"] == 3
    assert _item(rep, "outside_hours_bars")["count"] == 3


def test_boundary_exactly_threshold_does_not_affect_validity():
    # 50 dropped of 5000 bars seen (4950 retained) is exactly 1%: not above it.
    frames = {"S0": _good(_panel(n_days=66))}
    th = {"bad_bar_share": 0.01}
    col = DataCollector(interval="5m", symbols_loaded=1)
    col.record_clean("S0", duplicate_ts=50)
    rep = build_data_quality(frames, col, thresholds=th)
    assert rep["affects_validity"] is False
    assert rep["status"] == "PASS"
    assert _item(rep, "duplicate_timestamps")["severity"] == "info"
    col.record_clean("S0", duplicate_ts=1)
    rep = build_data_quality(frames, col, thresholds=th)
    assert rep["affects_validity"] is True and rep["status"] == "WARNING"


def test_low_completeness_share_affects_validity():
    idx = _panel(n_days=50)
    frames = {"S0": _good(idx)}
    df = frames["S0"]
    days = pd.Series(df.index.date, index=df.index)
    first_days = sorted(set(days))[:2]  # 2 of 50 = 4% of symbol-sessions
    mask = ~(days.isin(first_days) & (df.index.hour == 15))
    frames["S0"] = df[mask.to_numpy()]
    rep = build_data_quality(frames, _col(n=1))
    assert rep["status"] == "WARNING" and rep["affects_validity"] is True
    it = _item(rep, "incomplete_sessions")
    assert it["count"] == 2 and it["severity"] == "warning"


def test_low_completeness_share_below_two_percent_is_info():
    idx = _panel(n_days=100)
    df = _good(idx)
    days = pd.Series(df.index.date, index=df.index)
    first = sorted(set(days))[:2]  # exactly 2 of 100 = 2.0%, not above
    df = df[(~(days.isin(first) & (df.index.hour == 15))).to_numpy()]
    rep = build_data_quality({"S0": df}, _col(n=1))
    assert rep["affects_validity"] is False and rep["status"] == "PASS"
    assert _item(rep, "incomplete_sessions")["severity"] == "info"


def test_minor_findings_warn_without_validity_impact():
    col = _col()
    col.record_split_days("S0", [dt.date(2025, 1, 3)])
    col.record_min_sessions("TINY", 12)
    frames = _frames()
    frames["S1"] = frames["S1"].copy()
    frames["S1"]["Volume"] = 0
    rep = build_data_quality(frames, col)
    assert rep["status"] == "WARNING" and rep["affects_validity"] is False
    checks = {i["check"] for i in rep["items"]}
    assert {"split_days_dropped", "symbols_dropped_min_sessions", "zero_volume"} <= checks
    assert _item(rep, "split_days_dropped")["count"] == 1
    assert _item(rep, "symbols_dropped_min_sessions")["symbols"] == ["TINY"]
    assert _item(rep, "zero_volume")["symbols"] == ["S1"]
    assert all(i["severity"] in ("info", "warning") for i in rep["items"])


def test_stale_symbol_is_non_validity_warning():
    frames = _frames()
    frames["OLD"] = _good(_panel(n_days=5, start="2024-06-03"), 9)
    rep = build_data_quality(frames, _col(n=4))
    it = _item(rep, "stale_last_bar")
    assert it["severity"] == "warning" and it["symbols"] == ["OLD"]
    assert rep["status"] == "WARNING" and rep["affects_validity"] is False


def test_daily_frames_skip_session_completeness():
    days = pd.bdate_range("2024-01-01", periods=30, tz=IST)
    rep = build_data_quality({"S0": _good(days)}, _col(interval="1d", n=1))
    assert rep["status"] == "PASS"
    assert all(i["check"] != "incomplete_sessions" for i in rep["items"])


def test_symbols_list_capped_at_ten():
    col = _col()
    for i in range(15):
        col.record_split_days(f"X{i:02d}", [dt.date(2025, 1, 3)])
    rep = build_data_quality(_frames(), col)
    it = _item(rep, "split_days_dropped")
    assert it["count"] == 15 and it["symbols"] == [f"X{i:02d}" for i in range(10)]


def test_deterministic_and_json_safe():
    a = build_data_quality(_frames(), _col())
    b = build_data_quality(dict(reversed(list(_frames().items()))), _col())
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_sampling_is_deterministic_evenly_spaced_and_recorded():
    idx = _panel(n_days=5)
    frames = {f"S{i:02d}": _good(idx, i) for i in range(10)}
    rep = build_data_quality(frames, _col(n=10), max_symbols=4)
    audited = rep["summary"]["audited_symbols"]
    assert audited == sorted(audited) and len(audited) == 4
    assert audited[0] == "S00" and audited[-1] == "S09"
    assert rep["summary"]["symbols"] == 10
    rep2 = build_data_quality(dict(reversed(list(frames.items()))), _col(n=10), max_symbols=4)
    assert rep2["summary"]["audited_symbols"] == audited


def test_calendar_sessions_recorded_when_given():
    cal = [dt.date(2025, 1, 1), dt.date(2025, 1, 2)]
    rep = build_data_quality(_frames(), _col(), calendar=cal)
    assert rep["summary"]["calendar_sessions"] == 2


def test_threshold_override():
    col = _col()
    col.record_clean("S0", duplicate_ts=1)
    rep = build_data_quality(_frames(), col, thresholds={"bad_bar_share": 0.0})
    assert rep["affects_validity"] is True
    assert rep["thresholds"]["bad_bar_share"] == 0.0


def test_unknown_threshold_rejected():
    with pytest.raises(ValueError):
        build_data_quality(_frames(), _col(), thresholds={"nope": 1})


def test_speed_on_synthetic_panel():
    days = pd.bdate_range("2018-01-01", periods=506)
    idx = pd.DatetimeIndex(
        [d + pd.Timedelta(hours=9, minutes=15 + i) for d in days for i in range(375)]
    ).tz_localize(IST)
    assert len(idx) == 189750
    frames = {f"S{i}": _good(idx, i) for i in range(5)}
    t0 = time.perf_counter()
    rep = build_data_quality(frames, _col(interval="1m", n=5))
    elapsed = time.perf_counter() - t0
    print(f"audit of 5 symbols x 189750 bars: {elapsed:.2f}s")
    assert rep["status"] in ("PASS", "WARNING")
    assert elapsed < 30


def _resampled_panel(n_symbols: int, n_days: int = 20) -> dict:
    # 5-minute frames; the loader saw 5x as many 1-minute rows per retained bar
    idx = _panel(n_days=n_days)
    return {f"S{i:03d}": _good(idx, i) for i in range(n_symbols)}


def _seen(frames: dict, col: DataCollector, per_bar: int = 5) -> None:
    for s, df in frames.items():
        col.record_seen(s, len(df) * per_bar)


def test_realistic_drops_on_resampled_panel_stay_pass():
    frames = _resampled_panel(80)
    col = _col(n=80)
    _seen(frames, col)  # 1500 * 5 rows per symbol
    for i in range(80):
        col.record_clean(f"S{i:03d}", duplicate_ts=2, outside_hours=1)  # 3 / 7500 = 0.04%
    rep = build_data_quality(frames, col, max_symbols=60)
    assert rep["status"] == "PASS" and rep["affects_validity"] is False


def test_drops_above_share_of_rows_seen_affect_validity():
    frames = _resampled_panel(10)
    col = _col(n=10)
    _seen(frames, col)
    for i in range(10):
        col.record_clean(f"S{i:03d}", impossible=9)  # 9 / 7500 = 0.12%
    rep = build_data_quality(frames, col)
    assert rep["status"] == "WARNING" and rep["affects_validity"] is True


def test_sampling_does_not_change_share_beyond_the_sample():
    frames = _resampled_panel(30)
    col = _col(n=30)
    _seen(frames, col)
    for i in range(30):
        col.record_clean(f"S{i:03d}", duplicate_ts=2)
    full = build_data_quality(frames, col)
    samp = build_data_quality(frames, col, max_symbols=5)
    assert full["status"] == samp["status"] == "PASS"
    assert samp["summary"]["audited_symbols"] != full["summary"]["audited_symbols"]


def test_incomplete_sessions_detail_says_sample():
    idx = _panel(n_days=50)
    frames = {f"S{i}": _good(idx, i) for i in range(4)}
    d = frames["S0"]
    first = sorted(set(d.index.date))[:5]
    keep = ~(pd.Series(d.index.date, index=d.index).isin(first) & (d.index.hour == 15))
    frames["S0"] = d[keep.to_numpy()]
    rep = build_data_quality(frames, _col(n=4), max_symbols=2)
    assert rep["summary"]["audited_symbols"] == ["S0", "S3"]
    assert "sample" in _item(rep, "incomplete_sessions")["detail"]


def _drop_bins(df: pd.DataFrame, hhmm: tuple[str, ...]) -> pd.DataFrame:
    keep = ~pd.Index(df.index.strftime("%H:%M")).isin(hhmm)
    return df[keep]


def test_session_shape_from_the_dataset_replaces_the_fixed_75():
    # every session has 73 bars (15:15 and 15:20 absent), as in a closing-auction session
    df = _drop_bins(_good(_panel(n_days=50)), ("15:15", "15:20"))
    legacy = build_data_quality({"S0": df}, _col(n=1))
    assert legacy["affects_validity"] is True  # 73 of 75 is below the 98% floor
    shaped = build_data_quality({"S0": df}, _col(n=1), expected_bars_for=lambda d: 73)
    assert shaped["affects_validity"] is False and shaped["status"] == "PASS"


def test_session_shape_still_catches_a_missing_bar():
    # the dataset says 73; a session that also lost a mid-day bar is still incomplete
    df = _drop_bins(_good(_panel(n_days=50)), ("15:15", "15:20"))
    days = pd.Series(df.index.date, index=df.index)
    first = sorted(set(days))[:2]  # 4% of symbol-sessions
    # two mid-day bars: 71 of 73 is below the 98% floor (one missing bar is tolerated)
    hole = days.isin(first) & pd.Index(df.index.strftime("%H:%M")).isin(("12:00", "12:05"))
    df = df[(~hole).to_numpy()]
    rep = build_data_quality({"S0": df}, _col(n=1), expected_bars_for=lambda d: 73)
    assert rep["affects_validity"] is True
    assert _item(rep, "incomplete_sessions")["count"] == 2


def test_session_shape_is_per_date():
    # pre-auction days hold 75 bars, auction days 73; neither is incomplete
    df = _good(_panel(n_days=50))
    days = pd.Series(df.index.date, index=df.index)
    late = set(sorted(set(days))[25:])
    auction = days.isin(late) & pd.Index(df.index.strftime("%H:%M")).isin(("15:15", "15:20"))
    df = df[(~auction).to_numpy()]
    rep = build_data_quality(
        {"S0": df}, _col(n=1), expected_bars_for=lambda d: 73 if d in late else 75
    )
    assert rep["affects_validity"] is False and rep["status"] == "PASS"


def test_intraday_runs_use_the_published_session_template():
    from signal_engine.backtest.record_run import intraday_session_shape

    shape = intraday_session_shape()
    assert shape(dt.date(2026, 7, 31)) == 75
    assert shape(dt.date(2026, 8, 3)) == 73
