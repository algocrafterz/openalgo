"""A ranked weekly portfolio through the shared swing engine, checked against an independent oracle.

The strategy is test-only. The oracle recomputes the weekly held sets from the raw closes with plain
pandas and compares them with the positions the engine actually held.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import costs
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.ledgers import LedgerRecorder
from signal_engine.backtest.session import derive_calendar
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.tests.ranked_fixtures import RankedTop, RankParams, ranked_frames

P = RankParams(n=3, closeness=0.9, window=20)


def run_book(frames=None, params=P, recorder=None):
    frames = frames or ranked_frames()
    calendar = derive_calendar(frames)
    strategy = RankedTop()
    prepared = strategy.prepare_panel(frames, params, calendar)
    run = SwingRunConfig(cost_bps=24.0, **strategy.run_overrides)
    bt = Backtest(strategy, prepared, run, engine=simulate_swing, release_raw=False)
    trades = bt.trades(params, "all", recorder=recorder)
    return frames, calendar, trades


ties_at_cut: dict = {}


def oracle_held_sets(frames, calendar, params):
    """Per rebalance date, the set that should be held, from the raw closes."""
    sessions = list(calendar.trading_days)
    close = pd.DataFrame(
        {
            s: pd.Series(f["Close"].to_numpy(float), index=pd.DatetimeIndex(f.index.date))
            for s, f in frames.items()
        }
    ).reindex(pd.DatetimeIndex(sessions))
    held = {}
    ties_at_cut.clear()
    weeks = defaultdict(list)
    for d in sessions:
        weeks[d.isocalendar()[:2]].append(d)
    for week_sessions in weeks.values():
        d = max(week_sessions)
        if d == sessions[-1]:
            continue
        k = sessions.index(d)
        window_close = close.iloc[max(0, k - params.window + 1) : k + 1]
        high = window_close.max()
        listed = close.iloc[: k + 1].notna().sum()
        ok = listed >= params.window
        ratio = (close.iloc[k] / high).where(ok)
        qualifying = ratio[ratio >= params.closeness].dropna()
        order = sorted(qualifying.items(), key=lambda kv: (-kv[1], kv[0]))
        held[d] = {s for s, _ in order[: params.n]}
        scores = [v for _, v in order]
        # a tie across the cut is broken by a hash in the engine, so membership is not comparable
        ties_at_cut[d] = len(scores) > params.n and scores[params.n - 1] == scores[params.n]
    return held


def holdings_after(trades, sessions, dates):
    """For each rebalance date, the symbols held from the next session's open."""
    out = {}
    for d in dates:
        k = sessions.index(d)
        if k + 1 >= len(sessions):
            continue
        nxt = pd.Timestamp(sessions[k + 1])
        out[d] = {
            t.symbol
            for t in trades
            if pd.Timestamp(t.entry_time).tz_localize(None) <= nxt
            and (t.exit_time is None or pd.Timestamp(t.exit_time).tz_localize(None) > nxt)
        }
    return out


@pytest.fixture(scope="module")
def book():
    return run_book()


class TestAgainstTheOracle:
    def test_the_book_holds_exactly_the_reranked_set_after_every_rebalance(self, book):
        frames, calendar, trades = book
        sessions = list(calendar.trading_days)
        expected = oracle_held_sets(frames, calendar, P)
        got = holdings_after(trades, sessions, sorted(expected))
        compared = 0
        for d, want in expected.items():
            if d not in got:
                continue
            assert len(got[d]) == len(want), d
            if not ties_at_cut[d]:
                assert got[d] == want, d
            compared += 1
        assert compared >= 8
        assert any(not v for v in ties_at_cut.values())

    def test_names_that_drop_out_are_sold_at_the_next_open(self, book):
        _, calendar, trades = book
        sold = [t for t in trades if t.reason == "REBALANCE_EXIT"]
        assert sold
        assert all(t.exit_at_open for t in sold)
        sessions = {pd.Timestamp(d) for d in calendar.trading_days}
        for t in sold:
            assert pd.Timestamp(t.exit_time).tz_localize(None).normalize() in sessions
            assert t.exit == pytest.approx(float(_open_of(t)), rel=1e-6)


def _open_of(trade):
    frames = ranked_frames()
    f = frames[trade.symbol]
    ts = pd.Timestamp(trade.exit_time)
    return f.loc[ts, "Open"]


class TestPortfolioConstraints:
    def test_never_more_positions_than_the_cap(self, book):
        _, _, trades = book
        events = sorted(
            [(pd.Timestamp(t.entry_time), 1) for t in trades]
            + [(pd.Timestamp(t.exit_time), -1) for t in trades if t.exit_time is not None],
            key=lambda e: (e[0], e[1]),  # a close before an open at the same instant
        )
        live = peak = 0
        for _, delta in events:
            live += delta
            peak = max(peak, live)
        assert peak <= P.n

    def test_fewer_qualifiers_means_cash_not_a_fill(self):
        frames = ranked_frames()
        strict = RankParams(n=3, closeness=0.9999, window=20)  # almost nothing is at its high
        _, calendar, trades = run_book(frames, strict)
        sessions = list(calendar.trading_days)
        expected = oracle_held_sets(frames, calendar, strict)
        sizes = {d: len(s) for d, s in expected.items()}
        assert min(sizes.values()) < strict.n  # the fixture does produce short weeks
        got = holdings_after(trades, sessions, sorted(expected))
        for d, size in sizes.items():
            if d in got:
                assert len(got[d]) == size

    def test_exits_and_entries_can_share_a_session(self, book):
        _, _, trades = book
        entries = Counter(pd.Timestamp(t.entry_time) for t in trades)
        exits = Counter(pd.Timestamp(t.exit_time) for t in trades if t.reason == "REBALANCE_EXIT")
        assert set(entries) & set(exits)

    def test_orders_per_session_stay_within_entries_plus_exits_of_the_cap(self, book):
        _, _, trades = book
        per_session = Counter()
        for t in trades:
            per_session[pd.Timestamp(t.entry_time)] += 1
            if t.reason == "REBALANCE_EXIT":
                per_session[pd.Timestamp(t.exit_time)] += 1
        assert max(per_session.values()) <= 2 * P.n

    def test_no_symbol_is_bought_twice_in_a_session(self, book):
        _, _, trades = book
        keys = [(t.symbol, pd.Timestamp(t.entry_time)) for t in trades]
        assert len(keys) == len(set(keys))

    def test_a_symbol_is_never_held_twice_at_once(self, book):
        _, _, trades = book
        by_symbol = defaultdict(list)
        for t in trades:
            by_symbol[t.symbol].append((pd.Timestamp(t.entry_time), pd.Timestamp(t.exit_time)))
        for spans in by_symbol.values():
            spans.sort()
            for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
                assert end <= start

    def test_every_position_is_a_fixed_notional(self, book):
        _, _, trades = book
        model = costs.CostModel(
            capital=2_000_000.0,
            risk_per_trade=0.01,
            leverage=1.0,
            product="CNC",
            exchange="NSE",
            slippage_bps_per_side=4.0,
            fixed_notional=100_000.0,
        )
        assert {model.notional(t) for t in trades} == {100_000.0}

    def test_no_trade_has_a_stop_or_an_r(self, book):
        _, _, trades = book
        assert trades and all(not t.r_defined and np.isnan(t.sl) for t in trades)

    def test_the_ledgers_reconcile_with_exits_at_the_open(self):
        rec = LedgerRecorder()
        _, _, trades = run_book(recorder=rec)
        frames = rec.to_frames()
        assert rec.reconcile(frames, trades) == []
        assert frames["fills"]["exit_reason"].eq("REBALANCE_EXIT").any()
