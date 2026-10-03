"""Look-ahead (truncation) check: causal strategies pass, look-ahead strategies fail."""

from __future__ import annotations

import pandas as pd
import pytest

from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.truncation import TruncationResult, truncation_check
from signal_engine.backtest.types import EntrySignal
from signal_engine.tests.test_backtest_ledgers import (
    RUN,
    Momo,
    SwingScript,
    make_frame,
    random_sessions,
)


class LookaheadIntraday(Momo):
    """Entry decided by a whole-series statistic, so prepare() depends on future bars."""

    name = "lookahead"

    def prepare(self, df, p):
        df = df.copy()
        df["thr"] = df["Close"].mean()
        return df

    def entry(self, c, i, p, direction):
        if direction != 1 or i < 2 or float(c["Close"][i]) <= float(c["thr"][i]):
            return None
        cl = float(c["Close"][i])
        return EntrySignal(1, cl * 0.996, cl * 1.008, "L")


class CenteredWindow(Momo):
    """A centred window wider than a session reaches into the following sessions.

    A window of a few bars only looks ahead past the entry cutoff of the last session before
    the cut, which a session-boundary cut cannot observe; that is a known limit of the check.
    """

    name = "centered"

    def prepare(self, df, p):
        df = df.copy()
        df["ctr"] = df["Close"].rolling(301, center=True, min_periods=1).mean()
        return df

    def entry(self, c, i, p, direction):
        cl = float(c["Close"][i])
        if direction == 1 and i > 2 and cl > float(c["ctr"][i]) * 1.0005:
            return EntrySignal(1, cl * 0.996, cl * 1.008, "C")
        return None


class SwingCausal(SwingScript):
    def __init__(self):
        super().__init__({})

    def entry_at_open(self, c, i, p, direction):
        if direction != 1 or i < 3:
            return None
        if float(c["Close"][i - 1]) > float(c["Close"][i - 2]) > float(c["Close"][i - 3]):
            o = float(c["Open"][i])
            return EntrySignal(1, o * 0.96, o * 1.04, "W")
        return None


class SwingLookahead(SwingCausal):
    def prepare(self, df, p):
        df = df.copy()
        df["thr"] = df["Close"].mean()
        return df

    def prepare_for(self, symbol, df, p):
        return self.prepare(df, p)

    def entry_at_open(self, c, i, p, direction):
        if direction != 1 or i < 3 or float(c["Close"][i - 1]) <= float(c["thr"][i]):
            return None
        o = float(c["Open"][i])
        return EntrySignal(1, o * 0.96, o * 1.04, "W")


def swing_frame(seed, n=80):
    import numpy as np

    rng = np.random.default_rng(seed)
    px, out = 100.0, []
    for d in range(n):
        o = px
        cl = o * (1 + rng.normal(0.001, 0.012))
        out.append(
            (
                str((pd.Timestamp("2026-01-01") + pd.Timedelta(days=d)).date()),
                [(o, max(o, cl) * 1.004, min(o, cl) * 0.996, cl)],
            )
        )
        px = cl
    return make_frame(out)


def _stable(res):
    d = res.to_dict()
    d.pop("elapsed_s")
    return d


def intraday_frames(n=5, sessions=12):
    return {f"S{k}": random_sessions(k, sessions) for k in range(n)}


def swing_frames(n=4):
    return {f"W{k}": swing_frame(k) for k in range(n)}


SWING_RUN = SwingRunConfig(min_sl_pct=0.005, max_hold_bars=6)


class TestCausal:
    def test_intraday_passes(self):
        res = truncation_check(Momo(), None, intraday_frames(), RUN)
        assert isinstance(res, TruncationResult)
        assert res.ok and res.mismatches == []
        assert len(res.checked) == 3 * 4
        assert {c["cut_kind"] for c in res.checked} == {"session", "mid_session"}

    def test_swing_passes(self):
        res = truncation_check(
            SwingCausal(), None, swing_frames(), SWING_RUN, engine=simulate_swing
        )
        assert res.ok, res.mismatches
        assert res.checked

    def test_actually_compares_trades(self):
        # Guard against a vacuous pass: the full run must have trades before the cut.
        from signal_engine.backtest.harness import Backtest

        frames = intraday_frames()
        assert Backtest(Momo(), frames, RUN).trades(None, "all")


class TestLookahead:
    @pytest.mark.parametrize("strat", [LookaheadIntraday, CenteredWindow])
    def test_intraday_lookahead_fails(self, strat):
        res = truncation_check(strat(), None, intraday_frames(), RUN)
        assert not res.ok
        assert res.mismatches
        assert {m["kind"] for m in res.mismatches} <= {
            "trade_missing",
            "trade_extra",
            "trade_differs",
            "signal_differs",
        }
        m = res.mismatches[0]
        assert set(m) == {"symbol", "cut", "cut_kind", "kind", "detail"} and m["detail"]

    def test_swing_lookahead_fails(self):
        res = truncation_check(
            SwingLookahead(), None, swing_frames(), SWING_RUN, engine=simulate_swing
        )
        assert not res.ok and res.mismatches


class TestSelectionAndPurity:
    def test_deterministic(self):
        a = truncation_check(LookaheadIntraday(), None, intraday_frames(), RUN)
        b = truncation_check(LookaheadIntraday(), None, intraday_frames(), RUN)
        assert _stable(a) == _stable(b)

    def test_symbols_evenly_spaced_from_sorted_keys(self):
        frames = {f"S{k:02d}": random_sessions(k, 8) for k in range(9)}
        res = truncation_check(Momo(), None, frames, RUN, symbols=3)
        assert sorted({c["symbol"] for c in res.checked}) == ["S00", "S04", "S08"]

    def test_fewer_symbols_than_requested(self):
        frames = {"S0": random_sessions(0, 8), "S1": random_sessions(1, 8)}
        res = truncation_check(Momo(), None, frames, RUN, symbols=5)
        assert sorted({c["symbol"] for c in res.checked}) == ["S0", "S1"]

    def test_cut_is_first_session_of_later_part(self):
        frames = {"S0": random_sessions(0, 10)}
        res = truncation_check(Momo(), None, frames, RUN, symbols=1, cuts=(0.5,), mid_session=False)
        days = sorted(set(frames["S0"].index.date))
        assert [(c["symbol"], c["cut"], c["cut_kind"]) for c in res.checked] == [
            ("S0", str(days[5]), "session")
        ]

    def test_frames_unchanged(self):
        frames = intraday_frames(3)
        before = {k: v.copy(deep=True) for k, v in frames.items()}
        ids = {k: id(v) for k, v in frames.items()}
        truncation_check(Momo(), None, frames, RUN)
        assert {k: id(v) for k, v in frames.items()} == ids
        for k, v in frames.items():
            pd.testing.assert_frame_equal(v, before[k])

    def test_too_few_sessions_skipped_not_failed(self):
        frames = {"TINY": random_sessions(0, 2), "OK": random_sessions(1, 10)}
        res = truncation_check(Momo(), None, frames, RUN, symbols=2, cuts=(0.5,))
        assert res.ok
        assert [s["symbol"] for s in res.skipped] == ["TINY"]
        assert {c["symbol"] for c in res.checked} == {"OK"}

    def test_to_dict_shape(self):
        d = truncation_check(Momo(), None, intraday_frames(2), RUN).to_dict()
        assert set(d) >= {"ok", "checked", "mismatches", "skipped"}

    def test_empty_frames(self):
        res = truncation_check(Momo(), None, {}, RUN)
        assert res.ok and res.checked == []


class OneBarLookahead(Momo):
    """Entry condition reads the NEXT bar's close, inside one session."""

    name = "onebar"

    def prepare(self, df, p):
        df = df.copy()
        df["nxt"] = df["Close"].shift(-1)
        return df

    def entry(self, c, i, p, direction):
        cl, nxt = float(c["Close"][i]), float(c["nxt"][i])
        # Fires once per session, on the bar just before the mid-session cut (bar 44 of 75).
        if direction == 1 and i % 75 == 44 and nxt > cl:
            return EntrySignal(1, cl * 0.996, cl * 1.008, "O")
        return None


class FiveBarForward(OneBarLookahead):
    name = "fivebar"

    def prepare(self, df, p):
        df = df.copy()
        df["nxt"] = df["Close"].shift(-5)
        return df


class TestMidSession:
    def test_one_bar_lookahead_invisible_with_session_cuts_only(self):
        # Documented limit: at a session boundary the one-bar look-ahead cannot be observed.
        res = truncation_check(OneBarLookahead(), None, intraday_frames(), RUN, mid_session=False)
        assert res.ok

    @pytest.mark.parametrize("strat", [OneBarLookahead, FiveBarForward])
    def test_forward_looking_fails_with_mid_session_cut(self, strat):
        res = truncation_check(strat(), None, intraday_frames(), RUN)
        assert not res.ok
        assert any(m["cut_kind"] == "mid_session" for m in res.mismatches)

    def test_causal_passes_with_mid_session(self):
        res = truncation_check(Momo(), None, intraday_frames(), RUN, mid_session=True)
        assert res.ok, res.mismatches

    def test_swing_has_only_boundary_cuts(self):
        res = truncation_check(
            SwingCausal(), None, swing_frames(), SWING_RUN, engine=simulate_swing
        )
        assert {c["cut_kind"] for c in res.checked} == {"session"}

    def test_deterministic_and_pure(self):
        frames = intraday_frames(3)
        before = {k: v.copy(deep=True) for k, v in frames.items()}
        a = truncation_check(OneBarLookahead(), None, frames, RUN)
        b = truncation_check(OneBarLookahead(), None, frames, RUN)
        assert _stable(a) == _stable(b)
        for k, v in frames.items():
            pd.testing.assert_frame_equal(v, before[k])

    def test_runtime_on_large_fixture(self):
        import time

        frames = {f"S{k:02d}": random_sessions(k, 30) for k in range(40)}
        t0 = time.perf_counter()
        res = truncation_check(Momo(), None, frames, RUN)
        elapsed = time.perf_counter() - t0
        print(f"truncation 40x30 elapsed {elapsed:.2f}s")
        assert res.ok and elapsed < 30


class HoldAndSignal(Momo):
    """Causal: signals on every bar with wide exits, so a position is open at any cut."""

    name = "hold"

    def entry(self, c, i, p, direction):
        cl = float(c["Close"][i])
        if direction == 1 and i > 2:
            return EntrySignal(1, cl * 0.8, cl * 1.2, "H")
        return None


MULTI = RUN.with_(one_trade_per_direction=False, max_trades_per_day=9)


class Never(Momo):
    name = "never"

    def entry(self, c, i, p, direction):
        return None


class Stateful(Momo):
    name = "stateful"

    def __init__(self):
        self.calls = []

    def prepare(self, df, p):
        self.calls.append(len(df))
        return df


class TestReviewFixes:
    def test_position_open_at_cut_with_signal_on_last_bar_passes(self):
        res = truncation_check(HoldAndSignal(), None, intraday_frames(3), MULTI)
        assert res.ok, res.mismatches
        assert any(c["cut_kind"] == "mid_session" for c in res.checked)

    def test_nothing_to_compare_is_skipped_not_checked(self):
        res = truncation_check(Never(), None, intraday_frames(3), RUN)
        assert res.ok and res.checked == [] and res.compared_total == 0
        assert res.skipped and all(s["reason"] == "nothing to compare" for s in res.skipped)
        assert res.to_dict()["compared_total"] == 0

    def test_compared_counts_recorded(self):
        res = truncation_check(Momo(), None, intraday_frames(3), RUN)
        assert res.compared_total > 0
        assert all(c["n_trades_compared"] + c["n_signals_compared"] > 0 for c in res.checked)
        assert res.to_dict()["compared_total"] == res.compared_total

    def test_strategy_instance_never_used(self):
        strat = Stateful()
        truncation_check(strat, None, intraday_frames(2), RUN)
        assert strat.calls == []

    def test_coverage_and_elapsed_reported(self):
        d = truncation_check(Momo(), None, intraday_frames(2), RUN).to_dict()
        assert "NOT truncated" in d["coverage"] and d["elapsed_s"] >= 0

    def test_compares_stop_target_and_legs(self):
        from signal_engine.backtest.truncation import _COMPARED

        assert {"sl", "tp", "legs"} <= set(_COMPARED)

    def test_duplicate_cuts_deduped(self):
        frames = {"S0": random_sessions(0, 4)}
        res = truncation_check(
            Momo(), None, frames, RUN, symbols=1, cuts=(0.5, 0.51, 0.52), mid_session=False
        )
        cuts = [c["cut"] for c in res.checked] + [s["cut"] for s in res.skipped]
        assert len(cuts) == len(set(cuts))

    def test_cut_points_capped_per_symbol(self):
        frames = {"S0": random_sessions(0, 20)}
        res = truncation_check(
            Momo(), None, frames, RUN, symbols=1, cuts=(0.2, 0.3, 0.4, 0.5, 0.6, 0.7)
        )
        assert len(res.checked) + len(res.skipped) <= 4
