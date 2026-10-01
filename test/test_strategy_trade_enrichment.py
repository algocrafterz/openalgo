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
    estimate_trade_costs,
    summarize_costs,
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


class TestLegAwareCosts:
    """A position closed in several legs sent ONE entry order, not one per leg.

    2026-10-01: the page charged every closed-trade row a full entry+exit pair, so partial
    exits paid brokerage on the same entry again (Rs 5,734 estimated vs Rs 5,574 from the
    real order count, 2.9% high).
    """

    def _legs(self):
        # HDFCBANK-style: one entry, exits in two pieces
        return [
            _trade(direction="LONG", closed_quantity=50.0, entry_price=100.0, exit_price=102.0,
                   realized_pnl=100.0, closed_at="2026-09-29T11:00:00"),
            _trade(direction="LONG", closed_quantity=150.0, entry_price=100.0, exit_price=101.0,
                   realized_pnl=150.0, closed_at="2026-09-29T12:00:00"),
        ]

    def test_first_leg_pays_entry_and_exit_later_legs_pay_exit_only(self):
        legs = self._legs()
        costs = estimate_trade_costs(legs)
        assert costs[0] == pytest.approx(estimate_trade_cost(legs[0]))
        entry_order_brokerage_with_gst = min(100.0 * 150 * 0.0003, 20.0) * 1.18
        assert costs[1] == pytest.approx(
            estimate_trade_cost(legs[1]) - entry_order_brokerage_with_gst, abs=0.01
        )
        assert costs[1] < estimate_trade_cost(legs[1])

    def test_leg_order_follows_closing_time_not_list_order(self):
        legs = self._legs()
        reordered = estimate_trade_costs(list(reversed(legs)))
        forward = estimate_trade_costs(legs)
        assert reordered == [forward[1], forward[0]]

    def test_separate_round_trips_each_pay_in_full(self):
        a = _trade(entry_price=100.0, closed_at="2026-09-29T10:00:00")
        b = _trade(entry_price=105.0, closed_at="2026-09-29T11:00:00")
        costs = estimate_trade_costs([a, b])
        assert costs == [estimate_trade_cost(a), estimate_trade_cost(b)]

    def test_different_strategies_on_one_symbol_are_not_one_position(self):
        a = _trade(strategy="A", closed_at="2026-09-29T10:00:00")
        b = _trade(strategy="B", closed_at="2026-09-29T11:00:00")
        assert estimate_trade_costs([a, b]) == [estimate_trade_cost(a), estimate_trade_cost(b)]

    def test_unmodelled_instrument_stays_none(self):
        assert estimate_trade_costs([_trade(exchange="MCX")]) == [None]

    def test_enrich_trades_uses_the_leg_aware_cost(self, signals_db):
        legs = self._legs()
        enriched = enrich_trades(legs, "2026-09-29", "2026-09-29", db_path=signals_db)
        assert [t["cost"] for t in enriched] == pytest.approx(
            [round(c, 2) for c in estimate_trade_costs(legs)]
        )


class TestChargeDrag:
    """Charges as a share of the profit they eat, so the cost of trading is visible."""

    def _enriched(self, pnls, cost=10.0, **kw):
        return [{**_trade(realized_pnl=p, **kw), "cost": cost, "risk_amount": None, "r_multiple": None}
                for p in pnls]

    def test_percent_of_gross_profit(self):
        s = summarize_enrichment(self._enriched([100.0, 300.0]))
        assert s["est_costs"] == pytest.approx(20.0)
        assert s["charges_pct_of_gross"] == pytest.approx(5.0)  # 20 / 400

    def test_charges_can_exceed_profit(self):
        s = summarize_enrichment(self._enriched([10.0, 5.0], cost=10.0))
        assert s["charges_pct_of_gross"] == pytest.approx(133.33, abs=0.01)
        assert s["net_after_costs"] < 0

    def test_no_percent_when_there_is_no_gross_profit(self):
        assert summarize_enrichment(self._enriched([-50.0, 20.0]))["charges_pct_of_gross"] is None
        assert summarize_enrichment(self._enriched([0.0]))["charges_pct_of_gross"] is None

    def test_turnover_share_and_per_trade_average(self):
        enriched = self._enriched([100.0, 100.0], cost=5.0, closed_quantity=10.0,
                                  entry_price=100.0, exit_price=110.0)
        s = summarize_enrichment(enriched)
        assert s["turnover"] == pytest.approx(2 * (1000.0 + 1100.0))
        assert s["charges_pct_of_turnover"] == pytest.approx(100 * 10.0 / 4200.0, abs=1e-3)
        assert s["avg_charge_per_trade"] == pytest.approx(5.0)

    def test_unknown_cost_leaves_every_charge_metric_unknown(self):
        enriched = self._enriched([100.0]) + [{**_trade(), "cost": None, "risk_amount": None,
                                               "r_multiple": None}]
        s = summarize_enrichment(enriched)
        assert s["charges_pct_of_gross"] is None and s["charges_pct_of_turnover"] is None
        assert s["avg_charge_per_trade"] is None

    def test_empty(self):
        s = summarize_enrichment([])
        assert s["charges_pct_of_gross"] is None and s["turnover"] == 0


class TestSummarizeCosts:
    """The comparison table needs charges per strategy without the signal-DB R lookup."""

    def test_matches_the_enrichment_totals(self):
        trades = [
            _trade(realized_pnl=200.0, closed_at="2026-09-29T10:00:00"),
            _trade(realized_pnl=-50.0, entry_price=990.0, closed_at="2026-09-29T11:00:00"),
        ]
        s = summarize_costs(trades)
        full = summarize_enrichment(
            [{**t, "cost": c, "risk_amount": None, "r_multiple": None}
             for t, c in zip(trades, estimate_trade_costs(trades), strict=True)]
        )
        for key in ("est_costs", "net_after_costs", "charges_pct_of_gross", "turnover"):
            assert s[key] == full[key]


class TestBrokerAwareCosts:
    """Charges follow the broker the trade was placed through (live or paper)."""

    def test_flattrade_trade_pays_no_brokerage_unlike_the_default(self):
        flat = estimate_trade_cost(_trade(broker="flattrade"))
        base = estimate_trade_cost(_trade())  # no broker recorded -> assumed default
        brokerage = min(986.0 * 45 * 0.0003, 20.0) + min(976.0 * 45 * 0.0003, 20.0)
        assert base - flat == pytest.approx(brokerage * 1.18, abs=0.01)

    def test_unrecognised_broker_is_priced_like_the_default(self):
        assert estimate_trade_cost(_trade(broker="somebroker")) == estimate_trade_cost(_trade())

    def test_dp_charge_applies_to_a_flattrade_delivery_sell_only(self):
        sell = estimate_trade_cost(
            _trade(product="CNC", direction="LONG", broker="flattrade", entry_price=100.0,
                   exit_price=100.0, closed_quantity=10.0)
        )
        no_dp = estimate_trade_cost(
            _trade(product="CNC", direction="LONG", broker="somebroker", entry_price=100.0,
                   exit_price=100.0, closed_quantity=10.0)
        )
        assert sell - no_dp == pytest.approx(20.0 * 1.18, abs=0.01)
        short = estimate_trade_cost(
            _trade(product="CNC", direction="SHORT", broker="flattrade", entry_price=100.0,
                   exit_price=100.0, closed_quantity=10.0)
        )
        assert short == pytest.approx(no_dp, abs=0.01)  # no debit on a cover

    def test_dp_charge_is_taken_once_across_exit_legs(self):
        legs = [
            _trade(product="CNC", direction="LONG", broker="flattrade", entry_price=100.0,
                   exit_price=102.0, closed_quantity=10.0, closed_at="2026-09-29T11:00:00"),
            _trade(product="CNC", direction="LONG", broker="flattrade", entry_price=100.0,
                   exit_price=103.0, closed_quantity=10.0, closed_at="2026-09-29T12:00:00"),
        ]
        costs = estimate_trade_costs(legs)
        assert costs[0] - costs[1] > 20.0 * 1.18 - 0.5  # second leg carries no DP

    def test_leg_discount_uses_the_brokers_own_entry_order_charge(self):
        legs = [
            _trade(direction="LONG", broker="flattrade", closed_quantity=50.0, entry_price=100.0,
                   exit_price=102.0, closed_at="2026-09-29T11:00:00"),
            _trade(direction="LONG", broker="flattrade", closed_quantity=150.0, entry_price=100.0,
                   exit_price=101.0, closed_at="2026-09-29T12:00:00"),
        ]
        costs = estimate_trade_costs(legs)
        # no brokerage at this broker, so nothing to take off the later leg
        assert costs[1] == pytest.approx(estimate_trade_cost(legs[1]), abs=0.01)

    def test_summary_isolates_each_broker(self):
        trades = [
            _trade(broker="flattrade", realized_pnl=400.0),
            _trade(broker="flattrade", realized_pnl=-100.0),
            _trade(broker="somebroker", realized_pnl=300.0),
        ]
        s = summarize_costs(trades)
        by = {b["broker"]: b for b in s["by_broker"]}
        assert set(by) == {"flattrade", "default"}
        assert by["flattrade"]["trades"] == 2 and by["default"]["trades"] == 1
        assert by["flattrade"]["gross"] == pytest.approx(300.0)
        assert by["flattrade"]["assumed"] is False and by["default"]["assumed"] is True
        assert sum(b["est_costs"] for b in s["by_broker"]) == pytest.approx(s["est_costs"], abs=0.02)
        assert by["flattrade"]["label"] == "Flattrade"

    def test_assumed_flag_is_set_when_any_trade_used_a_stand_in_schedule(self):
        assert summarize_costs([_trade(broker="flattrade")])["fee_basis_assumed"] is False
        assert summarize_costs([_trade()])["fee_basis_assumed"] is True
        mixed = summarize_costs([_trade(broker="flattrade"), _trade()])
        assert mixed["fee_basis_assumed"] is True

    def test_enriched_summary_carries_the_broker_split_too(self):
        enriched = [{**_trade(broker="flattrade"), "cost": 5.0, "risk_amount": None, "r_multiple": None}]
        s = summarize_enrichment(enriched)
        assert s["by_broker"][0]["broker"] == "flattrade"


class TestFeeAttention:
    def test_verified_broker_has_no_attention_flag(self):
        row = summarize_costs([_trade(broker="flattrade")])["by_broker"][0]
        assert row["attention"] is None

    def test_unmodelled_broker_carries_a_named_warning(self):
        row = summarize_costs([_trade(broker="newbroker")])["by_broker"][0]
        assert row["assumed"] is True and "newbroker" in row["attention"]

    def test_unrecorded_broker_says_so(self):
        row = summarize_costs([_trade()])["by_broker"][0]
        assert "not recorded" in row["attention"]
