"""metrics.summary reports how many trades were long and short (G4.6 direction check)."""

import pandas as pd

from signal_engine.backtest import metrics
from signal_engine.backtest.types import Trade


def _trade(day, direction, r_net=0.5):
    return Trade(
        symbol="A.NS",
        day=day,
        direction=direction,
        tag="t",
        entry_time=pd.Timestamp(f"{day} 09:20"),
        entry=100.0,
        signal_price=100.0,
        sl=99.0,
        tp=0.0,
        risk=1.0,
        exit_time=f"{day} 15:00",
        reason="tp",
        r_gross=r_net + 0.1,
        r_net=r_net,
    )


class TestDirectionCounts:
    def test_counts_split_by_direction(self):
        trades = [
            _trade("2026-01-01", 1),
            _trade("2026-01-02", 1),
            _trade("2026-01-03", -1),
        ]
        out = metrics.summary(trades, "x")
        assert out["long_trades"] == 2
        assert out["short_trades"] == 1
        assert out["n"] == 3

    def test_long_only_book_has_zero_shorts(self):
        out = metrics.summary([_trade("2026-01-01", 1)], "x")
        assert out["short_trades"] == 0

    def test_empty_trade_list_has_zero_counts(self):
        out = metrics.summary([], "x")
        assert out["long_trades"] == 0
        assert out["short_trades"] == 0
