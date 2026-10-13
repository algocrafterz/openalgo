"""Shared factories for the tracker test modules."""

from datetime import datetime, timedelta, timezone

from signal_engine.risk import RiskEngine
from signal_engine.strategies import ORB
from signal_engine.tracker import TrackedPosition

_IST = timezone(timedelta(hours=5, minutes=30))
_AGED = datetime.now(_IST) - timedelta(minutes=5)  # past Guard 1 (30s min age)


def _make_engine(**overrides) -> RiskEngine:
    defaults = {
        "risk_per_trade": 0.01,
        "sizing_mode": "fixed_fractional",
        "pct_of_capital": 0.05,
        "daily_loss_limit": 0.03,
        "weekly_loss_limit": 0.06,
        "monthly_loss_limit": 0.10,
        "max_open_positions": 3,
        "max_trades_per_day": 5,
        "min_entry_price": 0,
        "max_entry_price": 0,
    }
    defaults.update(overrides)
    return RiskEngine(**defaults)


def _make_position(**overrides) -> TrackedPosition:
    defaults = {
        "symbol": "RELIANCE",
        "strategy": ORB,
        "exchange": "NSE",
        "product": "MIS",
        "entry_price": 2500.0,
        "quantity": 50,
        "sl": 2485.0,
        "tp": 2540.0,
        # 5 minutes old when the position is made: past Guard 1 (30 s) and short of the 30 min
        # Guard 2 timeout. The import-time _AGED aged with the suite (and with any wall-clock
        # jump), so in a long full run a "young" position could cross the timeout.
        "entry_time": datetime.now(_IST) - timedelta(minutes=5),
    }
    defaults.update(overrides)
    return TrackedPosition(**defaults)
