"""strategy_core.orb: the ORB15 rules of the Pine build behind the 2026-09-22..10-27 paper alerts."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from signal_engine.strategy_core import orb
from signal_engine.strategy_core.orb import OrbParams, evaluate_frame, stop_loss, targets
from signal_engine.timeutils import IST

DAY = dt.date(2026, 10, 14)


def _times(day, count=75):
    start = pd.Timestamp(dt.datetime.combine(day, dt.time(9, 15)), tz=IST)
    return pd.date_range(start, periods=count, freq="5min")


def _session(day, opens, highs, lows, closes, vols):
    n = len(opens)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=_times(day, n),
    )


def _flat_session(day, price=100.0, vol=1000.0, count=75):
    p = np.full(count, price)
    return _session(day, p, p + 0.2, p - 0.2, p, np.full(count, vol))


def _breakout_day(breakout_idx=5, level_close=101.8, entry_open=101.9, vol=5000.0, base=100.0):
    """A session: opening range 100.0-101.0 (09:15-09:25), quiet, then one close above 101.505."""
    n = 40
    opens = np.full(n, 100.5)
    highs = np.full(n, 100.9)
    lows = np.full(n, 100.1)
    closes = np.full(n, 100.5)
    vols = np.full(n, 1000.0)
    highs[:3], lows[:3] = 101.0, 100.0
    # bars 0-2 build the range; bar breakout_idx is the breakout bar, the next is the entry bar
    closes[breakout_idx] = level_close
    highs[breakout_idx] = level_close + 0.1
    vols[breakout_idx] = vol
    opens[breakout_idx + 1] = entry_open
    closes[breakout_idx + 1] = entry_open + 0.1
    highs[breakout_idx + 1] = entry_open + 0.2
    lows[breakout_idx + 1] = entry_open - 0.1
    df = _session(DAY, opens, highs, lows, closes, vols)
    return df


def _frame(today, prior_close=100.5, sessions=3):
    parts = []
    for k in range(sessions, 0, -1):
        d = DAY - dt.timedelta(days=k)
        flat = _flat_session(d, price=prior_close)
        parts.append(flat)
    parts.append(today)
    return pd.concat(parts)


class TestEntry:
    def test_long_breakout_enters_on_the_next_bar_at_its_open(self):
        # breakout bar 5 = 09:40, entry bar 6 = 09:45
        res = evaluate_frame(_frame(_breakout_day(breakout_idx=5)))
        assert len(res.entries) == 1
        e = res.entries[0]
        assert e.direction == 1
        assert e.bar_time.strftime("%H:%M") == "09:45"
        assert e.breakout_time.strftime("%H:%M") == "09:40"
        assert e.entry == pytest.approx(101.9)
        assert e.orb_high == pytest.approx(101.0) and e.orb_low == pytest.approx(100.0)

    def test_stop_is_past_the_orb_edge_and_target_is_point_eight_r(self):
        e = evaluate_frame(_frame(_breakout_day())).entries[0]
        assert e.sl < e.orb_low  # pushed past the far edge of the range
        risk = e.entry - e.sl
        assert e.tp1 - e.entry == pytest.approx(max(1.0, 0.8 * risk))
        assert e.tp1_5 - e.entry == pytest.approx(1.5 * (e.tp1 - e.entry))
        assert e.tp3 - e.entry == pytest.approx(3.0 * (e.tp1 - e.entry))

    def test_short_breakout_mirrors_the_long(self):
        n = 40
        o, h, lo, c, v = (
            np.full(n, 100.5),
            np.full(n, 100.9),
            np.full(n, 100.1),
            np.full(n, 100.5),
            np.full(n, 1000.0),
        )
        h[:3], lo[:3] = 101.0, 100.0
        c[5], lo[5], v[5] = 99.2, 99.1, 5000.0
        o[6], c[6], lo[6], h[6] = 99.1, 99.0, 98.9, 99.2
        e = evaluate_frame(_frame(_session(DAY, o, h, lo, c, v))).entries[0]
        assert e.direction == -1
        assert e.entry == pytest.approx(99.1)
        assert e.sl > e.orb_high
        assert e.tp1 < e.entry

    def test_one_entry_per_session_even_if_the_level_is_crossed_twice(self):
        day = _breakout_day()
        day.iloc[8, day.columns.get_loc("Close")] = 100.5  # back inside
        day.iloc[12, day.columns.get_loc("Close")] = 102.0
        day.iloc[12, day.columns.get_loc("Volume")] = 9000.0
        assert len(evaluate_frame(_frame(day)).entries) == 1


class TestGates:
    def test_volume_below_the_threshold_is_skipped(self):
        res = evaluate_frame(_frame(_breakout_day(vol=1100.0)))  # < 1.2 x the 1000 average
        assert res.entries == []
        assert (DAY, "LONG") in res.skips and res.skips[(DAY, "LONG")] == orb.SKIP_VOLUME

    def test_recent_volume_counts_the_two_bars_before_the_breakout_bar(self):
        day = _breakout_day(vol=1000.0)
        day.iloc[4, day.columns.get_loc("Volume")] = 5000.0  # the bar before the breakout bar
        assert len(evaluate_frame(_frame(day)).entries) == 1

    def test_entry_bar_before_0945_drops_the_session(self):
        # breakout bar 4 = 09:35, so the entry bar is 09:40
        res = evaluate_frame(_frame(_breakout_day(breakout_idx=4)))
        assert res.entries == []
        assert res.skips[(DAY, "LONG")] == orb.SKIP_ENTRY_TIME

    def test_entry_bar_at_1100_is_too_late_and_1055_is_allowed(self):
        late = evaluate_frame(
            _frame(_breakout_day(breakout_idx=19))
        )  # entry bar 10:55? idx 20 = 10:55
        assert [e.bar_time.strftime("%H:%M") for e in late.entries] == ["10:55"]
        too_late = evaluate_frame(_frame(_breakout_day(breakout_idx=20)))  # entry bar 11:00
        assert too_late.entries == []
        assert too_late.skips[(DAY, "LONG")] == orb.SKIP_ENTRY_TIME

    def test_gap_above_the_limit_is_skipped(self):
        # previous session closes at 95, today opens near 100.5: a 5.8% gap
        res = evaluate_frame(_frame(_breakout_day(), prior_close=95.0))
        assert res.entries == []
        assert res.skips[(DAY, "LONG")] == orb.SKIP_GAP

    @pytest.mark.parametrize("hi,lo", [(100.2, 100.0), (104.0, 100.0)])
    def test_range_width_outside_the_band_is_skipped(self, hi, lo):
        day = _breakout_day()
        day.iloc[:3, day.columns.get_loc("High")] = hi
        day.iloc[:3, day.columns.get_loc("Low")] = lo
        day.iloc[5, day.columns.get_loc("Close")] = hi * 1.01
        day.iloc[6, day.columns.get_loc("Open")] = hi * 1.01
        res = evaluate_frame(_frame(day))
        assert res.entries == []

    def test_no_entry_without_a_close_beyond_the_buffered_level(self):
        # 101.4 is above the range high 101.0 but below 101.0 x 1.005 = 101.505
        assert (
            evaluate_frame(_frame(_breakout_day(level_close=101.4, entry_open=101.2))).entries == []
        )


class TestVeto:
    @staticmethod
    def _below_the_daily_ema():
        parts = []
        for k in range(30, 1, -1):
            parts.append(_flat_session(DAY - dt.timedelta(days=k), price=106.0))
        parts.append(
            _flat_session(DAY - dt.timedelta(days=1), price=102.5)
        )  # a 2% gap down into today
        parts.append(_breakout_day())
        return pd.concat(parts)

    def test_one_opposing_vote_alone_does_not_block(self):
        # price sits more than 2% below its daily EMA(20): a single bearish vote
        res = evaluate_frame(self._below_the_daily_ema())
        assert len(res.entries) == 1 and res.entries[0].votes_against == 1

    def test_the_same_vote_blocks_when_a_single_vote_is_enough(self):
        res = evaluate_frame(self._below_the_daily_ema(), OrbParams(veto_votes=1))
        assert res.entries == []
        assert res.skips[(DAY, "LONG")] == orb.SKIP_VETO


class TestStopAndTargets:
    # Vectors from the paper alerts (trades.db, ORB, 2026-09-22 and 2026-09-23)
    def test_sbin_short_alert_2026_09_22(self):
        p = OrbParams()
        sl = stop_loss(
            -1, 991.0, 1.5, 1000.10, 996.00, p
        )  # wick = max(0.3 ATR, 0.15 x 4.10) = 0.615
        assert sl == pytest.approx(1000.72, abs=0.01)
        tp1, tp15, tp2, tp3 = targets(-1, 991.0, sl, 1000.10, 996.00, p)
        assert tp1 == pytest.approx(983.23, abs=0.01)
        assert tp15 == pytest.approx(979.34, abs=0.01)
        assert tp2 == pytest.approx(975.46, abs=0.01)
        assert tp3 == pytest.approx(967.68, abs=0.01)

    def test_itc_long_alert_2026_09_23(self):
        p = OrbParams()
        sl = stop_loss(1, 269.05, 0.5, 266.60, 265.15, p)
        assert sl == pytest.approx(264.93, abs=0.01)
        assert targets(1, 269.05, sl, 266.60, 265.15, p)[0] == pytest.approx(272.34, abs=0.01)

    def test_minimum_stop_distance_applies_when_the_orb_edge_is_close(self):
        # a 0.3% floor: entry just above a very tight range
        sl = stop_loss(1, 1000.0, 0.1, 999.5, 999.4, OrbParams())
        assert 1000.0 - sl >= 3.0 - 1e-9

    def test_a_missing_atr_falls_back_to_half_the_range(self):
        assert np.isfinite(stop_loss(1, 100.0, float("nan"), 101.0, 100.0, OrbParams()))


class TestSessionAnchor:
    def test_a_session_missing_its_0915_bar_is_skipped_not_shifted(self):
        day = _breakout_day()
        frame = _frame(day.iloc[1:])  # the 09:15 bar is absent
        res = evaluate_frame(frame)
        assert res.entries == []
        assert res.skips[(DAY, "-")] == orb.SKIP_NO_RANGE

    def test_a_pre_open_bar_does_not_become_the_first_bar(self):
        day = _breakout_day()
        pre = day.iloc[:1].copy()
        pre.index = pre.index - pd.Timedelta(minutes=5)  # a 09:10 bar
        res = evaluate_frame(_frame(pd.concat([pre, day])))
        assert len(res.entries) == 1
        assert res.entries[0].bar_time.strftime("%H:%M") == "09:45"
