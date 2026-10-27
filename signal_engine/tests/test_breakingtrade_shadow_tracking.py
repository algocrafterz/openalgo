"""Shadow tracking for BREAKINGTRADE paper signals (2026-10-28).

The range-integrity candidate (E1+E7: trigger bar starting at or after 10:15 and closing beyond
the IB edge) is recorded on every confirmed signal as a flag, without changing what is traded,
so paper results can be split by it. The signal's scan name is recorded too: until now the
alert row stored the strategy name in its `scan` column and left `strategy` empty.
"""

from datetime import datetime

import pytest

from signal_engine.analysis.breakingtrade import alerts, shadow, store, trigger


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    monkeypatch.setattr(alerts, "send", lambda text, kind=None, monospace=False: (False, None))
    monkeypatch.setattr(alerts.review, "trading_mode", lambda: ("analyze", True))
    alerts._mode_cache["checked_at"] = 0.0


def _plan(direction="up", entry=105.0, ib_high=104.0, ib_low=100.0, at="10:30"):
    return trigger.TradePlan(
        symbol="SBIN", direction=direction, day_type="Trend", entry=entry,
        stop=99.0 if direction == "up" else 105.0, targets=[entry + 4, entry + 6, entry + 8],
        split=(0.5, 0.3, 0.2), ib_high=ib_high, ib_low=ib_low, atr=1.0, risk_per_share=6.0,
        triggered_at=datetime.strptime(f"2026-10-28 {at}", "%Y-%m-%d %H:%M"),
    )


class TestRangeIntegrity:
    def test_long_closing_above_the_complete_ib_passes(self):
        plan = _plan(entry=105.0, at="10:30")
        assert plan.entry_vs_ib == pytest.approx(0.25)
        assert plan.range_integrity is True

    def test_long_entered_inside_the_ib_fails(self):
        plan = _plan(entry=103.0, at="10:30")
        assert plan.entry_vs_ib == pytest.approx(-0.25)
        assert plan.range_integrity is False

    def test_trigger_before_the_ib_completes_fails(self):
        assert _plan(entry=105.0, at="10:10").range_integrity is False

    def test_trigger_bar_starting_at_1015_counts_as_complete(self):
        assert _plan(entry=105.0, at="10:15").range_integrity is True

    def test_short_mirrors_below_the_ib_low(self):
        assert _plan(direction="down", entry=99.0, at="11:00").range_integrity is True
        assert _plan(direction="down", entry=101.0, at="11:00").range_integrity is False

    def test_unknown_without_a_trigger_time_or_ib(self):
        plan = _plan()
        plan.triggered_at = None
        assert plan.range_integrity is None
        assert _plan(ib_high=100.0, ib_low=100.0).entry_vs_ib is None


class TestAlertRecordsTheTrackingFields:
    def _row(self):
        with alerts._connect() as conn:
            return conn.execute(
                "SELECT scan, strategy, range_integrity, entry_vs_ib, message FROM alerts"
            ).fetchone()

    def test_scan_strategy_and_flag_are_recorded(self):
        alerts.alert_trade_signal(_plan(), scan_name="Gap-Down Rescue")
        scan, strategy, flag, location, message = self._row()
        assert (scan, strategy, flag) == ("Gap-Down Rescue", "BREAKINGTRADE", 1)
        assert location == pytest.approx(0.25)
        assert "range" not in message.lower()  # the Telegram text is unchanged

    def test_without_a_scan_name_the_scan_column_keeps_the_strategy(self):
        alerts.alert_trade_signal(_plan())
        assert self._row()[0] == "BREAKINGTRADE"

    def test_a_plan_without_the_fields_records_nulls(self):
        from types import SimpleNamespace

        plan = SimpleNamespace(symbol="TCS", direction="up", entry=10.0, stop=9.0,
                               targets=[11.0], triggered_at=None)
        alerts.alert_trade_signal(plan, scan_name="The Runaway")
        assert self._row()[2:4] == (None, None)


class TestShadowSummary:
    def _trade(self, symbol, pnl, entry=100.0, sl=98.0, qty=50):
        return {"strategy": "BREAKINGTRADE", "symbol": symbol, "entry": entry, "sl": sl,
                "quantity": qty, "total_pnl": pnl}

    def test_trades_split_by_flag_and_net_of_costs(self):
        trades = [("2026-10-28", self._trade("SBIN", 300.0)),
                  ("2026-10-28", self._trade("TCS", -100.0)),
                  ("2026-10-28", self._trade("INFY", 50.0))]
        tags = {("BREAKINGTRADE", "SBIN", "2026-10-28"): (1, "The Breakdown"),
                ("BREAKINGTRADE", "TCS", "2026-10-28"): (0, "Gap-Down Rescue")}
        rows = shadow.summarise(trades, tags, cost_of=lambda t: 10.0)
        assert [(r["group"], r["trades"]) for r in rows["strategy"]] == [("BREAKINGTRADE", 3)]
        by = {r["group"]: r for r in rows["range_integrity"]}
        assert by["pass"]["trades"] == 1 and by["pass"]["net"] == pytest.approx(290.0)
        assert by["fail"]["net"] == pytest.approx(-110.0)
        assert by["unknown"]["trades"] == 1  # INFY has no tracked signal
        assert by["pass"]["exp_r"] == pytest.approx(290.0 / 100.0)
        scans = {r["group"]: r["trades"] for r in rows["scan"]}
        assert scans == {"The Breakdown": 1, "Gap-Down Rescue": 1, "unknown": 1}
