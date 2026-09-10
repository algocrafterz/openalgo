"""Counter bookkeeping: trades, rejections, closes, drawdown, restart safety."""

from datetime import datetime as _dt
from datetime import timedelta as _td
from unittest.mock import patch

from signal_engine.risk_store import RiskStore
from signal_engine.tests.risk_fixtures import _engine

STRATEGY = "ORB"


class TestRecordTrade:
    def test_increments_counters(self):
        engine = _engine()
        engine.record_trade(strategy=STRATEGY, symbol="RELIANCE")
        assert engine.trades_today_for(STRATEGY) == 1
        assert engine.open_positions_for(STRATEGY) == 1

    def test_accumulates_trades(self):
        engine = _engine()
        engine.record_trade(strategy=STRATEGY, symbol="RELIANCE")
        engine.record_trade(strategy=STRATEGY, symbol="TCS")
        assert engine.trades_today_for(STRATEGY) == 2
        assert engine.open_positions_for(STRATEGY) == 2


class TestRecordRejection:
    def test_rejection_frees_slot_and_uncounts_trade(self):
        engine = _engine()
        engine.record_trade(strategy=STRATEGY, symbol="IIFL")
        assert engine.trades_today_for(STRATEGY) == 1
        assert engine.open_positions_for(STRATEGY) == 1

        engine.record_rejection(strategy=STRATEGY, symbol="IIFL")
        assert engine.open_positions_for(STRATEGY) == 0
        assert (
            engine.trades_today_for(STRATEGY) == 0
        )  # broker rejection: slot AND trade count restored

    def test_rejection_does_not_go_negative(self):
        engine = _engine()
        engine.record_rejection(strategy=STRATEGY, symbol="POONAWALLA")
        assert engine.trades_today_for(STRATEGY) == 0
        assert engine.open_positions_for(STRATEGY) == 0

    def test_rejection_does_not_touch_loss_counters(self):
        engine = _engine()
        engine.record_trade(strategy=STRATEGY, symbol="ITCHOTELS")
        engine.record_rejection(strategy=STRATEGY, symbol="ITCHOTELS")
        assert engine._state(STRATEGY).daily_realised_loss == 0
        assert engine._state(STRATEGY).weekly_realised_loss == 0


class TestRecordClose:
    def test_loss_updates_counters(self):
        engine = _engine()
        engine._state(STRATEGY).open_positions = 1
        engine.record_close(pnl=-500, strategy=STRATEGY, symbol="RELIANCE")
        assert engine.open_positions_for(STRATEGY) == 0
        assert engine._state(STRATEGY).daily_realised_loss == 500
        assert engine._state(STRATEGY).weekly_realised_loss == 500
        assert engine._state(STRATEGY).monthly_realised_loss == 500

    def test_profit_does_not_add_loss(self):
        engine = _engine()
        engine._state(STRATEGY).open_positions = 1
        engine.record_close(pnl=1000, strategy=STRATEGY, symbol="RELIANCE")
        assert engine.open_positions_for(STRATEGY) == 0
        assert engine._state(STRATEGY).daily_realised_loss == 0

    def test_open_positions_never_negative(self):
        engine = _engine()
        engine._state(STRATEGY).open_positions = 0
        engine.record_close(pnl=100, strategy=STRATEGY, symbol="RELIANCE")
        assert engine.open_positions_for(STRATEGY) == 0


class TestDailyReset:
    def test_counters_reset_on_new_day(self):
        engine = _engine()
        engine.record_trade(strategy=STRATEGY, symbol="RELIANCE")
        engine.record_close(pnl=-500, strategy=STRATEGY, symbol="RELIANCE")
        assert engine._state(STRATEGY).daily_realised_loss == 500

        engine._current_day = -1
        assert engine.check_exposure(STRATEGY) is True
        assert engine.trades_today_for(STRATEGY) == 0
        assert engine._state(STRATEGY).daily_realised_loss == 0


class TestUnrealisedDrawdown:
    def test_initial_unrealised_loss_is_zero(self):
        engine = _engine()
        assert engine._state(STRATEGY).unrealised_loss == 0.0

    def test_update_unrealised_sets_value(self):
        engine = _engine()
        engine.update_unrealised(300.0, STRATEGY)
        assert engine._state(STRATEGY).unrealised_loss == 300.0

    def test_update_unrealised_replaces_not_accumulates(self):
        engine = _engine()
        engine.update_unrealised(300.0, STRATEGY)
        engine.update_unrealised(500.0, STRATEGY)
        assert engine._state(STRATEGY).unrealised_loss == 500.0

    def test_combined_loss_blocks_when_over_daily_limit(self):
        # daily_loss_limit=0.03, capital=100_000 -> limit=3000
        # realised=2000, unrealised=1001 -> combined=3001 -> block
        engine = _engine(daily_loss_limit=0.03)
        state = engine._state(STRATEGY)
        state.last_known_capital = 100_000
        state.daily_realised_loss = 2000.0
        engine.update_unrealised(1001.0, STRATEGY)
        assert engine.check_exposure(STRATEGY) is False

    def test_combined_loss_allows_when_under_daily_limit(self):
        # realised=2000, unrealised=999 -> combined=2999 < 3000 -> allow
        engine = _engine(daily_loss_limit=0.03)
        state = engine._state(STRATEGY)
        state.last_known_capital = 100_000
        state.daily_realised_loss = 2000.0
        engine.update_unrealised(999.0, STRATEGY)
        assert engine.check_exposure(STRATEGY) is True

    def test_unrealised_zero_does_not_affect_existing_checks(self):
        # No unrealised loss -> behaves same as before
        engine = _engine(daily_loss_limit=0.03)
        state = engine._state(STRATEGY)
        state.last_known_capital = 100_000
        state.daily_realised_loss = 3100.0
        engine.update_unrealised(0.0, STRATEGY)
        assert engine.check_exposure(STRATEGY) is False

    def test_unrealised_loss_reset_on_new_day(self):
        engine = _engine()
        engine.update_unrealised(500.0, STRATEGY)
        engine._current_day = -1
        engine.check_exposure(STRATEGY)
        assert engine._state(STRATEGY).unrealised_loss == 0.0


class TestRestartSafeCounters:
    def test_counters_restored_from_store_on_init(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        from datetime import datetime, timedelta, timezone

        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        store.save(STRATEGY, "live", today, trades_today=3, daily_loss=1500.0, open_positions=2)

        engine = _engine(store=store, trade_mode="live")
        assert engine.trades_today_for(STRATEGY) == 3
        assert engine._state(STRATEGY).daily_realised_loss == 1500.0
        assert engine.open_positions_for(STRATEGY) == 2

    def test_no_store_works_as_before(self):
        engine = _engine()
        assert engine.trades_today_for(STRATEGY) == 0
        assert engine._state(STRATEGY).daily_realised_loss == 0.0

    def test_record_trade_persists_to_store(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")
        engine._state(STRATEGY).last_known_capital = 100_000
        engine.record_trade(strategy=STRATEGY, symbol="RELIANCE")

        from datetime import datetime, timedelta, timezone

        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        row = store.load(STRATEGY, "live", today)
        assert row["trades_today"] == 1
        assert row["open_positions"] == 1

    def test_record_close_persists_to_store(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")
        engine._state(STRATEGY).open_positions = 1
        engine.record_close(pnl=-400.0, strategy=STRATEGY, symbol="RELIANCE")

        from datetime import datetime, timedelta, timezone

        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        row = store.load(STRATEGY, "live", today)
        assert row["daily_loss"] == 400.0
        assert row["open_positions"] == 0

    def test_mode_isolation_live_vs_sandbox(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store_live = RiskStore(db_path)
        store_sandbox = RiskStore(db_path)

        engine_live = _engine(store=store_live, trade_mode="live")
        engine_sandbox = _engine(store=store_sandbox, trade_mode="sandbox")

        engine_live.record_trade(strategy=STRATEGY, symbol="RELIANCE")
        engine_live.record_trade(strategy=STRATEGY, symbol="TCS")

        engine_sandbox.record_trade(strategy=STRATEGY, symbol="RELIANCE")

        from datetime import datetime, timedelta, timezone

        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        live_row = store_live.load(STRATEGY, "live", today)
        sandbox_row = store_sandbox.load(STRATEGY, "sandbox", today)

        assert live_row["trades_today"] == 2
        assert sandbox_row["trades_today"] == 1

    def test_daily_reset_persists_zeroed_counters(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")
        engine.record_trade(strategy=STRATEGY, symbol="RELIANCE")
        engine.record_close(pnl=-200.0, strategy=STRATEGY, symbol="RELIANCE")

        # A bare `_current_day = -1` flip makes _maybe_reset_daily take the reset
        # branch, but a real day rollover ALSO moves the wall clock that store reads
        # key off of - mock it forward too, otherwise the post-reset store read just
        # finds the row this test wrote a moment ago under the still-current date.
        tomorrow = _dt.now() + _td(days=1)
        engine._current_day = -1
        with patch("signal_engine.risk.datetime") as mock_dt:
            mock_dt.now.return_value = tomorrow
            engine.check_exposure(STRATEGY)
            row = store.load(STRATEGY, "live", tomorrow.date())

        assert row["trades_today"] == 0
        assert row["daily_loss"] == 0.0
        assert row["open_positions"] == 0

    def test_engine_with_no_store_does_not_persist(self, tmp_path):
        # Just ensure no exception is raised when store=None
        engine = _engine()
        engine.record_trade(strategy=STRATEGY, symbol="RELIANCE")
        engine.record_close(pnl=-100.0, strategy=STRATEGY, symbol="RELIANCE")
        assert engine.trades_today_for(STRATEGY) == 1

    def test_day_start_capital_persists_and_restores_across_restart(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store1 = RiskStore(db_path)
        engine1 = _engine(store=store1, trade_mode="live", use_day_start_capital=True)

        # First trade of the day: caches and persists day-start capital
        engine1.get_sizing_capital(14_400.0, STRATEGY)
        assert engine1._state(STRATEGY).day_start_capital == 14_400.0

        from datetime import datetime, timedelta, timezone

        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        row = store1.load(STRATEGY, "live", today)
        assert row["day_start_capital"] == 14_400.0

        # Simulate restart: new engine with same DB, live capital depleted by open positions
        store2 = RiskStore(db_path)
        engine2 = _engine(store=store2, trade_mode="live", use_day_start_capital=True)
        assert engine2._state(STRATEGY).day_start_capital == 14_400.0

        # Subsequent sizing uses restored value, not the depleted live capital
        assert engine2.get_sizing_capital(8_000.0, STRATEGY) == 14_400.0

    def test_day_start_capital_reset_on_new_day(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live", use_day_start_capital=True)

        engine.get_sizing_capital(14_400.0, STRATEGY)
        assert engine._state(STRATEGY).day_start_capital == 14_400.0

        # See test_daily_reset_persists_zeroed_counters for why the clock is mocked
        # forward alongside the `_current_day` flip.
        tomorrow = _dt.now() + _td(days=1)
        engine._current_day = -1
        with patch("signal_engine.risk.datetime") as mock_dt:
            mock_dt.now.return_value = tomorrow
            engine.check_exposure(STRATEGY)
            assert engine._state(STRATEGY).day_start_capital == 0.0

            # Next capital fetch re-caches fresh value
            assert engine.get_sizing_capital(16_000.0, STRATEGY) == 16_000.0
