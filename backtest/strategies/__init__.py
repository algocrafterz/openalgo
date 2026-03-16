"""
Strategy registry for backtesting.

Dynamically loads strategy classes by name. To add a new strategy:
1. Create a new folder in backtest/strategies/ (e.g., vwap/)
2. Implement the Strategy base class in strategy.py
3. Register it in STRATEGY_REGISTRY below
"""

from backtest.strategies.base import Strategy
from backtest.strategies.orb import ORBConfig, ORBStrategy


class StrategyNotFoundError(Exception):
    """Raised when a strategy name is not found in the registry."""


# Registry: name -> (class, description)
STRATEGY_REGISTRY: dict[str, tuple[type[Strategy], str]] = {
    "orb": (ORBStrategy, "Opening Range Breakout (NSE intraday)"),
}


def get_strategy(name: str, **kwargs) -> Strategy:
    """
    Load a strategy by name.

    Args:
        name: Strategy name (case-insensitive).
        **kwargs: Passed to strategy constructor.
            For ORB: orb_config=ORBConfig(...), index_data=pd.DataFrame(...)

    Returns:
        Instantiated Strategy object.

    Raises:
        StrategyNotFoundError: If name not in registry.
    """
    key = name.lower()
    if key not in STRATEGY_REGISTRY:
        available = ", ".join(sorted(STRATEGY_REGISTRY.keys()))
        raise StrategyNotFoundError(
            f"Strategy '{name}' not found. Available: {available}"
        )

    strategy_cls, _ = STRATEGY_REGISTRY[key]

    # Strategy-specific construction
    if key == "orb":
        orb_config = kwargs.get("orb_config")
        index_data = kwargs.get("index_data")
        return strategy_cls(config=orb_config, index_data=index_data)

    return strategy_cls(**kwargs)


def list_strategies() -> dict[str, str]:
    """Return dict of {name: description} for all registered strategies."""
    return {name: desc for name, (_, desc) in STRATEGY_REGISTRY.items()}
