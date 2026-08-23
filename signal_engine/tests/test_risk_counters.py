"""Counter bookkeeping: trades, rejections, closes, drawdown, restart safety."""


from signal_engine.risk_store import RiskStore

from signal_engine.tests.risk_fixtures import _engine


class TestRecordTrade:
    def test_increments_counters(self):
        engine = _engine()
        engine.record_trade(symbol="RELIANCE")
        assert engine.trades_today == 1
        assert engine.open_positions == 1

    def test_accumulates_trades(self):
        engine = _engine()
        engine.record_trade(symbol="RELIANCE")
        engine.record_trade(symbol="TCS")
        assert engine.trades_today == 2
        assert engine.open_positions == 2


class TestRecordRejection:
    def test_rejection_frees_slot_and_uncounts_trade(self):
        engine = _engine()
        engine.record_trade(symbol="IIFL")
        assert engine.trades_today == 1
        assert engine.open_positions == 1

        engine.record_rejection(symbol="IIFL")
        assert engine.open_positions == 0
        assert engine.trades_today == 0  # broker rejection: slot AND trade count restored

    def test_rejection_does_not_go_negative(self):
        engine = _engine()
        engine.record_rejection(symbol="POONAWALLA")
        assert engine.trades_today == 0
        assert engine.open_positions == 0

    def test_rejection_does_not_touch_loss_counters(self):
        engine = _engine()
        engine.record_trade(symbol="ITCHOTELS")
        engine.record_rejection(symbol="ITCHOTELS")
        assert engine.daily_realised_loss == 0
        assert engine.weekly_realised_loss == 0


class TestRecordClose:
    def test_loss_updates_counters(self):
        engine = _engine()
        engine.open_positions = 1
        engine.record_close(pnl=-500, symbol="RELIANCE")
        assert engine.open_positions == 0
        assert engine.daily_realised_loss == 500
        assert engine.weekly_realised_loss == 500
        assert engine.monthly_realised_loss == 500

    def test_profit_does_not_add_loss(self):
        engine = _engine()
        engine.open_positions = 1
        engine.record_close(pnl=1000, symbol="RELIANCE")
        assert engine.open_positions == 0
        assert engine.daily_realised_loss == 0

    def test_open_positions_never_negative(self):
        engine = _engine()
        engine.open_positions = 0
        engine.record_close(pnl=100, symbol="RELIANCE")
        assert engine.open_positions == 0


class TestDailyReset:
    def test_counters_reset_on_new_day(self):
        engine = _engine()
        engine.record_trade(symbol="RELIANCE")
        engine.record_close(pnl=-500, symbol="RELIANCE")
        assert engine.daily_realised_loss == 500

        engine._current_day = -1
        assert engine.check_exposure() is True
        assert engine.trades_today == 0
        assert engine.daily_realised_loss == 0


class TestUnrealisedDrawdown:
    def test_initial_unrealised_loss_is_zero(self):
        engine = _engine()
        assert engine.unrealised_loss == 0.0

    def test_update_unrealised_sets_value(self):
        engine = _engine()
        engine.update_unrealised(300.0)
        assert engine.unrealised_loss == 300.0

    def test_update_unrealised_replaces_not_accumulates(self):
        engine = _engine()
        engine.update_unrealised(300.0)
        engine.update_unrealised(500.0)
        assert engine.unrealised_loss == 500.0

    def test_combined_loss_blocks_when_over_daily_limit(self):
        # daily_loss_limit=0.03, capital=100_000 -> limit=3000
        # realised=2000, unrealised=1001 -> combined=3001 -> block
        engine = _engine(daily_loss_limit=0.03)
        engine._last_known_capital = 100_000
        engine.daily_realised_loss = 2000.0
        engine.update_unrealised(1001.0)
        assert engine.check_exposure() is False

    def test_combined_loss_allows_when_under_daily_limit(self):
        # realised=2000, unrealised=999 -> combined=2999 < 3000 -> allow
        engine = _engine(daily_loss_limit=0.03)
        engine._last_known_capital = 100_000
        engine.daily_realised_loss = 2000.0
        engine.update_unrealised(999.0)
        assert engine.check_exposure() is True

    def test_unrealised_zero_does_not_affect_existing_checks(self):
        # No unrealised loss -> behaves same as before
        engine = _engine(daily_loss_limit=0.03)
        engine._last_known_capital = 100_000
        engine.daily_realised_loss = 3100.0
        engine.update_unrealised(0.0)
        assert engine.check_exposure() is False

    def test_unrealised_loss_reset_on_new_day(self):
        engine = _engine()
        engine.update_unrealised(500.0)
        engine._current_day = -1
        engine.check_exposure()
        assert engine.unrealised_loss == 0.0


class TestRestartSafeCounters:
    def test_counters_restored_from_store_on_init(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        from datetime import datetime, timezone, timedelta
        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        store.save("live", today, trades_today=3, daily_loss=1500.0, open_positions=2)

        engine = _engine(store=store, trade_mode="live")
        assert engine.trades_today == 3
        assert engine.daily_realised_loss == 1500.0
        assert engine.open_positions == 2

    def test_no_store_works_as_before(self):
        engine = _engine()
        assert engine.trades_today == 0
        assert engine.daily_realised_loss == 0.0

    def test_record_trade_persists_to_store(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")
        engine._last_known_capital = 100_000
        engine.record_trade(symbol="RELIANCE")

        from datetime import datetime, timezone, timedelta
        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        row = store.load("live", today)
        assert row["trades_today"] == 1
        assert row["open_positions"] == 1

    def test_record_close_persists_to_store(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")
        engine.open_positions = 1
        engine.record_close(pnl=-400.0, symbol="RELIANCE")

        from datetime import datetime, timezone, timedelta
        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        row = store.load("live", today)
        assert row["daily_loss"] == 400.0
        assert row["open_positions"] == 0

    def test_mode_isolation_live_vs_sandbox(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store_live = RiskStore(db_path)
        store_sandbox = RiskStore(db_path)

        engine_live = _engine(store=store_live, trade_mode="live")
        engine_sandbox = _engine(store=store_sandbox, trade_mode="sandbox")

        engine_live.record_trade(symbol="RELIANCE")
        engine_live.record_trade(symbol="TCS")

        engine_sandbox.record_trade(symbol="RELIANCE")

        from datetime import datetime, timezone, timedelta
        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        live_row = store_live.load("live", today)
        sandbox_row = store_sandbox.load("sandbox", today)

        assert live_row["trades_today"] == 2
        assert sandbox_row["trades_today"] == 1

    def test_daily_reset_persists_zeroed_counters(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")
        engine.record_trade(symbol="RELIANCE")
        engine.record_close(pnl=-200.0, symbol="RELIANCE")

        # Simulate new day
        engine._current_day = -1
        engine.check_exposure()

        from datetime import timezone
        from datetime import datetime
        today = datetime.now(timezone.utc).date()
        row = store.load("live", today)
        assert row["trades_today"] == 0
        assert row["daily_loss"] == 0.0
        assert row["open_positions"] == 0

    def test_engine_with_no_store_does_not_persist(self, tmp_path):
        # Just ensure no exception is raised when store=None
        engine = _engine()
        engine.record_trade(symbol="RELIANCE")
        engine.record_close(pnl=-100.0, symbol="RELIANCE")
        assert engine.trades_today == 1

    def test_day_start_capital_persists_and_restores_across_restart(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store1 = RiskStore(db_path)
        engine1 = _engine(store=store1, trade_mode="live", use_day_start_capital=True)

        # First trade of the day: caches and persists day-start capital
        engine1.get_sizing_capital(14_400.0)
        assert engine1._day_start_capital == 14_400.0

        from datetime import datetime, timezone, timedelta
        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        row = store1.load("live", today)
        assert row["day_start_capital"] == 14_400.0

        # Simulate restart: new engine with same DB, live capital depleted by open positions
        store2 = RiskStore(db_path)
        engine2 = _engine(store=store2, trade_mode="live", use_day_start_capital=True)
        assert engine2._day_start_capital == 14_400.0

        # Subsequent sizing uses restored value, not the depleted live capital
        assert engine2.get_sizing_capital(8_000.0) == 14_400.0

    def test_day_start_capital_reset_on_new_day(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live", use_day_start_capital=True)

        engine.get_sizing_capital(14_400.0)
        assert engine._day_start_capital == 14_400.0

        # Simulate new day
        engine._current_day = -1
        engine.check_exposure()
        assert engine._day_start_capital == 0.0

        # Next capital fetch re-caches fresh value
        assert engine.get_sizing_capital(16_000.0) == 16_000.0
