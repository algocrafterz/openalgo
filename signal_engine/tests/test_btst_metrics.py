"""BTST reports a basket result in rupees, not a column of bare percentages.

The EOD summary listed each stock's +/-% and nothing else, so the one question it existed to
answer - did this make or lose money - had to be done in the reader's head across ten rows.
BTST has no position size (it is a manual call), so the honest framing is the one a trader
would actually use: an EQUAL-WEIGHT BASKET, one position per stock, at a stated notional.

Reported: net % per position, that figure in rupees, hit rate, best/worst, and the payoff
shape (average winner against average loser) - which is what says whether an edge exists,
and which a list of percentages hides completely.
"""

import pytest

from signal_engine.analysis.breakingtrade.eod_summary import btst_metrics


def _t(symbol, pct, entry=100.0):
    return {"symbol": symbol, "recommended_on": "2026-09-10", "entry": entry,
            "exit": entry * (1 + pct / 100), "pct": pct}


CAPITAL = 100_000.0   # the STRATEGY's pot, split across the day's names


class TestBasketEconomics:
    def test_net_percent_is_the_average_across_positions(self):
        m = btst_metrics([_t("A", 2.0), _t("B", -1.0)], CAPITAL)
        assert m["net_pct"] == pytest.approx(0.5)

    def test_net_rupees_splits_the_strategy_capital(self):
        """Rs 100,000 across TWO names is Rs 50,000 each: +2% and -1% = +1,000 - 500 = +500."""
        m = btst_metrics([_t("A", 2.0), _t("B", -1.0)], CAPITAL)
        assert m["net_rupees"] == pytest.approx(500.0)

    def test_deployed_is_the_strategy_capital_not_a_multiple_of_it(self):
        """Ten names means smaller positions, not more money at work."""
        m = btst_metrics([_t("A", 1.0), _t("B", 1.0), _t("C", 1.0)], CAPITAL)
        assert m["deployed"] == pytest.approx(100_000.0)
        assert m["per_position"] == pytest.approx(100_000 / 3)

    def test_a_losing_basket_reports_a_negative_net(self):
        m = btst_metrics([_t("A", -1.19), _t("B", -1.04)], CAPITAL)
        assert m["net_rupees"] < 0
        assert m["net_pct"] == pytest.approx(-1.115)


class TestHitRateAndPayoff:
    def test_hit_rate_counts_winners(self):
        m = btst_metrics([_t("A", 1.0), _t("B", -1.0), _t("C", 2.0)], CAPITAL)
        assert m["hit_rate"] == pytest.approx(66.67, abs=0.01)

    def test_average_winner_and_loser_are_separated(self):
        m = btst_metrics([_t("A", 2.0), _t("B", 4.0), _t("C", -1.0)], CAPITAL)
        assert m["avg_win"] == pytest.approx(3.0)
        assert m["avg_loss"] == pytest.approx(-1.0)

    def test_payoff_is_average_win_over_average_loss(self):
        """Below 1.0 means the winners are smaller than the losers - the thing a column of
        percentages never tells you."""
        m = btst_metrics([_t("A", 1.0), _t("B", -2.0)], CAPITAL)
        assert m["payoff"] == pytest.approx(0.5)

    def test_payoff_is_none_when_nothing_lost(self):
        assert btst_metrics([_t("A", 1.0)], CAPITAL)["payoff"] is None

    def test_best_and_worst_are_named(self):
        m = btst_metrics([_t("A", 1.92), _t("B", -1.19), _t("C", 0.5)], CAPITAL)
        assert m["best"]["symbol"] == "A" and m["worst"]["symbol"] == "B"


class TestEdgeCases:
    def test_an_empty_basket_is_all_zeroes_not_a_crash(self):
        m = btst_metrics([], CAPITAL)
        assert m["net_pct"] == 0.0 and m["net_rupees"] == 0.0 and m["best"] is None

    def test_a_flat_position_counts_as_a_loss_not_a_win(self):
        """Zero return after costs is not a win; the existing summary already splits this way."""
        m = btst_metrics([_t("A", 0.0)], CAPITAL)
        assert m["hit_rate"] == 0.0

    def test_per_position_rupees_are_available_for_each_row(self):
        """One name gets the whole pot: 2% of Rs 100,000."""
        m = btst_metrics([_t("A", 2.0)], CAPITAL)
        assert m["rows"][0]["rupees"] == pytest.approx(2000.0)

    def test_the_same_return_on_more_names_earns_less_per_name(self):
        one = btst_metrics([_t("A", 2.0)], CAPITAL)["rows"][0]["rupees"]
        four = btst_metrics([_t(s, 2.0) for s in "ABCD"], CAPITAL)["rows"][0]["rupees"]
        assert four == pytest.approx(one / 4)


class TestOneWatchlistMessagePerDay:
    """The closing scan runs at 14:50 AND 15:10, and each run sent its own watchlist message.
    The paper book now keeps only the FIRST recommendation of the day (one position per
    stock), so a second identical message is noise. A later run whose SELECTION actually
    changed is worth seeing - and says so."""

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path, monkeypatch):
        from signal_engine.analysis.breakingtrade import alerts, store

        monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "bt.db"))
        monkeypatch.setattr(alerts, "send", lambda *a, **k: (True, 1))
        with alerts._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
                created_at TEXT, kind TEXT, symbol TEXT, direction TEXT, scan TEXT,
                message TEXT, delivered INTEGER, message_id INTEGER)""")

    def _wl(self, *symbols):
        import pandas as pd

        return pd.DataFrame([
            {"symbol": s, "price": 100.0, "change_pct": 1.0, "delivery_pct": 0.5,
             "day_type": "Normal"} for s in symbols
        ])

    def test_the_same_list_twice_sends_one_message(self):
        from datetime import datetime

        from signal_engine.analysis.breakingtrade import alerts

        alerts.alert_btst(self._wl("A", "B"), datetime(2026, 9, 11, 14, 50))
        sent_again = alerts.alert_btst(self._wl("A", "B"), datetime(2026, 9, 11, 15, 10))
        assert sent_again == 0

    def test_a_changed_list_is_sent_and_marked_as_revised(self):
        from datetime import datetime

        from signal_engine.analysis.breakingtrade import alerts

        alerts.alert_btst(self._wl("A", "B"), datetime(2026, 9, 11, 14, 50))
        assert alerts.alert_btst(self._wl("A", "C"), datetime(2026, 9, 11, 15, 10)) > 0
        with alerts._connect() as conn:
            latest = conn.execute(
                "SELECT message FROM alerts WHERE kind='btst' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        assert "REVISED" in latest

    def test_the_next_day_sends_normally(self):
        from datetime import datetime

        from signal_engine.analysis.breakingtrade import alerts

        alerts.alert_btst(self._wl("A"), datetime(2026, 9, 11, 14, 50))
        assert alerts.alert_btst(self._wl("A"), datetime(2026, 9, 12, 14, 50)) > 0
