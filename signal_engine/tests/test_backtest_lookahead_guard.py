"""Mandatory look-ahead protection: GuardedCtx, the per-hook read bound and the Backtest funnel.

A strategy hook receives the closed bar i (entry, on_bar, custom_exit, on_exit), or may read only
up to i - 1 (trail and a dynamic target: a resting order placed before the bar trades), or bars up
to i - 1 plus Open[i] (on_fill, on_fill_skipped, and the swing entry/exit at the open). Reading past
that raises LookAheadError naming the hook, column, requested index and allowed bound.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest.causal import GuardedCtx, LookAheadError
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.types import Ctx, EntrySignal
from signal_engine.tests.test_backtest_exit_ambiguity import _daily, _SwingOnce
from signal_engine.tests.test_backtest_missing_bar_guard import (
    FLAT100,
    RUN,
    FireOnce,
    contiguous,
    frame,
)


def ctx(n=30):
    rng = np.random.default_rng(3)
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    rows = [(c, c + 0.3, c - 0.3, c) for c in close]
    return Ctx(frame(rows, contiguous(n)), "T")


def at(gc, i, hook="entry", open_extra=0):
    gc.state.hook, gc.state.bound, gc.state.open_extra = hook, i, open_extra
    return gc


class TestGuardedArray:
    def test_scalar_up_to_bound_reads_and_past_it_raises(self):
        gc = at(GuardedCtx(ctx()), 10)
        assert gc["Close"][10] == ctx()["Close"][10]
        with pytest.raises(LookAheadError) as e:
            gc["Close"][11]
        msg = str(e.value)
        assert "entry" in msg and "Close" in msg and "11" in msg and "10" in msg

    def test_lookahead_error_is_not_a_value_error(self):
        # strategy code that catches ValueError must not be able to swallow a violation
        assert issubclass(LookAheadError, RuntimeError)
        assert not issubclass(LookAheadError, ValueError)

    def test_negative_index_raises(self):
        gc = at(GuardedCtx(ctx()), 10)
        with pytest.raises(LookAheadError):
            gc["Close"][-1]

    def test_numpy_integer_index_is_checked(self):
        gc = at(GuardedCtx(ctx()), 10)
        assert gc["Close"][np.int64(10)] == ctx()["Close"][10]
        with pytest.raises(LookAheadError):
            gc["Close"][np.int64(11)]

    def test_slice_within_bound_returns_a_plain_ndarray(self):
        gc = at(GuardedCtx(ctx()), 10)
        window = gc["Close"][5:11]
        assert type(window) is np.ndarray
        assert window.mean() == pytest.approx(ctx()["Close"][5:11].mean())

    def test_slice_past_bound_negative_or_open_ended_raises(self):
        gc = at(GuardedCtx(ctx()), 10)
        for bad in (slice(5, 12), slice(None, None), slice(5, None), slice(-5, None), slice(0, -1)):
            with pytest.raises(LookAheadError):
                gc["Close"][bad]

    def test_slice_ending_exactly_at_bound_plus_one_passes(self):
        gc = at(GuardedCtx(ctx()), 10)
        assert len(gc["Close"][:11]) == 11
        assert len(gc["Close"][0:0]) == 0

    def test_whole_array_reductions_raise(self):
        gc = at(GuardedCtx(ctx()), 10)
        col = gc["Close"]
        with pytest.raises(LookAheadError):
            col.mean()
        with pytest.raises(LookAheadError):
            col.sum()
        with pytest.raises(LookAheadError):
            col.max()
        with pytest.raises(LookAheadError):
            np.mean(col)
        with pytest.raises(LookAheadError):
            np.nanmax(col)
        with pytest.raises(LookAheadError):
            _ = col > 100.0
        with pytest.raises(LookAheadError):
            list(col)

    def test_len_and_shape_are_allowed(self):
        gc = at(GuardedCtx(ctx(30)), 10)
        assert len(gc["Close"]) == 30 and gc["Close"].shape == (30,)

    def test_index_symbol_n_and_has_are_plain(self):
        raw = ctx()
        gc = at(GuardedCtx(raw), 3)
        assert gc.index is raw.index and gc.symbol == "T" and gc.n == raw.n
        assert gc.has("Close") and not gc.has("nope")
        assert gc.index[20] == raw.index[20]

    def test_open_gets_the_extra_bar_only_when_asked(self):
        gc = at(GuardedCtx(ctx()), 9, hook="on_fill", open_extra=1)
        assert gc["Open"][10] == ctx()["Open"][10]
        with pytest.raises(LookAheadError):
            gc["Close"][10]
        with pytest.raises(LookAheadError):
            gc["Open"][11]
        assert gc["Close"][9] == ctx()["Close"][9]
        assert len(gc["Open"][:11]) == 11


# ---- the bound the engine sets for each hook --------------------------------------------------


class Probe(FireOnce):
    """Fires once at bar 1 and runs `reads[hook](c, i)` inside the named hook."""

    dynamic_target = False

    def __init__(self, reads, **kw):
        super().__init__(at=1, sl=90.0, tp=300.0)
        self.reads = reads
        self.dynamic_target = kw.get("dynamic_target", False)

    def _do(self, hook, c, i):
        fn = self.reads.get(hook)
        if fn is not None:
            fn(c, i)

    def entry(self, c, i, p, d):
        self._do("entry", c, i)
        return super().entry(c, i, p, d)

    def on_bar(self, c, i, p):
        self._do("on_bar", c, i)

    def trail(self, c, i, p, pos):
        self._do("trail", c, i)

    def target(self, c, j, p, pos):
        self._do("target", c, j)
        return pos.tp

    def custom_exit(self, c, i, p, pos):
        self._do("custom_exit", c, i)

    def on_fill(self, c, i, p, pos):
        self._do("on_fill", c, i)

    def on_exit(self, c, i, p, trade):
        self._do("on_exit", c, i)


def run_probe(reads, guarded=True, **kw):
    n = 12
    df = frame([FLAT100] * n, contiguous(n))
    c = Ctx(df, "T")
    if guarded:
        c = GuardedCtx(c)
    return simulate(c, Probe(reads, **kw), None, RUN)


class TestEngineBounds:
    def test_entry_may_read_the_closed_bar_but_not_the_next(self):
        run_probe({"entry": lambda c, i: c["Close"][i]})
        with pytest.raises(LookAheadError, match="entry"):
            run_probe({"entry": lambda c, i: c["Close"][i + 1]})

    def test_on_bar_and_custom_exit_and_on_exit_may_read_bar_i_only(self):
        for hook in ("on_bar", "custom_exit", "on_exit"):
            run_probe({hook: lambda c, i: c["Close"][i]})
            with pytest.raises(LookAheadError, match=hook):
                run_probe({hook: lambda c, i: c["Close"][i + 1]})

    def test_trail_reads_up_to_the_argument_and_not_the_bar_being_managed(self):
        # the engine passes i = j - 1 while managing bar j: bar j itself is off limits
        run_probe({"trail": lambda c, i: c["Close"][i]})
        with pytest.raises(LookAheadError, match="trail"):
            run_probe({"trail": lambda c, i: c["Close"][i + 1]})

    def test_dynamic_target_reads_up_to_j_minus_1(self):
        run_probe({"target": lambda c, j: c["Close"][j - 1]}, dynamic_target=True)
        with pytest.raises(LookAheadError, match="target"):
            run_probe({"target": lambda c, j: c["Close"][j]}, dynamic_target=True)

    def test_on_fill_reads_bars_before_i_plus_open_of_i(self):
        run_probe({"on_fill": lambda c, i: (c["Close"][i - 1], c["Open"][i])})
        with pytest.raises(LookAheadError, match="on_fill"):
            run_probe({"on_fill": lambda c, i: c["Close"][i]})
        with pytest.raises(LookAheadError, match="on_fill"):
            run_probe({"on_fill": lambda c, i: c["High"][i]})

    def test_open_extra_does_not_leak_into_the_next_hook(self):
        seen = []

        def later(c, i):
            seen.append(i)
            c["Open"][i]  # bar i is closed here: fine
            with pytest.raises(LookAheadError):
                c["Open"][i + 1]

        run_probe({"on_fill": lambda c, i: c["Open"][i], "on_bar": later})
        assert seen

    def test_on_fill_skipped_has_the_same_bound_as_on_fill(self):
        class Skipper(Probe):
            def on_fill_skipped(self, c, i, p, sig):
                c["Open"][i]
                c["Close"][i - 1]
                with pytest.raises(LookAheadError):
                    c["Close"][i]
                self.events.append("checked")

        times = ["09:15", "09:20", "09:25", "09:30", "10:00", "10:05"]
        df = frame([FLAT100] * 6, times)
        s = Skipper({})
        s.at = 3
        simulate(GuardedCtx(Ctx(df, "T")), s, None, RUN)
        assert "checked" in s.events

    def test_guarded_run_gives_the_same_trades_as_a_plain_run(self):
        rng = np.random.default_rng(5)
        close = 100 + np.cumsum(rng.normal(0, 0.5, 60))
        rows = [(c, c + 0.6, c - 0.6, c) for c in close]
        df = frame(rows, contiguous(60))

        class Honest(FireOnce):
            def entry(self, c, i, p, d):
                if d != 1 or i < 5:
                    return None
                cl = c["Close"]
                if cl[i] > cl[i - 5 : i].mean():
                    return EntrySignal(1, sl=cl[i] - 1.0, tp=cl[i] + 1.0, tag="H")
                return None

        plain = simulate(Ctx(df, "T"), Honest(0, 0, 0), None, RUN)
        guarded = simulate(GuardedCtx(Ctx(df, "T")), Honest(0, 0, 0), None, RUN)
        assert len(plain) > 3 and plain == guarded

    def test_a_cheating_strategy_aborts_the_run(self):
        class Peek(FireOnce):
            def entry(self, c, i, p, d):
                cl = c["Close"]
                if d == 1 and cl[i + 1] > cl[i]:
                    return EntrySignal(1, sl=cl[i] - 1.0, tp=cl[i] + 1.0, tag="P")
                return None

        df = frame([FLAT100] * 12, contiguous(12))
        # unguarded the peek runs silently (this is the Stage A finding) ...
        simulate(Ctx(df, "T"), Peek(0, 0, 0), None, RUN)
        # ... guarded it does not
        with pytest.raises(LookAheadError):
            simulate(GuardedCtx(Ctx(df, "T")), Peek(0, 0, 0), None, RUN)


class TestSwingBounds:
    CFG = SwingRunConfig(min_sl_pct=0.0)

    def rows(self):
        return [(100.0, 101.0, 99.0, 100.0)] * 8

    def test_entry_at_open_may_read_open_i_and_earlier_bars_only(self):
        class Good(_SwingOnce):
            def entry_at_open(self, c, i, p, d):
                c["Open"][i]
                if i > 0:
                    c["Close"][i - 1]
                return super().entry_at_open(c, i, p, d)

        simulate_swing(
            GuardedCtx(Ctx(_daily(self.rows()), "S")), Good(2, 90.0, 200.0), None, self.CFG
        )

        class Bad(_SwingOnce):
            def entry_at_open(self, c, i, p, d):
                c["Close"][i]
                return None

        with pytest.raises(LookAheadError, match="entry_at_open"):
            simulate_swing(
                GuardedCtx(Ctx(_daily(self.rows()), "S")), Bad(2, 90.0, 200.0), None, self.CFG
            )

    def test_swing_open_hooks_may_read_prepared_columns_at_i_but_not_ohlc(self):
        # prepare() shifts what must be known before the open (p_close, s_atr, ...): index i of
        # such a column is yesterday's value, so it is readable. High/Low/Close/Volume are not.
        df = _daily(self.rows())
        df["p_close"] = df["Close"].shift(1)

        class Good(_SwingOnce):
            def entry_at_open(self, c, i, p, d):
                c["p_close"][i]
                c["Open"][i]
                return super().entry_at_open(c, i, p, d)

        simulate_swing(GuardedCtx(Ctx(df, "S")), Good(2, 90.0, 200.0), None, self.CFG)
        for col in ("High", "Low", "Close", "Volume"):

            class Bad(_SwingOnce):
                def entry_at_open(self, c, i, p, d, col=col):
                    c[col][i]

            with pytest.raises(LookAheadError, match="entry_at_open"):
                simulate_swing(GuardedCtx(Ctx(df, "S")), Bad(2, 90.0, 200.0), None, self.CFG)

    def test_intraday_on_fill_does_not_get_the_prepared_column_exception(self):
        df = frame([FLAT100] * 6, contiguous(6))
        df["ind"] = df["Close"]

        class Bad(FireOnce):
            def on_fill(self, c, i, p, pos):
                c["ind"][i]

        with pytest.raises(LookAheadError, match="on_fill"):
            simulate(GuardedCtx(Ctx(df, "T")), Bad(1, 90.0, 300.0), None, RUN)

    def test_swing_custom_exit_may_read_the_closed_bar(self):
        class Exit(_SwingOnce):
            def custom_exit(self, c, i, p, pos):
                c["Close"][i]
                with pytest.raises(LookAheadError):
                    c["Close"][i + 1]

        simulate_swing(
            GuardedCtx(Ctx(_daily(self.rows()), "S")), Exit(1, 90.0, 200.0), None, self.CFG
        )

    def test_swing_trail_is_bounded_at_the_previous_bar(self):
        class Trail(_SwingOnce):
            def trail(self, c, i, p, pos):
                c["Close"][i]
                with pytest.raises(LookAheadError):
                    c["Close"][i + 1]

        simulate_swing(
            GuardedCtx(Ctx(_daily(self.rows()), "S")), Trail(1, 90.0, 200.0), None, self.CFG
        )


# ---- the funnel: Backtest cannot run a strategy without the guard -----------------------------


class TestBacktestFunnel:
    def frames(self):
        n = 40
        rng = np.random.default_rng(9)
        close = 100 + np.cumsum(rng.normal(0, 0.4, n))
        rows = [(c, c + 0.5, c - 0.5, c) for c in close]
        return {"AAA": frame(rows, contiguous(n)), "BBB": frame(rows, contiguous(n))}

    def test_backtest_aborts_on_a_peek(self):
        class Peek(FireOnce):
            def prepare(self, df, p):
                return df

            def entry(self, c, i, p, d):
                return (
                    EntrySignal(1, sl=1.0, tp=900.0, tag="P")
                    if c["Close"][i + 1] > 0 and d == 1
                    else None
                )

        bt = Backtest(Peek(0, 0, 0), self.frames(), RUN)
        with pytest.raises(LookAheadError):
            bt.trades(object(), "all")

    def test_backtest_runs_a_honest_strategy_and_reports_what_was_covered(self):
        class Honest(FireOnce):
            def entry(self, c, i, p, d):
                if d == 1 and i == 6:
                    return EntrySignal(1, sl=90.0, tp=900.0, tag="H")
                return None

        bt = Backtest(Honest(0, 0, 0), self.frames(), RUN)
        trades = bt.trades(object(), "all")
        assert len(trades) == 2
        g = bt.lookahead_guard()
        assert g["mode"] == "full" and g["violations"] == 0
        assert g["symbols_checked"] == 2 and g["sessions_checked"] == 2

    def test_there_is_no_switch_to_run_unguarded(self):
        import inspect

        assert "guard" not in inspect.signature(Backtest.__init__).parameters
        assert "guard" not in inspect.signature(Backtest.trades).parameters


# ---- the recorded-run path: guard block, truncation label, verdict gate ----------------------


def _sessions(n_days=8, bars=40):
    rng = np.random.default_rng(21)
    out = []
    days = pd.bdate_range("2026-05-04", periods=n_days)
    price = 100.0
    for d in days:
        idx = pd.date_range(f"{d.date()} 09:15", periods=bars, freq="5min", tz="Asia/Kolkata")
        close = price + np.cumsum(rng.normal(0, 0.4, bars))
        open_ = np.r_[price, close[:-1]]
        price = float(close[-1])
        out.append(
            pd.DataFrame(
                {
                    "Open": open_,
                    "High": np.maximum(open_, close) + 0.2,
                    "Low": np.minimum(open_, close) - 0.2,
                    "Close": close,
                    "Volume": 1000.0,
                },
                index=idx,
            )
        )
    return pd.concat(out)


def _registered(monkeypatch, strategy_cls):
    import dataclasses as dc

    from signal_engine.backtest import __main__ as cli
    from signal_engine.backtest import record_run as rr
    from signal_engine.tests.test_evidence_ledgers import deployed_model

    @dc.dataclass
    class Params:
        x: int = 1

    monkeypatch.setattr(cli, "_register", lambda: None)
    monkeypatch.setattr(cli, "REGISTRY", {"k": (strategy_cls, Params, "intraday")})
    monkeypatch.setattr(rr, "random_entry_baseline_intraday", lambda *a, **k: None)
    monkeypatch.setattr(rr, "deployed_cost_model", deployed_model)
    return rr


def _resolution():
    from signal_engine.tests.published_fixtures import frames_resolution

    return frames_resolution(intraday={"AAA": _sessions(), "BBB": _sessions()})


def _strategy(peek=False):
    from signal_engine.backtest import indicators as ind
    from signal_engine.backtest.strategies.base import Strategy

    class Toy(Strategy):
        name = "toy"
        run_overrides = {"skip_open_minutes": 0, "entry_cutoff_min": 15 * 60}

        def prepare(self, df, p):
            return ind.add_session_columns(df)

        def entry(self, c, i, p, d):
            if d != 1 or i < 8:
                return None
            cl = c["Close"]
            if peek:
                up = cl[i + 1] > cl[i]
            else:
                up = cl[i] > cl[i - 8 : i].mean()
            if up:
                return EntrySignal(1, sl=cl[i] - 0.8, tp=cl[i] + 1.2, tag="toy")
            return None

    return Toy


class TestRecordedRun:
    def test_guard_block_truncation_and_warnings_are_on_the_execution(self, monkeypatch):
        rr = _registered(monkeypatch, _strategy())
        ex = rr.execute_backtest("k", dataset=_resolution())
        g = ex.lookahead_guard
        assert g["mode"] == "full" and g["violations"] == 0
        assert g["symbols_checked"] == 2 and g["sessions_checked"] == 16
        assert g["lookahead_check"] == "run"
        assert g["truncation"]["status"] == "run" and g["truncation"]["ok"] is True
        # mid-session cut points are part of the mandatory check
        assert g["truncation"]["mid_session_cuts"] >= 1
        assert g["truncation"]["session_cuts"] >= 1
        assert ex.entries_skipped_missing_bar == 0

    def test_a_peeking_strategy_cannot_complete_a_recorded_run(self, monkeypatch):
        rr = _registered(monkeypatch, _strategy(peek=True))
        with pytest.raises(LookAheadError, match="entry"):
            rr.execute_backtest("k", dataset=_resolution())

    def test_a_peeking_strategy_cannot_complete_even_without_truncation(self, monkeypatch):
        rr = _registered(monkeypatch, _strategy(peek=True))
        with pytest.raises(LookAheadError):
            rr.execute_backtest("k", dataset=_resolution(), run_truncation=False)

    def test_no_truncation_is_labelled_not_run_and_cannot_be_promoted(self, monkeypatch):
        rr = _registered(monkeypatch, _strategy())
        ex = rr.execute_backtest("k", dataset=_resolution(), run_truncation=False)
        assert ex.truncation is None
        assert ex.lookahead_guard["lookahead_check"] == "not_run"
        assert ex.lookahead_guard["mode"] == "full"  # the guarded pass is not skippable
        from signal_engine.backtest.gate_semantics import outcome
        from signal_engine.backtest.promotion import evaluate_promotion
        from signal_engine.backtest.tearsheet import build_run_record
        from signal_engine.backtest.verdict import EDGE, derive

        record = build_run_record(
            ex.trades,
            run_id="r",
            run_date="2026-10-05",
            code_version="v",
            data=ex.data,
            period=ex.period,
            costs=ex.costs,
            n_trials=1,
            oos_start=ex.days[len(ex.days) // 2],
            cost_model=ex.cost_model,
            truncation=ex.truncation,
            lookahead_guard=ex.lookahead_guard,
            entries_skipped_missing_bar=ex.entries_skipped_missing_bar,
        )
        assert record["lookahead_guard"]["lookahead_check"] == "not_run"
        assert "execution_warnings" in record
        rows = {r["gate"]: r for d in record["evaluation"]["dimensions"] for r in d["rows"]}
        assert rows["G0.2"]["status"] == "not_evaluated"
        assert outcome(rows["G0.2"]) == "BLOCKED"
        validity = {"ok": True, "failures": [], "checks_run": ["lineage"]}
        verdict = derive(
            record["evaluation"], validity=validity, data_quality={}, readiness_missing=[]
        )
        assert verdict["run_verdict"] != EDGE
        promo = evaluate_promotion(
            record["evaluation"],
            code_dirty=False,
            reproducible=True,
            validity=validity,
            data_quality={},
            run_status="VALID",
        )
        assert promo["stages"]["backtest"]["decision"] != "READY_FOR_NEXT_STAGE"
        assert "gate G0.2 has not been run" in promo["stages"]["backtest"]["reasons"]
