"""Tests for the momentum-rank Python-native ranking logic.

The pure ranking functions (momentum_score, rank_universe, diff_basket,
sessions_elapsed) live inline in momentum_rank_strategy.py rather than a
separate module, because OpenAlgo's /python Strategy Host only accepts a
single uploaded .py file - see that file's module docstring.

Run: uv run pytest strategies/examples/tests/ -v
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import momentum_rank_strategy as core

from signal_engine.backtest.portfolio import build_factor


class TestMomentumScore:
    def test_returns_none_when_not_enough_history(self):
        assert core.momentum_score([100.0] * 50, lookback=250, skip=21) is None

    def test_matches_hand_computed_value(self):
        # 300 days of closes, oldest -> newest, price doubles linearly.
        closes = [100.0 + i for i in range(300)]
        score = core.momentum_score(closes, lookback=250, skip=21)
        recent = closes[-22]  # 21 sessions ago
        past = closes[-251]   # 250 sessions ago
        assert score == pytest.approx(recent / past - 1.0)

    def test_zero_skip_uses_latest_close(self):
        closes = [float(i) for i in range(1, 300)]
        score = core.momentum_score(closes, lookback=250, skip=0)
        assert score == pytest.approx(closes[-1] / closes[-251] - 1.0)

    def test_zero_past_price_returns_none(self):
        closes = [0.0] + [float(i) for i in range(1, 300)]
        score = core.momentum_score(closes, lookback=299, skip=0)
        assert score is None

    def test_matches_portfolio_build_factor_mom_skip(self):
        """Numerical parity with the backtest's build_factor("mom_skip", ...) -
        these two implementations must never numerically diverge."""
        import numpy as np

        rng = np.random.default_rng(42)
        n_days, symbols = 320, ["A", "B", "C"]
        dates = pd.date_range("2024-01-01", periods=n_days, freq="B")
        panel = {}
        prices = {}
        for col in ("Open", "High", "Low", "Close"):
            df = pd.DataFrame(
                {s: 100 + np.cumsum(rng.normal(0, 1, n_days)) for s in symbols},
                index=dates,
            )
            panel[col] = df
            if col == "Close":
                prices = df

        lookback, skip = 250, 21
        backtest_factor = build_factor(panel, "mom_skip", lookback, skip)
        expected = backtest_factor.iloc[-1]

        for s in symbols:
            closes = prices[s].tolist()
            got = core.momentum_score(closes, lookback, skip)
            assert got == pytest.approx(expected[s], rel=1e-9)


class TestRankUniverse:
    def test_picks_highest_scores_above_min_price(self):
        scores = {"A": 0.5, "B": 0.9, "C": 0.1, "D": None}
        prices = {"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0}
        assert core.rank_universe(scores, prices, top_n=2, min_price=20.0) == ["B", "A"]

    def test_excludes_names_below_min_price(self):
        scores = {"A": 0.9, "B": 0.5}
        prices = {"A": 5.0, "B": 100.0}
        assert core.rank_universe(scores, prices, top_n=2, min_price=20.0) == ["B"]

    def test_missing_price_treated_as_ineligible(self):
        scores = {"A": 0.9}
        prices = {}
        assert core.rank_universe(scores, prices, top_n=2, min_price=20.0) == []


class TestDiffBasket:
    def test_no_change_when_baskets_match(self):
        sells, buys = core.diff_basket(["A", "B"], ["B", "A"])
        assert sells == [] and buys == []

    def test_sells_dropped_names_buys_new_names(self):
        sells, buys = core.diff_basket(held=["A", "B", "C"], target=["B", "D"])
        assert sells == ["A", "C"]
        assert buys == ["D"]

    def test_empty_held_buys_everything(self):
        sells, buys = core.diff_basket(held=[], target=["A", "B"])
        assert sells == []
        assert buys == ["A", "B"]


class TestSessionsElapsed:
    def test_first_run_counts_all_observed_days(self):
        days = [date(2026, 1, d) for d in range(1, 6)]
        assert core.sessions_elapsed(None, days) == 5

    def test_counts_only_days_after_last_rebalance(self):
        days = [date(2026, 1, d) for d in range(1, 11)]
        assert core.sessions_elapsed(date(2026, 1, 7), days) == 3

    def test_no_new_days_returns_zero(self):
        days = [date(2026, 1, 1), date(2026, 1, 2)]
        assert core.sessions_elapsed(date(2026, 1, 5), days) == 0


class TestBuildDigest:
    def test_contains_action_sections_and_counts(self):
        days = [date(2026, 1, d) for d in range(1, 11)]
        digest = core._build_digest(
            days, target=["A", "B", "C"], sells=["X"], buys=["C"],
            rebalance_number=3)
        assert "REBALANCE #3" in digest
        assert "SELL (1):\n  X" in digest
        assert "BUY (1):\n  C" in digest
        assert "HOLD, no action (2):\n  A\n  B" in digest
        assert "Full basket (3 names" in digest
        assert "DIGEST ONLY" in digest

    def test_no_score_shown_anywhere(self):
        # Buying all TOP_N is mandatory (see STRATEGY-ANALYSIS.md's
        # concentration backtest) - a per-symbol score would bias a trader
        # toward cherry-picking a subset, so it must never appear. The
        # equal-weight capital % (a sizing note, not a score) is fine.
        days = [date(2026, 1, 1), date(2026, 1, 2)]
        digest = core._build_digest(
            days, target=["A", "B", "C"], sells=["X"], buys=["C"],
            rebalance_number=1)
        assert "score" not in digest.lower()
        assert "A (" not in digest and "B (" not in digest and "C (" not in digest
        assert "X (" not in digest

    def test_each_symbol_on_its_own_line(self):
        days = [date(2026, 1, 1), date(2026, 1, 2)]
        digest = core._build_digest(
            days, target=["ZEBRA", "APPLE", "MANGO"], sells=[],
            buys=["ZEBRA", "APPLE", "MANGO"], rebalance_number=1)
        # Alphabetical, one per line - not a rank order.
        assert "BUY (3):\n  APPLE\n  MANGO\n  ZEBRA" in digest
        assert "  APPLE\n  MANGO\n  ZEBRA" in digest  # Full basket section too

    def test_next_rebalance_estimate_scales_with_rebal_days(self):
        # 10 calendar days spanning 9 sessions -> ~1.11 cal days/session;
        # REBAL_DAYS sessions ahead should land at a proportionally later date.
        days = [date(2026, 1, 1) + (date(2026, 1, 10) - date(2026, 1, 1)) * i // 9
                for i in range(10)]
        digest = core._build_digest(days, target=["A"], sells=[], buys=[],
                                     rebalance_number=1)
        assert "Next rebalance expected: around" in digest
        assert str(days[-1]) in digest  # "as of" date present

    def test_no_buys_or_sells_reads_as_none(self):
        days = [date(2026, 1, 1), date(2026, 1, 2)]
        digest = core._build_digest(days, target=["A", "B"], sells=[], buys=[],
                                     rebalance_number=1)
        assert "SELL (0): none" in digest
        assert "BUY (0): none" in digest

    def test_no_buys_or_sells_does_not_claim_action_required(self):
        # Basket unchanged from last rebalance - nothing for the trader to
        # place, so the digest must not headline "ACTION REQUIRED".
        days = [date(2026, 1, 1), date(2026, 1, 2)]
        digest = core._build_digest(days, target=["A", "B"], sells=[], buys=[],
                                     rebalance_number=1)
        assert "NO ACTION NEEDED" in digest
        assert "ACTION REQUIRED" not in digest

    def test_buys_or_sells_still_flags_action_required(self):
        days = [date(2026, 1, 1), date(2026, 1, 2)]
        digest = core._build_digest(days, target=["A", "B", "C"], sells=["X"],
                                     buys=["C"], rebalance_number=1)
        assert "ACTION REQUIRED" in digest
        assert "NO ACTION NEEDED" not in digest

    def test_fetch_errors_surfaced_as_data_note(self):
        days = [date(2026, 1, 1), date(2026, 1, 2)]
        digest = core._build_digest(days, target=["A"], sells=[], buys=["A"],
                                     rebalance_number=1, fetch_errors=5,
                                     universe_size=211)
        assert "Data note: 5/211" in digest

    def test_no_fetch_errors_omits_data_note(self):
        days = [date(2026, 1, 1), date(2026, 1, 2)]
        digest = core._build_digest(days, target=["A"], sells=[], buys=["A"],
                                     rebalance_number=1, fetch_errors=0,
                                     universe_size=211)
        assert "Data note" not in digest

    def test_today_truncated_surfaced_as_presettlement_note(self):
        days = [date(2026, 1, 1), date(2026, 1, 2)]
        digest = core._build_digest(days, target=["A"], sells=[], buys=["A"],
                                     rebalance_number=1, universe_size=211,
                                     today_truncated=40)
        assert "Pre-settlement note: 40/211" in digest

    def test_no_today_truncated_omits_presettlement_note(self):
        days = [date(2026, 1, 1), date(2026, 1, 2)]
        digest = core._build_digest(days, target=["A"], sells=[], buys=["A"],
                                     rebalance_number=1, universe_size=211,
                                     today_truncated=0)
        assert "Pre-settlement note" not in digest


class _FakeClient:
    """Stub for openalgo.api - returns canned DataFrames keyed by symbol."""

    def __init__(self, frames: dict):
        self._frames = frames

    def history(self, symbol, exchange, interval, start_date, end_date):
        return self._frames[symbol]


class _FlakyClient:
    """Stub whose .history() fails `fail_times` times, then returns `df`."""

    def __init__(self, df, fail_times=1, error=None):
        self._df = df
        self._fail_times = fail_times
        self._error = error or RuntimeError("transient boom")
        self.calls = 0

    def history(self, symbol, exchange, interval, start_date, end_date):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error
        return self._df


class _AlwaysErrorClient:
    """Stub whose .history() always returns a broker-style error dict."""

    def __init__(self, message="jData is not valid json object"):
        self._message = message
        self.calls = 0

    def history(self, symbol, exchange, interval, start_date, end_date):
        self.calls += 1
        return {"status": "error", "message": self._message}


class TestFetchSymbolHistory:
    def test_retries_once_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(core.time, "sleep", lambda s: None)
        idx = pd.date_range("2026-01-01", periods=3)
        good = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=idx)
        client = _FlakyClient(good, fail_times=1)

        df, error = core._fetch_symbol_history(client, "SYM", "2026-01-01",
                                                 "2026-01-03")

        assert error is None
        assert df is not None
        assert client.calls == 2

    def test_gives_up_after_max_attempts(self, monkeypatch):
        # Mirrors the live GVT&D/M&M failure: a deterministic broker-side
        # error that repeats identically on retry.
        monkeypatch.setattr(core.time, "sleep", lambda s: None)
        client = _AlwaysErrorClient()

        df, error = core._fetch_symbol_history(client, "SYM", "2026-01-01",
                                                 "2026-01-03")

        assert df is None
        assert error is not None
        assert "jData" in error
        assert client.calls == core._FETCH_ATTEMPTS

    def test_empty_response_is_not_retried_or_treated_as_error(self, monkeypatch):
        monkeypatch.setattr(core.time, "sleep", lambda s: None)
        empty = pd.DataFrame({"close": []})
        client = _FlakyClient(empty, fail_times=0)

        df, error = core._fetch_symbol_history(client, "SYM", "2026-01-01",
                                                 "2026-01-03")

        assert df is None
        assert error is None
        assert client.calls == 1  # no error occurred, so no retry needed


class TestUniverse:
    def test_niftyfpi_is_not_in_the_live_universe(self):
        # Confirmed live (2026-09-16 dry run): this instance's symbol master
        # has no NSE-equity mapping for it, so every fetch permanently fails.
        assert "NIFTYFPI" not in core.UNIVERSE


class TestFetchUniverseCloses:
    # Fixed "now" well after every fixture's last bar date, so the
    # today-bar-settlement guard never fires unless a test asks it to.
    _FAR_FUTURE_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=core.IST)

    def test_excludes_symbol_with_non_positive_close(self, monkeypatch):
        idx = pd.date_range("2026-01-01", periods=3)
        good = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=idx)
        bad = pd.DataFrame({"close": [10.0, 0.0, 12.0]}, index=idx)
        monkeypatch.setattr(core, "UNIVERSE", ["GOOD", "BAD"])
        client = _FakeClient({"GOOD": good, "BAD": bad})

        closes, days, last_price, errors, truncated = core.fetch_universe_closes(
            client, now_ist=self._FAR_FUTURE_NOW)

        assert "GOOD" in closes
        assert "BAD" not in closes
        assert errors == 1
        assert truncated == 0

    def test_all_clean_data_reports_zero_errors(self, monkeypatch):
        idx = pd.date_range("2026-01-01", periods=3)
        good = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=idx)
        monkeypatch.setattr(core, "UNIVERSE", ["GOOD"])
        client = _FakeClient({"GOOD": good})

        closes, days, last_price, errors, truncated = core.fetch_universe_closes(
            client, now_ist=self._FAR_FUTURE_NOW)

        assert "GOOD" in closes
        assert errors == 0
        assert truncated == 0

    def test_drops_still_forming_today_bar_before_settle_cutoff(self, monkeypatch):
        # Last bar is "today" (2026-06-01) and now_ist is 14:45 IST - before
        # the 15:30 NSE close + settle buffer - so it must be excluded.
        idx = pd.date_range("2026-05-28", periods=3)  # ends 2026-05-30
        idx = idx.append(pd.DatetimeIndex([pd.Timestamp("2026-06-01")]))
        df = pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0]}, index=idx)
        monkeypatch.setattr(core, "UNIVERSE", ["SYM"])
        client = _FakeClient({"SYM": df})
        pre_close = datetime(2026, 6, 1, 14, 45, tzinfo=core.IST)

        closes, days, last_price, errors, truncated = core.fetch_universe_closes(
            client, now_ist=pre_close)

        assert closes["SYM"] == [10.0, 11.0, 12.0]  # today's 13.0 bar dropped
        assert last_price["SYM"] == 12.0
        assert date(2026, 6, 1) not in days
        assert truncated == 1
        assert errors == 0  # truncation is not a data-quality error

    def test_keeps_today_bar_after_settle_cutoff(self, monkeypatch):
        idx = pd.date_range("2026-05-28", periods=3)
        idx = idx.append(pd.DatetimeIndex([pd.Timestamp("2026-06-01")]))
        df = pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0]}, index=idx)
        monkeypatch.setattr(core, "UNIVERSE", ["SYM"])
        client = _FakeClient({"SYM": df})
        post_close = datetime(2026, 6, 1, 15, 45, tzinfo=core.IST)

        closes, days, last_price, errors, truncated = core.fetch_universe_closes(
            client, now_ist=post_close)

        assert closes["SYM"] == [10.0, 11.0, 12.0, 13.0]
        assert truncated == 0

    def test_keeps_yesterdays_bar_when_it_is_not_todays_date(self, monkeypatch):
        # Last bar is 2026-05-30, "now" is 2026-06-01 - the bar is not dated
        # today at all, so it must never be treated as unsettled.
        idx = pd.date_range("2026-05-28", periods=3)  # ends 2026-05-30
        df = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=idx)
        monkeypatch.setattr(core, "UNIVERSE", ["SYM"])
        client = _FakeClient({"SYM": df})
        now = datetime(2026, 6, 1, 9, 0, tzinfo=core.IST)

        closes, days, last_price, errors, truncated = core.fetch_universe_closes(
            client, now_ist=now)

        assert closes["SYM"] == [10.0, 11.0, 12.0]
        assert truncated == 0


class TestTodayBarIsIncomplete:
    def test_false_for_a_past_date(self):
        now = datetime(2026, 6, 1, 10, 0, tzinfo=core.IST)
        assert core._today_bar_is_incomplete(date(2026, 5, 30), now) is False

    def test_true_before_close_plus_buffer(self):
        now = datetime(2026, 6, 1, 15, 0, tzinfo=core.IST)
        assert core._today_bar_is_incomplete(date(2026, 6, 1), now) is True

    def test_false_after_close_plus_buffer(self):
        now = datetime(2026, 6, 1, 15, 41, tzinfo=core.IST)
        assert core._today_bar_is_incomplete(date(2026, 6, 1), now) is False


class TestCleanBotToken:
    def test_strips_redundant_bot_prefix(self):
        assert core._clean_bot_token("bot123456:ABC-DEF") == "123456:ABC-DEF"

    def test_leaves_a_correct_token_unchanged(self):
        assert core._clean_bot_token("123456:ABC-DEF") == "123456:ABC-DEF"

    def test_strips_quotes_and_cr(self):
        assert core._clean_bot_token('"123456:ABC-DEF"\r') == "123456:ABC-DEF"

    def test_empty_token_passes_through(self):
        assert core._clean_bot_token("") == ""
