"""The runner's warm-up must refuse stale or incomplete history instead of trading on it.

2026-10-14: the 15:35 backfill had skipped 10-13 (a symbol one day stale was not fetched), so the
warm-up's newest session was 10-12, and prev_session_value_area silently used it as the "previous"
session. BREAKOUT-PY's PFC entry carried 10-12's PVAH/PPOC/PVAL (333.875/333.125/332.375) instead
of 10-13's (337.375/336.625/334.375).

HistorifyLoader now checks every symbol against the NSE calendar: the newest warm-up session must
be the previous trading session, and that session must be complete. A symbol that fails is
excluded for the day and reported. These tests use the real calendar file, so weekends, the
2026-10-20 holiday and the 2026-11-08 special session are the real ones.
"""

import datetime as dt

import pandas as pd
import pytest

from signal_engine.backtest import data
from signal_engine.features.value_area import prev_session_value_area
from signal_engine.pipeline.context import HistorifyLoader, RollingContext

IST = "Asia/Kolkata"
D = dt.date


def _bars(day: str, level: float, start="09:15", end="15:29", freq="1min") -> pd.DataFrame:
    idx = pd.date_range(f"{day} {start}", f"{day} {end}", freq=freq, tz=IST)
    return pd.DataFrame(
        {"Open": level, "High": level + 0.5, "Low": level - 0.5, "Close": level, "Volume": 100.0},
        index=idx,
    )


def _store(monkeypatch, days_by_symbol, partial=None, extra=None):
    """Fake Historify: full 1m sessions (09:15-15:29) and 5m resampled from them.

    `partial` maps (symbol, day) to (first, last) bar times of a truncated session; `extra` maps a
    symbol to extra 1m bars (stray prints)."""
    partial = partial or {}
    frames1 = {}
    for sym, days in days_by_symbol.items():
        parts = [_bars(d, lvl, *partial.get((sym, d), ("09:15", "15:29"))) for d, lvl in days]
        if extra and sym in extra:
            parts.append(extra[sym])
        frames1[sym] = pd.concat(parts).sort_index()

    def fake(interval, symbols=None, start=None, end=None, **_kw):
        out = {}
        for sym in symbols:
            if sym not in frames1:
                continue
            f = frames1[sym]
            dates = pd.Index(f.index.date)
            f = f[(dates >= pd.Timestamp(start).date()) & (dates <= pd.Timestamp(end).date())]
            if interval == "5m":
                f = f.resample("5min", label="left", closed="left").agg(
                    {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
                ).dropna()
            out[sym] = f
        return out

    monkeypatch.setattr(data, "from_historify", fake)


def _load(day, symbols=("PFC",), sessions5=2, **kw):
    loader = HistorifyLoader(**kw)
    return loader, loader.load(list(symbols), day, sessions5, 2)


class TestThePfcCase:
    def test_a_store_missing_the_previous_session_excludes_the_symbol(self, monkeypatch):
        _store(monkeypatch, {"PFC": [("2026-10-09", 329.0), ("2026-10-12", 333.0)]})
        loader, (h5, h1, failed) = _load(D(2026, 10, 14))
        assert failed == ["PFC"]
        assert "PFC" not in h5 and "PFC" not in h1
        assert "2026-10-12" in loader.rejected["PFC"] and "2026-10-13" in loader.rejected["PFC"]

    def test_with_the_previous_session_present_the_levels_come_from_it(self, monkeypatch):
        _store(monkeypatch, {"PFC": [("2026-10-12", 333.0), ("2026-10-13", 337.0)]})
        _loader, (_h5, h1, failed) = _load(D(2026, 10, 14))
        assert failed == []
        today1 = _bars("2026-10-14", 340.0, end="10:29")
        both = pd.concat([h1["PFC"], today1])
        pva = prev_session_value_area(both, today1.index, tick=0.05)
        assert abs(pva["ppoc"].iloc[-1] - 337.0) < 1.0  # 10-13's level, not 10-12's 333

    def test_a_stale_symbol_is_never_warmed_so_it_cannot_be_evaluated(self, monkeypatch):
        _store(monkeypatch, {"PFC": [("2026-10-12", 333.0)], "NTPC": [("2026-10-13", 350.0)]})

        class _Strategy:
            name, warmup_sessions = "STUB", 2

        ctx = RollingContext(_Strategy(), HistorifyLoader())
        failed = ctx.warm(["PFC", "NTPC"], D(2026, 10, 14))
        assert failed == ["PFC"] and ctx.warmed == {"NTPC"}
        with pytest.raises(KeyError):
            ctx.window("PFC", _bars("2026-10-14", 340.0, freq="5min"), None)


class TestSessionsFromTheCalendar:
    def test_consecutive_trading_sessions(self, monkeypatch):
        _store(monkeypatch, {"PFC": [("2026-10-14", 330.0), ("2026-10-15", 331.0)]})
        assert _load(D(2026, 10, 16))[1][2] == []

    def test_monday_reads_friday(self, monkeypatch):
        _store(monkeypatch, {"PFC": [("2026-10-15", 330.0), ("2026-10-16", 331.0)]})
        assert _load(D(2026, 10, 19))[1][2] == []

    def test_monday_with_thursday_as_the_newest_session_is_stale(self, monkeypatch):
        _store(monkeypatch, {"PFC": [("2026-10-14", 330.0), ("2026-10-15", 331.0)]})
        assert _load(D(2026, 10, 19))[1][2] == ["PFC"]

    def test_the_day_after_a_holiday_reads_the_session_before_it(self, monkeypatch):
        # 2026-10-20 is an NSE holiday: 10-21's previous session is 10-19.
        _store(monkeypatch, {"PFC": [("2026-10-16", 330.0), ("2026-10-19", 331.0)]})
        assert _load(D(2026, 10, 21))[1][2] == []

    def test_a_stray_bar_on_a_non_session_day_is_ignored(self, monkeypatch):
        # BAJAJ-AUTO had a single 1m bar on Sunday 2026-09-13 in the real store.
        stray = _bars("2026-10-18", 999.0, start="10:00", end="10:00")
        _store(
            monkeypatch,
            {"PFC": [("2026-10-15", 330.0), ("2026-10-16", 331.0)]},
            extra={"PFC": stray},
        )
        _loader, (_h5, h1, failed) = _load(D(2026, 10, 19))
        assert failed == []
        assert D(2026, 10, 18) not in set(h1["PFC"].index.date)
        assert max(h1["PFC"].index.date) == D(2026, 10, 16)


class TestFailedOrPartialBackfill:
    def test_a_previous_session_cut_off_early_is_incomplete(self, monkeypatch):
        _store(
            monkeypatch,
            {"PFC": [("2026-10-12", 333.0), ("2026-10-13", 337.0)]},
            partial={("PFC", "2026-10-13"): ("09:15", "12:00")},
        )
        loader, (_h5, _h1, failed) = _load(D(2026, 10, 14))
        assert failed == ["PFC"]
        assert "incomplete" in loader.rejected["PFC"]

    def test_only_the_symbol_the_backfill_missed_is_excluded(self, monkeypatch):
        _store(monkeypatch, {"PFC": [("2026-10-12", 333.0)], "NTPC": [("2026-10-13", 350.0)]})
        _loader, (h5, _h1, failed) = _load(D(2026, 10, 14), symbols=("PFC", "NTPC"))
        assert failed == ["PFC"] and set(h5) == {"NTPC"}

    def test_a_symbol_with_no_bars_at_all_is_excluded(self, monkeypatch):
        _store(monkeypatch, {"NTPC": [("2026-10-13", 350.0)]})
        _loader, (_h5, _h1, failed) = _load(D(2026, 10, 14), symbols=("PFC", "NTPC"))
        assert failed == ["PFC"]

    def test_a_short_special_session_is_not_called_incomplete(self, monkeypatch):
        # 2026-11-08 (Sunday) is a listed special (Muhurat) session with unknown windows.
        _store(
            monkeypatch,
            {"PFC": [("2026-11-06", 330.0), ("2026-11-08", 331.0)]},
            partial={("PFC", "2026-11-08"): ("18:15", "19:15")},
        )
        assert _load(D(2026, 11, 9))[1][2] == []


class TestFailClosedAndReported:
    def test_an_unavailable_calendar_excludes_every_symbol(self, monkeypatch):
        from signal_engine.backtest.data_readiness.calendar import CalendarError

        _store(monkeypatch, {"PFC": [("2026-10-13", 337.0)]})

        class _Ended:
            specials = {}

            def expected_sessions(self, a, b):
                raise CalendarError("2027-01-04 is outside the calendar coverage 2016-2026")

        loader, (h5, _h1, failed) = _load(D(2026, 10, 14), calendar=_Ended())
        assert failed == ["PFC"] and h5 == {}
        assert "calendar" in loader.rejected["PFC"]

    def test_rejections_are_reported_once_a_day(self, monkeypatch):
        _store(monkeypatch, {"PFC": [("2026-10-12", 333.0)], "NTPC": [("2026-10-12", 350.0)]})
        reports = []
        loader = HistorifyLoader(on_rejected=lambda day, rejected: reports.append((day, dict(rejected))))
        loader.load(["PFC", "NTPC"], D(2026, 10, 14), 2, 2)
        loader.load(["PFC", "NTPC"], D(2026, 10, 14), 2, 2)  # second strategy, same day
        assert len(reports) == 1
        day, rejected = reports[0]
        assert day == D(2026, 10, 14) and set(rejected) == {"PFC", "NTPC"}

    def test_a_clean_warm_up_reports_nothing(self, monkeypatch):
        _store(monkeypatch, {"PFC": [("2026-10-12", 333.0), ("2026-10-13", 337.0)]})
        reports = []
        HistorifyLoader(on_rejected=lambda *a: reports.append(a)).load(["PFC"], D(2026, 10, 14), 2, 2)
        assert reports == []



class TestOnlyTheStaleStockLosesItsSignals:
    """End to end through the shared cycle: PFC (stale store) produces no signal, while NTPC
    (fresh history) in the same cycle still does."""

    def test_the_stale_stock_is_skipped_and_the_others_still_fire(self, monkeypatch, tmp_path):
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        from signal_engine.config import RunnerSettings, RunnerStrategy
        from signal_engine.pipeline.bars import ReplaySource
        from signal_engine.pipeline.clock import SimClock
        from signal_engine.pipeline.cycle import CycleDeps, run_cycle
        from signal_engine.pipeline.sink import RecordingSink
        from signal_engine.pipeline.state import RunnerStateStore
        from signal_engine.pipeline.strategy import Fire

        _store(monkeypatch, {"PFC": [("2026-10-12", 333.0)], "NTPC": [("2026-10-13", 350.0)]})

        class _FiresForEveryone:
            name, interval_min, warmup_sessions, needs_1m = "BREAKOUT", 5, 2, False
            entry_window_min = (9 * 60 + 45, 11 * 60 + 45)

            def evaluate(self, symbol, d5, d1):
                at, px = d5.index[-1], float(d5["Close"].iloc[-1])
                return [Fire(symbol, at, 1, px, px * 0.99, (px * 1.02, px * 1.03, px * 1.04), "PVAH", {})]

        strat = _FiresForEveryone()
        ctx = RollingContext(strat, HistorifyLoader())
        assert ctx.warm(["PFC", "NTPC"], D(2026, 10, 14)) == ["PFC"]
        today = {s: _bars("2026-10-14", lvl, end="10:00", freq="5min") for s, lvl in (("PFC", 340.0), ("NTPC", 352.0))}
        cfg = RunnerSettings(
            enabled=True, bar_interval_minutes=5, bar_delay_seconds=3.0, cycle_deadline_seconds=6.0,
            retry_backoff_seconds=2.0, history_timeout_seconds=10.0, max_concurrent_requests=4,
            warmup_sessions=2, universe="fno_all",
            strategies={"BREAKOUT": RunnerStrategy("python", "tp1_only")},
        )
        state = RunnerStateStore(str(tmp_path / "state.db"), trades_lookup=lambda s: False)
        deps = CycleDeps(
            strategy=strat, source=ReplaySource(today), context=ctx, sink=RecordingSink(), state=state,
            clock=SimClock(pd.Timestamp("2026-10-14 10:05:03", tz=IST)), symbols=["PFC", "NTPC"],
            cfg=cfg, mode="analyze", product="MIS", stale_limit_s=60.0, halted=lambda: False,
            price_band=(50.0, 1500.0), executor=ThreadPoolExecutor(2),
        )
        try:
            report = asyncio.run(run_cycle(pd.Timestamp("2026-10-14 10:05", tz=IST), deps))
        finally:
            state.close()
        assert [s.symbol for s in deps.sink.signals] == ["NTPC"]
        assert report.skipped.get("PFC") == "warm-up failed"
