"""Python port of breakout.pine's key-level engine (klTrackBreaks .. klFireGate).

Scenario prices are chosen so every expected number can be checked by hand:
PDH = 100.0, ATR = 1.0 (band 0.25, SL buffer 0.35, T1 pad 0.15), tick 0.05.
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.strategy_core.breakout import BreakoutParams, compute_signals

IST = "Asia/Kolkata"
BASE_COLS = {
    "atr": 1.0,
    "vwap": 99.0,
    "ema9": 99.5,
    "ema_slope": 0.1,
    "rvol": 2.0,
    "sess_vf": 1.5,
    "htf_close": np.nan,
    "pdh": 100.0,
    "pdl": np.nan,
    "ib_high": 101.0,
    "ib_low": 95.0,
    "ib_done": True,
    "orb_high": np.nan,
    "orb_low": np.nan,
    "pvah": np.nan,
    "ppoc": np.nan,
    "pval": np.nan,
    "adr": 5.0,
    "day_high": 101.0,
    "day_low": 99.0,
}
# (close, high, low, rvol) per bar: below PDH / break bar / retest bar / second qualifying bar
BARS = [
    (99.5, 99.8, 99.2, 1.0),
    (100.6, 100.7, 100.4, 1.0),
    (100.5, 100.7, 99.9, 2.0),
    (100.5, 100.7, 99.9, 2.0),
]


def frame(bars=BARS, start="10:00", **over) -> pd.DataFrame:
    idx = pd.date_range(f"2026-09-29 {start}", periods=len(bars), freq="5min", tz=IST)
    df = pd.DataFrame(BASE_COLS, index=idx)
    df["Close"] = [b[0] for b in bars]
    df["High"] = [b[1] for b in bars]
    df["Low"] = [b[2] for b in bars]
    df["rvol"] = [b[3] for b in bars]
    for k, v in over.items():
        df[k] = v
    return df


def mirror(df: pd.DataFrame) -> pd.DataFrame:
    m = df.copy()
    m["Close"], m["High"], m["Low"] = 200 - df["Close"], 200 - df["Low"], 200 - df["High"]
    for col in ("vwap", "ema9", "htf_close"):
        m[col] = 200 - df[col]
    m["ema_slope"] = -df["ema_slope"]
    m["pdl"], m["pdh"] = 200 - df["pdh"], 200 - df["pdl"]
    m["ib_low"], m["ib_high"] = 200 - df["ib_high"], 200 - df["ib_low"]
    m["orb_low"], m["orb_high"] = 200 - df["orb_high"], 200 - df["orb_low"]
    m["pval"], m["pvah"] = 200 - df["pvah"], 200 - df["pval"]
    m["day_low"], m["day_high"] = 200 - df["day_high"], 200 - df["day_low"]
    return m


def fires(df, **p):
    return compute_signals(df, BreakoutParams(**p))


class TestPdhRetestScenario:
    def test_fires_once_on_the_retest_bar(self):
        out = fires(frame())
        assert len(out) == 1
        row = out.iloc[0]
        assert out.index[0] == frame().index[2]
        assert (row.code, row.direction, row.level) == ("PDH-RT", 1, 100.0)

    def test_stop_and_targets_are_hand_computed(self):
        row = fires(frame()).iloc[0]
        assert row.entry_ref == 100.5
        assert row.sl == pytest.approx(99.65)  # level - 0.35 ATR, already beyond the 0.5 ATR floor
        assert row.risk == pytest.approx(0.85)
        assert row.t1 == pytest.approx(100.85)  # IBH 101.0 padded 0.15 short
        assert row.tp1 == pytest.approx(101.35)  # d = max(|t1-entry|, 1R) = 0.85
        assert row.tp3 == pytest.approx(100.5 + 3 * 0.85)

    def test_score_components_add_up(self):
        # 2 level + 2 retest + 1 vwap + 1 ema + 2 strong rvol + 1 clv + 1 session vf
        assert fires(frame()).iloc[0].score == 10

    def test_headroom_and_tp_reach_are_reported(self):
        row = fires(frame()).iloc[0]
        assert row.headroom == pytest.approx((99.0 + 5.0 - 100.5) / 0.85)
        assert row.tp_r == pytest.approx(0.35 / 0.85)

    def test_cooldown_suppresses_the_next_qualifying_bar(self):
        assert len(fires(frame())) == 1
        assert len(fires(frame(), cooldown_bars=0)) == 2

    def test_break_bar_scores_as_retest_when_it_touches_the_band(self):
        # shipped quirk: klTrackBreaks stamps the break bar BEFORE klResolveSetup, so the break
        # bar itself satisfies the retest branch, which outranks -BRK
        bars = [BARS[0], (100.6, 100.7, 100.2, 2.0), BARS[2], BARS[3]]
        first = fires(frame(bars)).iloc[0]
        assert first.code == "PDH-RT"

    def test_short_mirror_image(self):
        row = fires(mirror(frame())).iloc[0]
        assert (row.code, row.direction, row.level) == ("PDL-RT", -1, 100.0)
        assert row.sl == pytest.approx(200 - 99.65)
        assert row.score == 10


class TestGates:
    @pytest.mark.parametrize(
        "over,reason",
        [
            ({"atr": 0.4}, "chase: 0.5 / 0.4 ATR beyond max 1.0"),
            ({"sess_vf": 0.5}, "session volume factor below 0.8"),
            ({"adr": 1.0}, "no headroom to ADR projection"),
            ({"ib_high": 105.0}, "TP1 more than 2R away is refused"),
        ],
    )
    def test_gate_blocks_the_retest(self, over, reason):
        assert fires(frame(**over)).empty, reason

    def test_score_threshold_blocks_a_weak_retest(self):
        weak = frame(vwap=101.0, ema9=101.0, sess_vf=1.0)  # loses vwap, ema and vf points: 7
        assert len(fires(weak)) == 1
        assert fires(weak, score_threshold=8).empty

    def test_htf_close_on_wrong_side_blocks_without_strong_volume(self):
        bars = [BARS[0], BARS[1], (100.5, 100.7, 99.9, 1.5), (100.5, 100.7, 99.9, 1.5)]
        assert fires(frame(bars, htf_close=99.0)).empty
        assert len(fires(frame(bars, htf_close=100.4), cooldown_bars=99)) == 1

    def test_strong_rvol_bypasses_the_htf_wait(self):
        assert len(fires(frame(htf_close=99.0))) == 1  # rvol 2.0 >= 1.8

    def test_entry_time_window(self):
        assert fires(frame(start="11:50")).empty  # after the 11:45 cutoff
        assert fires(frame(start="09:20")).empty  # before the 09:45 minimum
        assert not fires(frame(start="09:45")).empty

    def test_counter_bias_veto_is_an_input(self):
        assert fires(frame(bias_ok=False)).empty


class TestConfluence:
    def test_each_nearby_level_adds_a_point_and_two_add_a_bonus(self):
        # the break bar can also fire once confluence lifts its score, so read the retest bar
        retest_bar = frame().index[2]
        one = fires(frame(orb_high=100.1), cooldown_bars=0).loc[retest_bar]
        two = fires(frame(orb_high=100.1, orb_low=99.9), cooldown_bars=0).loc[retest_bar]
        assert (one.conf, one.score) == (1, 11)
        assert (two.conf, two.score) == (2, 14)  # +2 for the count, +2 multi-level bonus

    def test_the_setup_level_is_not_its_own_confluence(self):
        assert fires(frame()).iloc[0].conf == 0


class TestValueAreaFamilies:
    def va_frame(self, **over):
        bars = [(99.0, 99.4, 98.8, 1.0), (100.5, 100.6, 99.7, 2.0), (100.5, 100.6, 99.7, 2.0)]
        return frame(
            bars, pdh=np.nan, ib_high=np.nan, ib_low=np.nan, ib_done=False, pval=100.0, **over
        )

    def test_val_rejection_long(self):
        row = fires(self.va_frame()).iloc[0]
        assert (row.code, row.direction, row.level) == ("VAL-REJ", 1, 100.0)
        assert row.score == 8  # rejection earns no retest bonus

    def test_acceptance_is_off_by_default(self):
        bars = [(101.0, 101.2, 100.6, 2.0)] * 3
        df = frame(bars, pdh=np.nan, ib_high=np.nan, ib_low=np.nan, ib_done=False, pvah=100.0)
        assert fires(df).empty
        assert fires(df, va_acceptance=True).iloc[0].code == "VAH-ACC"

    def test_value_area_outranks_previous_day_at_the_same_bar(self):
        df = frame(pvah=100.0)  # VAH retest and PDH retest both true on the retest bar
        assert fires(df).iloc[0].code == "VAH-RT"

    def test_value_area_triggers_can_be_disabled(self):
        assert fires(self.va_frame(), va_triggers=False).empty
