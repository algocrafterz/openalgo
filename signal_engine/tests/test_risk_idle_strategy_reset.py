"""M6: a stale counter for a strategy with nothing open must still be correctable.

The ANALYZE reconciliation branch only iterated strategies present in locally_open or the
broker book. A strategy carrying a non-zero open_positions in risk.db but no local open rows
(its position was closed while the engine was down, or a counter drifted) was never visited,
so it kept that phantom slot for the rest of the day — and with max_open_positions=2 in live,
one phantom slot is half the account's capacity.
"""

from signal_engine.tests.risk_fixtures import _engine


class TestKnownStrategies:
    def test_lists_every_strategy_with_loaded_state(self):
        engine = _engine()
        engine.record_trade(strategy="ORB", symbol="RELIANCE")
        engine.record_trade(strategy="BREAKOUT", symbol="SBIN")
        assert engine.known_strategies() == {"ORB", "BREAKOUT"}

    def test_is_empty_before_anything_trades(self):
        assert _engine().known_strategies() == set()

    def test_live_mode_reports_the_pooled_bucket(self):
        engine = _engine(trade_mode="live")
        engine.record_trade(strategy="ORB", symbol="RELIANCE")
        assert engine.known_strategies() == {"PORTFOLIO"}
