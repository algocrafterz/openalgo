"""Shared factories for the risk test modules."""


from signal_engine.risk import RiskEngine


def _engine(**overrides) -> RiskEngine:
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
        "slippage_factor": 0.0,
        "default_product": "MIS",
        "max_positions_per_symbol": 1,
        "max_positions_per_sector": 2,
        "sectors": {"BANKING": ["HDFCBANK", "SBIN"], "IT": ["TCS", "INFY"]},
    }
    defaults.update(overrides)
    return RiskEngine(**defaults)
