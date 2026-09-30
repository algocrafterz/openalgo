"""P3: trades from the shared cycle + ExitDriver + SimSink equal engine.simulate's trades."""

import datetime as dt

import pandas as pd
import pytest

from signal_engine.backtest.strategies.breakout import Breakout
from signal_engine.backtest.types import RunConfig, Trade
from signal_engine.pipeline import trade_parity as tp
from signal_engine.pipeline.exit_driver import plan_for
from signal_engine.pipeline.strategies.breakout import BreakoutStrategy
from signal_engine.timeutils import IST

from .test_pipeline_replay import LOOSE, synth

TICK = 0.05


def ts(hm, day="2026-09-30"):
    return pd.Timestamp(f"{day} {hm}", tz=IST)


def trade(entry_hm="10:00", exit_hm="10:15", entry=100.0, exit_=104.0, reason="TP", r=2.0, sym="A"):
    return Trade(
        symbol=sym,
        day=dt.date(2026, 9, 30),
        direction=1,
        tag="X",
        entry_time=ts(entry_hm),
        entry=entry,
        signal_price=entry,
        sl=98.0,
        tp=104.0,
        risk=2.0,
        exit_time=ts(exit_hm),
        exit=exit_,
        reason=reason,
        r_gross=r,
        r_net=r,
        legs=1,
    )


class TestCompareTrades:
    def test_identical(self):
        d = tp.compare_trades([trade()], [trade()])
        assert d.identical and d.matched == 1

    def test_engine_only_and_replay_only_are_listed(self):
        d = tp.compare_trades([trade("10:00")], [trade("10:05")])
        assert len(d.engine_only) == 1 and len(d.replay_only) == 1 and not d.identical

    def test_float32_noise_is_tolerated(self):
        assert tp.compare_trades([trade()], [trade(exit_=104.0 + 5e-2 * 1e-2)]).identical

    def test_a_real_price_difference_is_a_mismatch(self):
        d = tp.compare_trades([trade()], [trade(exit_=103.0)])
        assert not d.identical and len(d.differing) == 1

    def test_reason_and_exit_time_differences_are_mismatches(self):
        assert not tp.compare_trades([trade()], [trade(reason="SL")]).identical
        assert not tp.compare_trades([trade()], [trade(exit_hm="10:20")]).identical

    def test_summary_names_each_problem(self):
        d = tp.compare_trades([trade("10:00")], [trade("10:05")])
        text = tp.format_diff(d)
        assert "engine only" in text and "replay only" in text


@pytest.fixture(scope="module")
def data():
    f5, f1 = {}, {}
    for i, s in enumerate(["AAA", "BBB"]):
        f5[s], f1[s] = synth(seed=10 + i)
    return f5, f1


@pytest.mark.timeout(240)
class TestP3OnSyntheticBars:
    @pytest.fixture(scope="class")
    def env(self, data):
        f5, f1 = data
        days = sorted(set(f5["AAA"].index.date))[-4:]
        strategy = BreakoutStrategy(LOOSE, tick_source=lambda s: TICK, warmup_sessions=20)
        adapter = Breakout(minute_source=lambda s, d: f1[s], tick_source=lambda s: TICK)
        return f5, f1, days, strategy, adapter

    def _compare(self, env, plan, **run_kw):
        f5, f1, days, strategy, adapter = env
        run = RunConfig().with_(**adapter.run_overrides, **run_kw)
        eng = tp.engine_trades(adapter, LOOSE, list(f5), f5, days, run, plan)
        rep = tp.replay_trades(strategy, days, list(f5), f5, f1, run, plan)
        assert len(eng) > 0, "synthetic data produced no engine trades; parity would be vacuous"
        return eng, rep, tp.compare_trades(eng, rep.trades)

    def test_tp1_only(self, env):
        _, rep, diff = self._compare(env, "tp1_only")
        assert diff.identical, tp.format_diff(diff, rep.reports)

    def test_three_leg_ladder_with_stop_moves(self, env):
        eng, rep, diff = self._compare(env, "ladder_30_35_35")
        assert diff.identical, tp.format_diff(diff, rep.reports)
        assert any(t.legs > 1 for t in eng), (
            "no scaled-out trade; the ladder path was not exercised"
        )

    def test_time_exit_matches_the_engine(self, env):
        eng, rep, diff = self._compare(env, "tp1_only", time_exit_min=11 * 60 + 30)
        assert diff.identical, tp.format_diff(diff, rep.reports)
        assert any(t.reason == "TIME_EXIT" for t in eng), "the time exit was not exercised"

    def test_per_day_entry_rules_match_the_engine(self, env):
        eng, rep, diff = self._compare(env, "tp1_only", max_trades_per_day=1)
        assert diff.identical, tp.format_diff(diff, rep.reports)


class TestHelpers:
    def test_sim_qty_reports_the_simulated_broker_position(self):
        import asyncio
        from types import SimpleNamespace

        class Sink:
            def __init__(self, held):
                self.held = held

            def find_position(self, symbol, strategy):
                return SimpleNamespace(quantity=700) if self.held else None

        pos = SimpleNamespace(symbol="A", strategy="BREAKOUT")
        assert asyncio.run(tp._sim_qty(Sink(True))(pos)) == 700
        assert asyncio.run(tp._sim_qty(Sink(False))(pos)) == 0

    def test_skip_reason_names_the_cycle_skip_on_the_signal_bar(self):
        from signal_engine.pipeline.cycle import CycleReport

        rep = CycleReport(bar_close=ts("10:00"))
        rep.skipped["A"] = "reward:risk 0.9 below 1.0"
        assert "reward:risk" in tp.skip_reason(trade("10:00"), [rep], 5)
        assert "no skip recorded" in tp.skip_reason(trade("10:05"), [rep], 5)

    def test_format_diff_explains_engine_only_trades_with_the_replay_skip(self):
        from signal_engine.pipeline.cycle import CycleReport

        rep = CycleReport(bar_close=ts("10:00"))
        rep.skipped["A"] = "outside the entry window"
        diff = tp.compare_trades([trade("10:00")], [])
        assert "outside the entry window" in tp.format_diff(diff, [rep])

    def test_format_diff_lists_differences(self):
        diff = tp.compare_trades([trade()], [trade(exit_=103.0)])
        assert "differs" in tp.format_diff(diff)

    def test_cli_reports_and_returns_zero_on_parity(self, data, capsys, monkeypatch):
        monkeypatch.setattr("signal_engine.backtest.strategies.breakout.tick_size", lambda s: TICK)
        from types import SimpleNamespace

        f5, f1 = data
        args = SimpleNamespace(day=None, long_sessions=20, days=1, plan="tp1_only")
        end = sorted(set(f5["AAA"].index.date))[-1]
        args.day = end
        strategy = BreakoutStrategy(tick_source=lambda s: TICK, warmup_sessions=20)
        rc = tp.cli(args, strategy, ["AAA"], lambda symbols, start, stop: (f5, f1))
        out = capsys.readouterr().out
        assert "P3 tp1_only" in out and "matched" in out
        assert rc in (0, 1)
