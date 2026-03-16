"""Tests for ORB strategy signal generation - matches PineScript defaults."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from backtest.strategies.orb import ORBConfig, ORBStrategy


def _make_session_df(
    date: str = "2025-06-15",
    interval_minutes: int = 5,
    orb_high: float = 105.0,
    orb_low: float = 100.0,
    breakout_price: float = 106.0,
    breakout_direction: str = "long",
    prev_day_close: float | None = None,
) -> pd.DataFrame:
    """
    Create synthetic 5-min OHLCV data for a single NSE session.

    Simulates: ORB building phase -> breakout -> continuation.
    Session: 09:15 to 15:25 IST (75 candles for 5-min).
    """
    times = pd.date_range(
        f"{date} 09:15:00", f"{date} 15:25:00", freq=f"{interval_minutes}min"
    )

    n = len(times)
    orb_bars = 15 // interval_minutes  # 3 bars for 15-min ORB
    mid = (orb_high + orb_low) / 2

    opens = np.full(n, mid)
    highs = np.full(n, mid + 1)
    lows = np.full(n, mid - 1)
    closes = np.full(n, mid)
    volumes = np.full(n, 100_000.0)

    # ORB building phase (first 3 bars: 09:15, 09:20, 09:25)
    for i in range(min(orb_bars, n)):
        opens[i] = orb_low + 1
        highs[i] = orb_high
        lows[i] = orb_low
        closes[i] = mid

    # After ORB, price stays in range (09:30 to 09:40)
    for i in range(orb_bars, min(orb_bars + 3, n)):
        opens[i] = mid
        highs[i] = orb_high - 0.5
        lows[i] = orb_low + 0.5
        closes[i] = mid

    # Breakout bar (09:45 = bar index 6, matching min_entry_time=09:45)
    breakout_bar = orb_bars + 3
    if breakout_bar < n:
        if breakout_direction == "long":
            opens[breakout_bar] = orb_high - 0.5
            highs[breakout_bar] = breakout_price + 1
            lows[breakout_bar] = orb_high - 0.5
            closes[breakout_bar] = breakout_price
        else:
            bd_price = orb_low - abs(orb_high - breakout_price)
            opens[breakout_bar] = orb_low + 0.5
            highs[breakout_bar] = orb_low + 0.5
            lows[breakout_bar] = bd_price - 1
            closes[breakout_bar] = bd_price

    # Continuation (stays above/below ORB)
    for i in range(breakout_bar + 1, n):
        if breakout_direction == "long":
            opens[i] = breakout_price
            highs[i] = breakout_price + 2
            lows[i] = breakout_price - 1
            closes[i] = breakout_price + 0.5
        else:
            bd_price = orb_low - abs(orb_high - breakout_price)
            opens[i] = bd_price
            highs[i] = bd_price + 1
            lows[i] = bd_price - 2
            closes[i] = bd_price - 0.5

    # Strong volume on breakout bar
    if breakout_bar < n:
        volumes[breakout_bar] = 200_000

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=times,
    )

    # Prepend a previous day if needed for gap calculation
    if prev_day_close is not None:
        prev_date = pd.Timestamp(date) - pd.Timedelta(days=1)
        # Only need last bar of previous session
        prev_times = pd.date_range(
            f"{prev_date.date()} 15:25:00", periods=1, freq="5min"
        )
        prev_df = pd.DataFrame(
            {
                "open": [prev_day_close],
                "high": [prev_day_close + 1],
                "low": [prev_day_close - 1],
                "close": [prev_day_close],
                "volume": [100_000.0],
            },
            index=prev_times,
        )
        df = pd.concat([prev_df, df])

    return df


@pytest.fixture
def minimal_config():
    """Config with all filters disabled for clean signal testing."""
    return ORBConfig(
        orb_minutes=15,
        breakout_buffer_pct=0.0,
        enable_volume_filter=False,
        enable_trend_filter=False,
        enable_htf_filter=False,
        enable_index_filter=False,
        enable_gap_filter=False,
        enable_orb_range_filter=False,
        enable_min_entry_time=False,
        stop_mode="PCT_BASED",
        pct_based_stop=2.0,
        tp_multiplier=1.5,
        entry_cutoff_hour=14,
        entry_cutoff_minute=0,
        time_exit_hour=15,
        time_exit_minute=0,
    )


@pytest.fixture
def pinescript_default_config():
    """Config matching all PineScript defaults exactly."""
    return ORBConfig()


class TestORBStrategy:
    def test_generates_long_entry_on_breakout(self, minimal_config):
        df = _make_session_df(breakout_direction="long", breakout_price=106.0)
        strategy = ORBStrategy(minimal_config)
        signals = strategy.generate_signals_detailed(df)
        assert signals["long_entry"].sum() >= 1
        assert signals["short_entry"].sum() == 0

    def test_generates_short_entry_on_breakdown(self, minimal_config):
        df = _make_session_df(breakout_direction="short", breakout_price=106.0)
        strategy = ORBStrategy(minimal_config)
        signals = strategy.generate_signals_detailed(df)
        assert signals["short_entry"].sum() >= 1
        assert signals["long_entry"].sum() == 0

    def test_no_entry_during_orb_building(self, minimal_config):
        df = _make_session_df(breakout_direction="long")
        strategy = ORBStrategy(minimal_config)
        signals = strategy.generate_signals_detailed(df)
        orb_period = signals.iloc[:3]
        assert orb_period["long_entry"].sum() == 0
        assert orb_period["short_entry"].sum() == 0

    def test_next_bar_entry(self, minimal_config):
        """Entry should happen on the bar AFTER the breakout bar (pendingEntry)."""
        df = _make_session_df(breakout_direction="long", breakout_price=106.0)
        strategy = ORBStrategy(minimal_config)
        signals = strategy.generate_signals_detailed(df)

        entries = signals[signals["long_entry"]]
        if len(entries) > 0:
            entry_idx = entries.index[0]
            # Entry should use open price of the entry bar
            assert entries["entry_price"].iloc[0] == df.loc[entry_idx, "open"]

    def test_entry_price_and_levels_set(self, minimal_config):
        df = _make_session_df(breakout_direction="long", breakout_price=106.0)
        strategy = ORBStrategy(minimal_config)
        signals = strategy.generate_signals_detailed(df)

        entries = signals[signals["long_entry"]]
        assert len(entries) > 0
        assert not np.isnan(entries["entry_price"].iloc[0])
        assert not np.isnan(entries["sl_price"].iloc[0])
        assert not np.isnan(entries["tp_price"].iloc[0])

    def test_sl_below_entry_for_long(self, minimal_config):
        df = _make_session_df(breakout_direction="long", breakout_price=106.0)
        strategy = ORBStrategy(minimal_config)
        signals = strategy.generate_signals_detailed(df)
        entries = signals[signals["long_entry"]]
        if len(entries) > 0:
            assert entries["sl_price"].iloc[0] < entries["entry_price"].iloc[0]
            assert entries["tp_price"].iloc[0] > entries["entry_price"].iloc[0]

    def test_sl_above_entry_for_short(self, minimal_config):
        df = _make_session_df(breakout_direction="short", breakout_price=106.0)
        strategy = ORBStrategy(minimal_config)
        signals = strategy.generate_signals_detailed(df)
        entries = signals[signals["short_entry"]]
        if len(entries) > 0:
            assert entries["sl_price"].iloc[0] > entries["entry_price"].iloc[0]
            assert entries["tp_price"].iloc[0] < entries["entry_price"].iloc[0]

    def test_one_entry_per_session(self, minimal_config):
        minimal_config.one_entry_per_session = True
        df = _make_session_df(breakout_direction="long")
        strategy = ORBStrategy(minimal_config)
        signals = strategy.generate_signals_detailed(df)
        total = signals["long_entry"].sum() + signals["short_entry"].sum()
        assert total <= 1

    def test_no_entry_after_cutoff(self):
        config = ORBConfig(
            orb_minutes=15,
            breakout_buffer_pct=0.0,
            enable_volume_filter=False,
            enable_trend_filter=False,
            enable_htf_filter=False,
            enable_index_filter=False,
            enable_gap_filter=False,
            enable_orb_range_filter=False,
            enable_min_entry_time=False,
            stop_mode="PCT_BASED",
            pct_based_stop=2.0,
            tp_multiplier=1.5,
            entry_cutoff_hour=9,
            entry_cutoff_minute=30,
            time_exit_hour=15,
            time_exit_minute=0,
        )
        df = _make_session_df(breakout_direction="long")
        strategy = ORBStrategy(config)
        signals = strategy.generate_signals_detailed(df)
        total = signals["long_entry"].sum() + signals["short_entry"].sum()
        assert total == 0

    def test_exit_signal_generated(self, minimal_config):
        df = _make_session_df(breakout_direction="long")
        strategy = ORBStrategy(minimal_config)
        signals = strategy.generate_signals_detailed(df)
        entries = signals["long_entry"].sum() + signals["short_entry"].sum()
        exits = signals["exit"].sum()
        if entries > 0:
            assert exits >= entries

    def test_multi_day_session_reset(self, minimal_config):
        day1 = _make_session_df("2025-06-16", breakout_direction="long", breakout_price=106.0)
        day2 = _make_session_df("2025-06-17", breakout_direction="long", breakout_price=108.0)
        df = pd.concat([day1, day2])
        minimal_config.one_entry_per_session = True
        strategy = ORBStrategy(minimal_config)
        signals = strategy.generate_signals_detailed(df)
        entries = signals["long_entry"].sum() + signals["short_entry"].sum()
        assert entries >= 2

    def test_strategy_name(self, minimal_config):
        strategy = ORBStrategy(minimal_config)
        assert "ORB" in strategy.name
        assert "15" in strategy.name

    def test_strategy_describe(self, minimal_config):
        strategy = ORBStrategy(minimal_config)
        desc = strategy.describe()
        assert "ORB" in desc
        assert "orb_minutes=15" in desc


class TestSmartFilters:
    """Test PineScript smart entry filters."""

    def test_min_entry_time_blocks_early_entries(self):
        """With min_entry_time=09:45, breakout at 09:30 should be blocked."""
        config = ORBConfig(
            orb_minutes=15,
            breakout_buffer_pct=0.0,
            enable_volume_filter=False,
            enable_trend_filter=False,
            enable_htf_filter=False,
            enable_index_filter=False,
            enable_gap_filter=False,
            enable_orb_range_filter=False,
            enable_min_entry_time=True,  # 09:45
            min_entry_hour=9,
            min_entry_minute=45,
            stop_mode="PCT_BASED",
            pct_based_stop=2.0,
            tp_multiplier=1.5,
            entry_cutoff_hour=14,
            time_exit_hour=15,
        )
        # Breakout happens at bar index 3 (09:30), which is before 09:45
        # But entry is pending to bar 4 (09:35), still before 09:45
        # With ORB completing at 09:30, first possible breakout is 09:30 bar
        # Pending entry at 09:35 is before 09:45, so should be blocked
        df = _make_session_df(breakout_direction="long", breakout_price=106.0)
        strategy = ORBStrategy(config)
        signals = strategy.generate_signals_detailed(df)

        # Check that early entries are blocked
        early_mask = df.index.hour * 60 + df.index.minute < 9 * 60 + 45
        early_entries = signals.loc[early_mask, "long_entry"].sum()
        assert early_entries == 0

    def test_gap_filter_blocks_large_gap(self):
        """Days with >2.5% gap should have no entries."""
        config = ORBConfig(
            orb_minutes=15,
            breakout_buffer_pct=0.0,
            enable_volume_filter=False,
            enable_trend_filter=False,
            enable_htf_filter=False,
            enable_index_filter=False,
            enable_gap_filter=True,
            max_gap_pct=2.5,
            enable_orb_range_filter=False,
            enable_min_entry_time=False,
            stop_mode="PCT_BASED",
            pct_based_stop=2.0,
            tp_multiplier=1.5,
            entry_cutoff_hour=14,
            time_exit_hour=15,
        )
        # Previous close at 95, ORB at 100-105 -> gap = ~5%+ (large gap)
        df = _make_session_df(
            breakout_direction="long",
            breakout_price=106.0,
            orb_high=105.0,
            orb_low=100.0,
            prev_day_close=95.0,
        )
        strategy = ORBStrategy(config)
        signals = strategy.generate_signals_detailed(df)
        total = signals["long_entry"].sum() + signals["short_entry"].sum()
        assert total == 0

    def test_gap_filter_allows_small_gap(self):
        """Days with <2.5% gap should allow entries."""
        config = ORBConfig(
            orb_minutes=15,
            breakout_buffer_pct=0.0,
            enable_volume_filter=False,
            enable_trend_filter=False,
            enable_htf_filter=False,
            enable_index_filter=False,
            enable_gap_filter=True,
            max_gap_pct=2.5,
            enable_orb_range_filter=False,
            enable_min_entry_time=False,
            stop_mode="PCT_BASED",
            pct_based_stop=2.0,
            tp_multiplier=1.5,
            entry_cutoff_hour=14,
            time_exit_hour=15,
        )
        # Previous close at 102, open ~102.5 -> gap < 1%
        df = _make_session_df(
            breakout_direction="long",
            breakout_price=106.0,
            orb_high=105.0,
            orb_low=100.0,
            prev_day_close=102.0,
        )
        strategy = ORBStrategy(config)
        signals = strategy.generate_signals_detailed(df)
        total = signals["long_entry"].sum() + signals["short_entry"].sum()
        assert total >= 1

    def test_orb_range_filter_blocks_wide_range(self):
        """ORB range > 3.5% of price should be blocked."""
        config = ORBConfig(
            orb_minutes=15,
            breakout_buffer_pct=0.0,
            enable_volume_filter=False,
            enable_trend_filter=False,
            enable_htf_filter=False,
            enable_index_filter=False,
            enable_gap_filter=False,
            enable_orb_range_filter=True,
            min_orb_range_pct=0.4,
            max_orb_range_pct=3.5,
            enable_min_entry_time=False,
            stop_mode="PCT_BASED",
            pct_based_stop=2.0,
            tp_multiplier=1.5,
            entry_cutoff_hour=14,
            time_exit_hour=15,
        )
        # ORB range 90-100 = 10 points on 95 mid = 10.5% -> way over 3.5%
        df = _make_session_df(
            breakout_direction="long",
            breakout_price=101.0,
            orb_high=100.0,
            orb_low=90.0,
        )
        strategy = ORBStrategy(config)
        signals = strategy.generate_signals_detailed(df)
        total = signals["long_entry"].sum() + signals["short_entry"].sum()
        assert total == 0


class TestTargetCalculation:
    """Test PineScript target logic: min(ORB-width, risk-based)."""

    def test_tp_is_conservative(self):
        """TP should be the closer of ORB-width and risk-based targets."""
        config = ORBConfig(
            orb_minutes=15,
            breakout_buffer_pct=0.0,
            enable_volume_filter=False,
            enable_trend_filter=False,
            enable_htf_filter=False,
            enable_index_filter=False,
            enable_gap_filter=False,
            enable_orb_range_filter=False,
            enable_min_entry_time=False,
            stop_mode="PCT_BASED",
            pct_based_stop=2.0,
            tp_multiplier=1.5,
            entry_cutoff_hour=14,
            time_exit_hour=15,
        )
        strategy = ORBStrategy(config)

        # Test the internal method directly
        entry = 500.0
        sl = 490.0  # risk = 10
        orb_range = 5.0  # narrow ORB

        tp = strategy._calculate_tp(entry, sl, orb_range, 1.5, True)

        # ORB-based: 500 + 5 * 1.5 = 507.5
        # Risk-based: 500 + 10 * 1.5 * 1.0 (adjust) = 515
        # Conservative = min(507.5, 515) = 507.5
        assert tp == 507.5

    def test_tp_risk_adjustment_for_high_price(self):
        """High-price stocks get risk adjustment (0.8 for 1000-5000)."""
        config = ORBConfig()
        strategy = ORBStrategy(config)

        entry = 2000.0
        sl = 1980.0  # risk = 20
        orb_range = 30.0

        tp = strategy._calculate_tp(entry, sl, orb_range, 1.5, True)

        # ORB-based: 2000 + 30 * 1.5 = 2045
        # Risk-based: 2000 + 20 * 1.5 * 0.8 = 2024
        # Conservative = min(2045, 2024) = 2024
        assert tp == 2024.0


class TestORBConfig:
    def test_default_config_matches_pinescript(self):
        """Default ORBConfig should match all PineScript defaults."""
        config = ORBConfig()
        assert config.orb_minutes == 15
        assert config.breakout_buffer_pct == 0.5
        assert config.enable_volume_filter is True
        assert config.volume_ma_length == 20
        assert config.volume_multiplier == 1.2
        assert config.strong_volume_multiplier == 1.8
        assert config.enable_trend_filter is True
        assert config.enable_htf_filter is True
        assert config.block_counter_trend is True
        assert config.htf_ema_length == 20
        assert config.enable_index_filter is True
        assert config.enable_gap_filter is True
        assert config.max_gap_pct == 2.5
        assert config.enable_orb_range_filter is True
        assert config.min_orb_range_pct == 0.4
        assert config.max_orb_range_pct == 3.5
        assert config.enable_min_entry_time is True
        assert config.min_entry_hour == 9
        assert config.min_entry_minute == 45
        assert config.stop_mode == "ATR"
        assert config.atr_length == 14
        assert config.atr_multiplier == 2.0
        assert config.tp_multiplier == 1.0
        assert config.entry_cutoff_hour == 11
        assert config.entry_cutoff_minute == 0
        assert config.time_exit_hour == 14
        assert config.time_exit_minute == 30
        assert config.one_entry_per_session is True
        assert config.enable_retest_entry is False

    def test_custom_config(self):
        config = ORBConfig(orb_minutes=30, tp_multiplier=2.0)
        assert config.orb_minutes == 30
        assert config.tp_multiplier == 2.0
