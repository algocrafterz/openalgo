"""Guards on the parts of the backtest that decide whether a number can be trusted.

Every test here corresponds to a defect that was actually present and was actually
found producing a wrong answer - a silently-truncated download, a drawdown measured
over the wrong ordering, a t-statistic that counted one market move as two hundred
independent observations. A backtest cannot be audited by reading its equity curve,
so these pin the arithmetic instead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import data, metrics
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig, india_intraday_bps

IST = "Asia/Kolkata"


def bars(rows, day="2026-06-04", start="09:15"):
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq="5min", tz=IST)
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 1000.0
    d = pd.Series(df.index.date, index=df.index)
    df["day"] = d
    df["mins"] = df.index.hour * 60 + df.index.minute
    df["new_session"] = (d != d.shift(1)).to_numpy()
    df["from_open"] = df["mins"] - d.map(df.groupby(d)["mins"].min())
    return df


class FireOnce(Strategy):
    name = "fire-once"

    def __init__(self, at, sl, tp, direction=1):
        self.at, self.sl, self.tp, self.dir = at, sl, tp, direction
        self.fired = False

    def prepare(self, df, p):
        return df

    def reset_symbol(self, p):
        self.fired = False

    def entry(self, c, i, p, direction):
        if i != self.at or direction != self.dir or self.fired:
            return None
        return EntrySignal(direction=self.dir, sl=self.sl, tp=self.tp, tag="T")

    def on_entry(self, c, i, p, sig):
        self.fired = True


RUN = RunConfig(skip_open_minutes=0, entry_cutoff_min=15 * 60, time_exit_min=15 * 60,
                max_trades_per_day=9, min_sl_pct=0.0, cost_bps=0.0)


def run_one(df, strat, run=RUN):
    return simulate(Ctx(df, "TEST"), strat, None, run)


class TestIntervalAliases:
    """pandas reads "5m" as five MONTHS. Resampling with the CLI's own interval string
    therefore produced a handful of monthly bars and every downstream number silently
    described a different thing."""

    @pytest.mark.parametrize("alias,expected", [
        ("1m", "1min"), ("5m", "5min"), ("15m", "15min"), ("30m", "30min"),
        ("1h", "1h"), ("1d", "1D"),
    ])
    def test_minute_aliases_never_mean_months(self, alias, expected):
        assert data._freq(alias) == expected

    def test_resampling_a_session_with_the_alias_yields_intraday_bars(self):
        idx = pd.date_range("2026-06-04 09:15", periods=375, freq="1min", tz=IST)
        s = pd.Series(np.arange(375.0), index=idx)
        assert len(s.resample(data._freq("5m")).last()) == 75

    def test_an_unsupported_interval_is_refused_not_guessed(self):
        with pytest.raises(ValueError):
            data._freq("fortnightly")


class TestEntryGap:
    """A gap through the stop between the signal close and the fill.

    The live system takes this trade - `validator._check_price_ordering` compares the
    stop against the ALERT price, not the fill - so the backtest must take it too. What
    was missing was the ability to count them: they used to land in SL_GAP alongside
    ordinary gapped stops, which are a different and much less alarming thing.
    """

    def test_a_fill_already_through_the_stop_is_still_taken_and_tagged(self):
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (80, 85, 78, 82), (80, 85, 78, 82)])
        t = run_one(df, FireOnce(at=1, sl=90, tp=110))
        assert len(t) == 1
        assert t[0].reason == "SL_GAP_ENTRY"
        assert t[0].entry == pytest.approx(80.0) and t[0].exit == pytest.approx(80.0)

    def test_it_is_distinguishable_from_an_ordinary_gapped_stop(self):
        # stop breached by a LATER bar's open, not by the fill itself
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (100, 101, 99, 100), (85, 86, 84, 85)])
        t = run_one(df, FireOnce(at=1, sl=90, tp=110))
        assert len(t) == 1 and t[0].reason == "SL_GAP"

    def test_a_normal_gap_that_clears_neither_level_trades_as_usual(self):
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (102, 103, 101, 102), (102, 103, 101, 102)])
        t = run_one(df, FireOnce(at=1, sl=95, tp=110))
        assert len(t) == 1 and t[0].entry == pytest.approx(102.0)
        assert t[0].reason != "SL_GAP_ENTRY"


class TestDrawdownOrdering:
    """The harness collects trades symbol by symbol, so the raw list runs A's whole
    history and then B's. A drawdown over that order describes an untradeable curve."""

    def _trade(self, sym, day, r):
        from signal_engine.backtest.types import Trade
        ts = pd.Timestamp(day, tz=IST)
        return Trade(symbol=sym, day=ts.date(), direction=1, tag="", entry_time=ts,
                     entry=100.0, signal_price=100.0, sl=99.0, tp=102.0, risk=1.0,
                     exit_time=ts, exit=101.0, reason="TP", r_gross=r, r_net=r)

    def test_drawdown_uses_calendar_order_not_list_order(self):
        # In time the book alternates +10, -10, +10, -10 and never falls below its
        # start. Grouped by symbol the same trades read as A's two, then B's two, which
        # drops the curve to -10 and doubles the reported drawdown.
        trades = [self._trade("A", "2026-06-01", +10.0), self._trade("A", "2026-06-04", -10.0),
                  self._trade("B", "2026-06-02", -10.0), self._trade("B", "2026-06-03", +10.0)]

        def max_dd(seq):
            eq = np.cumsum(seq)
            return float(np.max(np.maximum.accumulate(eq) - eq))

        assert max_dd([10, -10, -10, 10]) == pytest.approx(20.0)  # old, symbol order
        assert max_dd([10, -10, 10, -10]) == pytest.approx(10.0)  # true, calendar order
        assert metrics.summary(trades)["max_dd_R"] == pytest.approx(10.0)

    def test_trades_frame_is_returned_in_calendar_order(self):
        trades = [self._trade("A", "2026-06-03", 1.0), self._trade("B", "2026-06-01", 1.0)]
        assert list(metrics.trades_frame(trades)["symbol"]) == ["B", "A"]


class TestClusteredT:
    """Forty names bought into the same afternoon rally is one bet, not forty."""

    def _trades(self, per_day, n_days, seed=0):
        from signal_engine.backtest.types import Trade
        rng = np.random.default_rng(seed)
        out = []
        for d in range(n_days):
            shock = rng.normal(0, 1.0)          # the day's common market move
            day = (pd.Timestamp("2026-01-01") + pd.Timedelta(days=d)).date()
            for k in range(per_day):
                r = shock + rng.normal(0, 0.2)  # mostly common, a little idiosyncratic
                ts = pd.Timestamp(day)
                out.append(Trade(symbol=f"S{k}", day=day, direction=1, tag="",
                                 entry_time=ts, entry=100.0, signal_price=100.0,
                                 sl=99.0, tp=102.0, risk=1.0, exit_time=ts, exit=101.0,
                                 reason="TP", r_gross=r, r_net=r))
        return out

    def test_clustering_shrinks_t_when_trades_share_a_day(self):
        s = metrics.summary(self._trades(per_day=40, n_days=30))
        assert abs(s["t"]) < abs(s["t_naive"])
        assert abs(s["t_naive"]) / max(abs(s["t"]), 1e-9) > 2.0

    def test_one_trade_per_day_leaves_t_essentially_unchanged(self):
        s = metrics.summary(self._trades(per_day=1, n_days=60))
        assert s["t"] == pytest.approx(s["t_naive"], rel=0.02)

    def test_summary_reports_one_row_per_session(self):
        s = metrics.summary(self._trades(per_day=5, n_days=20))
        assert s["n"] == 100 and s["days"] == 20


class TestMultipleTesting:
    def test_the_hurdle_rises_with_the_number_of_configurations_searched(self):
        assert metrics.hurdle_t(1) < metrics.hurdle_t(10) < metrics.hurdle_t(100)

    def test_a_single_trial_reproduces_the_usual_two(self):
        assert metrics.hurdle_t(1) == pytest.approx(1.97, abs=0.02)

    def test_ten_trials_need_materially_more_than_two(self):
        assert metrics.hurdle_t(10) > 2.75


class TestCostModel:
    """The backtest and the Portfolio Backtester must not hold different views of what
    a trade costs."""

    def test_intraday_is_cheaper_than_delivery(self):
        from portfolio.costs import india_delivery, india_intraday
        n = 1_00_000.0
        assert (india_intraday().charge(n, n, 2) < india_delivery().charge(n, n, 2))

    def test_statutory_charges_are_a_few_basis_points(self):
        bare = india_intraday_bps(1_00_000.0, slippage_bps_per_side=0.0)
        assert 5.0 < bare < 12.0

    def test_slippage_is_charged_on_both_legs(self):
        base = india_intraday_bps(1_00_000.0, slippage_bps_per_side=0.0)
        assert india_intraday_bps(1_00_000.0, slippage_bps_per_side=4.0) == pytest.approx(
            base + 8.0, abs=0.01)

    def test_the_shipped_default_matches_the_model(self):
        assert RunConfig().cost_bps == pytest.approx(
            india_intraday_bps(1_00_000.0), abs=0.5)


class TestDataQuality:
    def test_an_incomplete_session_is_detected(self):
        from signal_engine.backtest import dataquality
        idx = pd.date_range("2026-06-04 09:15", periods=60, freq="5min", tz=IST)
        df = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
                           "Volume": 1.0}, index=idx)
        r = dataquality.audit_frame(df, "5m", "X")
        assert r["median_bars"] == 60 and r["complete_pct"] == 0.0   # 60 of 75

    def test_an_impossible_bar_is_counted(self):
        from signal_engine.backtest import dataquality
        idx = pd.date_range("2026-06-04 09:15", periods=3, freq="5min", tz=IST)
        df = pd.DataFrame({"Open": [100.0, 100.0, 100.0], "High": [101.0, 99.0, 101.0],
                           "Low": [99.0, 98.0, 99.0], "Close": [100.0, 100.0, 100.0],
                           "Volume": 1.0}, index=idx)
        assert dataquality.audit_frame(df, "5m", "X")["impossible"] == 1

    def test_expected_bar_count_matches_the_nse_session(self):
        from signal_engine.backtest import dataquality
        assert dataquality.expected_bars("1m") == 375
        assert dataquality.expected_bars("5m") == 75


class TestSplitAdjustedDaily:
    """Historify's daily bars are RAW broker prices. A 1:2 split reads as -50% and
    corrupts any lookback (RSI, 252-day return) that spans it unless backed out."""

    def _daily(self, closes, opens=None):
        idx = pd.date_range("2024-01-01", periods=len(closes), freq="1D", tz=IST)
        opens = opens or closes
        return pd.DataFrame({
            "Open": opens, "High": [max(o, c) + 1 for o, c in zip(opens, closes, strict=True)],
            "Low": [min(o, c) - 1 for o, c in zip(opens, closes, strict=True)],
            "Close": closes, "Volume": 1000.0,
        }, index=idx)

    def test_a_clean_series_is_untouched(self):
        from signal_engine.backtest.data import _split_adjust_daily
        df = self._daily([100, 101, 99, 102, 103])
        out = _split_adjust_daily(df)
        assert list(out["Close"]) == pytest.approx(list(df["Close"]))

    def test_a_1_for_2_split_is_backed_out_of_every_earlier_bar(self):
        from signal_engine.backtest.data import _split_adjust_daily
        # day 3 opens at exactly half of day 2's close: a clean 1:2 split, no real move
        closes = [1000, 1010, 1004, 500, 505, 510]
        opens = [1000, 1005, 1010, 502, 500, 505]
        df = self._daily(closes, opens)
        out = _split_adjust_daily(df)
        # everything before the split is halved; the split bar and after are untouched
        assert out["Close"].iloc[0] == pytest.approx(500.0)
        assert out["Close"].iloc[1] == pytest.approx(505.0)
        assert out["Close"].iloc[2] == pytest.approx(502.0)
        assert out["Close"].iloc[3] == pytest.approx(500.0)   # split bar: unchanged
        assert out["Close"].iloc[5] == pytest.approx(510.0)

    def test_two_splits_compound_on_the_earliest_bars(self):
        from signal_engine.backtest.data import _split_adjust_daily
        # a 1:5 split, a normal day, then ANOTHER 1:5 split - the earliest bar carries
        # both factors (/25), the middle bar carries only the second (/5), and the
        # split bars themselves plus everything after are untouched.
        closes = [1000, 202, 205, 42, 43, 44]
        opens = [1000, 200, 202, 41, 42, 43]
        df = self._daily(closes, opens)
        out = _split_adjust_daily(df)
        assert out["Close"].iloc[0] == pytest.approx(40.0)      # 1000 / 5 / 5
        assert out["Close"].iloc[1] == pytest.approx(40.4)      # 202 / 5 (only 2nd split ahead)
        assert out["Close"].iloc[2] == pytest.approx(41.0)      # 205 / 5
        assert out["Close"].iloc[3] == pytest.approx(42.0)      # 2nd split's own bar: unchanged
        assert out["Close"].iloc[5] == pytest.approx(44.0)      # post both: untouched

    def test_high_low_are_adjusted_by_the_same_factor_as_close(self):
        from signal_engine.backtest.data import _split_adjust_daily
        closes = [1000, 500, 505]
        opens = [1000, 502, 500]
        df = self._daily(closes, opens)
        out = _split_adjust_daily(df)
        pre, post = df.iloc[0], out.iloc[0]
        factor = post["Close"] / pre["Close"]
        assert post["High"] == pytest.approx(pre["High"] * factor)
        assert post["Low"] == pytest.approx(pre["Low"] * factor)

    def test_volume_is_left_unadjusted(self):
        from signal_engine.backtest.data import _split_adjust_daily
        df = self._daily([1000, 500], opens=[1000, 502])
        df["Volume"] = [10_000.0, 20_000.0]
        out = _split_adjust_daily(df)
        assert list(out["Volume"]) == [10_000.0, 20_000.0]


class TestFromHistorifyDaily:
    """End-to-end against a real (temporary) duckdb file - this is exactly the shape
    of bug a pure-function unit test cannot catch: `.dt.tz_convert(...).normalize()`
    parses fine in isolation but `normalize()` after a `.dt` chain needs its OWN `.dt`,
    since each `.dt.method()` call returns a plain Series, not another accessor."""

    def _make_db(self, tmp_path, rows):
        import duckdb
        db_path = str(tmp_path / "test_historify.duckdb")
        con = duckdb.connect(db_path)
        con.execute("""
            CREATE TABLE market_data (
                symbol VARCHAR, exchange VARCHAR, interval VARCHAR, timestamp BIGINT,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, oi BIGINT
            )
        """)
        con.executemany(
            "INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        con.close()
        return db_path

    def test_loads_without_crashing_and_indexes_by_date(self, tmp_path):
        import pandas as pd

        from signal_engine.backtest.data import from_historify_daily

        base = int(pd.Timestamp("2024-01-01", tz="Asia/Kolkata").timestamp())
        day = 86400
        rows = [("RELIANCE", "NSE", "D", base + i * day,
                 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000, 0)
                for i in range(600)]
        db_path = self._make_db(tmp_path, rows)

        frames = from_historify_daily(symbols=["RELIANCE"], min_sessions=500,
                                      db_path=db_path)
        assert "RELIANCE" in frames
        df = frames["RELIANCE"]
        assert len(df) == 600
        assert isinstance(df.index, pd.DatetimeIndex)
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]

    def test_below_min_sessions_is_excluded(self, tmp_path):
        import pandas as pd

        from signal_engine.backtest.data import from_historify_daily

        base = int(pd.Timestamp("2024-01-01", tz="Asia/Kolkata").timestamp())
        day = 86400
        rows = [("THIN", "NSE", "D", base + i * day, 100.0, 101.0, 99.0, 100.5, 1000, 0)
                for i in range(10)]
        db_path = self._make_db(tmp_path, rows)

        with pytest.raises(RuntimeError):
            from_historify_daily(symbols=["THIN"], min_sessions=500, db_path=db_path)


class TestBySymbolEmptyTrades:
    """A strategy whose entry condition never fires over the window is a real,
    reportable outcome - `--full`'s per-symbol table must not crash on it."""

    def test_empty_trade_list_returns_correctly_shaped_frame(self):
        df = metrics.by_symbol([])
        assert list(df.columns) == ["symbol", "n", "total_R", "net_R"]
        assert len(df) == 0

    def test_sort_values_does_not_raise_on_the_empty_frame(self):
        df = metrics.by_symbol([])
        df.sort_values("total_R", ascending=False)   # must not raise KeyError
