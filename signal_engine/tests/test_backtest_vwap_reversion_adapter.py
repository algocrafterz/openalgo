"""IDEA-029 adapter: engine wiring, hand-computed trade paths and the information boundary.

Three groups:
  * wiring - registry, spec parameter binding, the run overrides and the entry window;
  * hand-computed paths - the adapter driven by injected inputs (VWAP, bands, EMA9, ATR, regime)
    so every price in a trade is known in advance;
  * information boundary - the real input builder on a synthetic multi-session series, with
    every bar after a cut replaced by garbage: nothing decided at or before the cut may change.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from signal_engine.backtest import __main__ as cli
from signal_engine.backtest import indicators as ind
from signal_engine.backtest import ledgers
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.ledgers import LedgerRecorder
from signal_engine.backtest.record_run import check_parameter_binding, jsonable
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.strategies.vwap_reversion import VwapReversion, VwapReversionParams
from signal_engine.backtest.truncation import truncation_check
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig
from signal_engine.strategy_core.vwap_reversion import (
    INPUT_COLUMNS,
    VwapReversionMachine,
    bar_from_arrays,
    build_inputs,
)

IST = "Asia/Kolkata"
SPEC = (
    Path(__file__).resolve().parents[1]
    / "research/ideas/idea-029-vwap-2sd-range-mean-reversion/spec.yaml"
)
P = VwapReversionParams()


def run_config(strategy=None) -> RunConfig:
    return RunConfig().with_(**(strategy or VwapReversion()).run_overrides)


# ---- wiring ------------------------------------------------------------------------------


class TestWiring:
    def test_registered_as_intraday(self):
        cli._register()
        cls, params_cls, kind = cli.REGISTRY["vwap_reversion"]
        assert (cls, params_cls, kind) == (VwapReversion, VwapReversionParams, "intraday")

    def test_parameters_bind_to_the_frozen_spec(self):
        spec = yaml.safe_load(SPEC.read_text())
        check_parameter_binding(spec["parameters"], jsonable(VwapReversionParams()))

    def test_run_overrides(self):
        run = run_config()
        assert run.skip_open_minutes == 0
        assert run.entry_cutoff_min == 14 * 60 + 15
        assert run.one_trade_per_direction is False
        assert run.max_trades_per_day == 2
        assert run.allow_longs and run.allow_shorts
        assert run.skip_fill_beyond_levels is True
        # execution constraint: the live validator's minimum stop distance stays on
        assert run.min_sl_pct == 0.005

    def test_stop_floor_matches_the_live_validator(self):
        cfg = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8")
        )
        # no per-strategy override exists for this tag, so live applies the global floor
        assert VwapReversion.tag not in cfg.get("strategy_profiles", {})
        assert VwapReversion.run_overrides["min_sl_pct"] == cfg["risk"]["min_sl_pct"]
        # the time exit is the deployment profile's; the adapter does not set it
        assert "time_exit_min" not in VwapReversion.run_overrides
        assert VwapReversion.dynamic_target is True

    @pytest.mark.parametrize(
        "signal_at,fill_at",
        [("09:15", "09:20"), ("14:10", "14:15"), ("14:15", None)],
    )
    def test_entry_window_fills_0920_through_1415(self, signal_at, fill_at):
        class At(Strategy):
            run_overrides = VwapReversion.run_overrides

            def prepare(self, df, p):
                return df

            def entry(self, c, i, p, direction):
                if c.index[i].strftime("%H:%M") == signal_at and direction == 1:
                    return EntrySignal(direction=1, sl=90.0, tp=200.0)
                return None

        df = ind.add_session_columns(day_frame("2026-06-01", [(100, 101, 99, 100, 1000)] * 72))
        trades = simulate(Ctx(df, "T"), At(), None, run_config())
        if fill_at is None:
            assert trades == []
        else:
            assert trades[0].entry_time.strftime("%H:%M") == fill_at


# ---- hand-computed trade paths (injected inputs) --------------------------------------------


def day_frame(day, rows, start="09:15"):
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq="5min", tz=IST)
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


class Injected(VwapReversion):
    """The adapter with its inputs supplied by the test instead of computed from bars."""

    def __init__(self, cols):
        super().__init__()
        self.cols = cols

    def _inputs(self, d, p):
        n = len(d)
        base = {
            "vwap": [100.0] * n,
            "vwap_sd": [1.0] * n,
            "upper": [102.0] * n,
            "lower": [98.0] * n,
            "atr5": [2.0] * n,
            "regime_ok": [True] * n,
            "warm": [True] * n,
            "confirmable": [True] * n,
        }
        base.update(self.cols)
        out = pd.DataFrame(base, index=d.index)
        if "ema9" not in out:
            out["ema9"] = d["Close"] - 0.1
        for col in INPUT_COLUMNS:
            if col not in out:
                out[col] = 0.0
        return out


FLAT = (99.0, 99.4, 98.6, 99.0, 1000)


def simulate_injected(rows, cols, start="09:15"):
    s = Injected(cols)
    df = day_frame("2026-06-01", rows, start)
    rec = LedgerRecorder()
    c = Ctx(s.prepare(df, P), "T")
    trades = simulate(c, s, P, run_config(s), rec)
    assert rec.reconcile() == []
    return trades, s, rec


LONG_ARM = (99.0, 99.5, 97.5, 98.2, 1000)  # low 97.5 <= 98, (100 - 97.5) / 2 = 1.25
LONG_CONF = (98.2, 99.2, 98.1, 98.6, 1000)  # close 98.6 > EMA9 98.5 and > band 98


class TestLongPath:
    def rows(self, bar2, bar3):
        return [LONG_ARM, LONG_CONF, bar2, bar3] + [FLAT] * 6

    def ema(self, n):
        return [99.0, 98.5] + [99.1] * (n - 2)

    def test_full_trade_exits_at_previous_bar_vwap(self):
        vwap = [100.0, 100.0, 99.8, 99.9] + [100.0] * 6
        trades, s, _ = simulate_injected(
            self.rows((98.6, 99.5, 98.3, 99.0, 1000), (99.0, 100.5, 98.9, 100.2, 1000)),
            {"vwap": vwap, "ema9": self.ema(10)},
        )
        assert len(trades) == 1
        t = trades[0]
        assert t.direction == 1
        assert t.entry_time.strftime("%H:%M") == "09:25"
        assert t.entry == pytest.approx(98.6) and t.signal_price == pytest.approx(98.6)
        assert t.sl == pytest.approx(97.5 - 0.2 * 2.0)
        assert t.tp == 100.0  # VWAP at the confirmation bar's close: the entry bar's target
        assert t.reason == "TP"
        assert t.exit == pytest.approx(99.8)  # VWAP of bar 2, not bar 3's 99.9
        d = s.diagnostics()
        assert d["setups_armed_long"] == 1 and d["confirmations"] == 1

    def test_open_beyond_target_fills_at_open(self):
        vwap = [100.0, 100.0, 99.8, 99.9] + [100.0] * 6
        trades, _, _ = simulate_injected(
            self.rows((98.6, 99.5, 98.3, 99.0, 1000), (99.9, 100.5, 99.7, 100.2, 1000)),
            {"vwap": vwap, "ema9": self.ema(10)},
        )
        assert trades[0].reason == "TP" and trades[0].exit == pytest.approx(99.9)

    def test_stop_first_when_one_bar_reaches_both(self):
        trades, _, _ = simulate_injected(
            self.rows((98.6, 99.5, 98.3, 99.0, 1000), (99.0, 100.5, 97.0, 99.0, 1000)),
            {"ema9": self.ema(10)},
        )
        assert trades[0].reason == "SL" and trades[0].exit == pytest.approx(97.1)

    @pytest.mark.parametrize("open_", [100.0, 100.4, 97.1, 96.5])
    def test_entry_open_beyond_a_level_is_skipped(self, open_):
        trades, s, rec = simulate_injected(
            self.rows((open_, open_ + 0.2, open_ - 0.2, open_, 1000), FLAT),
            {"ema9": self.ema(10)},
        )
        assert trades == []
        assert s.diagnostics()["entries_skipped_beyond_level"] == 1
        exc = rec.to_frames()["exclusions"]
        assert ledgers.ENTRY_SKIPPED_BEYOND_LEVEL in set(exc["reason_code"])


class TestShortPath:
    def test_full_trade(self):
        rows = [
            (101.0, 102.6, 100.9, 101.8, 1000),  # high 102.6 >= 102, (102.6 - 100) / 2 = 1.3
            (101.8, 101.9, 100.8, 101.4, 1000),  # close 101.4 < EMA9 101.5 and < band 102
            (101.4, 101.6, 100.6, 101.0, 1000),  # entry at 101.4, target vwap[1] = 100.0
            (101.0, 101.1, 99.9, 100.0, 1000),  # target vwap[2] = 100.2 reached
        ] + [(101.0, 101.4, 100.6, 101.0, 1000)] * 6
        vwap = [100.0, 100.0, 100.2, 99.0] + [100.0] * 6
        ema9 = [101.0, 101.5] + [100.9] * 8
        trades, s, _ = simulate_injected(rows, {"vwap": vwap, "ema9": ema9})
        assert len(trades) == 1
        t = trades[0]
        assert (t.direction, t.reason) == (-1, "TP") and t.entry == pytest.approx(101.4)
        assert t.sl == pytest.approx(102.6 + 0.4)
        assert t.exit == pytest.approx(100.2)
        assert s.diagnostics()["setups_armed_short"] == 1


class TestReEntryAndCap:
    def test_two_trades_then_cap_blocks_the_third(self):
        # each cycle: arm, confirm, fill, exit at the flat-VWAP target on the next bar
        cycle = [
            LONG_ARM,
            LONG_CONF,
            (98.6, 99.0, 98.3, 98.8, 1000),
            (98.8, 100.2, 98.7, 100.0, 1000),
        ]
        ema_cycle = [99.0, 98.5, 99.5, 99.5]
        rows = cycle * 3 + [FLAT] * 3
        trades, s, _ = simulate_injected(rows, {"ema9": ema_cycle * 3 + [99.5] * 3})
        assert len(trades) == 2
        assert all(t.reason == "TP" for t in trades)
        d = s.diagnostics()
        assert d["confirmations"] == 3 and d["confirmations_blocked_by_cap"] == 1

    def test_last_fill_is_1415(self):
        # extreme 14:05, confirmation 14:10, fill 14:15
        rows = [FLAT, LONG_ARM, LONG_CONF, (98.6, 99.0, 98.3, 98.8, 1000)] + [FLAT] * 3
        trades, _, _ = simulate_injected(
            rows, {"ema9": [99.5, 99.0, 98.5] + [99.5] * 4}, start="14:00"
        )
        assert trades[0].entry_time.strftime("%H:%M") == "14:15"

    def test_confirmation_at_1415_is_too_late(self):
        rows = [FLAT, FLAT, LONG_ARM, LONG_CONF] + [FLAT] * 3
        mins = [14 * 60 + 5 * k for k in range(7)]
        trades, s, _ = simulate_injected(
            rows,
            {
                "ema9": [99.5, 99.5, 99.0, 98.5] + [99.5] * 3,
                "confirmable": [m <= 14 * 60 + 10 for m in mins],
            },
            start="14:00",
        )
        assert trades == []
        assert s.diagnostics()["setups_cancelled_cutoff"] == 1


class TestDiagnosticsAcrossSymbols:
    def test_accumulate_over_symbols_on_one_instance(self):
        s = Injected({"ema9": [99.0, 98.5] + [99.1] * 8})
        rows = [LONG_ARM, LONG_CONF] + [FLAT] * 8
        for sym in ("A", "B"):
            c = Ctx(s.prepare(day_frame("2026-06-01", rows), P), sym)
            simulate(c, s, P, run_config(s))
        assert s.diagnostics()["confirmations"] == 2


# ---- the real input builder: synthetic multi-session series ---------------------------------


def synthetic(n_sess=10, seed=7, start="2026-03-02"):
    """Mean-reverting 5m bars with spikes, 09:15-15:25 (auction bars included)."""
    rng = np.random.default_rng(seed)
    frames = []
    level = 500.0
    for d in pd.bdate_range(start, periods=n_sess):
        idx = pd.date_range(f"{d.date()} 09:15", f"{d.date()} 15:25", freq="5min", tz=IST)
        n = len(idx)
        x = np.zeros(n)
        for k in range(1, n):
            shock = rng.normal(0, 0.6)
            if rng.random() < 0.06:
                shock += rng.choice([-1, 1]) * rng.uniform(3, 6)
            x[k] = 0.7 * x[k - 1] + shock
        close = level + x
        open_ = np.r_[level + rng.normal(0, 0.5), close[:-1]]
        high = np.maximum(open_, close) + rng.uniform(0.05, 0.8, n)
        low = np.minimum(open_, close) - rng.uniform(0.05, 0.8, n)
        vol = rng.integers(1_000, 20_000, n).astype(float)
        frames.append(
            pd.DataFrame(
                {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
                index=idx,
            )
        )
        level = close[-1]
    return pd.concat(frames)


def garbage_after(df, t, seed):
    rng = np.random.default_rng(seed)
    out = df.copy()
    after = out.index > t
    n = int(after.sum())
    o = rng.uniform(1, 2000, n)
    c = rng.uniform(1, 2000, n)
    out.loc[after, "Open"] = o
    out.loc[after, "Close"] = c
    out.loc[after, "High"] = np.maximum(o, c) + rng.uniform(0, 300, n)
    out.loc[after, "Low"] = np.minimum(o, c) - rng.uniform(0, 0.9, n) * np.minimum(o, c)
    out.loc[after, "Volume"] = rng.uniform(0, 1e7, n)
    return out


class Recording(VwapReversion):
    """The real adapter, logging every decision with the bar index it was made on."""

    def reset_symbol(self, p):
        super().reset_symbol(p)
        self.log = []

    def on_bar(self, c, i, p):
        super().on_bar(c, i, p)
        conf = self._pending_conf
        self.log.append(
            (
                i,
                "bar",
                None if conf is None else (conf.direction, conf.extreme, conf.stop, conf.target),
                tuple(self.diagnostics().values()),
            )
        )

    def target(self, c, j, p, pos):
        v = super().target(c, j, p, pos)
        self.log.append((j, "target", v))
        return v

    def on_fill(self, c, i, p, pos):
        super().on_fill(c, i, p, pos)
        self.log.append((i, "fill", pos.direction, pos.sl, pos.tp))

    def on_exit(self, c, i, p, trade):
        super().on_exit(c, i, p, trade)
        self.log.append((i, "exit", trade.reason, trade.exit))


def run_recorded(df, params=P):
    s = Recording()
    prepared = s.prepare(df.copy(), params)
    trades = simulate(Ctx(prepared, "SYN"), s, params, run_config(s))
    return prepared, s.log, trades, s.diagnostics()


#: A small warm-up so the synthetic series trades well inside ten sessions. Only the warm-up
#: length differs from the spec; the information boundary does not depend on it.
P_FAST = VwapReversionParams(warmup_15m_bars=30)


@pytest.fixture(scope="module")
def series():
    df = synthetic()
    prepared, log, trades, diag = run_recorded(df, P_FAST)
    return df, prepared, log, trades, diag


def frame_equal_upto(a, b, t, cols):
    left = a.loc[a.index <= t, list(cols)]
    right = b.loc[b.index <= t, list(cols)]
    pd.testing.assert_frame_equal(left, right, check_exact=True)


class TestSyntheticSeriesIsInformative:
    def test_the_series_exercises_the_machine(self, series):
        _, _, _, trades, diag = series
        assert len(trades) >= 4
        assert diag["confirmations"] >= 4
        assert diag["setups_armed_long"] > 0 and diag["setups_armed_short"] > 0
        assert diag["blocked_by_warmup"] + diag["blocked_by_regime"] > 0


class TestInformationBoundary:
    """(a) garbage after t changes nothing decided at or before t."""

    CUTS = (
        "2026-03-04 12:05",  # a fill bar
        "2026-03-05 09:20",
        "2026-03-05 09:25",
        "2026-03-06 11:40",
        "2026-03-09 13:05",
        "2026-03-10 14:10",
        "2026-03-11 14:00",  # inside an open position
        "2026-03-12 10:30",
        "2026-03-13 15:10",
    )

    @pytest.mark.parametrize("cut", CUTS)
    def test_perturbation(self, series, cut):
        df, prepared, log, trades, _ = series
        t = pd.Timestamp(cut, tz=IST)
        for seed in (1, 2):
            noisy = garbage_after(df, t, seed)
            p2, log2, trades2, _ = run_recorded(noisy, P_FAST)
            # every input value a decision at or before t can read
            frame_equal_upto(prepared, p2, t, INPUT_COLUMNS + ("Open", "High", "Low", "Close"))
            k_t = int((prepared.index <= t).sum()) - 1
            # arming / cancel / confirmation (with stop and target), targets, fills, exits
            assert [e for e in log if e[0] <= k_t] == [e for e in log2 if e[0] <= k_t]
            done = [x for x in trades if x.exit_time <= t]
            done2 = [x for x in trades2 if x.exit_time <= t]
            assert [vars(x) for x in done] == [vars(x) for x in done2]
            # the garbage did reach the run: something after t differs
            assert not prepared.loc[prepared.index > t, "Close"].equals(
                p2.loc[p2.index > t, "Close"]
            )

    def test_cuts_cover_decisions(self, series):
        _, prepared, log, _, _ = series
        last = prepared.index[-1]
        assert any(e[1] == "fill" for e in log)
        assert pd.Timestamp(self.CUTS[-1], tz=IST) <= last


class TestFifteenMinuteBoundary:
    """(b) the 09:25 decision sees the 09:15-09:30 block; the 09:20 decision does not."""

    def test_0925_sees_the_block_and_0920_does_not(self, series):
        df = series[0]
        inputs = build_inputs(df, P)
        m15_close = inputs.loc[pd.Timestamp("2026-03-04 09:25", tz=IST)]
        m15_prev = inputs.loc[pd.Timestamp("2026-03-04 09:20", tz=IST)]
        assert m15_close["n15"] == m15_prev["n15"] + 1
        # the 09:20 decision reads the previous session's last block (15:00)
        assert m15_prev["n15"] == inputs.loc[pd.Timestamp("2026-03-03 15:10", tz=IST), "n15"]

    def test_changing_the_0925_bar_leaves_the_0920_decision_alone(self, series):
        df = series[0]
        at = pd.Timestamp("2026-03-04 09:25", tz=IST)
        bumped = df.copy()
        bumped.loc[at, ["High", "Close"]] = bumped.loc[at, ["High", "Close"]] + 25.0
        a, b = build_inputs(df, P), build_inputs(bumped, P)
        early = a.index < at
        pd.testing.assert_frame_equal(a.loc[early], b.loc[early], check_exact=True)
        assert a.loc[at, "ema20_15"] != b.loc[at, "ema20_15"]


class TestTargetBoundary:
    """(c) bar j's own data never moves bar j's target."""

    def test_target_on_bar_j_ignores_bar_j(self, series):
        df = series[0]
        s = VwapReversion()
        j = int(np.flatnonzero(df.index == pd.Timestamp("2026-03-06 11:00", tz=IST))[0])
        bumped = df.copy()
        bumped.iloc[j, :] = [700.0, 900.0, 600.0, 880.0, 5e6]
        ca = Ctx(s.prepare(df.copy(), P), "A")
        cb = Ctx(s.prepare(bumped, P), "A")
        assert s.target(ca, j, P, None) == s.target(cb, j, P, None)
        assert ca["vwap"][j] != cb["vwap"][j]

    def test_recorded_targets_are_previous_bar_vwap(self, series):
        _, prepared, log, _, _ = series
        vwap = prepared["vwap"].to_numpy(dtype="float64")
        targets = [e for e in log if e[1] == "target"]
        assert targets
        assert all(v == float(vwap[j - 1]) for j, _, v in targets)


class TestContinuity:
    def two_sessions_and_a_gap(self):
        df = synthetic(n_sess=3)
        days = sorted(set(df.index.date))
        return df[pd.Index(df.index.date) != days[1]], days

    def test_missing_session_neither_resets_nor_adds_observations(self):
        df, days = self.two_sessions_and_a_gap()
        x = build_inputs(df, P)
        core = df[(df.index.hour * 60 + df.index.minute) <= 15 * 60 + 10]
        d1 = core[core.index.date == days[0]]
        d3 = core[core.index.date == days[2]]
        first3 = d3.index[0]
        # 15m count: 24 blocks of day 1, the gap adds none
        assert x.loc[first3, "n15"] == 24
        assert x.loc[d3.index[2], "n15"] == 25
        # EMA9 continues from day 1's last value
        alpha = 2 / (9 + 1)
        prev = x.loc[d1.index[-1], "ema9"]
        assert x.loc[first3, "ema9"] == pytest.approx(
            alpha * d3["Close"].iloc[0] + (1 - alpha) * prev, rel=1e-12
        )
        # ATR14 continues, and its true range uses day 1's last close
        tr = max(
            d3["High"].iloc[0] - d3["Low"].iloc[0],
            abs(d3["High"].iloc[0] - d1["Close"].iloc[-1]),
            abs(d3["Low"].iloc[0] - d1["Close"].iloc[-1]),
        )
        prev_atr = x.loc[d1.index[-1], "atr5"]
        assert x.loc[first3, "atr5"] == pytest.approx(prev_atr + (tr - prev_atr) / 14, rel=1e-12)

    def test_closing_auction_bars_feed_nothing(self):
        df, days = self.two_sessions_and_a_gap()
        mins = df.index.hour * 60 + df.index.minute
        x_all = build_inputs(df, P)
        x_core = build_inputs(df[mins <= 15 * 60 + 10], P)
        auction = mins > 15 * 60 + 10
        assert auction.any()
        pd.testing.assert_frame_equal(
            x_all.loc[~auction].drop(columns=["confirmable"]),
            x_core.drop(columns=["confirmable"]),
            check_exact=True,
        )
        assert not x_all.loc[auction, "confirmable"].any()
        assert x_all.loc[auction, ["vwap", "ema9", "atr5"]].isna().all().all()

    def test_warmup_is_100_completed_15m_bars(self):
        x = build_inputs(synthetic(n_sess=6), P)
        first_warm = x.index[x["warm"].to_numpy()][0]
        assert x.loc[first_warm, "n15"] == 100
        assert not x.loc[x.index < first_warm, "warm"].any()


class TestTruncationCheck:
    def test_lookahead_check_passes(self):
        frames = {"SYN": synthetic(n_sess=10)}
        s = VwapReversion()
        res = truncation_check(s, P_FAST, frames, run_config(s), symbols=1)
        assert res.ok, res.mismatches
        assert res.compared_total > 0


class TestFloat64Ties:
    def test_vwap_tie_is_not_a_touch(self):
        low, vwap = 95.2, 95.19999713410674
        # float32 storage would have merged these two prices into a tie
        assert np.float32(low) == np.float32(vwap)
        assert low > vwap
        n = 2
        df = pd.DataFrame(
            {
                "Open": [100.0, 99.0],
                "High": [103.0, 99.5],
                "Low": [99.0, low],
                "Close": [99.5, 99.0],
                "vwap": [100.0, vwap],
                "lower": [97.0, 97.0],
                "upper": [102.0, 102.0],
                "ema9": [100.0, 100.0],
                "atr5": [1.0, 1.0],
                "regime_ok": [True] * n,
                "warm": [True] * n,
                "confirmable": [True] * n,
            }
        )
        df["day"] = pd.Series([pd.Timestamp("2026-03-02").date()] * n)
        c = Ctx(df, "X")
        assert bar_from_arrays(c.a, 1).low > bar_from_arrays(c.a, 1).vwap
        m = VwapReversionMachine(P)
        m.on_bar(bar_from_arrays(c.a, 0))
        assert m.diagnostics()["setups_armed_short"] == 1
        before = m.diagnostics()["setups_cancelled_vwap_touch"]
        m.on_bar(bar_from_arrays(c.a, 1))
        assert m.diagnostics()["setups_cancelled_vwap_touch"] == before
