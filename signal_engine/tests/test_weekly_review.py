"""Tests for the weekly rollup (signal_engine/analysis/weekly_review.py).

Builds Position objects directly rather than through a trades.db fixture for the pure
aggregation tests - the join itself is ledger.py's job and already covered by
test_analysis_ledger.py. build_weekly_report()'s own DB/snapshot wiring gets one
end-to-end test with a real tmp trades.db, mirroring test_analysis_ledger.py's fixtures.
"""

import json
import sqlite3
from datetime import date

from signal_engine.analysis import weekly_review
from signal_engine.analysis.ledger import Leg, Position

_COLS = (
    "strategy",
    "direction",
    "symbol",
    "entry",
    "sl",
    "tp",
    "quantity",
    "order_id",
    "status",
    "message",
    "signal_time",
    "received_at",
    "executed_at",
    "raw_message",
    "context",
    "fill_price",
)


def _make_db(tmp_path, rows):
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE trades (id INTEGER PRIMARY KEY, {', '.join(_COLS)})")
    conn.executemany(
        f"INSERT INTO trades ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
        [tuple(r.get(c) for c in _COLS) for r in rows],
    )
    conn.commit()
    conn.close()
    return str(path)


def _ev(
    direction,
    strategy="ORB",
    symbol="SBIN",
    entry=100.0,
    sl=98.0,
    tp=104.0,
    qty=10,
    oid="O1",
    status="SUCCESS",
    at="2026-09-15T09:50:00+05:30",
):
    return {
        "strategy": strategy,
        "direction": direction,
        "symbol": symbol,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "quantity": qty,
        "order_id": oid,
        "status": status,
        "message": "ok",
        "signal_time": "",
        "received_at": at,
        "executed_at": at,
        "raw_message": "",
        "context": json.dumps({}),
        "fill_price": None,
    }


def _trade(oid, qty, price):
    return {
        "orderid": oid,
        "quantity": qty,
        "average_price": price,
        "symbol": "SBIN",
        "action": "BUY",
        "timestamp": "09:50:01",
    }


def _winning_position(day, strategy="ORB") -> Position:
    entry = Leg(
        kind="ENTRY",
        at=None,
        signal_price=100.0,
        order_qty=10,
        order_id="E1",
        status="SUCCESS",
        fill_price=100.0,
        fill_qty=10,
    )
    exit_leg = Leg(
        kind="EXIT",
        at=None,
        signal_price=104.0,
        order_qty=10,
        order_id="X1",
        status="SUCCESS",
        reason="TP1",
        fill_price=104.0,
        fill_qty=10,
    )
    return Position(
        strategy=strategy,
        symbol="SBIN",
        day=day,
        direction=1,
        entry=entry,
        exits=[exit_leg],
        signal_sl=98.0,
    )


def _day(
    day,
    is_trading_day=True,
    engine_events=1,
    snapshot_verified=True,
    positions=None,
    declined=None,
    anomaly=None,
):
    return weekly_review.DayResult(
        day=day,
        is_trading_day=is_trading_day,
        engine_events=engine_events,
        snapshot_verified=snapshot_verified,
        positions=positions or [],
        declined=declined or [],
        anomaly=anomaly,
    )


# ---------------------------------------------------------------------------
# Outage / anomaly detection
# ---------------------------------------------------------------------------


class TestOutageDetection:
    def test_trading_day_with_zero_events_is_an_outage(self):
        d = _day(date(2026, 9, 14), engine_events=0, positions=[])
        assert d.is_outage is True

    def test_trading_day_with_events_is_not_an_outage(self):
        d = _day(date(2026, 9, 15), engine_events=5)
        assert d.is_outage is False

    def test_weekend_with_zero_events_is_not_an_outage(self):
        d = _day(date(2026, 9, 13), is_trading_day=False, engine_events=0)
        assert d.is_outage is False


# ---------------------------------------------------------------------------
# Verified vs unverified P&L
# ---------------------------------------------------------------------------


class TestVerifiedUnverifiedSplit:
    def test_verified_day_pnl_counted_in_verified_total(self):
        d = _day(
            date(2026, 9, 15),
            snapshot_verified=True,
            positions=[_winning_position(date(2026, 9, 15))],
        )
        report = weekly_review.WeeklyReport(since=d.day, until=d.day, days=[d])
        assert report.verified_gross_pnl() == 40.0
        assert report.unverified_gross_pnl() == 0.0

    def test_unverified_day_pnl_excluded_from_verified_total(self):
        d = _day(
            date(2026, 9, 16),
            snapshot_verified=False,
            positions=[_winning_position(date(2026, 9, 16))],
        )
        report = weekly_review.WeeklyReport(since=d.day, until=d.day, days=[d])
        assert report.verified_gross_pnl() == 0.0
        assert report.unverified_gross_pnl() == 40.0

    def test_mixed_week_keeps_totals_separate(self):
        verified_day = _day(
            date(2026, 9, 15),
            snapshot_verified=True,
            positions=[_winning_position(date(2026, 9, 15))],
        )
        unverified_day = _day(
            date(2026, 9, 16),
            snapshot_verified=False,
            positions=[_winning_position(date(2026, 9, 16))],
        )
        report = weekly_review.WeeklyReport(
            since=date(2026, 9, 15), until=date(2026, 9, 16), days=[verified_day, unverified_day]
        )
        assert report.verified_gross_pnl() == 40.0
        assert report.unverified_gross_pnl() == 40.0


# ---------------------------------------------------------------------------
# Per-strategy aggregation
# ---------------------------------------------------------------------------


class TestStrategyTable:
    def test_aggregates_trades_wins_and_pnl_per_strategy(self):
        d = _day(
            date(2026, 9, 15),
            snapshot_verified=True,
            positions=[_winning_position(date(2026, 9, 15), strategy="ORB")],
            declined=[{"strategy": "ORB", "message": "[validator:IGNORED] R:R low"}],
        )
        report = weekly_review.WeeklyReport(since=d.day, until=d.day, days=[d])
        table = report.strategy_table()
        b = table[("ORB", True)]
        assert b["trades"] == 1
        assert b["declined"] == 1
        assert b["scored"] == 1
        assert b["wins"] == 1
        assert b["sum_r"] == 2.0
        assert b["gross_pnl"] == 40.0

    def test_declined_reasons_are_tallied_by_stage(self):
        d = _day(
            date(2026, 9, 15),
            declined=[
                {"strategy": "ORB", "message": "[validator:IGNORED] R:R 0.5 below minimum 0.75"},
                {"strategy": "ORB", "message": "[validator:IGNORED] R:R 0.6 below minimum 0.75"},
                {"strategy": "BREAKOUT", "message": "[risk:MAX_OPEN] too many positions"},
            ],
        )
        report = weekly_review.WeeklyReport(since=d.day, until=d.day, days=[d])
        reasons = report.decline_reasons()
        assert reasons["validator:IGNORED"] == 2
        assert reasons["risk:MAX_OPEN"] == 1


# ---------------------------------------------------------------------------
# Plain-language summary
# ---------------------------------------------------------------------------


class TestPlainLanguageSummary:
    def test_zero_trades_week_says_so_plainly(self):
        d = _day(date(2026, 9, 15), engine_events=0, positions=[])
        report = weekly_review.WeeklyReport(since=d.day, until=d.day, days=[d])
        summary = weekly_review.plain_language_summary(report)
        assert "No trades were taken" in summary

    def test_unverified_week_flags_the_number_as_untrusted(self):
        d = _day(
            date(2026, 9, 15),
            snapshot_verified=False,
            positions=[_winning_position(date(2026, 9, 15))],
        )
        report = weekly_review.WeeklyReport(since=d.day, until=d.day, days=[d])
        summary = weekly_review.plain_language_summary(report)
        assert "not a verified number" in summary or "system's own estimate" in summary

    def test_verified_week_states_a_trustworthy_figure(self):
        d = _day(
            date(2026, 9, 15),
            snapshot_verified=True,
            positions=[_winning_position(date(2026, 9, 15))],
        )
        report = weekly_review.WeeklyReport(since=d.day, until=d.day, days=[d])
        summary = weekly_review.plain_language_summary(report)
        assert "can be trusted" in summary
        assert "40" in summary

    def test_known_anomaly_day_is_surfaced_with_its_cause(self):
        d = _day(
            date(2026, 9, 14),
            engine_events=0,
            positions=[],
            anomaly="OpenAlgo unreachable all session.",
        )
        report = weekly_review.WeeklyReport(since=d.day, until=d.day, days=[d])
        summary = weekly_review.plain_language_summary(report)
        assert "OpenAlgo unreachable all session." in summary


# ---------------------------------------------------------------------------
# build_weekly_report end-to-end (real trades.db + snapshot dir)
# ---------------------------------------------------------------------------


class TestBuildWeeklyReport:
    def test_reconciles_events_declines_and_snapshot_verification(self, tmp_path, monkeypatch):
        db = _make_db(
            tmp_path,
            [
                _ev("LONG", oid="E1", at="2026-09-15T09:50:00+05:30"),
                {
                    **_ev("SHORT", oid="D1", at="2026-09-16T10:00:00+05:30"),
                    "status": "DECLINED",
                    "message": "[validator:IGNORED] R:R low",
                },
            ],
        )
        monkeypatch.setattr("signal_engine.analysis.ledger._DB_PATH", db)
        snap_dir = tmp_path / "tradebook"
        snap_dir.mkdir()
        (snap_dir / "tradebook_2026-09-15.json").write_text(json.dumps([_trade("E1", 10, 100.5)]))
        monkeypatch.setattr(weekly_review, "SNAP_DIR", str(snap_dir))
        monkeypatch.setattr("signal_engine.analysis.__main__.SNAP_DIR", str(snap_dir))

        report = weekly_review.build_weekly_report(date(2026, 9, 14), date(2026, 9, 18))

        by_day = {d.day: d for d in report.days}
        assert by_day[date(2026, 9, 14)].is_outage is True
        assert by_day[date(2026, 9, 15)].snapshot_verified is True
        assert len(by_day[date(2026, 9, 15)].positions) == 1
        assert by_day[date(2026, 9, 16)].snapshot_verified is False
        assert len(by_day[date(2026, 9, 16)].declined) == 1
        assert by_day[date(2026, 9, 17)].is_trading_day is True
        assert by_day[date(2026, 9, 17)].engine_events == 0
