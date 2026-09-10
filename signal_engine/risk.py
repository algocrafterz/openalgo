"""Risk engine — position sizing and exposure limit enforcement.

Capital, position-count, trade-count and loss counters are tracked PER STRATEGY
in ANALYZE mode (2026-09-10) — see _StrategyState / _state(). Paper money has no
real contention: three strategies each get their own full-size sizing pool and
slot count, so none starves another and every trade is judged on its own signal
quality, not on which strategy happened to trade first that day.

In LIVE mode (2026-09-10, same day) every strategy's counters POOL into one
shared bucket instead — see _key(). There is exactly one real broker account:
if BREAKOUT already has a position open, ORB's next signal must size and gate
against what is ACTUALLY left, not against a second imaginary copy of the full
account. Pooling applies uniformly to sizing capital, open_positions,
trades_today, and daily/weekly/monthly realised loss — a loss in any live
strategy counts against the same daily/weekly/monthly limit as every other, so
one combined loss ceiling protects the whole account, not one per strategy.

Symbol/sector concentration limits follow the SAME split (2026-09-11). In LIVE
they pool: two strategies in one real name is genuine correlated exposure no
matter which one triggered it. In ANALYZE they isolate, for the same reason
every other counter does — BREAKINGTRADE and BREAKINGTRADE-WATCHLIST are built
to hold the same name at the same time so paper P&L can compare the two entry
philosophies, and a shared max_positions_per_symbol=1 meant the watchlist (which
fires on the scan-hit poll, before the confirming close exists) always took the
slot and the confirmed signal was declined "symbol concentration limit". The
comparison the design exists to produce could not run.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

# Indian Standard Time — all daily counters use IST so the "day" resets at
# midnight IST (18:30 UTC), not UTC midnight (05:30 IST next morning).

from loguru import logger

from signal_engine.models import Signal
from signal_engine.timeutils import IST


@dataclass
class _StrategyState:
    """Per-strategy counters — one instance per strategy tag, per trading day."""

    open_positions: int = 0
    trades_today: int = 0
    #: GROSS realised loss today — losing trades only, wins never offset. This is what the
    #: DAILY limit measures, on purpose: "4% daily = four full stops" is a loss-streak gate,
    #: and a four-stop-out day is worth halting on whatever the winners did (config.yaml).
    daily_realised_loss: float = 0.0
    weekly_realised_loss: float = 0.0
    monthly_realised_loss: float = 0.0
    #: NET realised P&L (signed, wins included) for the day/week/month. The WEEKLY and
    #: MONTHLY limits measure these — see check_exposure(). Gross over those horizons is
    #: not a drawdown at all: at Rs 350/trade on Rs 35k the gross weekly limit was 8 losing
    #: trades and the monthly 15, both reachable in days on a net-profitable account.
    daily_net_pnl: float = 0.0
    weekly_net_pnl: float = 0.0
    monthly_net_pnl: float = 0.0
    unrealised_loss: float = 0.0
    day_start_capital: float = 0.0
    last_known_capital: float = 0.0


class RiskEngine:
    """Manages position sizing and risk exposure limits.

    Supports two sizing modes:
    - fixed_fractional: Risk a fixed % of capital per trade, sized by SL distance
    - pct_of_capital: Allocate a fixed % of capital per trade position

    Capital is fetched from OpenAlgo funds API (live or sandbox). ANALYZE mode
    isolates every strategy's counters (its own day-start capital, open
    positions, trades, and loss) — see the module docstring for why. LIVE mode
    POOLS every strategy into one shared set of counters instead, because a
    live account is one real pot of money, not one per strategy — see _key().
    max_open_positions/max_trades_per_day/loss-limit fractions are the same
    config values either way; only what they're measured against changes.

    Optional RiskStore integration provides restart-safe counters: on init,
    every counter bucket with a row for today is restored from the store; one
    seen for the first time today is created lazily on first touch.
    """

    # Default soft-blacklist multiplier when a strategy has soft symbols configured
    # but the per-strategy multiplier is missing. 0.5 = half size.
    DEFAULT_SOFT_MULTIPLIER: float = 0.5

    # Bucket for signals with no/blank strategy tag — should not happen in practice
    # (parser always fills it from the alert header) but keeps counters well-defined.
    _UNSPECIFIED = "UNSPECIFIED"

    # LIVE mode's shared counter bucket — every strategy's calls resolve to this same
    # key (see _key()), because live money is one real account, not one per strategy.
    # Distinct from any real strategy tag (those are things like "ORB"/"BREAKOUT") and
    # from RiskStore.LEGACY_STRATEGY ("_LEGACY", pre-2026-09-10 rows).
    _LIVE_POOLED_KEY = "PORTFOLIO"

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
        slippage_factor: float = 0.0,
        max_sl_pct_for_sizing: float = 0.0,
        store=None,
        trade_mode: str = "live",
        max_positions_per_symbol: int = 0,
        max_positions_per_sector: int = 0,
        sectors: Dict[str, List[str]] = None,
        use_day_start_capital: bool = False,
        soft_blacklist: Optional[Dict[str, frozenset]] = None,
        soft_blacklist_multipliers: Optional[Dict[str, float]] = None,
        strategy_profiles: Optional[Dict[str, dict]] = None,
    ):
        self.risk_per_trade = risk_per_trade
        self.use_day_start_capital = use_day_start_capital
        self.sizing_mode = sizing_mode
        self.pct_of_capital = pct_of_capital
        self.max_sl_pct_for_sizing = max_sl_pct_for_sizing
        self.daily_loss_limit = daily_loss_limit
        self.weekly_loss_limit = weekly_loss_limit
        self.monthly_loss_limit = monthly_loss_limit
        self.max_open_positions = max_open_positions
        self.max_trades_per_day = max_trades_per_day
        self.min_entry_price = min_entry_price
        self.max_entry_price = max_entry_price
        self.slippage_factor = slippage_factor
        self._store = store
        self._trade_mode = trade_mode
        self.max_positions_per_symbol = max_positions_per_symbol
        self.max_positions_per_sector = max_positions_per_sector

        # Soft blacklist — per-strategy qty scaling for regime-flipped stocks.
        # Copy into immutable form so callers can't mutate engine state via the input.
        raw_soft = soft_blacklist if soft_blacklist is not None else {}
        self._soft_blacklist: Dict[str, frozenset] = {
            k.upper(): frozenset(v) for k, v in raw_soft.items()
        }
        raw_mult = soft_blacklist_multipliers if soft_blacklist_multipliers is not None else {}
        self._soft_blacklist_multipliers: Dict[str, float] = {
            k.upper(): float(v) for k, v in raw_mult.items()
        }
        self._strategy_profiles: Dict[str, dict] = strategy_profiles or {}

        # Build reverse lookup: symbol -> sector
        raw_sectors: Dict[str, List[str]] = sectors if sectors is not None else {}
        self._symbol_to_sector: Dict[str, str] = {}
        for sector_name, symbols in raw_sectors.items():
            for sym in symbols:
                self._symbol_to_sector[sym] = sector_name

        self._current_day: int = datetime.now(IST).timetuple().tm_yday

        # Per-strategy counters — see _StrategyState. Populated lazily on first touch
        # (calculate_quantity, get_sizing_capital, ...) and preloaded at __init__/mode
        # switch for any strategy that already has a row for today (restart recovery).
        self._by_strategy: Dict[str, _StrategyState] = {}

        # Correlation risk: per-symbol and per-sector position counts, keyed
        # (counter_key, name) where counter_key is _key(strategy) — so they pool in LIVE
        # and isolate in ANALYZE, exactly like every other counter. See module docstring.
        self._positions_by_symbol: Dict[tuple, int] = defaultdict(int)
        self._positions_by_sector: Dict[tuple, int] = defaultdict(int)

        self._restore()

    @property
    def isolates_per_strategy(self) -> bool:
        """True when each strategy has its own counters (ANALYZE), False when every
        strategy pools into one shared bucket (LIVE and any other/unrecognised mode —
        real money defaults to the safer, shared behaviour). Startup reconciliation
        uses this to decide whether to correct one strategy's counter at a time or
        the one shared total."""
        return self._trade_mode == "analyze"

    def _key(self, strategy: str) -> str:
        if not self.isolates_per_strategy:
            return self._LIVE_POOLED_KEY
        strategy = (strategy or "").strip().upper()
        return strategy or self._UNSPECIFIED

    def _state(self, strategy: str) -> _StrategyState:
        key = self._key(strategy)
        state = self._by_strategy.get(key)
        if state is None:
            state = self._load_strategy_state(key)
            self._by_strategy[key] = state
        return state

    def _load_strategy_state(self, key: str) -> _StrategyState:
        if self._store is None:
            return _StrategyState()
        today = datetime.now(IST).date()
        row = self._store.load(key, self._trade_mode, today)
        return _StrategyState(
            open_positions=row["open_positions"],
            trades_today=row["trades_today"],
            daily_realised_loss=row["daily_loss"],
            daily_net_pnl=row["daily_net_pnl"],
            day_start_capital=row["day_start_capital"],
            weekly_realised_loss=self._store.weekly_loss(key, self._trade_mode, today),
            monthly_realised_loss=self._store.monthly_loss(key, self._trade_mode, today),
            weekly_net_pnl=self._store.weekly_net_pnl(key, self._trade_mode, today),
            monthly_net_pnl=self._store.monthly_net_pnl(key, self._trade_mode, today),
        )

    def _restore(self) -> None:
        """Load every strategy with a persisted row for today (restart recovery).

        A strategy the store has never seen today is not created here — it is
        created lazily, the first time a signal for it is processed.
        """
        self._by_strategy = {}
        if self._store is None:
            return
        today = datetime.now(IST).date()
        for stored_key in self._store.strategies_for(self._trade_mode, today):
            # Through _key(): in LIVE every stored per-strategy row resolves to the one
            # pooled PORTFOLIO bucket. Loading them under their own tags instead left rows
            # _state() could never read but total_open_positions()/total_last_known_capital()
            # still summed, and log_startup_summary() still printed as phantom strategies.
            key = self._key(stored_key)
            if key in self._by_strategy:
                continue
            self._by_strategy[key] = self._load_strategy_state(key)

    def log_startup_summary(self, capital: float) -> None:
        """Log risk state summary on startup for visibility after restarts.

        `capital` is the value OpenAlgo reports right now (broker funds, or the
        sandbox override) — used only to DISPLAY where each restored strategy's
        loss counters sit relative to its limits. Each strategy's own cached
        day-start sizing capital (if any) is shown separately.
        """
        logger.info("--- Risk State (restored from DB) ---")
        if not self._by_strategy:
            logger.info(f"No strategy has traded yet today ({self._trade_mode} mode).")
        open_cap = self.max_open_positions if self.max_open_positions > 0 else "unlimited"
        trades_cap = self.max_trades_per_day if self.max_trades_per_day > 0 else "unlimited"
        for key in sorted(self._by_strategy):
            state = self._by_strategy[key]
            daily_limit = self.daily_loss_limit * capital
            weekly_limit = self.weekly_loss_limit * capital
            monthly_limit = self.monthly_loss_limit * capital
            if state.day_start_capital > 0:
                logger.info(
                    f"[{key}] Day-start capital: {state.day_start_capital:,.2f} INR "
                    f"(risk={self.risk_per_trade:.1%}="
                    f"{state.day_start_capital * self.risk_per_trade:,.0f}/trade)"
                )
            logger.info(
                f"[{key}] Positions: {state.open_positions}/{open_cap} | "
                f"Trades today: {state.trades_today}/{trades_cap}"
            )
            logger.info(
                f"[{key}] Daily loss: {state.daily_realised_loss:,.2f} / {daily_limit:,.2f} "
                f"({abs(state.daily_realised_loss / daily_limit * 100) if daily_limit else 0:.0f}%)"
            )
            # Weekly/monthly limits measure NET drawdown; gross is shown alongside because
            # it is the "how many stop-outs" figure the daily gate uses.
            logger.info(
                f"[{key}] Weekly net drawdown: {max(0.0, -state.weekly_net_pnl):,.2f} / "
                f"{weekly_limit:,.2f} (gross losses {state.weekly_realised_loss:,.2f}) | "
                f"Monthly net drawdown: {max(0.0, -state.monthly_net_pnl):,.2f} / "
                f"{monthly_limit:,.2f} (gross losses {state.monthly_realised_loss:,.2f})"
            )
        sl_cap_str = (
            f"{self.max_sl_pct_for_sizing:.1%}" if self.max_sl_pct_for_sizing > 0 else "off"
        )
        logger.info(
            f"Price filter: {self.min_entry_price}-{self.max_entry_price} | "
            f"Risk/trade: {self.risk_per_trade:.1%} | SL cap for sizing: {sl_cap_str}"
        )
        logger.info("-------------------------------------")

    def get_sizing_capital(self, live_capital: float, strategy: str) -> float:
        """Return the capital to use for sizing THIS strategy's position.

        When use_day_start_capital is enabled, caches the first capital value
        of each trading day PER STRATEGY and returns it for all of that
        strategy's subsequent calls this day — every trade for a given strategy
        gets equal risk regardless of how many of ITS OWN positions are open,
        and regardless of what any other strategy is doing.

        When disabled, returns live_capital as-is (original behavior).
        """
        if not self.use_day_start_capital:
            return live_capital

        state = self._state(strategy)
        if state.day_start_capital <= 0:
            state.day_start_capital = live_capital
            logger.info(
                f"[{strategy}] Day-start capital cached: {live_capital:,.2f} INR "
                f"(risk={self.risk_per_trade:.1%}={live_capital * self.risk_per_trade:,.0f}/trade)"
            )
            self._persist(strategy)
        return state.day_start_capital

    def _persist(self, strategy: str) -> None:
        """Save one strategy's current counters to the persistent store."""
        if self._store is None:
            return
        state = self._state(strategy)
        today = datetime.now(IST).date()
        self._store.save(
            self._key(strategy),
            self._trade_mode,
            today,
            trades_today=state.trades_today,
            daily_loss=state.daily_realised_loss,
            daily_net_pnl=state.daily_net_pnl,
            open_positions=state.open_positions,
            day_start_capital=state.day_start_capital,
        )

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(IST).timetuple().tm_yday
        if today != self._current_day:
            logger.info("New trading day detected, resetting daily counters for all strategies")
            self._current_day = today
            self._by_strategy = {}
            self._positions_by_symbol.clear()
            self._positions_by_sector.clear()

    @staticmethod
    def _limit_capital(state: _StrategyState) -> float:
        """The capital figure the loss limits are measured against.

        last_known_capital is stamped only inside calculate_quantity() and is NOT persisted,
        so on a fresh process it is 0.0 for every strategy — and main._handle_entry runs the
        risk gates BEFORE it resolves capital. Gating the loss checks on that alone meant the
        first signal after any restart skipped all three limits, even with the day's realised
        loss correctly restored from risk.db. day_start_capital IS persisted, so it is the
        fallback; a fresher in-session figure still wins when present.
        """
        return state.last_known_capital or state.day_start_capital

    def update_unrealised(self, loss: float, strategy: str) -> None:
        """Update mark-to-market unrealised loss for this strategy (replace, not accumulate)."""
        self._state(strategy).unrealised_loss = loss

    def calculate_quantity(self, signal: Signal, capital: float) -> int:
        """Calculate position size based on the configured sizing mode.

        Args:
            signal: The parsed signal with entry, sl, target, strategy.
            capital: Capital to size THIS strategy's trade off (sizing_capital,
                already resolved via get_sizing_capital for signal.strategy).

        Returns 0 if the trade should be skipped (price filter, unaffordable).
        Raises ValueError for unknown sizing mode.
        """
        self._state(signal.strategy).last_known_capital = capital

        # Price filter — reject stocks outside the configured price band. Per-strategy first:
        # a fixed-universe strategy's band is calibrated to that universe's tick-cost economics
        # and has no reason to also apply to a scanner-selected universe with different names
        # every day (see _price_band_for()).
        min_entry_price, max_entry_price = self._price_band_for(signal.strategy)
        if min_entry_price > 0 and signal.entry < min_entry_price:
            logger.warning(
                f"Skipping {signal.symbol}: entry {signal.entry} below "
                f"min price {min_entry_price}"
            )
            return 0
        if max_entry_price > 0 and signal.entry > max_entry_price:
            logger.warning(
                f"Skipping {signal.symbol}: entry {signal.entry} above "
                f"max price {max_entry_price}"
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

        # Soft-blacklist scaling — applied AFTER baseline sizing so risk_per_share
        # is unchanged. Final qty is reduced by the per-strategy multiplier.
        # Use case: regime-flipped stocks (e.g. CANBK Q1->Q2) where full block
        # discards optionality. Half-size keeps participation, halves downside.
        qty = self._apply_soft_scaling(signal, qty)

        # If the sizing formula produced qty <= 0, the stock is
        # too expensive for the allocated capital — do not force a trade.
        # (Also catches soft_multiplier=0 which legitimately drops qty to 0.)
        if qty <= 0:
            logger.warning(
                f"Skipping {signal.symbol}: stock price {signal.entry} exceeds "
                f"capital allocation ({self.sizing_mode})"
            )
            return 0

        return qty

    def _price_band_for(self, strategy: str) -> tuple:
        """(min_entry_price, max_entry_price) for this strategy, falling back to the global
        band when the strategy has no override.

        The global band is a property of a FIXED universe's execution economics (tick size vs.
        stop distance for a specific, unchanging symbol list) - see the `sizing:` comments in
        config.yaml. A scanner-selected strategy (BreakingTrade) picks from a different set of
        names every day with no such calibration behind it, so it needs to be able to opt out
        (min/max = 0, "no filter") rather than silently inherit a band tuned for someone else's
        universe. Each bound is independently overridable, same as min_sl_pct in validator.py.
        """
        profile = self._strategy_profiles.get(strategy.upper(), {})
        min_price = profile.get("min_entry_price")
        max_price = profile.get("max_entry_price")
        return (
            self.min_entry_price if min_price is None else float(min_price),
            self.max_entry_price if max_price is None else float(max_price),
        )

    def _apply_soft_scaling(self, signal: Signal, qty: int) -> int:
        """Scale qty by the per-strategy soft-blacklist multiplier if symbol matches.

        Returns qty unchanged when:
          - no soft blacklist configured
          - signal's strategy has no soft set
          - symbol not in the strategy's soft set

        Multiplier resolution order:
          1. soft_blacklist_multipliers[strategy] if present
          2. DEFAULT_SOFT_MULTIPLIER (0.5)
        """
        if not self._soft_blacklist:
            return qty

        strategy_key = signal.strategy.upper()
        soft_set = self._soft_blacklist.get(strategy_key)
        if not soft_set:
            return qty

        if signal.symbol.upper() not in soft_set:
            return qty

        multiplier = self._soft_blacklist_multipliers.get(
            strategy_key, self.DEFAULT_SOFT_MULTIPLIER
        )
        scaled = math.floor(qty * multiplier)
        logger.info(
            f"[soft-sized] {signal.symbol} ({signal.strategy}): "
            f"qty {qty} -> {scaled} (multiplier x{multiplier:.2f})"
        )
        return scaled

    def _fixed_fractional(self, signal: Signal, capital: float) -> int:
        """Risk a fixed % of capital, sized by distance to SL.

        When max_sl_pct_for_sizing > 0 and the signal's SL distance is wider than
        that cap (as a fraction of entry), the sizing uses the capped distance instead
        of the actual SL distance. The real SL ORDER is still placed at signal.sl.
        Effect: wide-SL stocks get proportionally more shares, improving capital
        utilisation on wide-range ORB days.
        """
        risk_amount = capital * self.risk_per_trade
        risk_per_share = abs(signal.entry - signal.sl)
        if risk_per_share <= 0:
            return 0

        # Apply SL cap for sizing if configured and SL is wider than the cap
        if self.max_sl_pct_for_sizing > 0 and signal.entry > 0:
            max_sl_distance = signal.entry * self.max_sl_pct_for_sizing
            if risk_per_share > max_sl_distance:
                sl_pct = risk_per_share / signal.entry
                logger.info(
                    f"SL cap applied for {signal.symbol}: actual_sl={risk_per_share:.2f} "
                    f"({sl_pct:.2%} of entry) capped at {max_sl_distance:.2f} "
                    f"({self.max_sl_pct_for_sizing:.1%}) for sizing. "
                    f"Real SL order still at {signal.sl:.2f}."
                )
                risk_per_share = max_sl_distance

        risk_per_share *= (1 + self.slippage_factor)
        return math.floor(risk_amount / risk_per_share)

    def _pct_of_capital(self, signal: Signal, capital: float) -> int:
        """Allocate a fixed % of capital to the position."""
        allocation = capital * self.pct_of_capital
        if signal.entry <= 0:
            return 0
        return math.floor(allocation / signal.entry)

    def check_exposure(self, strategy: str) -> bool:
        """Check if a new trade for THIS strategy is allowed under its own exposure limits.

        Uses the strategy's last known capital for limit calculations. Loss counters are
        updated by the position tracker when trades close.
        Combines realised and unrealised loss for the daily limit check.
        """
        self._maybe_reset_daily()
        state = self._state(strategy)

        capital = self._limit_capital(state)
        if capital > 0:
            # DAILY is gross (losing trades only) — a loss-streak gate. WEEKLY and MONTHLY
            # are NET drawdown. See _StrategyState's field comments.
            combined_daily = state.daily_realised_loss + state.unrealised_loss
            if combined_daily >= capital * self.daily_loss_limit:
                logger.warning(f"[{strategy}] Daily loss limit breached")
                return False

            if -state.weekly_net_pnl >= capital * self.weekly_loss_limit:
                logger.warning(f"[{strategy}] Weekly loss limit breached")
                return False

            if -state.monthly_net_pnl >= capital * self.monthly_loss_limit:
                logger.warning(f"[{strategy}] Monthly loss limit breached")
                return False

        # 0 = unlimited, matching the same convention already used by
        # max_positions_per_symbol/sector, min_capital_for_entry and test_qty_cap.
        if self.max_open_positions > 0 and state.open_positions >= self.max_open_positions:
            logger.warning(f"[{strategy}] Max open positions reached")
            return False

        if self.max_trades_per_day > 0 and state.trades_today >= self.max_trades_per_day:
            logger.warning(f"[{strategy}] Max trades per day reached")
            return False

        return True

    def exposure_block_reason(self, strategy: str) -> str:
        """Return a human-readable reason why check_exposure() returned False."""
        state = self._state(strategy)
        capital = self._limit_capital(state)
        if capital > 0:
            combined_daily = state.daily_realised_loss + state.unrealised_loss
            if combined_daily >= capital * self.daily_loss_limit:
                return f"Daily loss limit hit ({combined_daily:,.0f} >= {capital * self.daily_loss_limit:,.0f})"
            if -state.weekly_net_pnl >= capital * self.weekly_loss_limit:
                return (
                    f"Weekly net drawdown limit hit ({-state.weekly_net_pnl:,.0f} >= "
                    f"{capital * self.weekly_loss_limit:,.0f})"
                )
            if -state.monthly_net_pnl >= capital * self.monthly_loss_limit:
                return (
                    f"Monthly net drawdown limit hit ({-state.monthly_net_pnl:,.0f} >= "
                    f"{capital * self.monthly_loss_limit:,.0f})"
                )
        if self.max_open_positions > 0 and state.open_positions >= self.max_open_positions:
            return f"Max positions ({state.open_positions}/{self.max_open_positions})"
        if self.max_trades_per_day > 0 and state.trades_today >= self.max_trades_per_day:
            return f"Max trades/day ({state.trades_today}/{self.max_trades_per_day})"
        return "Unknown"

    def capacity_status(self, strategy: str) -> str:
        """Return a formatted capacity summary string for this strategy."""
        state = self._state(strategy)
        cap = self.max_open_positions if self.max_open_positions > 0 else "unlimited"
        return f"{state.open_positions}/{cap} positions open"

    def open_positions_for(self, strategy: str) -> int:
        return self._state(strategy).open_positions

    def trades_today_for(self, strategy: str) -> int:
        return self._state(strategy).trades_today

    def last_known_capital_for(self, strategy: str) -> float:
        return self._state(strategy).last_known_capital

    def total_open_positions(self) -> int:
        """Open positions across every strategy — cheap existence check only
        (e.g. "is there anything to reconcile at all"), not a limit."""
        return sum(s.open_positions for s in self._by_strategy.values())

    def total_last_known_capital(self) -> float:
        """Sum of every strategy's own last-known capital — for the aggregate
        day-summary notification, which reports across all strategies at once."""
        return sum(s.last_known_capital for s in self._by_strategy.values())

    def set_open_positions(self, strategy: str, count: int) -> None:
        """Force-correct one strategy's open_positions counter (startup reconciliation
        against the broker's actual positionbook) and persist it."""
        self._state(strategy).open_positions = max(0, count)
        self._persist(strategy)

    def can_trade_symbol(self, symbol: str, strategy: str) -> bool:
        """Return True if opening another position in this symbol is allowed.

        Pooled across strategies in LIVE, isolated per strategy in ANALYZE — see module
        docstring.
        """
        if self.max_positions_per_symbol == 0:
            return True
        held = self._positions_by_symbol.get((self._key(strategy), symbol), 0)
        return held < self.max_positions_per_symbol

    def can_trade_sector(self, symbol: str, strategy: str) -> bool:
        """Return True if opening another position in this symbol's sector is allowed.

        Same pooled/isolated split as can_trade_symbol — see module docstring.
        """
        if self.max_positions_per_sector == 0:
            return True
        sector = self._symbol_to_sector.get(symbol)
        if sector is None:
            return True
        held = self._positions_by_sector.get((self._key(strategy), sector), 0)
        return held < self.max_positions_per_sector

    def _adjust_concentration(self, strategy: str, symbol: str, delta: int) -> None:
        """Move this strategy's symbol/sector position counts by delta, never below zero."""
        if not symbol:
            return
        key = self._key(strategy)
        sym_key = (key, symbol)
        self._positions_by_symbol[sym_key] = max(
            0, self._positions_by_symbol.get(sym_key, 0) + delta
        )
        sector = self._symbol_to_sector.get(symbol)
        if sector:
            sec_key = (key, sector)
            self._positions_by_sector[sec_key] = max(
                0, self._positions_by_sector.get(sec_key, 0) + delta
            )

    def record_trade(self, strategy: str, symbol: str = "") -> None:
        """Record a new trade entry for this strategy, incrementing its counters."""
        state = self._state(strategy)
        state.trades_today += 1
        state.open_positions += 1
        self._adjust_concentration(strategy, symbol, +1)
        self._persist(strategy)

    def record_rejection(self, strategy: str, symbol: str = "") -> None:
        """Release an open-position slot for a rejected/phantom entry.

        Called when the tracker detects that an order was never filled (broker rejection,
        cancelled, or zero-PnL orphan). Frees the slot AND un-counts the trade —
        the position never existed at the broker so it should not consume a daily slot.
        """
        state = self._state(strategy)
        state.open_positions = max(0, state.open_positions - 1)
        state.trades_today = max(0, state.trades_today - 1)
        self._adjust_concentration(strategy, symbol, -1)
        logger.info(f"[{strategy}] Position slot released (rejection): {symbol or 'unknown'}")
        self._persist(strategy)

    def record_close(self, pnl: float, strategy: str, symbol: str = "") -> None:
        """Record a position close for this strategy. Negative pnl = loss."""
        state = self._state(strategy)
        state.open_positions = max(0, state.open_positions - 1)
        self._adjust_concentration(strategy, symbol, -1)
        # NET counters take every close, both signs — these are what the weekly/monthly
        # drawdown limits measure. The gross counters below stay losses-only for the daily
        # loss-streak gate and for reporting.
        state.daily_net_pnl += pnl
        state.weekly_net_pnl += pnl
        state.monthly_net_pnl += pnl
        if pnl < 0:
            realized_loss = abs(pnl)
            state.daily_realised_loss += realized_loss
            state.weekly_realised_loss += realized_loss
            state.monthly_realised_loss += realized_loss
            logger.info(f"[{strategy}] Position closed with loss: {realized_loss:,.2f}")
        else:
            logger.info(f"[{strategy}] Position closed with profit: {pnl:,.2f}")
        self._persist(strategy)
