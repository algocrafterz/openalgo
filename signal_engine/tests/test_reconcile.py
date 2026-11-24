"""The canary that would have caught 2026-09-11's +986.41 against a real +119.31."""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import reconcile
from signal_engine.reconcile import Reconciliation


def _trades(*pnls):
    return [{"total_pnl": p} for p in pnls]


class TestComparison:
    def test_agreement_within_tolerance(self):
        r = Reconciliation(119.31, 119.31, 4, "analyze")
        assert r.agrees and r.difference == pytest.approx(0.0)

    def test_rounding_noise_is_not_a_mismatch(self):
        assert Reconciliation(119.31, 119.60, 4, "analyze").agrees

    def test_the_real_2026_09_11_gap_is_a_mismatch(self):
        r = Reconciliation(986.41, 119.31, 4, "analyze")
        assert not r.agrees
        assert r.difference == pytest.approx(867.10)

    def test_an_unreadable_broker_figure_is_not_a_mismatch(self):
        """Unknown must never be reported as disagreement."""
        r = Reconciliation(119.31, None, 4, "analyze")
        assert r.agrees and not r.comparable

    def test_a_flat_day_agrees(self):
        assert Reconciliation(0.0, 0.0, 0, "analyze").agrees


class TestSummaryText:
    def test_a_mismatch_names_both_figures_and_the_difference(self):
        text = Reconciliation(986.41, 119.31, 4, "analyze").summary()
        assert "986.41" in text and "119.31" in text and "867.10" in text
        assert "MISMATCH" in text

    def test_agreement_reads_as_agreement(self):
        assert "Reconciled" in Reconciliation(119.31, 119.31, 4, "analyze").summary()

    def test_an_unreadable_broker_says_skipped(self):
        assert "SKIPPED" in Reconciliation(119.31, None, 4, "analyze").summary()


class TestReconcileDay:
    @pytest.mark.asyncio
    async def test_sums_the_days_trades_against_the_broker(self):
        with patch("signal_engine.db.fetch_day_trades", return_value=_trades(50.0, -20.0)), \
             patch("signal_engine.api_client.fetch_realised_pnl", AsyncMock(return_value=30.0)):
            r = await reconcile.reconcile_day("analyze")
        assert (r.engine_pnl, r.broker_pnl, r.trades) == (30.0, 30.0, 2)
        assert r.agrees

    @pytest.mark.asyncio
    async def test_an_unreadable_database_does_not_raise(self):
        with patch("signal_engine.db.fetch_day_trades", side_effect=RuntimeError("locked")):
            r = await reconcile.reconcile_day("analyze")
        assert not r.comparable

    @pytest.mark.asyncio
    async def test_an_unreadable_broker_does_not_raise(self):
        with patch("signal_engine.db.fetch_day_trades", return_value=_trades(50.0)), \
             patch("signal_engine.api_client.fetch_realised_pnl",
                   AsyncMock(side_effect=RuntimeError("down"))):
            r = await reconcile.reconcile_day("analyze")
        assert not r.comparable and r.agrees


class TestAlerting:
    @pytest.mark.asyncio
    async def test_a_live_mismatch_is_logged_critical(self, logged):
        """Live-money mismatch: the broker's own real P&L is trustworthy, so a disagreement
        is a genuine signal_engine data-integrity concern - keep this CRITICAL."""
        with patch("signal_engine.db.fetch_day_trades", return_value=_trades(986.41)), \
             patch("signal_engine.api_client.fetch_realised_pnl", AsyncMock(return_value=119.31)):
            r = await reconcile.check_and_alert("live")
        assert not r.agrees
        assert len(logged.messages("CRITICAL")) == 1

    @pytest.mark.asyncio
    async def test_an_analyze_mode_mismatch_is_not_logged_critical(self, logged):
        """2026-09-21: OpenAlgo's own analyze-mode sandbox is confirmed to undercount
        today_realized_pnl on a multi-leg close - a mismatch here means the SANDBOX is
        wrong, not signal_engine (see Reconciliation.is_known_sandbox_limitation). It must
        not be logged as a CRITICAL "day's numbers are NOT trustworthy" claim."""
        with patch("signal_engine.db.fetch_day_trades", return_value=_trades(986.41)), \
             patch("signal_engine.api_client.fetch_realised_pnl", AsyncMock(return_value=119.31)):
            r = await reconcile.check_and_alert("analyze")
        assert logged.messages("CRITICAL") == []
        assert logged.has("known OpenAlgo sandbox limitation", "WARNING")
        assert not r.agrees
        assert r.is_known_sandbox_limitation

    @pytest.mark.asyncio
    async def test_agreement_logs_nothing_above_info(self, logged):
        with patch("signal_engine.db.fetch_day_trades", return_value=_trades(119.31)), \
             patch("signal_engine.api_client.fetch_realised_pnl", AsyncMock(return_value=119.31)):
            await reconcile.check_and_alert("analyze")
        assert logged.messages("WARNING") == []
