"""Composition root for the signal engine's long-lived objects.

Keeps the wiring from `settings` to `RiskEngine` in one place. Both the live engine
(`main.py`) and the pre-session dry run (`smoke_test.py`) build their engine here, so
a sizing-relevant setting can never be applied in one and forgotten in the other —
the two constructions had already drifted by three parameters before this was shared.
"""

from loguru import logger

from signal_engine.config import resolve_mode_profile, settings
from signal_engine.risk import RiskEngine

#: Risk limits a mode_profile may override. Deliberately a fixed list rather than "anything
#: in the profile": a typo in config would otherwise set an attribute nothing reads, and the
#: operator would believe a limit was in force when it was not.
_MODE_OVERRIDABLE = (
    "risk_per_trade",
    "daily_loss_limit",
    "weekly_loss_limit",
    "monthly_loss_limit",
    "max_open_positions",
    "max_trades_per_day",
    "min_entry_price",
    "max_entry_price",
    "max_positions_per_symbol",
    "max_positions_per_sector",
)


def apply_trade_mode(engine: RiskEngine, trade_mode: str) -> None:
    """Point an engine at a mode's own counters and limits, once the mode is known.

    `risk_engine` is constructed at import time, before OpenAlgo can be asked which mode it
    is in, so it starts as "live" by default. Left uncorrected that has two consequences, and
    the second is the serious one: the paper week's limits would be the live ones, and every
    paper loss would be written into the LIVE row of risk_store -- the precise mixing that
    store keys on (mode, date) to prevent.

    Called from startup immediately after fetch_trading_mode(), before any signal is handled.
    """
    base = {name: getattr(engine, name) for name in _MODE_OVERRIDABLE}
    resolved = resolve_mode_profile(base, getattr(settings, "mode_profiles", {}), trade_mode)

    changed = []
    for name, value in resolved.items():
        if getattr(engine, name) != value:
            setattr(engine, name, value)
            changed.append(f"{name}={value}")

    if engine._trade_mode != trade_mode:
        engine._trade_mode = trade_mode
        # Counters were loaded against the previous mode's row; reload against this one.
        engine._restore()

    logger.info(
        f"Risk limits for {trade_mode.upper()} mode"
        + (f": {', '.join(changed)}" if changed else " (profile matches base config)")
    )


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
