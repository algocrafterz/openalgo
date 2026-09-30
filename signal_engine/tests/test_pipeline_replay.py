"""P1 in code: a bar-by-bar replay through the runner equals the strategy over the full day."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from signal_engine.pipeline import replay
from signal_engine.pipeline.strategies.breakout import BreakoutStrategy
from signal_engine.pipeline.strategy import Fire
from signal_engine.strategy_core.breakout import BreakoutParams

IST = "Asia/Kolkata"

#: every filter loosened so a random walk produces fires; the logic under test is the plumbing
LOOSE = BreakoutParams(
    score_threshold=0,
    min_session_vf=0.0,
    rvol_min=0.0,
    rvol_strong=0.0,
    break_min_rvol=0.0,
    require_htf_close=False,
    min_headroom_r=0.0,
    max_tp_r=0.0,
    max_chase_atr=0.0,
    cooldown_bars=0,
)


def synth(seed: int, sessions: int = 21, end="2026-09-29"):
    """Random-walk 1-minute bars over consecutive weekdays, and their 5-minute resample."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(end=end, periods=sessions)
    price, parts = 500.0, []
    for d in days:
        idx = pd.date_range(f"{d.date()} 09:15", periods=375, freq="1min", tz=IST)
        steps = rng.normal(0, 0.35, len(idx)).cumsum()
        close = price + steps
        price = float(close[-1])
        parts.append(
            pd.DataFrame(
                {
                    "Open": close - rng.normal(0, 0.05, len(idx)),
                    "High": close + np.abs(rng.normal(0.2, 0.1, len(idx))),
                    "Low": close - np.abs(rng.normal(0.2, 0.1, len(idx))),
                    "Close": close,
                    "Volume": rng.integers(500, 1500, len(idx)).astype(float),
                },
                index=idx,
            )
        )
    m1 = pd.concat(parts)
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    m5 = m1.resample("5min", label="left", closed="left").agg(agg).dropna()
    return m5, m1


@pytest.fixture(scope="module")
def data():
    f5, f1 = {}, {}
    for i, s in enumerate(["AAA", "BBB"]):
        f5[s], f1[s] = synth(seed=10 + i)
    return f5, f1


@pytest.fixture
def strategy():
    return BreakoutStrategy(LOOSE, tick_source=lambda s: 0.05, warmup_sessions=20)


class TestParityP1:
    def test_replay_walk_equals_full_day_evaluate(self, data, strategy):
        f5, f1 = data
        total = 0
        for day in sorted(set(f5["AAA"].index.date))[-2:]:
            res = replay.replay_day(strategy, day, list(f5), f5, f1)
            for s in f5:
                want = replay.windowed_reference(strategy, s, day, f5, f1)
                got = [f for f in res.fires if f.symbol == s]
                diff = replay.compare_fires(want, got)
                assert diff.identical, (s, day, diff)
                total += len(want)
        assert total > 0, "synthetic data produced no fires; the parity check would be vacuous"

    def test_replay_never_reads_a_forming_bar(self, data, strategy):
        f5, f1 = data
        day = sorted(set(f5["AAA"].index.date))[-1]
        res = replay.replay_day(strategy, day, ["AAA"], f5, f1)
        for r in res.reports:
            assert r.bar_close.minute % 5 == 0 and not r.stale


class TestCompare:
    def fire(self, at="10:00", sl=99.0):
        return Fire("A", pd.Timestamp(f"2026-09-30 {at}", tz=IST), 1, 100.0, sl, (101.0,), "X", {})

    def test_identical(self):
        assert replay.compare_fires([self.fire()], [self.fire()]).identical

    def test_reports_each_side(self):
        d = replay.compare_fires([self.fire("10:00")], [self.fire("10:05")])
        assert len(d.only_expected) == 1 and len(d.only_got) == 1 and not d.identical

    def test_prices_are_compared(self):
        assert not replay.compare_fires([self.fire(sl=99.0)], [self.fire(sl=98.0)]).identical

    def test_float_noise_is_tolerated(self):
        assert replay.compare_fires([self.fire(sl=99.0)], [self.fire(sl=99.0 + 1e-9)]).identical


class TestInMemoryLoader:
    def test_returns_prior_sessions_only_and_last_n(self, data):
        f5, f1 = data
        day = sorted(set(f5["AAA"].index.date))[-1]
        h5, h1, failed = replay.InMemoryLoader(f5, f1).load(["AAA", "ZZZ"], day, 3, 2)
        assert failed == ["ZZZ"]
        assert len(set(h5["AAA"].index.date)) == 3
        assert max(h5["AAA"].index.date) < day
        assert len(set(h1["AAA"].index.date)) == 2


class TestArgs:
    def test_day_is_parsed(self):
        a = replay.parse_args(["--day", "2026-09-29", "--symbols", "TCS,INFY"])
        assert a.day == dt.date(2026, 9, 29) and a.symbols == ["TCS", "INFY"]

    def test_parity_mode(self):
        a = replay.parse_args(["--parity", "--days", "5", "--limit", "3"])
        assert a.parity and a.days == 5 and a.limit == 3


class TestTradeParityArgs:
    def test_trade_parity_flag_and_plan(self):
        a = replay.parse_args(["--trade-parity", "--plan", "ladder_30_35_35", "--days", "3"])
        assert a.trade_parity and a.plan == "ladder_30_35_35" and a.days == 3

    def test_plan_defaults_to_tp1_only(self):
        assert replay.parse_args(["--trade-parity"]).plan == "tp1_only"
