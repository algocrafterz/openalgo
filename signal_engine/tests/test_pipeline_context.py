"""RollingContext builds windows from warm-up history plus today's closed bars."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from signal_engine.pipeline.context import RollingContext
from signal_engine.pipeline.strategy import Fire

IST = "Asia/Kolkata"


def day_bars(date, n=6, base=100.0, start="09:15", freq="5min"):
    idx = pd.date_range(f"{date} {start}", periods=n, freq=freq, tz=IST)
    return pd.DataFrame(
        {"Open": base, "High": base + 1, "Low": base - 1, "Close": base, "Volume": 10.0}, index=idx
    )


class StubStrategy:
    name = "STUB"
    interval_min = 5
    warmup_sessions = 2
    needs_1m = True
    entry_window_min = (0, 1440)

    def __init__(self):
        self.seen = []

    def evaluate(self, symbol, df5m, df1m):
        self.seen.append((symbol, df5m, df1m))
        return []


class Loader:
    def __init__(self, hist5, hist1, fail=()):
        self.hist5, self.hist1, self.fail = hist5, hist1, set(fail)
        self.calls = []

    def load(self, symbols, day, sessions5, sessions1):
        self.calls.append((tuple(symbols), day, sessions5, sessions1))
        ok = [s for s in symbols if s not in self.fail]
        return (
            {s: self.hist5[s] for s in ok},
            {s: self.hist1[s] for s in ok},
            [s for s in symbols if s in self.fail],
        )


def build(fail=()):
    prior = pd.concat([day_bars("2026-09-28"), day_bars("2026-09-29")])
    m1 = pd.concat(
        [
            day_bars("2026-09-28", n=30, freq="1min"),
            day_bars("2026-09-29", n=30, freq="1min"),
            day_bars("2026-09-29", n=3, start="15:16", freq="1min"),  # closing auction
        ]
    )
    loader = Loader({"A": prior, "B": prior}, {"A": m1, "B": m1}, fail)
    strat = StubStrategy()
    return RollingContext(strat, loader), strat, loader


class TestWarm:
    def test_loads_once_per_symbol_and_names_failures(self):
        ctx, _, loader = build(fail=["B"])
        failed = ctx.warm(["A", "B"], pd.Timestamp("2026-09-30", tz=IST).date())
        assert failed == ["B"]
        assert ctx.warmed == {"A"}
        assert loader.calls[0][2] == 2  # strategy.warmup_sessions
        ctx.warm(["A"], pd.Timestamp("2026-09-30", tz=IST).date())
        assert len(loader.calls) == 1  # A is not reloaded

    def test_new_symbol_is_warmed_lazily(self):
        ctx, _, loader = build()
        d = pd.Timestamp("2026-09-30", tz=IST).date()
        ctx.warm(["A"], d)
        ctx.warm(["A", "B"], d)
        assert len(loader.calls) == 2 and loader.calls[1][0] == ("B",)


class TestWindow:
    def today(self):
        return day_bars("2026-09-30", n=3)

    def test_window_is_prior_sessions_plus_today(self):
        ctx, _, _ = build()
        d = pd.Timestamp("2026-09-30", tz=IST).date()
        ctx.warm(["A"], d)
        d5, d1 = ctx.window("A", self.today(), None)
        assert len(d5) == 12 + 3
        assert d5.index.is_monotonic_increasing
        assert d5.index[-1] == self.today().index[-1]

    def test_one_minute_frame_excludes_the_closing_auction(self):
        ctx, _, _ = build()
        ctx.warm(["A"], pd.Timestamp("2026-09-30", tz=IST).date())
        _, d1 = ctx.window("A", self.today(), None)
        assert (d1.index.time < pd.Timestamp("15:15").time()).all()
        assert len(d1) == 60

    def test_today_one_minute_bars_are_appended_when_given(self):
        ctx, _, _ = build()
        ctx.warm(["A"], pd.Timestamp("2026-09-30", tz=IST).date())
        _, d1 = ctx.window("A", self.today(), day_bars("2026-09-30", n=10, freq="1min"))
        assert len(d1) == 70

    def test_unwarmed_symbol_raises(self):
        ctx, _, _ = build()
        with pytest.raises(KeyError, match="A"):
            ctx.window("A", self.today(), None)

    def test_live_shaped_today_frame_joins_warmup_bars_into_a_datetime_index(self):
        # Live bars come from parse_history (a fixed +05:30 offset); warm-up bars from Historify
        # carry the Asia/Kolkata zone. Mixing the two once produced an object Index, and every
        # symbol failed to evaluate against the real endpoint while replay tests passed.
        from signal_engine.pipeline.bars import parse_history

        base = int(pd.Timestamp("2026-09-30 09:15", tz=IST).timestamp())
        rows = [
            {"timestamp": base + 300 * i, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 9}
            for i in range(3)
        ]
        live = parse_history(rows)
        ctx, _, _ = build()
        ctx.warm(["A"], pd.Timestamp("2026-09-30", tz=IST).date())
        d5, d1 = ctx.window("A", live, live)
        for frame in (d5, d1):
            assert isinstance(frame.index, pd.DatetimeIndex)
            assert frame.index.is_monotonic_increasing and frame.index.is_unique
            assert frame.index.date is not None
        assert len(d5) == 12 + 3

    def test_overlapping_today_bars_do_not_duplicate(self):
        ctx, _, _ = build()
        ctx.warm(["A"], pd.Timestamp("2026-09-30", tz=IST).date())
        t = self.today()
        d5, _ = ctx.window("A", pd.concat([t, t]), None)
        assert d5.index.is_unique


class TestEvaluate:
    def test_calls_strategy_with_the_window(self):
        ctx, strat, _ = build()
        ctx.warm(["A"], pd.Timestamp("2026-09-30", tz=IST).date())
        assert ctx.evaluate("A", day_bars("2026-09-30", n=2), None) == []
        sym, d5, d1 = strat.seen[0]
        assert sym == "A" and len(d5) == 14


class TestHistorifyLoader:
    def test_slices_last_sessions_and_names_symbols_without_bars(self, monkeypatch):
        from signal_engine.backtest import data
        from signal_engine.pipeline.context import HistorifyLoader

        five = pd.concat([day_bars(f"2026-09-{d}") for d in (25, 28, 29)])
        one = pd.concat([day_bars(f"2026-09-{d}", n=30, freq="1min") for d in (25, 28, 29)])
        calls = []

        def fake(interval, **kw):
            calls.append((interval, kw["end"]))
            return {"A": five if interval == "5m" else one}

        monkeypatch.setattr(data, "from_historify", fake)
        h5, h1, failed = HistorifyLoader().load(["A", "B"], pd.Timestamp("2026-09-30").date(), 2, 1)
        assert failed == ["B"]
        assert len(set(h5["A"].index.date)) == 2 and len(set(h1["A"].index.date)) == 1
        assert {c[1] for c in calls} == {"2026-09-29"}  # strictly before the replayed day
