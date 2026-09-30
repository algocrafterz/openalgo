# ruff: noqa: F811  (fixtures imported from test_pipeline_cycle are re-declared as test arguments)
"""The shared cycle also steps open positions: sink.on_bar, then the ExitDriver, then entries."""

import asyncio
import datetime as dt

import pytest

from signal_engine.models import Direction
from signal_engine.pipeline.cycle import run_cycle
from signal_engine.pipeline.exit_driver import ExitDriver, plan_for
from signal_engine.pipeline.sink import RecordingSink
from signal_engine.timeutils import IST
from signal_engine.tracker import TrackedPosition

from .test_pipeline_cycle import ALL_TIMES, bars, fire, logs, make, t  # noqa: F401


class BarSink(RecordingSink):
    def __init__(self):
        super().__init__()
        self.seen = []

    async def on_bar(self, symbol, bar_open, o, h, low, c):
        self.seen.append((symbol, bar_open, o, h, low, c))


class Book:
    def __init__(self, *positions):
        self.d = {(p.symbol, p.strategy): p for p in positions}

    def find_position(self, symbol, strategy):
        return self.d.get((symbol, strategy))


async def flat(_pos):
    return 0


def held(symbol="AAA", tp=101.5):
    return TrackedPosition(
        symbol=symbol,
        strategy="BREAKOUT",
        exchange="NSE",
        product="MIS",
        entry_price=100.0,
        quantity=10,
        sl=98.0,
        tp=tp,
        direction=Direction.LONG,
        fill_price=100.0,
        original_quantity=10,
        entry_time=dt.datetime(2026, 9, 30, 9, 50, tzinfo=IST),
        sig_id="AAA-20260930-0945",
    )


def driver(*positions):
    return ExitDriver(
        strategy="BREAKOUT",
        plan=plan_for("tp1_only"),
        positions=Book(*positions),
        broker_qty=flat,
        interval_min=5,
        safety_bars=2,
    )


def run(deps, close="10:05"):
    return asyncio.run(run_cycle(t(close), deps))


class TestSinkOnBar:
    def test_sink_sees_every_processed_symbols_closed_bar(self, make):
        deps = make({}, symbols=("AAA", "BBB"))
        deps.sink = BarSink()
        run(deps)
        seen = {s[0]: s for s in deps.sink.seen}
        assert set(seen) == {"AAA", "BBB"}
        assert seen["AAA"][1] == t("10:00")
        assert seen["AAA"][2:] == (100.0, 101.0, 99.0, 100.0)

    def test_sink_on_bar_runs_before_a_fire_is_submitted(self, make):
        deps = make({"AAA": [fire(at="10:00")]})
        order = []

        class Sink(BarSink):
            async def on_bar(self, *a):
                order.append("bar")

            async def submit(self, signal):
                order.append("submit")

        deps.sink = Sink()
        run(deps)
        assert order == ["bar", "submit"]


class TestExits:
    def test_target_hit_submits_an_exit_signal_for_a_held_position(self, make):
        frames = {"AAA": bars(ALL_TIMES)}
        frames["AAA"].loc[t("10:00"), "High"] = 102.0  # through TP1 at 101.5
        deps = make({}, frames=frames)
        deps.exits = driver(held())
        deps.sink = BarSink()
        run(deps)
        exits = [s for s in deps.sink.signals if s.direction == Direction.EXIT]
        assert [(s.symbol, s.tp_level) for s in exits] == [("AAA", "TP1")]

    def test_no_exit_driver_means_no_exit_processing(self, make):
        deps = make({})
        deps.sink = BarSink()
        run(deps)
        assert deps.sink.signals == []

    def test_exits_are_stepped_but_entries_are_not_taken_after_the_window(self, make):
        times = ALL_TIMES + ["11:45"]
        frames = {"AAA": bars(times)}
        frames["AAA"].loc[t("11:45"), "High"] = 102.0
        deps = make({"AAA": [fire(at="11:45")]}, frames=frames, now="11:50:03")
        deps.exits = driver(held())
        run(deps, close="11:50")
        exits = [s for s in deps.sink.signals if s.direction == Direction.EXIT]
        entries = [s for s in deps.sink.signals if s.direction != Direction.EXIT]
        assert len(exits) == 1 and entries == []

    def test_after_the_window_only_held_symbols_are_processed(self, make):
        times = ALL_TIMES + ["11:45"]
        frames = {s: bars(times) for s in ("AAA", "BBB")}
        deps = make({}, symbols=("AAA", "BBB"), frames=frames, now="11:50:03")
        deps.exits = driver(held("AAA"))
        deps.sink = BarSink()
        rep = run(deps, close="11:50")
        assert [s[0] for s in deps.sink.seen] == ["AAA"]
        assert rep.evaluated == 1

    def test_halted_cycle_steps_nothing(self, make):
        frames = {"AAA": bars(ALL_TIMES)}
        frames["AAA"].loc[t("10:00"), "High"] = 102.0
        deps = make({}, frames=frames, halted=True)
        deps.exits = driver(held())
        deps.sink = BarSink()
        run(deps)
        assert deps.sink.signals == [] and deps.sink.seen == []

    def test_exit_submit_failure_is_reported_not_swallowed(self, make, logs):
        frames = {"AAA": bars(ALL_TIMES)}
        frames["AAA"].loc[t("10:00"), "High"] = 102.0
        deps = make({}, frames=frames)
        deps.exits = driver(held())

        class Boom(BarSink):
            async def submit(self, signal):
                raise RuntimeError("broker down")

        deps.sink = Boom()
        rep = run(deps)
        assert rep.skipped["AAA"] == "exit submit error"
        assert any("exit submit failed" in line for line in logs)

    def test_a_sink_on_bar_failure_does_not_drop_the_other_symbols(self, make):
        deps = make({"BBB": [fire("BBB", at="10:00")]}, symbols=("AAA", "BBB"))

        class Boom(BarSink):
            async def on_bar(self, symbol, *a):
                if symbol == "AAA":
                    raise RuntimeError("x")

        deps.sink = Boom()
        rep = run(deps)
        assert rep.skipped["AAA"] == "evaluate error"
        assert [s.symbol for s in deps.sink.signals] == ["BBB"]


class TestHolds:
    def test_holds(self):
        d = driver(held())
        assert d.holds("AAA") and not d.holds("ZZZ")


class PendingSink(BarSink):
    """A simulated broker that filled nothing yet: an order armed on the last window bar."""

    def __init__(self):
        super().__init__()
        self.pending = set()

    def has_pending(self, symbol):
        return symbol in self.pending

    async def submit(self, signal):
        await super().submit(signal)
        self.pending.add(signal.symbol)


class TestPendingEntryAfterTheWindow:
    def test_an_entry_armed_on_the_last_window_bar_still_gets_its_next_bar(self, make):
        times = ALL_TIMES + ["11:40", "11:45"]
        frames = {"AAA": bars(times)}
        deps = make({"AAA": [fire(at="11:40")]}, frames=frames, now="11:45:03")
        deps.exits = driver()  # nothing held yet
        deps.sink = PendingSink()
        run(deps, close="11:45")
        assert [s.symbol for s in deps.sink.signals] == ["AAA"]
        deps.clock = deps.clock.__class__(t("11:50:03"))
        run(deps, close="11:50")
        assert [s[1] for s in deps.sink.seen] == [t("11:40"), t("11:45")]

    def test_without_a_pending_order_the_symbol_is_skipped_after_the_window(self, make):
        times = ALL_TIMES + ["11:40", "11:45"]
        deps = make({}, frames={"AAA": bars(times)}, now="11:50:03")
        deps.exits = driver()
        deps.sink = BarSink()
        run(deps, close="11:50")
        assert deps.sink.seen == []
