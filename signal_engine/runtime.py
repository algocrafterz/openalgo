"""Composition root for the signal engine's long-lived objects.

Keeps the wiring from `settings` to `RiskEngine` in one place. Both the live engine
(`main.py`) and the pre-session dry run (`smoke_test.py`) build their engine here, so
a sizing-relevant setting can never be applied in one and forgotten in the other —
the two constructions had already drifted by three parameters before this was shared.
"""

from signal_engine.config import settings
from signal_engine.risk import RiskEngine


def build_risk_engine(store, trade_mode: str = "live") -> RiskEngine:
    """Build a RiskEngine wired to the current configuration.

    Args:
        store: RiskStore backing the persistent day/week/month counters.
        trade_mode: "live" or "analyze" — isolates counters between the two.
    """
    return RiskEngine(
        risk_per_trade=settings.risk_per_trade,
        sizing_mode=settings.sizing_mode,
        pct_of_capital=settings.pct_of_capital,
        daily_loss_limit=settings.daily_loss_limit,
        weekly_loss_limit=settings.weekly_loss_limit,
        monthly_loss_limit=settings.monthly_loss_limit,
        max_open_positions=settings.max_open_positions,
        max_trades_per_day=settings.max_trades_per_day,
        min_entry_price=settings.min_entry_price,
        max_entry_price=settings.max_entry_price,
        slippage_factor=settings.slippage_factor,
        max_sl_pct_for_sizing=settings.max_sl_pct_for_sizing,
        store=store,
        trade_mode=trade_mode,
        max_positions_per_symbol=settings.max_positions_per_symbol,
        max_positions_per_sector=settings.max_positions_per_sector,
        sectors=settings.sectors,
        use_day_start_capital=settings.use_day_start_capital,
        soft_blacklist=settings.soft_blacklist,
        soft_blacklist_multipliers=settings.soft_blacklist_multipliers,
    )
