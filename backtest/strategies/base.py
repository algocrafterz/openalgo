"""
Base strategy interface for backtesting.

All strategies must implement the generate_signals method that
returns entry/exit boolean Series from OHLCV data.
"""

from abc import ABC, abstractmethod

import pandas as pd


class Strategy(ABC):
    """Abstract base class for backtesting strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy name."""

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """Current parameter values as a dict."""

    @abstractmethod
    def generate_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """
        Generate entry and exit signals from OHLCV data.

        Args:
            df: DataFrame with columns [open, high, low, close, volume]
                and a datetime index.

        Returns:
            Tuple of (entries, exits) - boolean Series aligned with df.index.
            True = signal fired on that bar.
        """

    def describe(self) -> str:
        """One-line description of the strategy and its parameters."""
        params = ", ".join(f"{k}={v}" for k, v in self.parameters.items())
        return f"{self.name}({params})"
