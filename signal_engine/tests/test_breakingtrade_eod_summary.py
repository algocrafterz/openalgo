"""EOD Telegram summaries (eod_summary.py): scoring today's watchlist calls and today's
settled BTST positions against what actually happened, once per day."""

from datetime import datetime

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import alerts, eod_summary, extractor, paper, store


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    monkeypatch.setattr(alerts, "send", lambda text, kind=None: (False, None))


def _store_snapshot(captured_at: str, symbol: str, price: float):
    frame = pd.DataFrame([{"symbol": symbol, "price": price, "day_type": "Normal Var"}])
    store.save_snapshot(
        extractor.Snapshot(
            kind="market_profile",
            captured_at=datetime.strptime(captured_at, "%Y-%m-%d %H:%M"),
            frame=frame,
        )
    )


def _record_watchlist_alert(symbol: str, scan: str, created_at: str):
    # message includes scan so two alerts for the same symbol (different scans) don't collide
    # when the UPDATE below scopes itself to this exact row.
    message = f"BT WATCHLIST call for {symbol} ({scan})"
    alerts.record(
        "intraday_transition", message, symbol=symbol, scan=scan,
        deliver=False, delivered=True,
    )
    with alerts._connect() as conn:
        conn.execute("UPDATE alerts SET created_at = ? WHERE message = ?", (created_at, message))


class TestIntradaySummary:
    def test_no_calls_today_sends_nothing(self):
        assert eod_summary.alert_intraday_eod_summary("2026-09-07", datetime(2026, 9, 7, 15, 5)) is False

    def test_scores_a_long_call_that_worked(self, monkeypatch):
        _store_snapshot("2026-09-07 09:20", "PNBHOUSING", 1166.0)
        _record_watchlist_alert("PNBHOUSING", "Breakaway Above PDH", "2026-09-07 09:20:50")
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": 1182.1)

        sent = eod_summary.alert_intraday_eod_summary("2026-09-07", datetime(2026, 9, 7, 15, 5))

        assert sent is True
        with alerts._connect() as conn:
            message = conn.execute(
                "SELECT message FROM alerts WHERE kind = 'eod_summary' AND scan = 'INTRADAY'"
            ).fetchone()[0]
        assert "PNBHOUSING" in message
        assert "right" in message
        assert "1/1 moved as called" in message

    def test_scores_a_short_call_that_failed(self, monkeypatch):
        _store_snapshot("2026-09-07 09:20", "MARUTI", 12694.0)
        _record_watchlist_alert("MARUTI", "Breakaway Below PDL", "2026-09-07 09:20:50")
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": 12760.0)

        eod_summary.alert_intraday_eod_summary("2026-09-07", datetime(2026, 9, 7, 15, 5))

        with alerts._connect() as conn:
            message = conn.execute(
                "SELECT message FROM alerts WHERE kind = 'eod_summary' AND scan = 'INTRADAY'"
            ).fetchone()[0]
        assert "wrong" in message
        assert "0/1 moved as called" in message

    def test_missing_quote_is_reported_not_skipped(self, monkeypatch):
        _store_snapshot("2026-09-07 09:20", "TCS", 2283.1)
        _record_watchlist_alert("TCS", "Gap-Down Rescue", "2026-09-07 09:20:50")
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": None)

        sent = eod_summary.alert_intraday_eod_summary("2026-09-07", datetime(2026, 9, 7, 15, 5))

        assert sent is True
        with alerts._connect() as conn:
            message = conn.execute(
                "SELECT message FROM alerts WHERE kind = 'eod_summary' AND scan = 'INTRADAY'"
            ).fetchone()[0]
        assert "price unavailable" in message

    def test_only_sends_once_per_day(self, monkeypatch):
        # _already_sent_today compares against created_at's real wall-clock date (record()
        # always stamps datetime.now()), so the dedup check itself must be exercised against
        # today's real date, not a fixed historical one the other tests use freely.
        today = datetime.now().strftime("%Y-%m-%d")
        _store_snapshot(f"{today} 09:20", "TCS", 2283.1)
        _record_watchlist_alert("TCS", "Gap-Down Rescue", f"{today} 09:20:50")
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": 2270.0)

        first = eod_summary.alert_intraday_eod_summary(today, datetime.now())
        second = eod_summary.alert_intraday_eod_summary(today, datetime.now())

        assert first is True
        assert second is False
        with alerts._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE kind = 'eod_summary' AND scan = 'INTRADAY'"
            ).fetchone()[0]
        assert count == 1

    def test_uses_the_first_alert_of_the_day_not_the_last(self, monkeypatch):
        """A symbol re-flagged later the same day (e.g. a second scan) must still be scored
        from its FIRST call, not a later, more favourable price."""
        _store_snapshot("2026-09-07 09:20", "TCS", 2283.1)
        _store_snapshot("2026-09-07 11:00", "TCS", 2250.0)
        _record_watchlist_alert("TCS", "Gap-Down Rescue", "2026-09-07 09:20:50")
        _record_watchlist_alert("TCS", "Live Print in Formation Up", "2026-09-07 11:00:10")
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": 2270.0)

        eod_summary.alert_intraday_eod_summary("2026-09-07", datetime(2026, 9, 7, 15, 5))

        with alerts._connect() as conn:
            message = conn.execute(
                "SELECT message FROM alerts WHERE kind = 'eod_summary' AND scan = 'INTRADAY'"
            ).fetchone()[0]
        assert "2,283.10" in message  # the 09:20 price, not the 11:00 one


class TestBtstSummary:
    def test_no_settlements_today_sends_nothing(self):
        assert eod_summary.alert_btst_eod_summary("2026-09-07") is False

    def test_summarizes_settled_positions(self):
        watchlist = pd.DataFrame([{"symbol": "SWIGGY", "price": 276.1}])
        paper.record_entries(watchlist, datetime(2026, 9, 4, 14, 50))
        _store_snapshot("2026-09-07 14:50", "SWIGGY", 279.1)
        paper.settle_open_trades()

        sent = eod_summary.alert_btst_eod_summary("2026-09-07")

        assert sent is True
        with alerts._connect() as conn:
            message = conn.execute(
                "SELECT message FROM alerts WHERE kind = 'eod_summary' AND scan = 'BTST'"
            ).fetchone()[0]
        assert "SWIGGY" in message
        assert "1 settled, 1/1 winners" in message

    def test_only_sends_once_per_day(self):
        # See the equivalent note in TestIntradaySummary.test_only_sends_once_per_day: the
        # dedup check compares against created_at's real wall-clock date.
        today = datetime.now().strftime("%Y-%m-%d")
        watchlist = pd.DataFrame([{"symbol": "SWIGGY", "price": 276.1}])
        paper.record_entries(watchlist, datetime(2020, 1, 1, 14, 50))
        _store_snapshot(f"{today} 14:50", "SWIGGY", 279.1)
        paper.settle_open_trades()

        first = eod_summary.alert_btst_eod_summary(today)
        second = eod_summary.alert_btst_eod_summary(today)

        assert first is True
        assert second is False
