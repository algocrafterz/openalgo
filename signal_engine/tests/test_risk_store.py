"""Tests for persistent risk counter storage."""

import sqlite3
from datetime import date

import pytest

from signal_engine.risk_store import RiskStore


@pytest.fixture
def store(tmp_path):
    """Create a RiskStore with a temp DB."""
    db_path = str(tmp_path / "risk.db")
    return RiskStore(db_path)


class TestSaveAndLoad:
    def test_save_and_load_counters(self, store):
        store.save("live", date(2026, 3, 10), trades_today=3, daily_loss=500.0,
                    open_positions=2)
        row = store.load("live", date(2026, 3, 10))
        assert row["trades_today"] == 3
        assert row["daily_loss"] == 500.0
        assert row["open_positions"] == 2

    def test_load_missing_returns_zeros(self, store):
        row = store.load("live", date(2026, 3, 10))
        assert row["trades_today"] == 0
        assert row["daily_loss"] == 0.0
        assert row["open_positions"] == 0

    def test_save_overwrites_existing(self, store):
        store.save("live", date(2026, 3, 10), trades_today=1, daily_loss=100.0,
                    open_positions=1)
        store.save("live", date(2026, 3, 10), trades_today=3, daily_loss=800.0,
                    open_positions=2)
        row = store.load("live", date(2026, 3, 10))
        assert row["trades_today"] == 3
        assert row["daily_loss"] == 800.0


class TestModeSeparation:
    def test_sandbox_and_live_isolated(self, store):
        store.save("live", date(2026, 3, 10), trades_today=5, daily_loss=1000.0,
                    open_positions=3)
        store.save("sandbox", date(2026, 3, 10), trades_today=2, daily_loss=200.0,
                    open_positions=1)

        live = store.load("live", date(2026, 3, 10))
        sandbox = store.load("sandbox", date(2026, 3, 10))

        assert live["trades_today"] == 5
        assert sandbox["trades_today"] == 2
        assert live["daily_loss"] == 1000.0
        assert sandbox["daily_loss"] == 200.0


class TestWeeklyMonthlyAggregation:
    def test_weekly_loss(self, store):
        # Mon-Fri losses
        store.save("live", date(2026, 3, 9), trades_today=2, daily_loss=300.0,
                    open_positions=0)
        store.save("live", date(2026, 3, 10), trades_today=3, daily_loss=500.0,
                    open_positions=1)
        weekly = store.weekly_loss("live", date(2026, 3, 10))
        assert weekly == 800.0

    def test_monthly_loss(self, store):
        store.save("live", date(2026, 3, 1), trades_today=1, daily_loss=100.0,
                    open_positions=0)
        store.save("live", date(2026, 3, 5), trades_today=2, daily_loss=400.0,
                    open_positions=0)
        store.save("live", date(2026, 3, 10), trades_today=1, daily_loss=200.0,
                    open_positions=1)
        monthly = store.monthly_loss("live", date(2026, 3, 10))
        assert monthly == 700.0

    def test_weekly_loss_excludes_previous_week(self, store):
        store.save("live", date(2026, 3, 2), trades_today=1, daily_loss=999.0,
                    open_positions=0)  # Previous week (Mon Mar 2)
        store.save("live", date(2026, 3, 9), trades_today=1, daily_loss=100.0,
                    open_positions=0)  # This week (Mon Mar 9)
        weekly = store.weekly_loss("live", date(2026, 3, 10))
        assert weekly == 100.0
