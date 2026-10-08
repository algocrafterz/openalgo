"""Cross-sectional selection baseline: strategy names against every eligible symbol, same session."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import baseline_cross_sectional as xs
from signal_engine.backtest import costs, indicators
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig
from signal_engine.tests.cost_fixtures import make_model

SIGNAL_MINS = 9 * 60 + 20
BARS = 75
EXIT_BAR = 66  # the 14:45 bar, where the time exit fires


class _Short(Strategy):
    """Shorts the picked names at the 09:20 close with a fixed percent stop and a far target."""

    name = "synthetic-short"
    supports_selection_null = True

    def __init__(self, picked=(), stop_pct=0.01, outside=()):
        self.picked = set(picked)
        self.stop_pct = stop_pct
        self.outside = set(outside)

    def prepare(self, df, p):
        d = indicators.add_session_columns(df)
        d["atr"] = (d["High"] - d["Low"]).rolling(14, min_periods=1).mean()
        return d

    def _signal(self, c, i, direction):
        close = float(c["Close"][i])
        if direction != -1 or c["mins"][i] != SIGNAL_MINS:
            return None
        return EntrySignal(-1, close * (1 + self.stop_pct), close * 0.5, tag="t")

    def entry(self, c, i, p, direction):
        return self._signal(c, i, direction) if c.symbol in self.picked else None

    def null_entry(self, c, i, p, direction):
        if c.symbol in self.outside:
            return None
        return self._signal(c, i, direction)


class _AtrShort(_Short):
    """Stop two of the symbol's own ATRs away."""

    def _signal(self, c, i, direction):
        close = float(c["Close"][i])
        if direction != -1 or c["mins"][i] != SIGNAL_MINS:
            return None
        return EntrySignal(-1, close + 2 * float(c["atr"][i]), close * 0.5, tag="t")


def _sessions(n):
    return [d.date() for d in pd.bdate_range("2026-01-05", periods=n)]


def _frame(days, drift_of, price=100.0, wick=0.0, spikes=None):
    """One symbol: a straight path from the 09:25 open to the 14:45 close worth `drift_of(day)` bps."""
    frames = []
    for day in days:
        step = drift_of(day) / 1e4 / (EXIT_BAR - 1)
        k = np.arange(BARS)
        close = price * (1 + step * np.maximum(0, k - 1))
        open_ = np.r_[close[0], close[:-1]]
        high = np.maximum(open_, close) * (1 + 2e-5 + wick)
        low = np.minimum(open_, close) * (1 - 2e-5)
        if spikes and day in spikes:
            high[spikes[day]] = price * 1.5
        index = pd.date_range(f"{day} 09:15", periods=BARS, freq="5min", tz="Asia/Kolkata")
        frames.append(
            pd.DataFrame(
                {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": 1000.0},
                index=index,
            )
        )
    return pd.concat(frames)


def _market(picked_bps, other_bps, noise=3.0, n_symbols=20, n_days=60, picked_n=5, seed=1):
    """`picked_n` names (S00..) drift `picked_bps` on average, the rest `other_bps`."""
    rng = np.random.default_rng(seed)
    days = _sessions(n_days)
    common = {d: rng.normal(0, 8) for d in days}
    frames, picked = {}, []
    for s in range(n_symbols):
        sym = f"S{s:02d}"
        base = picked_bps if s < picked_n else other_bps
        if s < picked_n:
            picked.append(sym)
        shocks = {d: base + common[d] + rng.normal(0, noise) for d in days}
        frames[sym] = _frame(days, shocks.__getitem__, price=100.0 + s)
    return frames, picked


def _run(**kw):
    return RunConfig(skip_open_minutes=0, allow_longs=False, **kw)


def _strategy_trades(strategy, frames, run, model):
    out = []
    for sym, df in frames.items():
        ctx = Ctx(strategy.prepare_for(sym, df.copy(), None), symbol=sym)
        out += simulate(ctx, strategy, None, run)
    return costs.recost(out, model)


def _block(strategy, frames, trades=None, run=None, model=None, **kw):
    run = run or _run()
    model = model or make_model()
    if trades is None:
        trades = _strategy_trades(strategy, frames, run, model)
    return xs.cross_sectional_baseline(
        trades,
        lambda chunk: {s: frames[s].copy() for s in chunk if s in frames},
        strategy,
        SimpleNamespace(),
        run,
        model,
        symbols=list(frames),
        **kw,
    )


class TestKnownAnswers:
    def test_pure_drift_makes_strategy_positive_but_excess_zero(self):
        frames, picked = _market(-30.0, -30.0)
        out = _block(_Short(picked), frames)
        assert out["strategy_gross_bps"] > 20
        assert abs(out["null_gross_bps"] - out["strategy_gross_bps"]) < 1.0
        assert abs(out["excess_gross_bps"]) < 1.0
        assert abs(out["excess_t_stat"]) < 2.0
        assert out["null_gross_t_stat"] > 3.0

    def test_selection_adds_value(self):
        frames, picked = _market(-80.0, -20.0)
        out = _block(_Short(picked), frames)
        assert out["null_gross_bps"] == pytest.approx(35.0, abs=1.0)
        assert out["excess_gross_bps"] == pytest.approx(45.0, abs=1.0)
        assert out["excess_t_stat"] > 10.0

    def test_no_drift_null_is_zero(self):
        frames, picked = _market(0.0, 0.0)
        out = _block(_Short(picked), frames)
        assert abs(out["null_gross_bps"]) < 1.0
        assert abs(out["null_gross_t_stat"]) < 3.0

    def test_random_selection_gives_excess_near_zero(self):
        # D15 known answer: the picks are a seeded random draw from the eligible pool.
        frames, _ = _market(-30.0, -30.0, noise=10.0, picked_n=0)
        days = _sessions(60)
        rng = np.random.default_rng(7)
        symbols = sorted(frames)
        run, model = _run(), make_model()
        trades = []
        for day in days:
            chosen = rng.choice(symbols, size=3, replace=False)
            for sym in chosen:
                strat = _Short([sym])
                ctx = Ctx(strat.prepare_for(sym, frames[sym].copy(), None), symbol=sym)
                trades += [t for t in simulate(ctx, strat, None, run) if t.day == day]
        trades = costs.recost(trades, model)
        out = _block(_Short(), frames, trades=trades)
        assert out["trades"] == len(trades)
        assert abs(out["excess_net_bps"]) < 3.0
        assert abs(out["excess_t_stat"]) < 2.5


class TestReplayRules:
    def _prepared(self, strategy, frame, symbol="A"):
        return strategy.prepare_for(symbol, frame.copy(), None)

    def _wanted(self, trade):
        key = xs.trade_key(trade)
        return {key[0]: [(0, key[1], key[2])]}

    def test_stop_rule_replayed_on_the_null_symbol(self):
        days = _sessions(3)
        calm = _frame(days, lambda d: -10.0, wick=0.002)
        wild = _frame(days, lambda d: -10.0, wick=0.008, price=100.0)
        strat = _AtrShort(["A"])
        run, model = _run(), make_model()
        trades = _strategy_trades(strat, {"A": calm}, run, model)
        wanted = self._wanted(trades[0])
        got = {}
        for name, frame in (("A", calm), ("B", wild)):
            filled, _, _ = xs.replay_symbol(
                strat, name, self._prepared(strat, frame, name), wanted, None, run
            )
            got[name] = filled[0][1]
        # each replay's stop is two of its OWN symbol's ATRs from its own signal close
        for name, frame in (("A", calm), ("B", wild)):
            prep = self._prepared(strat, frame, name)
            row = prep.loc[prep.index == pd.Timestamp(f"{days[0]} 09:20", tz="Asia/Kolkata")]
            trade = got[name]
            assert trade.sl - trade.signal_price == pytest.approx(2 * float(row["atr"].iloc[0]))
        assert got["B"].risk > got["A"].risk * 2

    def test_a_spike_through_the_replay_stop_exits_sl(self):
        days = _sessions(3)
        spiky = _frame(days, lambda d: -10.0, spikes={days[0]: 30})
        strat = _Short(["A"])
        run, model = _run(), make_model()
        flat = _frame(days, lambda d: -10.0)
        trades = _strategy_trades(strat, {"A": flat}, run, model)
        trade = [t for t in trades if t.day == days[0]][0]
        filled, _, _ = xs.replay_symbol(
            strat, "B", self._prepared(strat, spiky, "B"), self._wanted(trade), None, run
        )
        assert filled[0][1].reason in ("SL", "SL_GAP")

    def test_same_entry_bar_and_time_exit(self):
        frames, picked = _market(-30.0, -30.0, n_days=3)
        strat = _Short(picked)
        run, model = _run(), make_model()
        trades = _strategy_trades(strat, frames, run, model)
        trade = [t for t in trades if t.symbol == "S00"][0]
        wanted = self._wanted(trade)
        for sym in ("S00", "S07"):
            filled, _, _ = xs.replay_symbol(
                strat, sym, self._prepared(strat, frames[sym], sym), wanted, None, run
            )
            replay = filled[0][1]
            assert replay.entry_time == trade.entry_time
            assert replay.reason == "TIME_EXIT"
            assert replay.exit_time == pd.Timestamp(f"{trade.day} 14:45", tz="Asia/Kolkata")
            assert replay.exit_time <= pd.Timestamp(f"{trade.day} 14:50", tz="Asia/Kolkata")

    def test_ineligible_symbol_session_not_in_pool(self):
        frames, picked = _market(-30.0, -30.0, n_days=10)
        day = _sessions(10)[4]
        frames["S19"] = frames["S19"][frames["S19"].index.date != day]
        full = _block(_Short(picked), {**frames, "S19": frames["S19"]})
        # the five picks share one key a session, and S19 is not in that session's pool
        assert full["replays"] == 10 * 20 - 1

    def test_opportunity_set_none_counted(self):
        frames, picked = _market(-30.0, -30.0, n_days=10)
        out = _block(_Short(picked, outside=["S10", "S11"]), frames)
        assert out["replay_skips"]["NOT_IN_OPPORTUNITY_SET"] == 2 * 10
        assert out["replays_filled"] == out["replays"] - 2 * 10

    def test_missing_signal_bar_is_counted(self):
        frames, picked = _market(-30.0, -30.0, n_days=5)
        day = _sessions(5)[2]
        gap = pd.Timestamp(f"{day} 09:20", tz="Asia/Kolkata")
        frames["S12"] = frames["S12"][frames["S12"].index != gap]
        out = _block(_Short(picked), frames)
        assert out["replay_skips"]["NO_ENTRY_BAR"] == 1

    def test_tight_stop_is_a_counted_skip(self):
        days = _sessions(3)
        frame = _frame(days, lambda d: -10.0)
        trades = _strategy_trades(_Short(["A"]), {"A": frame}, _run(), make_model())
        tight = _Short(["A"], stop_pct=0.001)
        filled, skipped, attempts = xs.replay_symbol(
            tight,
            "A",
            tight.prepare_for("A", frame.copy(), None),
            self._wanted(trades[0]),
            None,
            _run(),
        )
        assert filled == [] and attempts == 1
        assert skipped == {"STOP_TOO_TIGHT": 1}

    def test_each_replay_pays_own_statutory_cost(self):
        days = _sessions(2)
        calm = _frame(days, lambda d: -10.0, wick=0.002)
        wild = _frame(days, lambda d: -10.0, wick=0.008)
        frames = {"A": calm, "B": wild}
        strat = _AtrShort(["A"])
        # a broker with a capped flat brokerage prices each notional differently
        run, model = _run(), make_model(broker="zerodha")
        trades = _strategy_trades(strat, frames, run, model)
        out = _block(strat, frames, trades=trades, run=run, model=model)
        assert out is not None
        wanted = {}
        for t in trades:
            key = xs.trade_key(t)
            wanted.setdefault(key[0], []).append((len(wanted), key[1], key[2]))
        replays = []
        for sym, frame in frames.items():
            filled, _, _ = xs.replay_symbol(
                strat, sym, strat.prepare_for(sym, frame.copy(), None), wanted, None, run
            )
            replays += [t for _, t in filled]
        raw_costs = {round(model.trade_cost_bps(t), 6) for t in replays}
        assert len(raw_costs) > 1  # the two stop widths land in different notional buckets
        per_key = {}
        for t in costs.recost(replays, model):
            per_key.setdefault(t.day, []).append(t.net_bps)
        # the null net is the mean of replays each costed on its own notional
        expected = np.mean([np.mean(v) for v in per_key.values()])
        assert out["null_net_bps"] == pytest.approx(expected, abs=0.01)


class TestStatistics:
    def test_deterministic_for_seed(self):
        frames, picked = _market(-30.0, -30.0, n_days=20)
        first = _block(_Short(picked), frames, max_replays=20 * 10, seed=3)
        again = _block(_Short(picked), frames, max_replays=20 * 10, seed=3)
        assert first == again

    def test_session_budget_sampling(self):
        frames, picked = _market(-30.0, -30.0, n_days=20)
        # one shared key a session x 20 symbols = 20 replays a session: a 100 budget keeps 5
        out = _block(_Short(picked), frames, max_replays=100)
        assert out["sampled"] is True
        assert out["sessions"] == 5
        assert out["sessions_population"] == 20
        assert out["replays"] <= 100
        full = _block(_Short(picked), frames)
        assert full["sampled"] is False and full["sessions"] == 20

    def test_sample_sessions_is_seeded_and_whole_sessions(self):
        keys = [(f"2026-01-{d:02d}", 1, -1) for d in range(1, 21) for _ in range(1)]
        keys += [(f"2026-01-{d:02d}", 2, -1) for d in range(1, 21)]
        a = xs.sample_sessions(keys, 10, seed=0)
        assert a == xs.sample_sessions(keys, 10, seed=0)
        assert len(a) == 5
        assert a != xs.sample_sessions(keys, 10, seed=1)

    def test_excess_t_unchanged_when_trades_duplicated_within_session(self):
        frames, picked = _market(-60.0, -30.0, n_days=30)
        run, model = _run(), make_model()
        trades = _strategy_trades(_Short(picked), frames, run, model)
        once = _block(_Short(picked), frames, trades=trades)
        twice = _block(_Short(picked), frames, trades=trades + trades)
        assert twice["excess_t_stat"] == once["excess_t_stat"]
        assert twice["excess_net_bps"] == once["excess_net_bps"]
        assert twice["trades"] == 2 * once["trades"]

    def test_no_hook_no_block(self):
        class _Plain(_Short):
            supports_selection_null = False

        frames, picked = _market(-30.0, -30.0, n_days=3)
        assert _block(_Plain(picked), frames) is None

    def test_default_hook_raises(self):
        class _NoHook(Strategy):
            def prepare(self, df, p):
                return df

            def entry(self, c, i, p, direction):
                return None

        with pytest.raises(NotImplementedError):
            _NoHook().null_entry(None, 0, None, 1)
        assert _NoHook.supports_selection_null is False

    def test_replay_leaves_ledgers_untouched(self):
        # The replay calls the engine without a recorder, so it can never write a ledger row;
        # the strategy instance the run keeps is not the one the replay mutates.
        frames, picked = _market(-30.0, -30.0, n_days=3)
        strat = _Short(picked)
        before = dict(vars(strat))
        _block(strat, frames)
        assert vars(strat) == before

    def test_chunking_does_not_change_the_result(self):
        frames, picked = _market(-30.0, -30.0, n_days=10)
        whole = _block(_Short(picked), frames, chunk_size=100)
        chunked = _block(_Short(picked), frames, chunk_size=3)
        assert whole == chunked

    def test_cost_is_deducted_from_net(self):
        frames, picked = _market(-30.0, -30.0, n_days=10)
        out = _block(_Short(picked), frames)
        assert out["null_gross_bps"] - out["null_net_bps"] > 4.0


class _PanelShort(_Short):
    """Ranks the universe in prepare_panel; prepare_for and null_entry never read the rank."""

    def prepare_panel(self, frames, params, calendar):
        raise AssertionError("the replays must never run prepare_panel")


class _NeedsRank(_Short):
    def prepare(self, df, p):
        d = super().prepare(df, p)
        d["x"] = d["rank"]
        return d


class TestPanelStrategies:
    def test_panel_strategy_block_matches_non_panel_equivalent(self):
        frames, picked = _market(-60.0, -30.0, n_days=10)
        plain = _block(_Short(picked), frames)
        panel = _block(_PanelShort(picked), frames)
        assert panel is not None
        assert panel == plain

    def test_missing_panel_column_fails_loudly(self):
        frames, picked = _market(-30.0, -30.0, n_days=3)
        trades = _strategy_trades(_Short(picked), frames, _run(), make_model())
        with pytest.raises(xs.SelectionNullContractError, match="null_entry hook contract"):
            _block(_NeedsRank(picked), frames, trades=trades)


class TestReplayReproducesTheTrade:
    """A replay on the symbol the strategy picked must be the strategy's own trade."""

    #: Lineage ids are set only when a ledger recorder is attached, so they are not compared.
    SKIP = {"signal_id", "order_id", "entry_fill_id", "exit_fill_id", "position_id"}

    def _same(self, replay, trade):
        from dataclasses import fields

        for f in fields(trade):
            if f.name in self.SKIP:
                continue
            a, b = getattr(replay, f.name), getattr(trade, f.name)
            if isinstance(a, float) and isinstance(b, float) and np.isnan(a) and np.isnan(b):
                continue
            assert a == b, f"{f.name}: replay {a!r} != trade {b!r}"

    @pytest.mark.parametrize("strategy_class", [_Short, _AtrShort])
    def test_every_field_matches_on_the_picked_symbol(self, strategy_class):
        days = _sessions(6)
        # day 1 time-exits, day 2 spikes through the stop, day 3 drifts against the short
        frame = _frame(
            days,
            lambda d: {days[3]: 25.0}.get(d, -20.0),
            spikes={days[2]: 30},
            wick=0.001,
        )
        strat = strategy_class(["A"])
        run, model = _run(), make_model()
        trades = _strategy_trades(strat, {"A": frame}, run, model)
        assert {t.reason for t in trades} >= {"TIME_EXIT"}
        assert any(t.reason in ("SL", "SL_GAP") for t in trades)
        prepared = strat.prepare_for("A", frame.copy(), None)
        for trade in trades:
            key = xs.trade_key(trade)
            wanted = {key[0]: [(0, key[1], key[2])]}
            filled, skipped, attempts = xs.replay_symbol(strat, "A", prepared, wanted, None, run)
            assert attempts == 1 and not skipped
            # the strategy's trades are on the statutory basis, so the replay is costed the same
            (replay,) = costs.recost([filled[0][1]], model)
            self._same(replay, trade)

    def test_recosted_replay_equals_the_recosted_trade(self):
        days = _sessions(4)
        frame = _frame(days, lambda d: -20.0, spikes={days[1]: 30})
        strat = _Short(["A"])
        run, model = _run(), make_model()
        trades = _strategy_trades(strat, {"A": frame}, run, model)
        prepared = strat.prepare_for("A", frame.copy(), None)
        for trade in trades:
            key = xs.trade_key(trade)
            filled, _, _ = xs.replay_symbol(
                strat, "A", prepared, {key[0]: [(0, key[1], key[2])]}, None, run
            )
            (replay,) = costs.recost([filled[0][1]], model)
            assert replay.gross_bps == pytest.approx(trade.gross_bps)
            assert replay.net_bps == pytest.approx(trade.net_bps)

    def test_single_pick_market_has_zero_excess_on_its_own_pool(self):
        # a one-symbol pool is the strategy's own trade, so excess must be exactly zero
        days = _sessions(8)
        frame = _frame(days, lambda d: -20.0, spikes={days[2]: 30})
        out = _block(_Short(["A"]), {"A": frame})
        assert out["excess_net_bps"] == pytest.approx(0.0, abs=1e-9)
        assert out["excess_gross_bps"] == pytest.approx(0.0, abs=1e-9)
