"""Risk engine — position sizing and exposure limit enforcement."""

import math
from datetime import date, datetime, timezone
from typing import Optional

from loguru import logger

from signal_engine.models import Signal


class RiskEngine:
    """Manages position sizing and risk exposure limits.

    Supports two sizing modes:
    - fixed_fractional: Risk a fixed % of capital per trade, sized by SL distance
    - pct_of_capital: Allocate a fixed % of capital per trade position

    Capital is always fetched from OpenAlgo funds API (live or sandbox).

    Optional RiskStore integration provides restart-safe counters: on init,
    today's counters are restored from the store; on every mutation, counters
    are persisted back.
    """

    def __init__(
        self,
        risk_per_trade: float,
        sizing_mode: str,
        pct_of_capital: float,
        max_position_size: float,
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
        margin_multiplier: dict = None,
        max_capital_utilization: float = 0.0,
        default_product: str = "MIS",
    ):
        self.risk_per_trade = risk_per_trade
        self.sizing_mode = sizing_mode
        self.pct_of_capital = pct_of_capital
        self.max_position_size = max_position_size
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
        self.margin_multiplier: dict = margin_multiplier if margin_multiplier is not None else {}
        self.max_capital_utilization = max_capital_utilization
        self._default_product = default_product

        # Counters
        self.open_positions: int = 0
        self.trades_today: int = 0
        self.daily_realised_loss: float = 0.0
        self.weekly_realised_loss: float = 0.0
        self.monthly_realised_loss: float = 0.0
        self._last_known_capital: float = 0.0
        self._current_day: int = datetime.now(timezone.utc).timetuple().tm_yday

        # Feature 1: Portfolio heat
        self.portfolio_heat: float = 0.0

        # Feature 2: Unrealised drawdown
        self.unrealised_loss: float = 0.0

        # Feature 3: Committed margin tracking
        self.committed_margin: float = 0.0

        # Restore counters from store if provided
        if self._store is not None:
            self._restore()

    def _restore(self) -> None:
        """Load today's counters from the persistent store."""
        today = datetime.now(timezone.utc).date()
        row = self._store.load(self._trade_mode, today)
        self.trades_today = row["trades_today"]
        self.daily_realised_loss = row["daily_loss"]
        self.open_positions = row["open_positions"]

    def _persist(self) -> None:
        """Save current counters to the persistent store."""
        if self._store is None:
            return
        today = datetime.now(timezone.utc).date()
        self._store.save(
            self._trade_mode,
            today,
            trades_today=self.trades_today,
            daily_loss=self.daily_realised_loss,
            open_positions=self.open_positions,
        )

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).timetuple().tm_yday
        if today != self._current_day:
            logger.info("New trading day detected, resetting daily counters")
            self._current_day = today
            self.trades_today = 0
            self.daily_realised_loss = 0.0
            self.open_positions = 0
            self.portfolio_heat = 0.0
            self.unrealised_loss = 0.0
            self.committed_margin = 0.0
            self._persist()

    def add_heat(self, qty: int, risk_per_share: float) -> None:
        """Accumulate open risk into portfolio heat when opening a position."""
        self.portfolio_heat += qty * risk_per_share

    def remove_heat(self, qty: int, risk_per_share: float) -> None:
        """Reduce portfolio heat when closing a position. Never goes below zero."""
        self.portfolio_heat = max(0.0, self.portfolio_heat - qty * risk_per_share)

    def add_margin(self, qty: int, entry_price: float, product: str) -> None:
        """Accumulate committed margin when opening a position."""
        margin_rate = self.margin_multiplier.get(product, 1.0)
        self.committed_margin += qty * entry_price * margin_rate

    def remove_margin(self, qty: int, entry_price: float, product: str) -> None:
        """Release committed margin when closing a position. Never goes below zero."""
        margin_rate = self.margin_multiplier.get(product, 1.0)
        self.committed_margin = max(0.0, self.committed_margin - qty * entry_price * margin_rate)

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

        # Determine product for margin calculations
        product = signal.product if signal.product else self._default_product
        margin_rate = self.margin_multiplier.get(product, 1.0)

        # Margin affordability cap (AFTER risk sizing, BEFORE max_position_size cap)
        if signal.entry > 0:
            max_affordable_qty = math.floor(capital / (signal.entry * margin_rate))
            if qty > max_affordable_qty:
                logger.warning(
                    f"Qty reduced from {qty} to {max_affordable_qty} for {signal.symbol} "
                    f"due to margin affordability ({product} rate={margin_rate})"
                )
                qty = max_affordable_qty

        # Remaining margin budget cap
        if self.max_capital_utilization > 0 and capital > 0:
            max_margin_budget = (capital * self.max_capital_utilization) - self.committed_margin
            if max_margin_budget <= 0:
                logger.warning(
                    f"No margin budget remaining for {signal.symbol}, "
                    f"committed={self.committed_margin:,.0f}, limit={capital * self.max_capital_utilization:,.0f}"
                )
                return 0
            if signal.entry > 0 and margin_rate > 0:
                max_qty_by_margin = math.floor(max_margin_budget / (signal.entry * margin_rate))
                if qty > max_qty_by_margin:
                    logger.warning(
                        f"Qty reduced from {qty} to {max_qty_by_margin} for {signal.symbol} "
                        f"due to remaining margin budget"
                    )
                    qty = max_qty_by_margin

        # Cap by max position size
        capped = False
        if self.max_position_size > 0 and signal.entry > 0:
            max_position_value = capital * self.max_position_size
            max_qty = math.floor(max_position_value / signal.entry)
            if qty > max_qty:
                capped = True
                qty = max_qty

        # If the sizing formula or the cap produced qty <= 0, the stock is
        # too expensive for the allocated capital — do not force a trade.
        if qty <= 0:
            reason = (
                "max position value" if capped
                else "capital allocation"
            )
            logger.warning(
                f"Skipping {signal.symbol}: stock price {signal.entry} exceeds "
                f"{reason} ({self.sizing_mode})"
            )
            return 0

        final_qty = qty

        # Log position sizing breakdown
        risk_per_share = abs(signal.entry - signal.sl)
        reward_per_share = abs(signal.tp - signal.entry)
        rr_ratio = reward_per_share / risk_per_share if risk_per_share > 0 else 0
        position_value = final_qty * signal.entry

        logger.info(
            f"Position sizing [{signal.symbol}]: "
            f"mode={self.sizing_mode}, capital={capital:,.0f}, "
            f"entry={signal.entry}, sl={signal.sl}, tp={signal.tp}, "
            f"risk/share={risk_per_share:.2f}, reward/share={reward_per_share:.2f}, "
            f"R:R=1:{rr_ratio:.1f}, "
            f"qty={final_qty}, value={position_value:,.0f}"
            f"{' (capped by max_position_size)' if capped else ''}"
        )

        return final_qty

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

            if self.max_capital_utilization > 0:
                if self.committed_margin / capital >= self.max_capital_utilization:
                    logger.warning("Max capital utilization reached")
                    return False

        if self.open_positions >= self.max_open_positions:
            logger.warning("Max open positions reached")
            return False

        if self.trades_today >= self.max_trades_per_day:
            logger.warning("Max trades per day reached")
            return False

        return True

    def record_trade(self) -> None:
        """Record a new trade entry, incrementing counters."""
        self.trades_today += 1
        self.open_positions += 1
        self._persist()

    def record_close(self, pnl: float) -> None:
        """Record a position close. Negative pnl = loss."""
        self.open_positions = max(0, self.open_positions - 1)
        if pnl < 0:
            realized_loss = abs(pnl)
            self.daily_realised_loss += realized_loss
            self.weekly_realised_loss += realized_loss
            self.monthly_realised_loss += realized_loss
            logger.info(f"Position closed with loss: {realized_loss:,.2f}")
        else:
            logger.info(f"Position closed with profit: {pnl:,.2f}")
        self._persist()
