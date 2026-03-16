"""
Opening Range Breakout (ORB) strategy - exact Python translation.

Translated from signal_engine/pinescripts/intraday/orb/orb.pine (PineScript v6).
All default PineScript input values are preserved exactly.

Implemented features matching PineScript defaults:
- ORB15 range (09:15-09:30 IST)
- Breakout detection with 0.5% buffer on confirmed bar close
- Volume filter (1.2x MA20, strong volume 1.8x bypass)
- VWAP intraday trend filter
- HTF (daily) trend filter with counter-trend blocking
- Index direction filter (requires NIFTY data)
- Multi-filter blocking: entry blocked when 2+ filters oppose
- Gap filter (skip days with >2.5% opening gap)
- ORB range filter (skip if range <0.4% or >3.5% of price)
- Min entry time (no trades before 09:45)
- Entry cutoff at 11:00, time exit at 14:30
- Next-bar entry (pendingEntry pattern from PineScript)
- Target = min(ORB-width-based, risk-based) with price risk adjustment
- ATR stop loss with price adjustment
- One entry per session
- EOD close at session end

Session: NSE 09:15-15:30 IST
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from backtest.strategies.base import Strategy

# IST session boundaries (hour, minute)
SESSION_START = (9, 15)
SESSION_END = (15, 30)


@dataclass
class ORBConfig:
    """Configuration matching all PineScript defaults exactly."""

    # ORB range period
    orb_minutes: int = 15  # 5, 15, 30, or 60

    # Breakout detection
    breakout_buffer_pct: float = 0.5  # % buffer above/below ORB for breakout

    # Volume filter
    enable_volume_filter: bool = True
    volume_ma_length: int = 20
    volume_multiplier: float = 1.2
    strong_volume_multiplier: float = 1.8  # immediate entry on strong volume

    # Trend filter (VWAP - intraday)
    enable_trend_filter: bool = True

    # HTF (daily) trend filter
    enable_htf_filter: bool = True
    block_counter_trend: bool = True
    htf_ema_length: int = 20

    # Index direction filter (NIFTY)
    enable_index_filter: bool = True
    index_filter_method: str = "vwap"  # "vwap" (PineScript default), "ema", "orb_direction"

    # Smart entry filters
    enable_gap_filter: bool = True
    max_gap_pct: float = 2.5

    enable_orb_range_filter: bool = True
    min_orb_range_pct: float = 0.4
    max_orb_range_pct: float = 3.5

    enable_min_entry_time: bool = True
    min_entry_hour: int = 9
    min_entry_minute: int = 45

    # Stop loss
    stop_mode: str = "ATR"  # ATR, ORB_PCT, PCT_BASED
    atr_length: int = 14
    atr_multiplier: float = 2.0
    pct_based_stop: float = 1.0
    orb_stop_fraction: float = 0.20

    # Targets - PineScript defaults: showTP1=true, showTP1.5=false, showTP2=false
    tp_multiplier: float = 1.0  # 1.0, 1.5, 2.0, 3.0

    # Entry cutoff
    entry_cutoff_hour: int = 11
    entry_cutoff_minute: int = 0

    # Time exit
    time_exit_hour: int = 14
    time_exit_minute: int = 30

    # One entry per session (sessionEntryTaken in PineScript)
    one_entry_per_session: bool = True

    # Retest entry mode (default off in PineScript)
    enable_retest_entry: bool = False


def _session_mask(idx: pd.DatetimeIndex) -> pd.Series:
    """Boolean mask: True during trading session."""
    hours = idx.hour
    minutes = idx.minute
    time_minutes = hours * 60 + minutes
    start = SESSION_START[0] * 60 + SESSION_START[1]
    end = SESSION_END[0] * 60 + SESSION_END[1]
    return pd.Series((time_minutes >= start) & (time_minutes < end), index=idx)


def _compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Compute intraday VWAP, resetting each session."""
    idx = df.index
    dates = pd.Series(idx.date, index=idx)

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol = typical_price * df["volume"]

    cum_tp_vol = tp_vol.groupby(dates).cumsum()
    cum_vol = df["volume"].groupby(dates).cumsum()

    vwap = cum_tp_vol / cum_vol.replace(0, np.nan)
    return vwap


def _compute_daily_ema(df: pd.DataFrame, length: int) -> pd.Series:
    """
    Compute daily EMA for HTF trend filter.

    Resamples intraday data to daily, computes EMA,
    then forward-fills back to intraday bars.
    """
    daily = df["close"].resample("D").last().dropna()
    daily_ema = daily.ewm(span=length, adjust=False).mean()
    # Forward-fill to intraday bars
    ema_daily = daily_ema.reindex(df.index, method="ffill")
    return ema_daily


def _compute_previous_close(df: pd.DataFrame) -> pd.Series:
    """Get previous session's closing price for gap calculation."""
    idx = df.index
    dates = pd.Series(idx.date, index=idx)
    daily_close = df["close"].groupby(dates).last()
    prev_daily_close = daily_close.shift(1)
    # Map back to intraday: each bar gets previous day's close
    return dates.map(lambda d: prev_daily_close.get(d, np.nan))


class ORBStrategy(Strategy):
    """
    Opening Range Breakout strategy for Indian markets (NSE).

    Exact translation of orb-strategy-india.pine defaults.
    """

    def __init__(
        self,
        config: ORBConfig | None = None,
        index_data: pd.DataFrame | None = None,
    ):
        """
        Args:
            config: Strategy configuration. Defaults match PineScript.
            index_data: NIFTY OHLCV DataFrame for index direction filter.
                        Must have same datetime index as main data.
                        If None and enable_index_filter=True, index filter is skipped.
        """
        self.config = config or ORBConfig()
        self.index_data = index_data

    @property
    def name(self) -> str:
        return f"ORB-{self.config.orb_minutes}min"

    @property
    def parameters(self) -> dict:
        c = self.config
        return {
            "orb_minutes": c.orb_minutes,
            "breakout_buffer_pct": c.breakout_buffer_pct,
            "tp_multiplier": c.tp_multiplier,
            "stop_mode": c.stop_mode,
            "atr_multiplier": c.atr_multiplier,
            "entry_cutoff": f"{c.entry_cutoff_hour:02d}:{c.entry_cutoff_minute:02d}",
            "time_exit": f"{c.time_exit_hour:02d}:{c.time_exit_minute:02d}",
            "gap_filter": c.enable_gap_filter,
            "orb_range_filter": c.enable_orb_range_filter,
            "min_entry_time": f"{c.min_entry_hour:02d}:{c.min_entry_minute:02d}" if c.enable_min_entry_time else "off",
            "htf_filter": c.enable_htf_filter,
            "index_filter": c.enable_index_filter,
        }

    def generate_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """
        Generate ORB entry/exit signals.

        Returns (entries, exits) boolean Series.
        """
        detailed = self.generate_signals_detailed(df)
        return detailed["long_entry"] | detailed["short_entry"], detailed["exit"]

    def generate_signals_detailed(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate detailed ORB signals with entry/exit prices.

        Returns DataFrame with columns:
            long_entry, short_entry, exit: boolean signals
            entry_price, sl_price, tp_price: float (NaN when no signal)
            orb_high, orb_low: ORB range levels
            direction: 1 (long), -1 (short), 0 (flat)
        """
        c = self.config
        n = len(df)
        idx = df.index

        # Pre-compute indicators
        atr = self._compute_atr(df, c.atr_length)
        volume_ma = (
            df["volume"].rolling(c.volume_ma_length).mean()
            if c.enable_volume_filter
            else None
        )
        vwap = _compute_vwap(df) if c.enable_trend_filter else None

        # HTF daily EMA for trend
        daily_ema = (
            _compute_daily_ema(df, c.htf_ema_length)
            if c.enable_htf_filter
            else None
        )

        # Previous day close for gap calculation
        prev_day_close = (
            _compute_previous_close(df) if c.enable_gap_filter else None
        )

        # Index data for direction filter (PineScript default: "Price vs VWAP")
        index_close = None
        index_vwap = None
        index_high_roll = None
        index_low_roll = None
        if c.enable_index_filter and self.index_data is not None:
            idx_df = self.index_data
            index_close = idx_df["close"].reindex(idx, method="ffill")
            if c.index_filter_method == "vwap":
                index_vwap = _compute_vwap(idx_df).reindex(idx, method="ffill")
            elif c.index_filter_method == "orb_direction":
                index_high_roll = idx_df["high"].rolling(3).max().reindex(idx, method="ffill")
                index_low_roll = idx_df["low"].rolling(3).min().reindex(idx, method="ffill")

        # Session info
        in_session = _session_mask(idx)
        hours = idx.hour
        minutes = idx.minute
        time_mins = hours * 60 + minutes
        dates = pd.Series(idx.date, index=idx)

        # Time constants
        orb_end_mins = SESSION_START[0] * 60 + SESSION_START[1] + c.orb_minutes
        cutoff_mins = c.entry_cutoff_hour * 60 + c.entry_cutoff_minute
        time_exit_mins = c.time_exit_hour * 60 + c.time_exit_minute
        min_entry_mins = c.min_entry_hour * 60 + c.min_entry_minute

        # Output arrays
        long_entry = np.zeros(n, dtype=bool)
        short_entry = np.zeros(n, dtype=bool)
        exit_signal = np.zeros(n, dtype=bool)
        entry_prices = np.full(n, np.nan)
        sl_prices = np.full(n, np.nan)
        tp_prices = np.full(n, np.nan)
        orb_highs = np.full(n, np.nan)
        orb_lows = np.full(n, np.nan)
        directions = np.zeros(n, dtype=int)

        # State tracking (per-session)
        orb_high = np.nan
        orb_low = np.nan
        orb_building = False
        orb_complete = False
        entry_taken = False
        in_trade = False
        trade_direction = 0
        trade_entry = np.nan
        trade_sl = np.nan
        trade_tp = np.nan
        prev_date = None
        session_open = np.nan  # first bar's open for gap calculation
        gap_filter_passed = True
        orb_range_filter_passed = True

        # Pending entry state (PineScript's pendingLongEntry/pendingShortEntry)
        pending_long = False
        pending_short = False
        pending_bar = -1

        close_arr = df["close"].values
        open_arr = df["open"].values
        high_arr = df["high"].values
        low_arr = df["low"].values
        vol_arr = df["volume"].values

        for i in range(n):
            cur_date = idx[i].date()
            cur_time = time_mins.iloc[i] if hasattr(time_mins, "iloc") else time_mins[i]
            cur_in_session = bool(in_session.iloc[i])

            # ===================== NEW SESSION RESET =====================
            if cur_date != prev_date:
                orb_high = np.nan
                orb_low = np.nan
                orb_building = False
                orb_complete = False
                entry_taken = False
                session_open = np.nan
                gap_filter_passed = True
                orb_range_filter_passed = True
                pending_long = False
                pending_short = False

                # Close any open trade from previous session (EOD close)
                if in_trade:
                    exit_signal[i] = True
                    in_trade = False
                    trade_direction = 0

                prev_date = cur_date

            if not cur_in_session:
                continue

            # Record session open for gap calculation
            if np.isnan(session_open):
                session_open = open_arr[i]

                # Gap filter: check opening gap vs previous close
                if c.enable_gap_filter and prev_day_close is not None:
                    pdc = prev_day_close.iloc[i]
                    if not np.isnan(pdc) and pdc > 0:
                        gap_pct = abs(session_open - pdc) / pdc * 100
                        gap_filter_passed = gap_pct <= c.max_gap_pct

            # ===================== PHASE 1: BUILD ORB RANGE =====================
            session_start_mins = SESSION_START[0] * 60 + SESSION_START[1]
            if cur_time >= session_start_mins and cur_time < orb_end_mins:
                if not orb_building:
                    orb_building = True
                    orb_high = high_arr[i]
                    orb_low = low_arr[i]
                else:
                    orb_high = max(orb_high, high_arr[i])
                    orb_low = min(orb_low, low_arr[i])
                continue

            # ===================== PHASE 2: ORB COMPLETE =====================
            if orb_building and not orb_complete and cur_time >= orb_end_mins:
                orb_building = False
                orb_complete = True

                if np.isnan(orb_high) or np.isnan(orb_low) or orb_high <= orb_low:
                    orb_complete = False
                    continue

                # ORB range filter (from PineScript Smart Entry)
                orb_range = orb_high - orb_low
                orb_mid = (orb_high + orb_low) / 2
                if c.enable_orb_range_filter and orb_mid > 0:
                    orb_range_pct = (orb_range / orb_mid) * 100
                    orb_range_filter_passed = (
                        orb_range_pct >= c.min_orb_range_pct
                        and orb_range_pct <= c.max_orb_range_pct
                    )

            if not orb_complete:
                continue

            orb_highs[i] = orb_high
            orb_lows[i] = orb_low
            orb_range = orb_high - orb_low

            # ===================== PHASE 3: PROCESS PENDING ENTRY =====================
            # PineScript pattern: breakout detected on bar N, entry on bar N+1's open
            if (pending_long or pending_short) and i > pending_bar:
                if not entry_taken or not c.one_entry_per_session:
                    # Check smart filters still pass
                    smart_ok = gap_filter_passed and orb_range_filter_passed
                    min_time_ok = (
                        not c.enable_min_entry_time
                        or cur_time >= min_entry_mins
                    )

                    if smart_ok and min_time_ok and cur_time < cutoff_mins:
                        entry = open_arr[i]  # next bar's open
                        cur_atr = atr.iloc[i] if not np.isnan(atr.iloc[i]) else orb_range * 0.5

                        if pending_long:
                            sl = self._calculate_sl(entry, orb_high, orb_low, orb_range, cur_atr, True)
                            tp = self._calculate_tp(entry, sl, orb_range, c.tp_multiplier, True)
                            risk = abs(entry - sl)
                            if risk > 0:
                                long_entry[i] = True
                                entry_prices[i] = entry
                                sl_prices[i] = sl
                                tp_prices[i] = tp
                                directions[i] = 1
                                in_trade = True
                                trade_direction = 1
                                trade_entry = entry
                                trade_sl = sl
                                trade_tp = tp
                                entry_taken = True

                        elif pending_short:
                            sl = self._calculate_sl(entry, orb_high, orb_low, orb_range, cur_atr, False)
                            tp = self._calculate_tp(entry, sl, orb_range, c.tp_multiplier, False)
                            risk = abs(entry - sl)
                            if risk > 0:
                                short_entry[i] = True
                                entry_prices[i] = entry
                                sl_prices[i] = sl
                                tp_prices[i] = tp
                                directions[i] = -1
                                in_trade = True
                                trade_direction = -1
                                trade_entry = entry
                                trade_sl = sl
                                trade_tp = tp
                                entry_taken = True

                pending_long = False
                pending_short = False

                if in_trade:
                    continue

            # ===================== PHASE 4: MANAGE OPEN TRADE =====================
            if in_trade:
                directions[i] = trade_direction

                # Time exit
                if cur_time >= time_exit_mins:
                    exit_signal[i] = True
                    in_trade = False
                    trade_direction = 0
                    continue

                # SL/TP check
                if trade_direction == 1:  # long
                    if low_arr[i] <= trade_sl:
                        exit_signal[i] = True
                        in_trade = False
                        trade_direction = 0
                        continue
                    if high_arr[i] >= trade_tp:
                        exit_signal[i] = True
                        in_trade = False
                        trade_direction = 0
                        continue
                elif trade_direction == -1:  # short
                    if high_arr[i] >= trade_sl:
                        exit_signal[i] = True
                        in_trade = False
                        trade_direction = 0
                        continue
                    if low_arr[i] <= trade_tp:
                        exit_signal[i] = True
                        in_trade = False
                        trade_direction = 0
                        continue
                continue

            # ===================== PHASE 5: DETECT BREAKOUT =====================
            if entry_taken and c.one_entry_per_session:
                continue
            if cur_time >= cutoff_mins:
                continue

            # Smart filters gate
            if not gap_filter_passed or not orb_range_filter_passed:
                continue

            # Min entry time filter
            if c.enable_min_entry_time and cur_time < min_entry_mins:
                continue

            # Need previous bar for crossover detection
            if i == 0:
                continue

            # Breakout buffer
            buffer_up = orb_high * (c.breakout_buffer_pct / 100)
            buffer_down = orb_low * (c.breakout_buffer_pct / 100)

            prev_close = close_arr[i - 1]
            cur_close = close_arr[i]

            # Volume filter
            volume_ok = True
            is_strong_volume = False
            if c.enable_volume_filter and volume_ma is not None:
                vm = volume_ma.iloc[i]
                if not np.isnan(vm) and vm > 0:
                    vol_ratio = vol_arr[i] / vm
                    volume_ok = vol_ratio >= c.volume_multiplier
                    is_strong_volume = vol_ratio >= c.strong_volume_multiplier

            if not volume_ok:
                continue

            # ===== Multi-filter blocking logic (PineScript "both against") =====
            # Intraday trend (VWAP)
            trend_bullish = True
            trend_bearish = True
            if c.enable_trend_filter and vwap is not None:
                v = vwap.iloc[i]
                if not np.isnan(v):
                    trend_bullish = cur_close > v
                    trend_bearish = cur_close < v

            # HTF daily trend
            htf_bullish = True
            htf_bearish = True
            if c.enable_htf_filter and c.block_counter_trend and daily_ema is not None:
                de = daily_ema.iloc[i]
                if not np.isnan(de):
                    htf_bullish = cur_close > de
                    htf_bearish = cur_close < de

            # Index direction (only active when index data is provided)
            idx_bullish = False
            idx_bearish = False
            has_index_data = index_close is not None
            if c.enable_index_filter and has_index_data:
                ic = index_close.iloc[i]
                if not np.isnan(ic):
                    if c.index_filter_method == "vwap" and index_vwap is not None:
                        iv = index_vwap.iloc[i]
                        if not np.isnan(iv):
                            idx_bullish = ic > iv
                            idx_bearish = ic < iv
                    elif c.index_filter_method == "orb_direction" and index_high_roll is not None:
                        ih = index_high_roll.iloc[i]
                        il = index_low_roll.iloc[i]
                        if not np.isnan(ih) and not np.isnan(il):
                            idx_bullish = ic > ih
                            idx_bearish = ic < il

            # Count filters against LONG (block when 2+ oppose)
            filters_against_long = 0
            if c.enable_trend_filter and not trend_bullish:
                filters_against_long += 1
            if c.enable_htf_filter and c.block_counter_trend and htf_bearish:
                filters_against_long += 1
            if c.enable_index_filter and has_index_data and idx_bearish:
                filters_against_long += 1
            trend_ok_up = filters_against_long < 2

            # Count filters against SHORT
            filters_against_short = 0
            if c.enable_trend_filter and not trend_bearish:
                filters_against_short += 1
            if c.enable_htf_filter and c.block_counter_trend and htf_bullish:
                filters_against_short += 1
            if c.enable_index_filter and has_index_data and idx_bullish:
                filters_against_short += 1
            trend_ok_down = filters_against_short < 2

            # ===== Breakout UP =====
            crossed_above = (
                cur_close > orb_high + buffer_up
                and prev_close <= orb_high + buffer_up
            )
            if crossed_above and trend_ok_up:
                # Mark pending entry for next bar
                pending_long = True
                pending_short = False
                pending_bar = i

            # ===== Breakout DOWN =====
            crossed_below = (
                cur_close < orb_low - buffer_down
                and prev_close >= orb_low - buffer_down
            )
            if crossed_below and trend_ok_down:
                pending_short = True
                pending_long = False
                pending_bar = i

        # End-of-data: close any open trade
        if in_trade and n > 0:
            exit_signal[n - 1] = True

        result = pd.DataFrame(
            {
                "long_entry": long_entry,
                "short_entry": short_entry,
                "exit": exit_signal,
                "entry_price": entry_prices,
                "sl_price": sl_prices,
                "tp_price": tp_prices,
                "orb_high": orb_highs,
                "orb_low": orb_lows,
                "direction": directions,
            },
            index=idx,
        )
        return result

    def _compute_atr(self, df: pd.DataFrame, length: int) -> pd.Series:
        """Compute Average True Range using Wilder's smoothing (RMA).

        PineScript's ta.atr() uses Wilder's smoothing (RMA), which is
        equivalent to EWM with span=length. This differs from a simple
        rolling mean (SMA) and produces smoother, more reactive values.
        """
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        return tr.ewm(span=length, adjust=False).mean()

    def _calculate_sl(
        self,
        entry: float,
        orb_high: float,
        orb_low: float,
        orb_range: float,
        atr: float,
        is_long: bool,
    ) -> float:
        """Calculate stop loss - exact PineScript calculateStopLoss logic."""
        c = self.config
        valid_atr = atr if not np.isnan(atr) and atr > 0 else orb_range * 0.5

        # ATR-based minimum stop (from PineScript)
        atr_pct = (valid_atr / entry) * 100 if entry > 0 else 1.0
        if atr_pct > 5:
            min_stop_mult = 1.5
        elif atr_pct > 3:
            min_stop_mult = 1.0
        elif atr_pct > 1.5:
            min_stop_mult = 0.7
        else:
            min_stop_mult = 0.5

        min_stop_distance = valid_atr * min_stop_mult
        absolute_min = entry * 0.003  # 0.3% for stocks
        min_stop_distance = max(min_stop_distance, absolute_min)

        if c.stop_mode == "ATR":
            if is_long:
                # PineScript: priceAdjust (price-level based)
                price_adjust = 1.0 if entry < 1000 else 0.7 if entry < 5000 else 0.5
                sl = entry - (valid_atr * c.atr_multiplier * price_adjust)
            else:
                # PineScript: volAdjust (volatility-based) for short side
                vol_adjust = 1.2 if atr_pct > 3 else 1.0 if atr_pct > 1.5 else 0.8
                sl = entry + (valid_atr * c.atr_multiplier * vol_adjust)

        elif c.stop_mode == "ORB_PCT":
            if is_long:
                sl = orb_low - (orb_range * c.orb_stop_fraction)
            else:
                sl = orb_high + (orb_range * c.orb_stop_fraction)

        elif c.stop_mode == "PCT_BASED":
            if is_long:
                sl = entry * (1 - c.pct_based_stop / 100)
            else:
                sl = entry * (1 + c.pct_based_stop / 100)

        else:
            if is_long:
                sl = entry - (valid_atr * c.atr_multiplier)
            else:
                sl = entry + (valid_atr * c.atr_multiplier)

        # Enforce minimum stop distance
        actual_distance = abs(entry - sl)
        if actual_distance < min_stop_distance:
            sl = entry - min_stop_distance if is_long else entry + min_stop_distance

        return sl

    def _calculate_tp(
        self,
        entry: float,
        sl: float,
        orb_range: float,
        tp_mult: float,
        is_long: bool,
    ) -> float:
        """
        Calculate target price - exact PineScript calculateTargets logic.

        PineScript uses min(ORB-width-based, risk-based) for conservative targets.
        Risk-based targets are adjusted for price level.
        """
        risk = abs(entry - sl)

        # Price-based risk adjustment (from PineScript)
        if entry < 1000:
            risk_adjust = 1.0
        elif entry < 5000:
            risk_adjust = 0.8
        else:
            risk_adjust = 0.6

        # ORB-width based target
        if is_long:
            tp_orb = entry + (orb_range * tp_mult)
            tp_risk = entry + (risk * tp_mult * risk_adjust)
            # Conservative: take the smaller (closer) target
            tp = min(tp_orb, tp_risk)
        else:
            tp_orb = entry - (orb_range * tp_mult)
            tp_risk = entry - (risk * tp_mult * risk_adjust)
            # Conservative: take the larger (closer to entry) target
            tp = max(tp_orb, tp_risk)

        return tp
