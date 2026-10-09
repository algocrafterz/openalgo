"""EVWAP-EARLY-PY: indicators, entry rules, trailing stop, trail driver and backtest parity."""

import asyncio
import dataclasses
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.strategies.ema_vwap_early import BASELINES, EmaVwapEarly
from signal_engine.backtest.types import RunConfig
from signal_engine.features.price import atr, ema
from signal_engine.models import Direction
from signal_engine.pipeline.signals import fire_to_signal
from signal_engine.pipeline.strategies.ema_vwap_early import TAG, EarlyDisplacementStrategy
from signal_engine.pipeline.trail_driver import TrailExitDriver
from signal_engine.strategy_core import ema_vwap_early as core
from signal_engine.strategy_core.ema_vwap_early import (
    EarlyDisplacementParams,
    TrailBar,
    TrailState,
    build_inputs,
    evaluate_bar,
    evaluate_frame,
    trigger,
)
from signal_engine.timeutils import IST
from signal_engine.tracker import TrackedPosition

P = EarlyDisplacementParams()


# ---- synthetic data -----------------------------------------------------------------------------


def session_index(day: dt.date) -> pd.DatetimeIndex:
    start = pd.Timestamp(dt.datetime.combine(day, dt.time(9, 15)), tz=IST)
    return pd.date_range(start, periods=75, freq="5min")  # 09:15 .. 15:25


def random_bars(sessions: int = 8, seed: int = 7, start=dt.date(2026, 9, 1)) -> pd.DataFrame:
    """A seeded random walk with trending stretches, so displacement swings through +/-0.5."""
    rng = np.random.default_rng(seed)
    days, d = [], start
    while len(days) < sessions:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    idx = pd.DatetimeIndex(np.concatenate([session_index(x) for x in days]))
    n = len(idx)
    drift = np.repeat(rng.choice([-0.6, -0.3, 0.0, 0.3, 0.6], size=n // 15 + 1), 15)[:n]
    close = 1000 + np.cumsum(drift + rng.normal(0, 1.2, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 1.5, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 1.5, n)
    vol = rng.integers(1_000, 10_000, n).astype(float)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx
    )


def row(**kw) -> pd.Series:
    """One input row with neutral defaults, for exercising evaluate_bar's filters directly."""
    base = {
        "Open": 100.0, "High": 100.5, "Low": 99.5, "Close": 100.2, "Volume": 1000.0,
        "ema_f": 100.0, "ema_s": 100.0, "vwap": 100.4, "atr": 1.0,
        "disp": -0.45, "disp_prev": -0.55, "roc": 0.10,
        "swing_low": 99.0, "swing_high": 101.0, "fallback_low": 99.2, "fallback_high": 100.8,
        "swing_low_bar": 496.0, "swing_high_bar": 495.0, "fallback_low_bar": 498.0,
        "fallback_high_bar": 497.0,
        "chop_changes": 0.0, "chop_mean_abs": 0.7, "reversal": 0, "minute": 600, "bar_no": 500,
    }
    base.update(kw)
    return pd.Series(base)


# ---- indicators ---------------------------------------------------------------------------------


class TestIndicators:
    def test_displacement_is_ema9_minus_session_vwap_over_atr14(self):
        df = random_bars(sessions=3)
        inp = build_inputs(df, P)
        today = inp[inp.index.date == inp.index[-1].date()]
        tp = (today["High"] + today["Low"] + today["Close"]) / 3
        vwap = (tp * today["Volume"]).cumsum() / today["Volume"].cumsum()
        assert np.allclose(today["vwap"], vwap)
        kept = df[df.index.hour * 60 + df.index.minute <= core.LAST_BIN_MIN]
        assert np.allclose(inp["ema_f"], ema(kept["Close"], 9))
        assert np.allclose(inp["ema_s"], ema(kept["Close"], 20))
        assert np.allclose(inp["atr"], atr(kept, 14))
        assert np.allclose(inp["disp"], (inp["ema_f"] - inp["vwap"]) / inp["atr"])
        assert np.allclose(inp["roc"].dropna(), (inp["disp"] - inp["disp_prev"]).dropna())

    def test_closing_auction_bars_are_dropped(self):
        inp = build_inputs(random_bars(sessions=2), P)
        assert (inp["minute"] <= 15 * 60 + 10).all()

    def test_first_bar_of_session_has_no_previous_displacement(self):
        inp = build_inputs(random_bars(sessions=3), P)
        firsts = inp.groupby(inp.index.date).head(1)
        assert firsts["disp_prev"].isna().all()

    def test_swing_is_known_only_after_its_confirmation_bars(self):
        idx = session_index(dt.date(2026, 9, 1))[:8]
        low = np.array([10, 9, 8, 7, 8, 9, 10, 11], dtype=float)
        sw_lo, _, lo_bar, _ = core._confirmed_swings(low, low + 1, np.asarray(idx.date), 2, 2)
        assert np.isnan(sw_lo[:5]).all()  # pivot at bar 3, confirmed at bar 5
        assert sw_lo[5] == 7 and sw_lo[7] == 7
        assert lo_bar[5] == 3 and lo_bar[7] == 3


# ---- entry --------------------------------------------------------------------------------------


class TestTrigger:
    def test_long_triggers_through_negative_threshold_not_at_zero(self):
        assert trigger(-0.43, -0.52, P) == 1
        assert trigger(0.05, -0.08, P) == 0  # the crossover is not the entry
        assert trigger(-0.40, -0.45, P) == 0  # already above the threshold

    def test_short_triggers_through_positive_threshold(self):
        assert trigger(0.43, 0.54, P) == -1
        assert trigger(-0.05, 0.10, P) == 0

    def test_threshold_is_a_parameter(self):
        p = P.with_(early_threshold=0.35)
        assert trigger(-0.30, -0.40, p) == 1
        assert trigger(-0.30, -0.40, P) == 0

    def test_crossover_baseline_enters_at_zero(self):
        p = P.with_(entry_mode="crossover")
        assert trigger(0.05, -0.08, p) == 1
        assert trigger(-0.05, 0.08, p) == -1
        assert trigger(-0.43, -0.52, p) == 0

    def test_long_conditions_never_produce_a_short_and_vice_versa(self):
        rng = np.random.default_rng(1)
        for prev, cur in rng.uniform(-2, 2, size=(5000, 2)):
            got = trigger(cur, prev, P)
            if got == 1:
                assert prev <= -0.5 < cur and cur > prev
            if got == -1:
                assert prev >= 0.5 > cur and cur < prev

    def test_early_entries_happen_before_the_crossover(self):
        decisions = evaluate_frame(build_inputs(random_bars(sessions=12), P), P)
        accepted = [d for d in decisions.values() if d.accepted]
        assert accepted
        for d in accepted:
            assert d.direction * d.context["disp"] < 0  # EMA9 still on the far side of VWAP


class TestFilters:
    def test_accepted_long_has_reason_stop_and_diagnostics(self):
        d = evaluate_bar(row(), P)
        assert d.accepted and d.reason == core.EARLY_LONG_MOMENTUM_CONFIRMED
        assert d.stop == pytest.approx(99.0 - 0.1)
        assert d.stop_basis == "SWING" and d.context["swing_age_bars"] == 4
        for key in ("ema9", "ema20", "vwap", "atr", "disp", "disp_prev", "disp_roc",
                    "early_threshold", "dist_vwap_atr", "dist_ema20_atr", "initial_stop",
                    "initial_risk", "stop_atr"):
            assert key in d.context

    def test_no_trigger_returns_none(self):
        assert evaluate_bar(row(disp=-0.7, disp_prev=-0.8), P) is None

    def test_ema20_strict_preferred_disabled(self):
        r = row(ema_s=100.5)  # close 100.2 below EMA20 on a long
        assert evaluate_bar(r, P).reason == core.FILTERED_EMA20_CONFLICT
        pref = evaluate_bar(r, P.with_(ema20_filter="preferred"))
        assert pref.accepted and pref.context["ema20_aligned"] is False
        assert evaluate_bar(r, P.with_(ema20_filter="disabled")).accepted

    def test_vwap_chop_is_filtered(self):
        p = P.with_(early_threshold=0.1)
        r = row(disp=-0.05, disp_prev=-0.12, chop_changes=3.0, chop_mean_abs=0.10)
        assert evaluate_bar(r, p).reason == core.FILTERED_VWAP_CHOP
        assert evaluate_bar(r, p.with_(chop_filter=False)).accepted

    def test_chop_statistics_count_zero_crossings(self):
        disp = np.array([0.10, -0.08, 0.15, -0.12, 0.09])
        changes, mean_abs = core._chop_stats(disp, np.zeros(5), 5)
        assert changes[-1] == 4 and mean_abs[-1] == pytest.approx(0.108)

    def test_extension_filter_both_sides(self):
        p = P.with_(entry_mode="crossover")
        assert evaluate_bar(row(disp=1.6, disp_prev=-0.1), p).reason == core.FILTERED_EXCESSIVE_EXTENSION
        r = row(disp=-1.6, disp_prev=0.1, Close=99.8, ema_s=100.5)
        assert evaluate_bar(r, p).reason == core.FILTERED_EXCESSIVE_EXTENSION

    def test_short_mirror_of_the_long(self):
        r = row(disp=0.45, disp_prev=0.55, roc=-0.10, Close=99.8, ema_s=100.0)
        d = evaluate_bar(r, P)
        assert d.accepted and d.direction == -1 and d.reason == core.EARLY_SHORT_MOMENTUM_CONFIRMED
        assert d.stop == pytest.approx(101.0 + 0.1)

    def test_warmup_and_window(self):
        assert evaluate_bar(row(bar_no=10), P).reason == core.FILTERED_WARMUP
        assert evaluate_bar(row(minute=14 * 60 + 15), P).reason == core.FILTERED_OUTSIDE_WINDOW

    def test_falls_back_to_session_extreme_without_a_swing(self):
        d = evaluate_bar(row(swing_low=np.nan), P)
        assert d.stop_basis == "FALLBACK_EXTREME" and d.stop == pytest.approx(99.2 - 0.1)
        assert d.context["swing_age_bars"] == 2

    def test_no_structure_on_the_loss_side(self):
        r = row(swing_low=np.nan, fallback_low=100.5)
        assert evaluate_bar(r, P).reason == core.FILTERED_NO_STRUCTURE


class TestNoLookAhead:
    def test_future_bars_do_not_change_past_inputs_or_decisions(self):
        df = random_bars(sessions=10, seed=3)
        full = build_inputs(df, P)
        cut = df.index[len(df) - 40]
        part = build_inputs(df[df.index <= cut], P)
        cols = ["ema_f", "ema_s", "vwap", "atr", "disp", "disp_prev", "swing_low", "swing_high",
                "fallback_low", "chop_changes", "reversal"]
        pd.testing.assert_frame_equal(part[cols], full.loc[part.index, cols])
        d_full = {k: v for k, v in evaluate_frame(full, P).items() if k <= cut}
        d_part = evaluate_frame(part, P)
        assert d_full.keys() == d_part.keys()
        for k, v in d_part.items():
            assert v.reason == d_full[k].reason
            assert v.stop == pytest.approx(d_full[k].stop, nan_ok=True)


class TestReversalState:
    def test_one_event_per_reversal_run(self):
        disp = np.array([-0.6, -0.5, -0.4, -0.3, -0.2, -0.1])
        close = np.arange(6, dtype=float)
        out = core._reversals(disp, close, 0.5, 0.05)
        assert out.tolist().count(1) == 1


# ---- trailing stop ------------------------------------------------------------------------------


def bar(o, h, low, c, disp=0.2, a=1.0, swing_low=np.nan, swing_high=np.nan):
    return TrailBar(o, h, low, c, disp, a, swing_low, swing_high)


class TestTrailState:
    def test_rejects_stop_on_the_wrong_side(self):
        with pytest.raises(ValueError):
            TrailState(1, 100.0, 101.0, P)

    def test_initial_stop_touch_is_left_to_the_broker(self):
        st = TrailState(1, 100.0, 98.0, P)
        assert st.step(bar(99.5, 100, 97.9, 98.5, disp=-0.6)).reason == core.INITIAL_STOP
        assert st.mae_pre == pytest.approx(1.0)  # adverse excursion counted to the stop, not the low
        assert len(st.path) == 1 and st.path[0]["mae_r"] == pytest.approx(1.0)

    def test_gap_through_the_initial_stop_counts_to_the_open(self):
        st = TrailState(1, 100.0, 98.0, P)
        st.step(bar(97.0, 97.5, 96.0, 96.5, disp=-0.9))
        assert st.mae_pre == pytest.approx(1.5)

    def test_no_trailing_before_activation(self):
        st = TrailState(1, 100.0, 98.0, P)  # activation: crossover
        st.step(bar(100, 103, 99.5, 102.5, disp=-0.2))
        assert st.stop == 98.0 and not st.activated

    def test_trails_after_crossover_and_exits_on_touch(self):
        st = TrailState(1, 100.0, 98.0, P)
        assert st.step(bar(100, 103, 99.5, 102.8, disp=0.1)) is None  # crossover: 103 - 1 ATR
        assert st.crossed and st.stop == pytest.approx(102.0)
        out = st.step(bar(102.5, 102.6, 101.9, 102.4, disp=0.15))
        assert out.reason == core.TRAILING_STOP and out.trigger == "touched"
        assert out.stop == pytest.approx(102.0)

    def test_closed_beyond_the_new_stop(self):
        st = TrailState(1, 100.0, 98.0, P.with_(trailing_activation="immediate"))
        out = st.step(bar(100, 104, 100.0, 102.5, disp=-0.3))  # new stop 103 above close 102.5
        assert out.reason == core.TRAILING_STOP and out.trigger == "closed_beyond"

    def test_short_mirror(self):
        st = TrailState(-1, 100.0, 102.0, P)
        assert st.step(bar(100, 100.5, 97, 97.2, disp=-0.1)) is None
        assert st.stop == pytest.approx(98.0)
        assert st.step(bar(97.5, 98.1, 97.4, 97.9, disp=-0.2)).reason == core.TRAILING_STOP

    @pytest.mark.parametrize("direction", [1, -1])
    def test_stop_never_loosens(self, direction):
        rng = np.random.default_rng(direction + 5)
        p = P.with_(trailing_activation="immediate")
        st = TrailState(direction, 100.0, 100.0 - direction * 3.0, p)
        price, stops = 100.0, [st.stop]
        for _ in range(300):
            if st.exit is not None:
                break
            o = price
            price += direction * 0.05 + rng.normal(0, 0.4)
            h, low = max(o, price) + rng.uniform(0, 0.3), min(o, price) - rng.uniform(0, 0.3)
            st.step(bar(o, h, low, price, disp=float(rng.normal(direction * 0.3, 0.5)),
                        a=float(rng.uniform(0.3, 1.5)), swing_low=low - 1, swing_high=h + 1))
            stops.append(st.stop)
        assert len(stops) > 3
        assert (np.diff(stops) * direction >= -1e-12).all()

    def test_contraction_and_failure_tighten_without_exiting(self):
        st = TrailState(1, 100.0, 98.0, P)
        st.step(bar(100, 104, 100, 103.9, disp=1.0))
        st.step(bar(103.9, 104.5, 103.6, 104.2, disp=0.8))
        st.step(bar(104.2, 104.6, 104.0, 104.3, disp=0.6))  # two falling bars: contraction
        assert st.contracted and st.stop == pytest.approx(104.6 - 0.75)
        assert st.step(bar(104.3, 104.7, 104.1, 104.5, disp=-0.1)) is None  # failure: tighten only
        assert st.failed and st.state == "MOMENTUM_FAILURE"
        assert st.stop == pytest.approx(104.7 - 0.5)

    def test_failure_exit_is_configurable(self):
        p = P.with_(on_momentum_failure="exit", trailing_atr_multiple=3.0, failure_atr_multiple=3.0)
        st = TrailState(1, 100.0, 98.0, p)
        st.step(bar(100, 101, 99.9, 100.8, disp=0.2))
        assert st.step(bar(100.8, 101, 100.5, 100.7, disp=-0.1)).reason == core.MOMENTUM_FAILURE

    def test_activation_modes(self):
        p = P.with_(trailing_activation="r_multiple", trailing_activation_r=0.5)
        st = TrailState(1, 100.0, 98.0, p)
        st.step(bar(100, 100.9, 99.9, 100.5, disp=-0.3))
        assert not st.activated
        st.step(bar(100.5, 101.1, 100.4, 101.0, disp=-0.3))
        assert st.activated
        st = TrailState(1, 100.0, 98.0, P.with_(trailing_activation="displacement"))
        st.step(bar(100, 101, 99.9, 100.8, disp=0.3))
        assert not st.activated
        st.step(bar(100.8, 101.2, 100.6, 101, disp=0.55))
        assert st.activated

    def test_crossover_recorded_once_with_split_excursions(self):
        st = TrailState(1, 100.0, 98.0, P.with_(trailing_atr_multiple=5.0))
        st.step(bar(100, 100.4, 99.0, 100.2, disp=-0.3))
        st.step(bar(100.2, 101, 100.1, 100.9, disp=0.1))
        first = st.cross_time, st.cross_price
        st.step(bar(100.9, 102, 100.8, 101.8, disp=0.3))
        assert (st.cross_time, st.cross_price) == first
        snap = st.snapshot()
        assert snap["crossover_occurred"] and snap["mae_pre_cross_r"] == pytest.approx(0.5)
        assert snap["mfe_post_cross_r"] == pytest.approx(1.0)
        assert snap["entry_advantage_vs_cross_r"] == pytest.approx(0.45)


# ---- pipeline strategy and trail driver ---------------------------------------------------------


class FakePositions:
    def __init__(self):
        self.by_symbol = {}

    def find_position(self, symbol, strategy):
        pos = self.by_symbol.get(symbol)
        return pos if pos is not None and pos.strategy == strategy else None


class WholeFrameContext:
    """RollingContext stand-in: the frame passed in already holds the full history."""

    def window(self, symbol, today5, today1):
        return today5, None


def pos_for(fire, fill: float, entry_time: pd.Timestamp) -> TrackedPosition:
    return TrackedPosition(
        symbol=fire.symbol, strategy=TAG, exchange="NSE", product="MIS",
        entry_price=fire.entry_ref, quantity=10, sl=fire.sl, tp=0.0,
        direction=Direction.LONG if fire.direction == 1 else Direction.SHORT, fill_price=fill,
        original_quantity=10, entry_time=entry_time.to_pydatetime(), context=dict(fire.context),
        sig_id=f"{TAG}-SYN",
    )


def make_driver(positions, diagnostics_dir=None, manage_until_min=14 * 60 + 45) -> TrailExitDriver:
    return TrailExitDriver(strategy=EarlyDisplacementStrategy(), context=WholeFrameContext(),
                           positions=positions, interval_min=5, manage_until_min=manage_until_min,
                           diagnostics_dir=diagnostics_dir)


def run_driver(df: pd.DataFrame, fire, fill_bar: pd.Timestamp):
    """Step a TrailExitDriver bar by bar from the fill; returns (decided bar, exit signals)."""
    positions = FakePositions()
    driver = make_driver(positions)
    fill = float(df.loc[fill_bar, "Open"])
    positions.by_symbol["SYN"] = pos_for(fire, fill, fill_bar + pd.Timedelta(seconds=5))
    sent, decided = [], None
    for b in df.index[(df.index >= fill_bar) & (df.index.date == fill_bar.date())]:
        if b.hour * 60 + b.minute >= 14 * 60 + 45:
            break
        r = df.loc[b]
        out = asyncio.run(
            driver.on_bar("SYN", b, r.Open, r.High, r.Low, r.Close, frame=df[df.index <= b])
        )
        if out and decided is None:
            decided = b
        sent += out
    return decided, sent


def all_fires(df: pd.DataFrame) -> dict:
    fires, pipe = {}, EarlyDisplacementStrategy()
    for day in sorted(set(df.index.date)):
        for f in pipe.evaluate("SYN", df[df.index.date <= day], None):
            fires[f.bar_time] = f
    return fires


class TestPipelineStrategy:
    def test_fires_carry_no_take_profit(self):
        fires = list(all_fires(random_bars(sessions=12)).values())
        assert fires
        for f in fires:
            assert f.targets == () and f.context["take_profit"] == "none"
            assert f.context["signal_reason"] == f.tag
            signal = fire_to_signal(f, "MIS", strategy=TAG)
            assert signal.tp == 0.0  # 0 = none, never a price

    def test_structural_stop_is_beyond_the_swing(self):
        for f in all_fires(random_bars(sessions=12)).values():
            ctx = f.context
            assert ctx["stop_basis"] in ("SWING", "FALLBACK_EXTREME")
            assert f.sl == pytest.approx(ctx["swing_level"] - f.direction * 0.10 * ctx["atr"])
            assert ctx["entry_to_swing_atr"] == pytest.approx(abs(f.entry_ref - ctx["swing_level"]) / ctx["atr"])

    def test_only_the_latest_session_is_evaluated(self):
        df = random_bars(sessions=12)
        fires = EarlyDisplacementStrategy().evaluate("SYN", df, None)
        assert {f.bar_time.date() for f in fires} <= {df.index[-1].date()}


def _trades(df, p=P):
    strat = EmaVwapEarly()
    run = dataclasses.replace(RunConfig(), **strat.run_overrides)
    return Backtest(strat, {"SYN": df}, run=run).trades(p)


class TestTrailDriverAndParity:
    def test_driver_decides_the_same_exit_bar_as_the_backtest(self):
        df = random_bars(sessions=14, seed=11)
        trails = [t for t in _trades(df) if t.reason == core.TRAILING_STOP]
        assert trails, "fixture produced no trailing exits"
        fires = all_fires(df)
        for t in trails[:6]:
            fill_bar = pd.Timestamp(t.entry_time)
            fire = fires[fill_bar - pd.Timedelta(minutes=5)]
            decided, sent = run_driver(df, fire, fill_bar)
            # backtest fills the exit at the open of the bar after the deciding bar
            assert decided == pd.Timestamp(t.exit_time) - pd.Timedelta(minutes=5)
            assert len(sent) == 1  # one exit per position
            sig = sent[0]
            assert sig.direction == Direction.EXIT and sig.tp_level == core.TRAILING_STOP
            assert sig.exit_qty_pct == 1.0 and sig.context["exit_reason"] == core.TRAILING_STOP
            assert sig.tp == core.PAPER_TP_NONE  # the exit carries no target either
            assert "mfe_r" in sig.context and "crossover_occurred" in sig.context

    @pytest.mark.parametrize("name", list(BASELINES))
    def test_backtest_takes_no_target_and_flattens_each_day(self, name):
        trades = _trades(random_bars(sessions=14, seed=11), BASELINES[name])
        assert trades
        assert not any(t.reason.startswith("TP") for t in trades)
        assert all(np.isnan(t.tp) for t in trades)
        assert all(pd.Timestamp(t.exit_time).date() == pd.Timestamp(t.entry_time).date() for t in trades)

    def test_driver_ignores_symbols_without_a_position(self):
        driver = make_driver(FakePositions())
        b = pd.Timestamp("2026-09-01 10:00", tz=IST)
        assert asyncio.run(driver.on_bar("SYN", b, 1, 1, 1, 1, frame=None)) == []
        assert not driver.holds("SYN")

    def test_every_close_path_writes_one_record(self, tmp_path):
        df = random_bars(sessions=14, seed=11)
        trades = _trades(df)
        fires = all_fires(df)
        seen = set()
        for t in trades:
            if t.reason not in ("SL", "TRAILING_STOP", "TIME_EXIT") or t.reason in seen:
                continue
            seen.add(t.reason)
            fill_bar = pd.Timestamp(t.entry_time)
            fire = fires[fill_bar - pd.Timedelta(minutes=5)]
            positions = FakePositions()
            driver = make_driver(positions, diagnostics_dir=tmp_path)
            positions.by_symbol["SYN"] = pos_for(fire, float(df.loc[fill_bar, "Open"]),
                                                 fill_bar + pd.Timedelta(seconds=5))
            for b in df.index[(df.index >= fill_bar) & (df.index.date == fill_bar.date())]:
                if b.hour * 60 + b.minute >= 14 * 60 + 45:
                    break
                r = df.loc[b]
                frame = df[df.index <= b]
                touched = r.Low <= fire.sl if fire.direction == 1 else r.High >= fire.sl
                if t.reason == "SL" and touched:
                    positions.by_symbol.pop("SYN")  # the broker SL-M filled; the tracker removed it
                out = asyncio.run(driver.on_bar("SYN", b, r.Open, r.High, r.Low, r.Close, frame=frame))
                if out:
                    positions.by_symbol.pop("SYN")  # main booked the trailing exit
                if not driver.holds("SYN"):
                    break
            want = {"SL": core.INITIAL_STOP, "TRAILING_STOP": core.TRAILING_STOP, "TIME_EXIT": core.FORCE_EXIT}
            assert [r["exit_reason"] for r in driver.records] == [want[t.reason]]
            rec = driver.records[0]
            assert rec["fill_price_source"] == "broker"
            assert len(rec["path"]) == rec["bars_held"] >= 1
            for bar in rec["path"]:
                for key in ("time", "open", "high", "low", "close", "ema9", "ema20", "vwap", "atr",
                            "disp", "roc", "stop_in_force", "stop_after", "mfe_r", "mae_r", "state"):
                    assert key in bar, key
            assert rec["path"][-1]["mfe_r"] == pytest.approx(rec["mfe_r"], abs=1e-4)
            pre = rec["pre_signal_path"]
            assert 1 <= len(pre) <= core.PRE_SIGNAL_BARS and pre[-1]["time"] == rec["signal_time"]
            if t.reason == "SL":
                assert rec["mae_r"] >= 1.0 - 1e-9  # stopped at the initial stop: at least 1R against
            for key in ("sig_id", "signal_reason", "stop_atr", "entry_to_swing_atr", "mae_pre_cross_r",
                        "mfe_pre_cross_r", "mfe_post_cross_r", "crossover_occurred", "minutes_in_trade",
                        "entry_half_hour", "entry_advantage_vs_cross_r", "signal_time", "entry_time",
                        "fill_price", "signal_price", "stop_basis", "swing_level", "initial_stop",
                        "swing_age_bars", "crossover_time", "mae_post_cross_r", "exit_reason",
                        "exit_price_reference", "symbol", "fill_price_source", "atr_regime",
                        "pre_signal_path", "signals_today_before", "trades_today_before",
                        "disp_at_last_bar", "disp_max_in_trade", "disp_min_in_trade", "bars_to_mfe",
                        "bars_to_mae", "dist_vwap_atr", "dist_ema20_atr", "ema9_to_vwap_atr", "path"):
                assert key in rec, key
        assert seen, "fixture produced no trades"
        files = list(tmp_path.glob("evwap-early-py_diagnostics_*.jsonl"))
        lines = [ln for f in files for ln in f.read_text().splitlines()]
        assert len(lines) == len(seen)


class TestReport:
    def test_summary_and_join(self, tmp_path):
        from signal_engine.analysis import evwap_early_report as rep

        recs = [
            {"symbol": "TCS", "sig_id": "a", "direction": "LONG", "fill_price": 100.0, "initial_stop": 98.0,
             "exit_reason": "TRAILING_STOP", "crossover_occurred": True, "mae_pre_cross_r": 0.2,
             "mfe_pre_cross_r": 0.4, "entry_advantage_vs_cross_r": 0.3, "mfe_post_cross_r": 1.5,
             "mae_post_cross_r": 0.1,
             "mfe_r": 1.9, "mae_r": 0.2, "stop_atr": 2.0, "entry_to_swing_atr": 1.9,
             "minutes_in_trade": 40, "entry_half_hour": "10:00", "stop_basis": "SWING"},
            {"symbol": "INFY", "sig_id": "b", "direction": "SHORT", "fill_price": 50.0, "initial_stop": 51.0,
             "exit_reason": "INITIAL_STOP", "crossover_occurred": False, "mae_pre_cross_r": 1.0,
             "mfe_pre_cross_r": 0.1, "entry_advantage_vs_cross_r": None, "mfe_post_cross_r": 0.0,
             "mae_post_cross_r": 0.0,
             "mfe_r": 0.1, "mae_r": 1.0, "stop_atr": 3.0, "entry_to_swing_atr": 2.9,
             "minutes_in_trade": 10, "entry_half_hour": "11:30", "stop_basis": "FALLBACK_EXTREME"},
        ]
        path = tmp_path / "evwap-early-py_diagnostics_2026-10-09.jsonl"
        path.write_text("\n".join(__import__("json").dumps(r) for r in recs))
        df = rep.load_records(["2026-10-09"], tmp_path)
        df = rep.attach_pnl(df, {"a": {"pnl": 300.0, "exit_fill": 103.0}, "b": {"pnl": -100.0, "exit_fill": 51.0}})
        assert df["realized_r"].round(3).tolist() == [1.5, -1.0]
        s = rep.summarise(df)
        assert s["trades"] == 2 and s["A_reached_crossover_pct"] == 50.0
        assert s["A_stopped_before_crossover_pct"] == 50.0
        assert s["A_entry_advantage_vs_cross_r_median"] == 0.3 and s["pnl_sum"] == 200.0
        assert s["B_post_cross_extension_r_median"] == 1.2  # 1.5 MFE after cross - 0.3 advantage
        assert s["B_early_entry_capture_median"] == 0.2  # 0.3 of the 1.5 R best excursion
        assert s["B_trail_capture_median"] == pytest.approx(round(1.5 / 1.9, 3))
        assert s["C_initial_stop_hit_pct"] == 50.0 and s["C_mae_atr_median"] == pytest.approx(1.7)
        assert set(rep.by(df, "entry_half_hour").index) == {"10:00", "11:30"}
        split = rep.by_stop_basis(df)
        assert split.loc["net_r", "SWING"] == 1.5 and split.loc["net_r", "FALLBACK_EXTREME"] == -1.0
        assert split.loc["stop_out_rate", "FALLBACK_EXTREME"] == 1.0
        assert split.loc["crossover_conversion", "SWING"] == 1.0
        assert split.loc["median_stop_atr", "FALLBACK_EXTREME"] == 3.0



class TestFallbackStop:
    def test_fallback_uses_the_preceding_bars_and_excludes_the_signal_bar(self):
        idx = session_index(dt.date(2026, 9, 1))[:9]
        low = np.array([10.0, 9.0, 8.0, 9.5, 9.6, 9.7, 9.8, 9.9, 7.0])  # bar 8 is the signal bar
        df = pd.DataFrame({"Open": low + 0.5, "High": low + 1, "Low": low, "Close": low + 0.5,
                           "Volume": 1.0}, index=idx)
        inp = build_inputs(df, P)
        # bar 8: preceding 6 bars are 2..7, min 8.0; the signal bar's own 7.0 is excluded
        assert inp["fallback_low"].iloc[8] == 8.0
        # bar 7: preceding 6 bars are 1..6, min 8.0; bar 1: only bar 0 precedes
        assert inp["fallback_low"].iloc[7] == 8.0 and inp["fallback_low"].iloc[1] == 10.0
        assert np.isnan(inp["fallback_low"].iloc[0])  # nothing precedes the first bar
        assert inp["fallback_high"].iloc[8] == (low[2:8] + 1).max()
        assert inp["fallback_low_bar"].iloc[8] == 2  # bar 2 holds the 8.0 low

    def test_fallback_does_not_cross_sessions(self):
        inp = build_inputs(random_bars(sessions=3), P)
        firsts = inp.groupby(inp.index.date).head(1)
        assert firsts["fallback_low"].isna().all() and firsts["fallback_high"].isna().all()

    def test_excursion_timing_and_slippage(self):
        from signal_engine.analysis import evwap_early_report as rep

        path = [{"mfe_r": 0.1 * k, "mae_r": 0.05 * k} for k in range(1, 20)]  # 19 bars = 95 minutes
        df = pd.DataFrame([{
            "symbol": "TCS", "sig_id": "a", "direction": "LONG", "fill_price": 100.5,
            "signal_price": 100.0, "fill_price_source": "broker", "atr": 1.0, "initial_risk": 2.0,
            "initial_stop": 98.0, "exit_reason": "TRAILING_STOP", "exit_price_reference": 103.0,
            "entry_advantage_vs_cross_r": 0.2, "mfe_post_cross_r": 1.0, "mfe_r": 1.9, "mae_r": 0.9,
            "path": path,
        }])
        df = rep.attach_pnl(df, {"a": {"pnl": 100.0, "exit_fill": 102.8}})
        t = rep.excursion_by_time(df)
        assert t.loc["median_mfe_r", "5m"] == pytest.approx(0.1)
        assert t.loc["median_mfe_r", "30m"] == pytest.approx(0.6)
        assert t.loc["median_mae_r", "90m"] == pytest.approx(0.9)
        s = rep.slippage(df)
        assert s.loc["median_inr", "ENTRY"] == pytest.approx(0.5)  # paid 0.5 above the signal close
        assert s.loc["median_r", "ENTRY"] == pytest.approx(0.25)
        assert s.loc["median_inr", "EXIT TRAILING_STOP"] == pytest.approx(0.2)  # sold 0.2 below the stop



class TestMissedInitialStop:
    """The paper sandbox can miss a short spike to the SL-M; the driver must not hold the trade blind,
    and must never send a duplicate close."""

    @staticmethod
    def _sl_trade():
        df = random_bars(sessions=14, seed=11)
        trade = next(t for t in _trades(df) if t.reason == "SL")
        fill_bar = pd.Timestamp(trade.entry_time)
        fire = all_fires(df)[fill_bar - pd.Timedelta(minutes=5)]
        return df, fire, fill_bar

    def _run(self, broker_answers, close_on_send=True, max_bars=None):
        """broker_qty answers are magnitudes as service.make_broker_qty returns them (-1 unknown).
        Step bar by bar. A send closes the position (main booked it) unless close_on_send is
        False (main aborted). Returns (touch bar, [(bar, signal)], broker calls, driver)."""
        df, fire, fill_bar = self._sl_trade()
        positions = FakePositions()
        answers = list(broker_answers)
        calls = []

        async def broker_qty(pos):
            calls.append(pos.symbol)
            return answers.pop(0) if answers else 436

        driver = TrailExitDriver(strategy=EarlyDisplacementStrategy(), context=WholeFrameContext(),
                                 positions=positions, interval_min=5, manage_until_min=14 * 60 + 45,
                                 diagnostics_dir=None, broker_qty=broker_qty)
        positions.by_symbol["SYN"] = pos_for(fire, float(df.loc[fill_bar, "Open"]),
                                             fill_bar + pd.Timedelta(seconds=5))
        touch, sent = None, []
        bars = df.index[(df.index >= fill_bar) & (df.index.date == fill_bar.date())]
        for i, b in enumerate(bars):
            if b.hour * 60 + b.minute >= 14 * 60 + 45 or (max_bars and i >= max_bars):
                break
            r = df.loc[b]
            hit = r.Low <= fire.sl if fire.direction == 1 else r.High >= fire.sl
            if hit and touch is None:
                touch = b
            out = asyncio.run(driver.on_bar("SYN", b, r.Open, r.High, r.Low, r.Close, frame=df[df.index <= b]))
            sent += [(b, o) for o in out]
            if out and close_on_send:
                positions.by_symbol.pop("SYN")
            if "SYN" not in positions.by_symbol and not driver.holds("SYN"):
                break
        return touch, sent, calls, driver

    def test_check_fires_ten_minutes_after_the_touch_bar_closes(self):
        touch, sent, calls, driver = self._run([436])
        assert touch is not None and len(sent) == 1 and len(calls) == 1
        bar, sig = sent[0]
        # the cycle for `bar` runs when it closes: touch bar close + 10 minutes on the 5m timeframe
        assert (bar + pd.Timedelta(minutes=5)) - (touch + pd.Timedelta(minutes=5)) == pd.Timedelta(minutes=10)
        assert sig.tp_level == "INITIAL_STOP_SAFETY" and sig.exit_qty_pct == 1.0 and sig.tp == 0.0

    def test_record_is_written_at_the_close_and_keeps_touch_and_execution_apart(self):
        touch, _, _, driver = self._run([436])
        assert len(driver.records) == 1
        rec = driver.records[0]
        assert rec["exit_reason"] == core.INITIAL_STOP  # the strategy event
        assert rec["stop_touch_bar"] == str(touch)  # the bar fact
        assert rec["safety_attempts"] == 1 and rec["safety_broker_answers"] == [436]

    def test_broker_flat_reconciles_as_a_stop_hit(self):
        _, sent, _, driver = self._run([0])
        assert [s.tp_level for _, s in sent] == ["SL"]
        assert driver.records[0]["exit_reason"] == core.INITIAL_STOP

    def test_unknown_broker_position_sends_nothing_and_waits_a_bar(self):
        touch, sent, calls, _ = self._run([-1, 436])
        assert len(calls) == 2 and len(sent) == 1
        assert sent[0][0] == touch + pd.Timedelta(minutes=15)

    def test_main_abort_is_retried_once_per_bar_and_capped(self):
        from signal_engine.pipeline.trail_driver import MAX_SAFETY_ATTEMPTS

        touch, sent, calls, _ = self._run([436] * 10, close_on_send=False)
        bars = [b for b, _ in sent]
        assert len(sent) == MAX_SAFETY_ATTEMPTS  # never more, whatever main does
        assert len(set(bars)) == len(bars)  # at most one send per bar
        assert bars[0] == touch + pd.Timedelta(minutes=10)
        assert len(calls) == MAX_SAFETY_ATTEMPTS  # no broker query once capped


    def test_realized_r_falls_back_to_booked_pnl_without_an_exit_fill(self):
        from signal_engine.analysis import evwap_early_report as rep

        df = pd.DataFrame([{"symbol": "PFC", "sig_id": "p", "direction": "SHORT", "fill_price": 326.2,
                            "initial_stop": 328.2, "entry_advantage_vs_cross_r": None,
                            "mfe_post_cross_r": 0.0, "mae_r": 1.0, "mfe_r": 0.0, "exit_reason": "FORCE_EXIT"}])
        out = rep.attach_pnl(df, {"p": {"pnl": -1417.0, "exit_fill": None, "quantity": 436}})
        assert out["r_source"].iloc[0] == "pnl"
        assert out["realized_r"].iloc[0] == pytest.approx(-1417.0 / (436 * 2.0))

    def test_exit_reason_mismatch_is_flagged(self):
        from signal_engine.analysis import evwap_early_report as rep

        base = {"direction": "SHORT", "fill_price": 326.2, "initial_stop": 328.2,
                "entry_advantage_vs_cross_r": None, "mfe_post_cross_r": 0.0, "mae_r": 1.0, "mfe_r": 0.0,
                "exit_decided_bar": "x"}
        df = pd.DataFrame([{**base, "symbol": "PFC", "sig_id": "p", "exit_reason": "INITIAL_STOP"},
                           {**base, "symbol": "MCX", "sig_id": "m", "exit_reason": "INITIAL_STOP"}])
        closes = {"p": {"pnl": -1417.0, "exit_fill": None, "quantity": 436, "exit_types": ["TIME"]},
                  "m": {"pnl": -861.3, "exit_fill": 3318.0, "quantity": 100, "exit_types": ["SL"]}}
        bad = rep.exit_mismatches(rep.attach_pnl(df, closes))
        assert bad["symbol"].tolist() == ["PFC"]


    def test_stop_execution_separates_touch_from_execution(self):
        from signal_engine.analysis import evwap_early_report as rep

        base = {"direction": "SHORT", "fill_price": 326.2, "initial_stop": 328.2,
                "entry_advantage_vs_cross_r": None, "mfe_post_cross_r": 0.0, "mae_r": 1.0, "mfe_r": 0.0,
                "exit_reason": "INITIAL_STOP"}
        df = pd.DataFrame([
            {**base, "symbol": "A", "sig_id": "a", "stop_touch_bar": "2026-10-12 11:15:00+05:30",
             "exit_decided_bar": "2026-10-12 11:15:00+05:30"},
            {**base, "symbol": "B", "sig_id": "b", "stop_touch_bar": "2026-10-12 11:15:00+05:30",
             "exit_decided_bar": "2026-10-12 11:15:00+05:30"},
            {**base, "symbol": "C", "sig_id": "c", "exit_reason": "TRAILING_STOP", "stop_touch_bar": None,
             "exit_decided_bar": "2026-10-12 12:00:00+05:30"},
        ])
        closes = {
            "a": {"pnl": -900.0, "exit_fill": 328.3, "quantity": 436, "exit_types": ["SL"],
                  "closed_at": "2026-10-12T11:18:00"},
            "b": {"pnl": -1000.0, "exit_fill": 328.6, "quantity": 436, "exit_types": ["INITIAL_STOP_SAFETY"],
                  "closed_at": "2026-10-12T11:30:05"},
            "c": {"pnl": 100.0, "exit_fill": 326.0, "quantity": 436, "exit_types": ["TRAILING_STOP"],
                  "closed_at": "2026-10-12T12:05:00"},
        }
        se = rep.stop_execution(rep.attach_pnl(df, closes))
        assert set(se.index) == {"broker_executed", "safety_market_close"}  # trailing exit not a touch
        assert se.loc["broker_executed", "median_minutes_to_booked"] == pytest.approx(3.0)
        assert se.loc["safety_market_close", "median_minutes_to_booked"] == pytest.approx(15.08, abs=0.01)
        assert rep.exit_mismatches(rep.attach_pnl(df, closes)).empty  # a safety close agrees with INITIAL_STOP
