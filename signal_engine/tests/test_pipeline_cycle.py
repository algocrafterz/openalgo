"""One shared cycle: fetch, gate, evaluate, dedupe, submit. Every skip is named."""

import asyncio
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest
from loguru import logger

from signal_engine.config import RunnerSettings, RunnerStrategy
from signal_engine.pipeline.bars import ReplaySource
from signal_engine.pipeline.clock import SimClock
from signal_engine.pipeline.context import RollingContext
from signal_engine.pipeline.cycle import CycleDeps, run_cycle
from signal_engine.pipeline.sink import RecordingSink
from signal_engine.pipeline.state import RunnerStateStore
from signal_engine.pipeline.strategy import Fire

IST = "Asia/Kolkata"
DAY = dt.date(2026, 9, 30)


def t(s):
    return pd.Timestamp(f"2026-09-30 {s}", tz=IST)


def bars(times, base=100.0):
    idx = pd.DatetimeIndex([t(x) for x in times])
    return pd.DataFrame(
        {"Open": base, "High": base + 1, "Low": base - 1, "Close": base, "Volume": 10.0}, index=idx
    )


ALL_TIMES = [
    "09:15",
    "09:20",
    "09:25",
    "09:30",
    "09:35",
    "09:40",
    "09:45",
    "09:50",
    "09:55",
    "10:00",
]


def fire(symbol="AAA", at="09:55", price=100.0, direction=1, sl_pct=0.01, rr=2.0):
    risk = price * sl_pct
    sl = price - risk if direction == 1 else price + risk
    tp = price + direction * risk * rr
    return Fire(
        symbol, t(at), direction, price, sl, (tp, tp + risk, tp + 2 * risk), "PDH-RT", {"score": 9}
    )


class ScriptedStrategy:
    name = "BREAKOUT"
    interval_min = 5
    warmup_sessions = 1
    needs_1m = False
    entry_window_min = (9 * 60 + 45, 11 * 60 + 45)

    def __init__(self, script, boom=()):
        self.script, self.boom = script, set(boom)

    def evaluate(self, symbol, d5, d1):
        if symbol in self.boom:
            raise RuntimeError("bad input")
        last = d5.index[-1]
        return [f for f in self.script.get(symbol, []) if f.bar_time <= last]


class Loader:
    def __init__(self, fail=()):
        self.fail = set(fail)

    def load(self, symbols, day, s5, s1):
        ok = [s for s in symbols if s not in self.fail]
        prior = bars(["09:15"])
        prior.index = prior.index - pd.Timedelta(days=1)
        return (
            dict.fromkeys(ok, prior),
            dict.fromkeys(ok, prior),
            [s for s in symbols if s in self.fail],
        )


def cfg():
    return RunnerSettings(
        enabled=True,
        bar_interval_minutes=5,
        bar_delay_seconds=3.0,
        cycle_deadline_seconds=6.0,
        retry_backoff_seconds=2.0,
        history_timeout_seconds=10.0,
        max_concurrent_requests=4,
        warmup_sessions=1,
        universe="fno_all",
        strategies={"BREAKOUT": RunnerStrategy("python", "tp1_only")},
    )


@pytest.fixture
def make(tmp_path):
    stores = []

    def _make(
        script,
        symbols=("AAA",),
        now="10:05:03",
        frames=None,
        boom=(),
        fail=(),
        halted=False,
        band=(50.0, 1500.0),
        stale=60.0,
        state=None,
    ):
        frames = frames or {s: bars(ALL_TIMES) for s in symbols}
        strat = ScriptedStrategy(script, boom)
        st = state or RunnerStateStore(
            str(tmp_path / f"s{len(stores)}.db"), trades_lookup=lambda s: False
        )
        stores.append(st)
        deps = CycleDeps(
            strategy=strat,
            source=ReplaySource(frames),
            context=RollingContext(strat, Loader(fail)),
            sink=RecordingSink(),
            state=st,
            clock=SimClock(t(now)),
            symbols=list(symbols),
            cfg=cfg(),
            mode="analyze",
            product="MIS",
            stale_limit_s=stale,
            halted=lambda: halted,
            price_band=band,
            executor=ThreadPoolExecutor(2),
        )
        return deps

    yield _make
    for s in stores:
        s.close()


@pytest.fixture
def logs():
    lines = []
    hid = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    yield lines
    logger.remove(hid)


def run(deps, close="10:05"):
    return asyncio.run(run_cycle(t(close), deps))


class TestFires:
    def test_fire_on_the_just_closed_bar_is_submitted_once(self, make):
        deps = make({"AAA": [fire(at="10:00")]})
        rep = run(deps)
        assert [s.sig_id for s in deps.sink.signals] == ["AAA-20260930-1000"]
        assert rep.submitted == 1 and len(rep.fires) == 1

    def test_older_fire_is_not_resubmitted(self, make):
        deps = make({"AAA": [fire(at="09:55")]})
        run(deps)
        assert deps.sink.signals == []

    def test_second_run_of_the_same_bar_does_nothing(self, make):
        deps = make({"AAA": [fire(at="10:00")]})
        run(deps)
        rep = run(deps)
        assert len(deps.sink.signals) == 1
        assert rep.skipped["AAA"] == "bar already processed"

    def test_restart_with_a_fresh_sink_does_not_duplicate(self, make, tmp_path):
        first = make({"AAA": [fire(at="10:00")]})
        run(first)
        second = make({"AAA": [fire(at="10:00")]}, state=first.state)
        run(second)
        assert second.sink.signals == []

    def test_sig_id_already_seen_is_skipped_by_name(self, make, logs):
        deps = make({"AAA": [fire(at="10:00")]})
        deps.state.record_sig_id("AAA-20260930-1000")
        rep = run(deps)
        assert deps.sink.signals == []
        assert "AAA" in rep.skipped and "sig_id" in rep.skipped["AAA"]

    def test_state_marks_the_bar_even_without_fires(self, make):
        deps = make({})
        run(deps)
        assert deps.state.last_bar("analyze", DAY, "BREAKOUT", "AAA") == t("10:00")


class TestGates:
    def test_price_outside_band_is_skipped_and_named(self, make, logs):
        deps = make({"AAA": [fire(at="10:00", price=2000.0)]})
        rep = run(deps)
        assert deps.sink.signals == []
        assert "above" in rep.skipped["AAA"]
        assert any("AAA" in ln and "above" in ln for ln in logs)

    def test_pretrade_rejection_is_skipped_with_reason(self, make):
        deps = make({"AAA": [fire(at="10:00", rr=0.4)]})
        rep = run(deps)
        assert deps.sink.signals == []
        assert "R:R" in rep.skipped["AAA"]

    def test_outside_the_entry_window_is_skipped(self, make):
        frames = {"AAA": bars(["09:15", "09:20", "09:25", "09:30"])}
        deps = make({"AAA": [fire(at="09:30")]}, frames=frames, now="09:35:03")
        rep = run(deps, close="09:35")
        assert deps.sink.signals == []
        assert "window" in rep.skipped["AAA"]

    def test_halted_engine_submits_nothing(self, make, logs):
        deps = make({"AAA": [fire(at="10:00")]}, halted=True)
        rep = run(deps)
        assert deps.sink.signals == [] and rep.submitted == 0
        assert any("halted" in ln for ln in logs)


class TestDataProblems:
    def test_stale_cycle_is_skipped_and_logged_not_dispatched(self, make, logs):
        deps = make({"AAA": [fire(at="10:00")]}, now="10:07:00", stale=60.0)
        rep = run(deps)
        assert deps.sink.signals == []
        assert rep.stale is True
        assert any("stale" in ln for ln in logs)

    def test_late_symbol_is_named_and_others_still_processed(self, make, logs):
        frames = {"AAA": bars(ALL_TIMES), "BBB": bars(ALL_TIMES[:-1])}
        deps = make(
            {"AAA": [fire(at="10:00")], "BBB": [fire("BBB", at="10:00")]},
            symbols=("AAA", "BBB"),
            frames=frames,
        )
        rep = run(deps)
        assert rep.late == ["BBB"]
        assert [s.symbol for s in deps.sink.signals] == ["AAA"]
        assert any("BBB" in ln and "late" in ln for ln in logs)

    def test_symbol_with_no_bars_at_all_is_late(self, make):
        deps = make({}, symbols=("AAA", "ZZZ"), frames={"AAA": bars(ALL_TIMES)})
        assert run(deps).late == ["ZZZ"]

    def test_warmup_failure_is_named_and_excluded(self, make, logs):
        deps = make(
            {"AAA": [fire(at="10:00")], "BBB": [fire("BBB", at="10:00")]},
            symbols=("AAA", "BBB"),
            fail=["BBB"],
        )
        rep = run(deps)
        assert "BBB" in rep.skipped and "warm-up" in rep.skipped["BBB"]
        assert [s.symbol for s in deps.sink.signals] == ["AAA"]


class TestIsolation:
    def test_one_symbol_raising_does_not_drop_the_others(self, make, logs):
        deps = make({"BBB": [fire("BBB", at="10:00")]}, symbols=("AAA", "BBB"), boom=["AAA"])
        rep = run(deps)
        assert [s.symbol for s in deps.sink.signals] == ["BBB"]
        assert "AAA" in rep.skipped and "error" in rep.skipped["AAA"]

    def test_sink_failure_is_logged_and_the_sig_id_is_not_recorded(self, make, logs):
        deps = make({"AAA": [fire(at="10:00")]})

        async def boom(signal):
            raise RuntimeError("sink down")

        deps.sink.submit = boom
        rep = run(deps)
        assert "AAA" in rep.skipped and "submit" in rep.skipped["AAA"]
        assert not deps.state.seen_sig_id("AAA-20260930-1000")


class TestShadowRecording:
    def test_submitted_signal_is_recorded_durably(self, make):
        deps = make({"AAA": [fire(at="10:00")]})
        run(deps)
        rows = deps.state.shadow_events(DAY, "BREAKOUT")
        sigs = [r for r in rows if r.kind == "signal"]
        assert [(r.symbol, r.sig_id, r.direction) for r in sigs] == [
            ("AAA", "AAA-20260930-1000", "LONG")
        ]
        assert sigs[0].bar_time == t("10:00") and sigs[0].entry == 100.0

    def test_rejected_fire_is_recorded_with_its_reason(self, make):
        deps = make({"AAA": [fire(at="10:00", price=2000.0)]})
        run(deps)
        skips = [r for r in deps.state.shadow_events(DAY, "BREAKOUT") if r.kind == "skip"]
        assert len(skips) == 1 and skips[0].symbol == "AAA" and "above" in skips[0].reason

    def test_late_and_warmup_failed_symbols_are_recorded(self, make):
        frames = {"AAA": bars(ALL_TIMES), "BBB": bars(ALL_TIMES[:-1])}
        deps = make({}, symbols=("AAA", "BBB", "CCC"), frames=frames, fail=["CCC"])
        run(deps)
        reasons = {r.symbol: r.reason for r in deps.state.shadow_events(DAY, "BREAKOUT")}
        assert "late" in reasons["BBB"] and "warm-up" in reasons["CCC"]
        assert "AAA" not in reasons

    def test_stale_cycle_records_every_symbol_it_did_not_evaluate(self, make):
        deps = make({"AAA": [fire(at="10:00")]}, now="10:07:00", stale=60.0)
        run(deps)
        skips = deps.state.shadow_events(DAY, "BREAKOUT")
        assert [(r.symbol, r.kind) for r in skips] == [("AAA", "skip")]
        assert "stale" in skips[0].reason

    def test_trades_db_does_not_hide_a_fire_the_pine_already_sent(self, make, tmp_path):
        # Shadow must record what the runner would have sent even when Pine already produced the
        # same sig_id, otherwise every match would look like a runner miss in the report.
        deps = make(
            {"AAA": [fire(at="10:00")]},
            state=RunnerStateStore(str(tmp_path / "x.db"), trades_lookup=lambda s: True),
        )
        run(deps)
        assert [s.sig_id for s in deps.sink.signals] == ["AAA-20260930-1000"]
        deps.state.close()

    def test_a_dispatching_sink_records_nothing_and_still_honours_trades_db(self, make, tmp_path):
        deps = make(
            {"AAA": [fire(at="10:00")]},
            state=RunnerStateStore(str(tmp_path / "y.db"), trades_lookup=lambda s: True),
        )
        deps.sink.records_shadow = False
        rep = run(deps)
        assert deps.state.shadow_events(DAY, "BREAKOUT") == []
        assert "sig_id" in rep.skipped["AAA"]
        deps.state.close()
