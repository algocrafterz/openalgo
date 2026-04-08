"""Risk engine — position sizing and exposure limit enforcement."""

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

# Indian Standard Time — all daily counters use IST so the "day" resets at
# midnight IST (18:30 UTC), not UTC midnight (05:30 IST next morning).
_IST = timezone(timedelta(hours=5, minutes=30))

from loguru import logger

from signal_engine.models import Signal


class RiskEngine:
    """Manages position sizing and risk exposure limits.

    Supports two sizing modes:
    - fixed_fractional: Risk a fixed % of capital per trade, sized by SL distance
    - pct_of_capital: Allocate a fixed % of capital per trade position

    Capital is fetched from OpenAlgo funds API (live or sandbox).
    When use_day_start_capital is enabled, the first capital fetch of each
    trading day is cached and used for all subsequent trades — ensuring equal
    risk per trade regardless of how many positions are open.

    Optional RiskStore integration provides restart-safe counters: on init,
    today's counters are restored from the store; on every mutation, counters
    are persisted back.
    """

    def __init__(
        self,
        risk_per_trade: float,
        sizing_mode: str,
        pct_of_capital: float,
        daily_loss_limit: float,
        weekly_loss_limit: float,
        monthly_loss_limit: float,
        max_open_positions: int,
        max_trades_per_day: int,
        min_entry_price: float,
        max_entry_price: float,
        max_portfolio_heat: float,
        slippage_factor: float = 0.0,
        store=None,
        trade_mode: str = "live",
        default_product: str = "MIS",
        max_positions_per_symbol: int = 0,
        max_positions_per_sector: int = 0,
        sectors: Dict[str, List[str]] = None,
        use_day_start_capital: bool = False,
    ):
        self.risk_per_trade = risk_per_trade
        self.use_day_start_capital = use_day_start_capital
        self.sizing_mode = sizing_mode
        self.pct_of_capital = pct_of_capital
        self.daily_loss_limit = daily_loss_limit
        self.weekly_loss_limit = weekly_loss_limit
        self.monthly_loss_limit = monthly_loss_limit
        self.max_open_positions = max_open_positions
        self.max_trades_per_day = max_trades_per_day
        self.min_entry_price = min_entry_price
        self.max_entry_price = max_entry_price
        self.max_portfolio_heat = max_portfolio_heat
        self.slippage_factor = slippage_factor
        self._store = store
        self._trade_mode = trade_mode
        self._default_product = default_product
        self.max_positions_per_symbol = max_positions_per_symbol
        self.max_positions_per_sector = max_positions_per_sector

        # Build reverse lookup: symbol -> sector
        raw_sectors: Dict[str, List[str]] = sectors if sectors is not None else {}
        self._symbol_to_sector: Dict[str, str] = {}
        for sector_name, symbols in raw_sectors.items():
            for sym in symbols:
                self._symbol_to_sector[sym] = sector_name

        # Counters
        self.open_positions: int = 0
        self.trades_today: int = 0
        self.daily_realised_loss: float = 0.0
        self.weekly_realised_loss: float = 0.0
        self.monthly_realised_loss: float = 0.0
        self._last_known_capital: float = 0.0
        self._current_day: int = datetime.now(_IST).timetuple().tm_yday

        # Feature 1: Portfolio heat
        self.portfolio_heat: float = 0.0

        # Feature 2: Unrealised drawdown
        self.unrealised_loss: float = 0.0

        # Day-start capital: cached on first fetch of each trading day
        self._day_start_capital: float = 0.0

        # Correlation risk: per-symbol and per-sector position counts
        self._positions_by_symbol: Dict[str, int] = defaultdict(int)
        self._positions_by_sector: Dict[str, int] = defaultdict(int)

        # Restore counters from store if provided
        if self._store is not None:
            self._restore()

    def _restore(self) -> None:
        """Load today's counters from the persistent store."""
        today = datetime.now(_IST).date()
        row = self._store.load(self._trade_mode, today)
        self.trades_today = row["trades_today"]
        self.daily_realised_loss = row["daily_loss"]
        self.open_positions = row["open_positions"]
        self.portfolio_heat = row.get("portfolio_heat", 0.0)

        # Load weekly/monthly losses
        self.weekly_realised_loss = self._store.weekly_loss(self._trade_mode, today)
        self.monthly_realised_loss = self._store.monthly_loss(self._trade_mode, today)

    def log_startup_summary(self, capital: float) -> None:
        """Log risk state summary on startup for visibility after restarts."""
        self._last_known_capital = capital
        daily_limit = self.daily_loss_limit * capital
        weekly_limit = self.weekly_loss_limit * capital
        monthly_limit = self.monthly_loss_limit * capital

        logger.info("--- Risk State (restored from DB) ---")
        logger.info(
            f"Positions: {self.open_positions}/{self.max_open_positions} | "
            f"Trades today: {self.trades_today}/{self.max_trades_per_day}"
        )
        logger.info(
            f"Daily loss: {self.daily_realised_loss:,.2f} / {daily_limit:,.2f} "
            f"({abs(self.daily_realised_loss / daily_limit * 100) if daily_limit else 0:.0f}%)"
        )
        logger.info(
            f"Weekly loss: {self.weekly_realised_loss:,.2f} / {weekly_limit:,.2f} | "
            f"Monthly loss: {self.monthly_realised_loss:,.2f} / {monthly_limit:,.2f}"
        )
        logger.info(
            f"Price filter: {self.min_entry_price}-{self.max_entry_price} | "
            f"Risk/trade: {self.risk_per_trade:.1%} | "
            f"Max heat: {self.max_portfolio_heat:.1%}"
        )
        logger.info("-------------------------------------")

    def get_sizing_capital(self, live_capital: float) -> float:
        """Return the capital to use for position sizing.

        When use_day_start_capital is enabled, caches the first capital value
        of each trading day and returns it for all subsequent calls. This
        ensures every trade gets equal risk regardless of how many positions
        are currently open (margin blocked doesn't shrink the sizing capital).

        When disabled, returns live_capital as-is (original behavior).
        """
        if not self.use_day_start_capital:
            return live_capital

        if self._day_start_capital <= 0:
            self._day_start_capital = live_capital
            logger.info(
                f"Day-start capital cached: {live_capital:,.2f} INR "
                f"(risk={self.risk_per_trade:.1%}={live_capital * self.risk_per_trade:,.0f}/trade)"
            )
        return self._day_start_capital

    def _persist(self) -> None:
        """Save current counters to the persistent store."""
        if self._store is None:
            return
        today = datetime.now(_IST).date()
        self._store.save(
            self._trade_mode,
            today,
            trades_today=self.trades_today,
            daily_loss=self.daily_realised_loss,
            open_positions=self.open_positions,
            portfolio_heat=self.portfolio_heat,
        )

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(_IST).timetuple().tm_yday
        if today != self._current_day:
            logger.info("New trading day detected, resetting daily counters")
            self._current_day = today
            self.trades_today = 0
            self.daily_realised_loss = 0.0
            self.open_positions = 0
            self.portfolio_heat = 0.0
            self.unrealised_loss = 0.0
            self._day_start_capital = 0.0
            self._positions_by_symbol.clear()
            self._positions_by_sector.clear()
            self._persist()

    def add_heat(self, qty: int, risk_per_share: float) -> None:
        """Accumulate open risk into portfolio heat when opening a position."""
        self.portfolio_heat += qty * risk_per_share

    def remove_heat(self, qty: int, risk_per_share: float) -> None:
        """Reduce portfolio heat when closing a position. Never goes below zero."""
        self.portfolio_heat = max(0.0, self.portfolio_heat - qty * risk_per_share)

    def update_unrealised(self, loss: float) -> None:
        """Update mark-to-market unrealised loss (replace, not accumulate)."""
        self.unrealised_loss = loss

    def calculate_quantity(self, signal: Signal, capital: float) -> int:
        """Calculate position size based on the configured sizing mode.

        Args:
            signal: The parsed signal with entry, sl, target.
            capital: Available capital from OpenAlgo funds API.

        Returns 0 if the trade should be skipped (price filter, unaffordable).
        Raises ValueError for unknown sizing mode.
        """
        self._last_known_capital = capital

        # Price filter — reject stocks outside configured price band
        if self.min_entry_price > 0 and signal.entry < self.min_entry_price:
            logger.warning(
                f"Skipping {signal.symbol}: entry {signal.entry} below "
                f"min price {self.min_entry_price}"
            )
            return 0
        if self.max_entry_price > 0 and signal.entry > self.max_entry_price:
            logger.warning(
                f"Skipping {signal.symbol}: entry {signal.entry} above "
                f"max price {self.max_entry_price}"
            )
            return 0

        if self.sizing_mode == "fixed_fractional":
            qty = self._fixed_fractional(signal, capital)
        elif self.sizing_mode == "pct_of_capital":
            qty = self._pct_of_capital(signal, capital)
        else:
            raise ValueError(
                f"Unknown sizing mode: '{self.sizing_mode}'. "
                f"Must be 'fixed_fractional' or 'pct_of_capital'."
            )

        # If the sizing formula produced qty <= 0, the stock is
        # too expensive for the allocated capital — do not force a trade.
        if qty <= 0:
            logger.warning(
                f"Skipping {signal.symbol}: stock price {signal.entry} exceeds "
                f"capital allocation ({self.sizing_mode})"
            )
            return 0

        return qty

    def _fixed_fractional(self, signal: Signal, capital: float) -> int:
        """Risk a fixed % of capital, sized by distance to SL."""
        risk_amount = capital * self.risk_per_trade
        risk_per_share = abs(signal.entry - signal.sl)
        if risk_per_share <= 0:
            return 0
        risk_per_share *= (1 + self.slippage_factor)
        return math.floor(risk_amount / risk_per_share)

    def _pct_of_capital(self, signal: Signal, capital: float) -> int:
        """Allocate a fixed % of capital to the position."""
        allocation = capital * self.pct_of_capital
        if signal.entry <= 0:
            return 0
        return math.floor(allocation / signal.entry)

    def check_exposure(self) -> bool:
        """Check if a new trade is allowed under current exposure limits.

        Uses last known capital for limit calculations. Loss counters are
        updated by the position tracker when trades close.
        Combines realised and unrealised loss for the daily limit check.
        """
        self._maybe_reset_daily()

        capital = self._last_known_capital
        if capital > 0:
            combined_daily = self.daily_realised_loss + self.unrealised_loss
            if combined_daily >= capital * self.daily_loss_limit:
                logger.warning("Daily loss limit breached")
                return False

            if self.weekly_realised_loss >= capital * self.weekly_loss_limit:
                logger.warning("Weekly loss limit breached")
                return False

            if self.monthly_realised_loss >= capital * self.monthly_loss_limit:
                logger.warning("Monthly loss limit breached")
                return False

            if self.portfolio_heat / capital >= self.max_portfolio_heat:
                logger.warning("Portfolio heat limit breached")
                return False

        if self.open_positions >= self.max_open_positions:
            logger.warning("Max open positions reached")
            return False

        if self.trades_today >= self.max_trades_per_day:
            logger.warning("Max trades per day reached")
            return False

        return True

    def exposure_block_reason(self) -> str:
        """Return a human-readable reason why check_exposure() returned False."""
        capital = self._last_known_capital
        if capital > 0:
            combined_daily = self.daily_realised_loss + self.unrealised_loss
            if combined_daily >= capital * self.daily_loss_limit:
                return f"Daily loss limit hit ({combined_daily:,.0f} >= {capital * self.daily_loss_limit:,.0f})"
            if self.weekly_realised_loss >= capital * self.weekly_loss_limit:
                return f"Weekly loss limit hit"
            if self.monthly_realised_loss >= capital * self.monthly_loss_limit:
                return f"Monthly loss limit hit"
            if self.portfolio_heat / capital >= self.max_portfolio_heat:
                return f"Portfolio heat limit ({self.portfolio_heat:,.0f}/{capital * self.max_portfolio_heat:,.0f})"
        if self.open_positions >= self.max_open_positions:
            return f"Max positions ({self.open_positions}/{self.max_open_positions})"
        if self.trades_today >= self.max_trades_per_day:
            return f"Max trades/day ({self.trades_today}/{self.max_trades_per_day})"
        return "Unknown"

    def capacity_status(self) -> str:
        """Return a formatted capacity summary string."""
        capital = self._last_known_capital
        heat_pct = (self.portfolio_heat / capital * 100) if capital > 0 else 0
        heat_limit = self.max_portfolio_heat * 100
        return (
            f"{self.open_positions}/{self.max_open_positions} positions, "
            f"heat={heat_pct:.1f}%/{heat_limit:.1f}%"
        )

    def can_trade_symbol(self, symbol: str) -> bool:
        """Return True if opening another position in this symbol is allowed."""
        if self.max_positions_per_symbol == 0:
            return True
        return self._positions_by_symbol.get(symbol, 0) < self.max_positions_per_symbol

    def can_trade_sector(self, symbol: str) -> bool:
        """Return True if opening another position in this symbol's sector is allowed."""
        if self.max_positions_per_sector == 0:
            return True
        sector = self._symbol_to_sector.get(symbol)
        if sector is None:
            return True
        return self._positions_by_sector.get(sector, 0) < self.max_positions_per_sector

    def record_trade(self, symbol: str = "") -> None:
        """Record a new trade entry, incrementing counters."""
        self.trades_today += 1
        self.open_positions += 1
        if symbol:
            self._positions_by_symbol[symbol] += 1
            sector = self._symbol_to_sector.get(symbol)
            if sector:
                self._positions_by_sector[sector] += 1
        self._persist()

    def record_close(self, pnl: float, symbol: str = "") -> None:
        """Record a position close. Negative pnl = loss."""
        self.open_positions = max(0, self.open_positions - 1)
        if symbol:
            self._positions_by_symbol[symbol] = max(0, self._positions_by_symbol.get(symbol, 0) - 1)
            sector = self._symbol_to_sector.get(symbol)
            if sector:
                self._positions_by_sector[sector] = max(0, self._positions_by_sector.get(sector, 0) - 1)
        if pnl < 0:
            realized_loss = abs(pnl)
            self.daily_realised_loss += realized_loss
            self.weekly_realised_loss += realized_loss
            self.monthly_realised_loss += realized_loss
            logger.info(f"Position closed with loss: {realized_loss:,.2f}")
        else:
            logger.info(f"Position closed with profit: {pnl:,.2f}")
        self._persist()
