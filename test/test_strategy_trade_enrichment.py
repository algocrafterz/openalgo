"""Enrichment of closed trades for the Performance page: estimated charges,
R multiples (from the signal engine's recorded stop) and per-symbol P&L.

Nothing here may invent data: a trade with no matching or an ambiguous signal
gets no R, and coverage is reported so the page can say how many trades the
figure is based on.

Run with: uv run pytest test/test_strategy_trade_enrichment.py -v
"""

import sqlite3

import pytest

from services.strategy_trade_enrichment import (
    enrich_trades,
    estimate_trade_cost,
    summarize_enrichment,
    symbol_breakdown,
)


def _trade(**kw):
    base = {
        "strategy": "ORB",
        "symbol": "INFY",
        "exchange": "NSE",
        "product": "MIS",
        "mode": "analyze",
        "direction": "SHORT",
        "closed_quantity": 45.0,
        "entry_price": 986.0,
        "exit_price": 976.0,
        "realized_pnl": 450.0,
        "trade_date": "2026-09-29",
        "closed_at": "2026-09-29T10:40:00",
    }
    base.update(kw)
    return base


@pytest.fixture
def signals_db(tmp_path):
    path = tmp_path / "trades.db"
    con = sqlite3.connect(path)
    con.execute(
        "create table trades (id integer primary key, strategy text, direction text, "
        "symbol text, entry real, sl real, status text, trade_mode text, executed_at text)"
    )
    con.commit()
    con.close()
    return path


def _signal(path, strategy, symbol, direction, entry, sl, when, status="SUCCESS", mode="analyze"):
    con = sqlite3.connect(path)
    con.execute(
        "insert into trades (strategy,direction,symbol,entry,sl,status,trade_mode,executed_at) "
        "values (?,?,?,?,?,?,?,?)",
        (strategy, direction, symbol, entry, sl, status, mode, when),
    )
    con.commit()
    con.close()


class TestCost:
    def test_intraday_round_trip_is_positive_and_small(self):
        cost = estimate_trade_cost(_trade())
        assert cost is not None
        # Hand-checked: brokerage 2 x 13.31 x 1.18 = 31.4, STT 11.1, exchange fee
        # 2.6, stamp 1.3 -> ~46.9 on a 44,370 notional (~10.6 bps; the Rs 20
        # brokerage cap only bites on larger orders, giving ~6 bps there).
        assert cost == pytest.approx(46.86, abs=0.5)

    def test_stt_applies_to_the_sell_leg_whichever_way_the_trade_went(self):
        long_cost = estimate_trade_cost(
            _trade(direction="LONG", entry_price=100.0, exit_price=110.0)
        )
        short_cost = estimate_trade_cost(
            _trade(direction="SHORT", entry_price=110.0, exit_price=100.0)
        )
        # same two legs (buy 100 / sell 110): identical charges
        assert long_cost == short_cost

    def test_unmodelled_exchange_or_product_has_no_cost(self):
        assert estimate_trade_cost(_trade(exchange="NFO", product="NRML")) is None
        assert estimate_trade_cost(_trade(exchange="MCX")) is None


class TestRMultiple:
    def test_matched_signal_gives_r(self, signals_db):
        _signal(signals_db, "ORB", "INFY", "SHORT", 986.6, 1006.72, "2026-09-29T09:55:06")
        [t] = enrich_trades([_trade()], "2026-09-29", "2026-09-29", db_path=signals_db)
        # risk/share = 20.12, qty 45 -> 905.4 ; pnl 450 -> ~0.497R
        assert t["risk_amount"] == pytest.approx(20.12 * 45, abs=0.01)
        assert t["r_multiple"] == pytest.approx(450 / (20.12 * 45), abs=0.001)

    def test_no_matching_signal_means_no_r(self, signals_db):
        [t] = enrich_trades([_trade()], "2026-09-29", "2026-09-29", db_path=signals_db)
        assert t["r_multiple"] is None and t["risk_amount"] is None

    def test_ambiguous_signals_mean_no_r(self, signals_db):
        _signal(signals_db, "ORB", "INFY", "SHORT", 986.6, 1006.72, "2026-09-29T09:55:06")
        _signal(signals_db, "ORB", "INFY", "SHORT", 987.0, 1012.00, "2026-09-29T11:00:00")
        [t] = enrich_trades([_trade()], "2026-09-29", "2026-09-29", db_path=signals_db)
        assert t["r_multiple"] is None

    def test_identical_duplicate_signals_are_not_ambiguous(self, signals_db):
        for when in ("2026-09-29T09:55:06", "2026-09-29T09:55:09"):
            _signal(signals_db, "ORB", "INFY", "SHORT", 986.6, 1006.72, when)
        [t] = enrich_trades([_trade()], "2026-09-29", "2026-09-29", db_path=signals_db)
        assert t["r_multiple"] is not None

    @pytest.mark.parametrize(
        "kw",
        [
            {"strategy": "BREAKOUT"},
            {"symbol": "TCS"},
            {"direction": "LONG"},
            {"status": "DECLINED"},
            {"mode": "live"},
            {"when": "2026-09-28T09:55:06"},
            {"entry": 1100.0},  # far from the ledger entry price
        ],
    )
    def test_non_matching_signal_is_ignored(self, signals_db, kw):
        args = {
            "strategy": "ORB", "symbol": "INFY", "direction": "SHORT",
            "entry": 986.6, "sl": 1006.72, "when": "2026-09-29T09:55:06",
        }
        args.update(kw)
        _signal(
            signals_db, args["strategy"], args["symbol"], args["direction"], args["entry"],
            args["sl"], args["when"], status=kw.get("status", "SUCCESS"),
            mode=kw.get("mode", "analyze"),
        )
        [t] = enrich_trades([_trade()], "2026-09-29", "2026-09-29", db_path=signals_db)
        assert t["r_multiple"] is None

    def test_zero_or_missing_stop_is_not_used(self, signals_db):
        _signal(signals_db, "ORB", "INFY", "SHORT", 986.6, 0, "2026-09-29T09:55:06")
        [t] = enrich_trades([_trade()], "2026-09-29", "2026-09-29", db_path=signals_db)
        assert t["r_multiple"] is None

    def test_missing_signal_db_degrades_to_no_r(self, tmp_path):
        [t] = enrich_trades(
            [_trade()], "2026-09-29", "2026-09-29", db_path=tmp_path / "absent.db"
        )
        assert t["r_multiple"] is None
        assert t["cost"] is not None

    def test_input_trades_are_not_mutated(self, signals_db):
        original = _trade()
        enrich_trades([original], "2026-09-29", "2026-09-29", db_path=signals_db)
        assert "r_multiple" not in original


class TestSummary:
    def test_expectancy_r_uses_only_covered_trades_and_reports_coverage(self):
        enriched = [
            {**_trade(realized_pnl=100.0), "cost": 10.0, "risk_amount": 100.0, "r_multiple": 1.0},
            {**_trade(realized_pnl=-100.0), "cost": 10.0, "risk_amount": 100.0, "r_multiple": -1.0},
            {**_trade(realized_pnl=300.0), "cost": 10.0, "risk_amount": 100.0, "r_multiple": 3.0},
            {**_trade(realized_pnl=999.0), "cost": 10.0, "risk_amount": None, "r_multiple": None},
        ]
        s = summarize_enrichment(enriched)
        assert s["expectancy_r"] == pytest.approx(1.0)
        assert s["total_r"] == pytest.approx(3.0)
        assert s["r_covered"] == 3 and s["r_total"] == 4
        assert s["est_costs"] == pytest.approx(40.0)
        assert s["net_after_costs"] == pytest.approx(100.0 - 100.0 + 300.0 + 999.0 - 40.0)

    def test_no_r_coverage_gives_none_not_zero(self):
        s = summarize_enrichment(
            [{**_trade(), "cost": 5.0, "risk_amount": None, "r_multiple": None}]
        )
        assert s["expectancy_r"] is None and s["total_r"] is None

    def test_any_unmodelled_cost_makes_net_after_costs_unknown(self):
        s = summarize_enrichment(
            [
                {**_trade(realized_pnl=50.0), "cost": 5.0, "risk_amount": None, "r_multiple": None},
                {**_trade(realized_pnl=50.0), "cost": None, "risk_amount": None, "r_multiple": None},
            ]
        )
        assert s["net_after_costs"] is None and s["est_costs"] is None

    def test_empty(self):
        s = summarize_enrichment([])
        assert s["r_total"] == 0 and s["expectancy_r"] is None


class TestSymbolBreakdown:
    def test_groups_and_sorts_by_net_pnl(self):
        rows = symbol_breakdown(
            [
                _trade(symbol="A", realized_pnl=100.0),
                _trade(symbol="A", realized_pnl=-40.0),
                _trade(symbol="B", realized_pnl=-200.0),
                _trade(symbol="C", realized_pnl=10.0),
            ]
        )
        assert [r["symbol"] for r in rows] == ["A", "C", "B"]
        a = rows[0]
        assert a["net_pnl"] == 60.0 and a["trades"] == 2 and a["win_rate"] == 50.0

    def test_empty(self):
        assert symbol_breakdown([]) == []
