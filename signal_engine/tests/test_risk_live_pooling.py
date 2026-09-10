"""LIVE mode pools every strategy's counters into one shared bucket (2026-09-10).

Companion to test_risk_strategy_isolation.py: ANALYZE isolates each strategy (fake
money, no real contention); LIVE pools them (one real broker account, one real pot
of money and one real loss ceiling — a second live strategy must see what the
first one already used, not a second imaginary copy of the full account).
"""

from datetime import date

from signal_engine.risk_store import RiskStore
from signal_engine.tests.risk_fixtures import _engine

ORB = "ORB"
BREAKOUT = "BREAKOUT"


class TestIsolatesPerStrategyFlag:
    def test_analyze_isolates(self):
        assert _engine(trade_mode="analyze").isolates_per_strategy is True

    def test_live_pools(self):
        assert _engine(trade_mode="live").isolates_per_strategy is False

    def test_unrecognised_mode_pools_like_live(self):
        # Real money defaults to the SAFER, shared behaviour rather than guessing.
        assert _engine(trade_mode="some_typo").isolates_per_strategy is False


class TestDayStartCapitalPooling:
    def test_second_strategy_gets_first_strategys_cached_value(self):
        engine = _engine(trade_mode="live", use_day_start_capital=True)
        assert engine.get_sizing_capital(35_000.0, ORB) == 35_000.0
        # BREAKOUT asks with a DIFFERENT live_capital (as it would if margin had
        # already shrunk after ORB's trade) — it must still get ORB's cached figure,
        # not its own fresh number, because they share the same real account.
        assert engine.get_sizing_capital(28_000.0, BREAKOUT) == 35_000.0

    def test_contrast_analyze_does_not_share(self):
        engine = _engine(trade_mode="analyze", use_day_start_capital=True)
        assert engine.get_sizing_capital(35_000.0, ORB) == 35_000.0
        assert engine.get_sizing_capital(28_000.0, BREAKOUT) == 28_000.0


class TestPositionAndTradePooling:
    def test_one_strategys_trade_counts_against_the_other(self):
        engine = _engine(trade_mode="live", max_open_positions=2, max_trades_per_day=5)
        engine.record_trade(strategy=ORB, symbol="RELIANCE")
        engine.record_trade(strategy=BREAKOUT, symbol="TCS")
        # 2 total across BOTH strategies already used the shared cap of 2.
        assert engine.open_positions_for(ORB) == 2
        assert engine.open_positions_for(BREAKOUT) == 2
        assert engine.check_exposure(ORB) is False
        assert engine.check_exposure(BREAKOUT) is False

    def test_close_on_one_strategy_frees_the_shared_slot_for_the_other(self):
        engine = _engine(trade_mode="live", max_open_positions=1, max_trades_per_day=5)
        engine.record_trade(strategy=ORB, symbol="RELIANCE")
        assert engine.check_exposure(BREAKOUT) is False

        engine.record_close(pnl=100.0, strategy=ORB, symbol="RELIANCE")
        assert engine.check_exposure(BREAKOUT) is True


class TestLossLimitPooling:
    def test_one_strategys_loss_blocks_the_other(self):
        engine = _engine(trade_mode="live", daily_loss_limit=0.02)
        engine._state(ORB).last_known_capital = 100_000
        engine._state(BREAKOUT).last_known_capital = 100_000

        engine.record_close(pnl=-2_100.0, strategy=ORB, symbol="RELIANCE")
        # BREAKOUT never lost a rupee itself, but the ACCOUNT (shared with ORB) is
        # over its 2% daily ceiling, so BREAKOUT must be blocked too.
        assert engine.check_exposure(BREAKOUT) is False

    def test_contrast_analyze_does_not_share_loss(self):
        engine = _engine(trade_mode="analyze", daily_loss_limit=0.02)
        engine._state(ORB).last_known_capital = 100_000
        engine._state(BREAKOUT).last_known_capital = 100_000

        engine.record_close(pnl=-2_100.0, strategy=ORB, symbol="RELIANCE")
        assert engine.check_exposure(ORB) is False
        assert engine.check_exposure(BREAKOUT) is True


class TestRiskStorePooling:
    def test_two_live_strategies_persist_under_the_same_row(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")

        engine.record_trade(strategy=ORB, symbol="RELIANCE")
        engine.record_trade(strategy=BREAKOUT, symbol="TCS")

        today = date.today()
        pooled_row = store.load(engine._LIVE_POOLED_KEY, "live", today)
        assert pooled_row["trades_today"] == 2
        # Nothing was ever written under the real strategy names in LIVE mode.
        assert store.load(ORB, "live", today)["trades_today"] == 0
        assert store.load(BREAKOUT, "live", today)["trades_today"] == 0

    def test_restart_restores_the_pooled_bucket_not_per_strategy_rows(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store1 = RiskStore(db_path)
        engine1 = _engine(store=store1, trade_mode="live")
        engine1.record_trade(strategy=ORB, symbol="RELIANCE")
        engine1.record_trade(strategy=BREAKOUT, symbol="TCS")

        store2 = RiskStore(db_path)
        engine2 = _engine(store=store2, trade_mode="live")
        assert engine2.open_positions_for(ORB) == 2
        assert engine2.open_positions_for(BREAKOUT) == 2
