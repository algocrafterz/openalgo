"""The daily check that the Strategy P&L / Performance pages agree with the broker's fills."""

import json
import sqlite3

import pytest

from signal_engine.analysis import pages_vs_fills as pvf
from signal_engine.analysis.pages_vs_fills import Fill, compare, realised_from_fills

DAY = "2026-10-01"


def F(strategy, symbol, action, qty, price, product="MIS"):
    return Fill(strategy, symbol, product, action, qty, price)


def K(strategy, symbol, product="MIS"):
    return (strategy, symbol, product)


class TestRealisedFromFills:
    def test_short_round_trip(self):
        st = realised_from_fills([F("A", "X", "SELL", 100, 50.0), F("A", "X", "BUY", 100, 48.0)])
        assert st[K("A", "X")].realised == pytest.approx(200.0)
        assert st[K("A", "X")].net_qty == 0

    def test_average_cost_across_two_entries_and_a_partial_close(self):
        st = realised_from_fills(
            [
                F("A", "X", "BUY", 10, 100.0),
                F("A", "X", "BUY", 30, 104.0),  # avg 103
                F("A", "X", "SELL", 20, 108.0),  # (108-103)*20
            ]
        )
        assert st[K("A", "X")].realised == pytest.approx(100.0)
        assert st[K("A", "X")].net_qty == 20

    def test_strategies_on_one_symbol_are_kept_apart(self):
        st = realised_from_fills(
            [
                F("PINE", "TCS", "BUY", 134, 2091.0),
                F("PY", "TCS", "BUY", 107, 2091.0),
                F("PINE", "TCS", "SELL", 134, 2090.4),
            ]
        )
        assert st[K("PINE", "TCS")].realised == pytest.approx(-80.4)
        assert st[K("PY", "TCS")].net_qty == 107

    def test_flip_through_zero_reprices_the_remainder(self):
        st = realised_from_fills([F("A", "X", "BUY", 10, 100.0), F("A", "X", "SELL", 25, 110.0)])
        assert st[K("A", "X")].realised == pytest.approx(100.0)
        assert st[K("A", "X")].net_qty == -15

    def test_untagged_close_goes_to_the_strategy_holding_the_symbol(self):
        """The 14:45 time exit carries no strategy tag."""
        un = []
        st = realised_from_fills(
            [F("A", "X", "BUY", 10, 100.0), F("", "X", "SELL", 10, 103.0)], unattributed=un
        )
        assert st[K("A", "X")].realised == pytest.approx(30.0)
        assert st[K("A", "X")].net_qty == 0
        assert un == []

    def test_untagged_close_is_shared_by_each_holders_own_quantity(self):
        st = realised_from_fills(
            [
                F("A", "X", "BUY", 10, 100.0),
                F("B", "X", "BUY", 5, 102.0),
                F("", "X", "SELL", 15, 101.0),
            ]
        )
        assert st[K("A", "X")].realised == pytest.approx(10.0)
        assert st[K("B", "X")].realised == pytest.approx(-5.0)

    def test_untagged_fill_that_nothing_holds_is_reported(self):
        un = []
        realised_from_fills([F("", "X", "SELL", 10, 100.0)], unattributed=un)
        assert len(un) == 1 and un[0].quantity == 10


class TestCompare:
    def test_agreement_passes(self):
        fills = realised_from_fills([F("A", "X", "BUY", 10, 100.0), F("A", "X", "SELL", 10, 105.0)])
        res = compare(DAY, "analyze", fills, {K("A", "X"): 50.0}, {K("A", "X"): 0.0})
        assert res.passed
        assert res.by_strategy["A"] == (50.0, 50.0)

    def test_page_pnl_that_no_fill_backs_fails(self):
        fills = realised_from_fills([F("A", "X", "BUY", 10, 100.0), F("A", "X", "SELL", 10, 105.0)])
        res = compare(DAY, "analyze", fills, {K("A", "X"): 80.0}, {})
        assert not res.passed
        assert "page realised 80.0 vs fills 50.0" in res.failures[0]

    def test_within_tolerance_passes(self):
        fills = realised_from_fills([F("A", "X", "BUY", 3, 100.0), F("A", "X", "SELL", 3, 101.0)])
        assert compare(DAY, "analyze", fills, {K("A", "X"): 3.4}, {}).passed

    def test_2026_10_01_phantom_adaniports_leg_is_caught(self):
        """Five duplicate stops filled; the account held a long the page did not show."""
        fills = realised_from_fills(
            [F("PY", "ADANIPORTS", "BUY", 171, 1768.4)] * 5
            + [F("PY", "ADANIPORTS", "SELL", 171, 1762.5)]
        )
        res = compare(DAY, "analyze", fills, {}, {K("PY", "ADANIPORTS"): 0.0})
        assert not res.passed
        assert any("missing open leg" in f for f in res.failures)

    def test_page_holds_a_position_the_fills_do_not(self):
        res = compare(DAY, "analyze", {}, {}, {K("A", "X"): 9.0})
        assert any("phantom open leg" in f for f in res.failures)

    def test_open_position_agreeing_on_both_sides_passes(self):
        fills = realised_from_fills([F("A", "X", "BUY", 10, 100.0)])
        assert compare(DAY, "analyze", fills, {}, {K("A", "X"): 10.0}).passed

    def test_carried_cnc_position_is_skipped_not_failed(self):
        fills = realised_from_fills([F("A", "X", "SELL", 10, 120.0, "CNC")])
        res = compare(DAY, "analyze", fills, {K("A", "X", "CNC"): 70.0}, {K("A", "X", "CNC"): 0.0})
        # page start = 0 - (-10) = 10 carried in; the day's fills alone cannot price it
        assert res.passed
        assert res.skipped and "carried in" in res.skipped[0]

    def test_a_mis_leg_that_did_not_start_flat_fails(self):
        fills = realised_from_fills([F("A", "X", "SELL", 10, 120.0)])
        res = compare(DAY, "analyze", fills, {}, {K("A", "X"): 4.0})
        assert not res.passed

    def test_reviewed_exception_is_exempt_and_visible(self):
        fills = realised_from_fills([F("A", "X", "BUY", 10, 100.0)])
        res = compare(DAY, "analyze", fills, {}, {}, exempt={("A", "X"): "stray order, reviewed"})
        assert res.passed
        assert "stray order, reviewed" in res.exempt[0]
        assert "exempt" in res.report()

    def test_an_unlisted_mismatch_still_fails_alongside_an_exemption(self):
        fills = realised_from_fills([F("A", "X", "BUY", 10, 100.0), F("B", "Y", "BUY", 5, 10.0)])
        res = compare(DAY, "analyze", fills, {}, {}, exempt={("A", "X"): "ok"})
        assert not res.passed


@pytest.fixture
def dbs(tmp_path):
    sandbox = tmp_path / "sandbox.db"
    con = sqlite3.connect(sandbox)
    con.execute(
        "create table sandbox_trades (id integer primary key, strategy text, symbol text, "
        "product text, action text, quantity real, price real, trade_timestamp text)"
    )
    rows = [
        ("BREAKOUT-PY", "MCX", "MIS", "SELL", 92, 3283.7, f"{DAY} 10:15:24"),
        ("BREAKOUT-PY", "MCX", "MIS", "BUY", 92, 3297.0, f"{DAY} 10:22:24"),
        ("", "HAL", "MIS", "SELL", 5, 100.0, f"{DAY} 10:00:00"),
    ]
    con.executemany(
        "insert into sandbox_trades (strategy,symbol,product,action,quantity,price,trade_timestamp) "
        "values (?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    core = tmp_path / "core.db"
    con = sqlite3.connect(core)
    con.execute(
        "create table strategy_closed_trades (strategy text, symbol text, product text, mode text, "
        "realized_pnl real, trade_date text)"
    )
    con.execute(
        "create table strategy_positions (strategy text, symbol text, product text, mode text, "
        "quantity real)"
    )
    con.execute("create table strategy_order_tags (orderid text, strategy text, mode text)")
    con.execute(
        "insert into strategy_closed_trades values ('BREAKOUT-PY','MCX','MIS','analyze',-1223.6,?)",
        (DAY,),
    )
    con.execute("insert into strategy_positions values ('BREAKOUT-PY','MCX','MIS','analyze',0)")
    con.commit()
    con.close()
    return sandbox, core


class TestLoaders:
    def test_analyze_fills_keep_untagged_rows_as_untagged(self, dbs):
        fills = pvf.load_fills_analyze(DAY, sandbox_db=dbs[0])
        assert len(fills) == 3
        assert any(f.strategy == "" and f.symbol == "HAL" for f in fills)

    def test_auto_square_off_tag_counts_as_untagged(self):
        assert pvf._tag("AUTO_SQUARE_OFF") == ""
        assert pvf._tag(None) == ""
        assert pvf._tag("BREAKOUT") == "BREAKOUT"

    def test_pages_are_read_per_leg(self, dbs):
        realised, qty = pvf.load_pages(DAY, "analyze", core_db=dbs[1])
        assert realised[K("BREAKOUT-PY", "MCX")] == pytest.approx(-1223.6)
        assert qty[K("BREAKOUT-PY", "MCX")] == 0

    def test_live_fills_are_tagged_through_the_order_tag_table(self, dbs, tmp_path):
        con = sqlite3.connect(dbs[1])
        con.execute("insert into strategy_order_tags values ('O1','BREAKOUT','live')")
        con.commit()
        con.close()
        snap = tmp_path / "snaps"
        snap.mkdir()
        (snap / f"tradebook_{DAY}.json").write_text(
            json.dumps(
                [
                    {
                        "orderid": "O1",
                        "symbol": "TCS",
                        "action": "BUY",
                        "quantity": 5,
                        "average_price": 2000.0,
                        "product": "MIS",
                        "timestamp": f"{DAY} 10:00:00",
                    },
                    {
                        "orderid": "O2",
                        "symbol": "TCS",
                        "action": "SELL",
                        "quantity": 5,
                        "average_price": 2010.0,
                        "product": "MIS",
                        "timestamp": f"{DAY} 10:30:00",
                    },
                ]
            )
        )
        fills = pvf.load_fills_live(DAY, snap_dir=snap, core_db=dbs[1])
        assert [(f.strategy, f.action) for f in fills] == [("BREAKOUT", "BUY"), ("", "SELL")]

    def test_live_without_a_snapshot_is_an_error_not_a_pass(self, dbs, tmp_path):
        with pytest.raises(FileNotFoundError):
            pvf.load_fills_live(DAY, snap_dir=tmp_path / "none", core_db=dbs[1])

    def test_exceptions_file_filters_by_day_and_mode(self, tmp_path):
        f = tmp_path / "e.json"
        f.write_text(
            json.dumps(
                [
                    {"day": DAY, "mode": "analyze", "strategy": "A", "symbol": "X", "reason": "r1"},
                    {
                        "day": "2026-09-30",
                        "mode": "analyze",
                        "strategy": "B",
                        "symbol": "Y",
                        "reason": "r2",
                    },
                ]
            )
        )
        assert pvf.load_exceptions(DAY, "analyze", path=f) == {("A", "X"): "r1"}
        assert pvf.load_exceptions(DAY, "live", path=f) == {}
        assert pvf.load_exceptions(DAY, "analyze", path=tmp_path / "missing.json") == {}

    def test_run_rejects_unknown_mode(self):
        with pytest.raises(ValueError):
            pvf.run(DAY, "unknown")


class TestAccountView:
    def _fills(self):
        return [
            F("PY", "ADANIPORTS", "SELL", 171, 1770.8),
            F("PY", "ADANIPORTS", "BUY", 171, 1763.0),  # +1333.8 real trade
            F("PY", "ADANIPORTS", "BUY", 100, 1768.4),  # stray long
            F("", "ADANIPORTS", "SELL", 100, 1738.4),  # closed by the time exit: -3000
        ]

    def test_stray_gap_is_listed_and_explained_by_an_exemption(self):
        pages = {K("PY", "ADANIPORTS"): 1333.8}
        lines = pvf.account_view(
            self._fills(), pages, exempt={("PY", "ADANIPORTS"): "duplicate stop"}
        )
        text = "\n".join(lines)
        assert "stray ADANIPORTS" in text and "-3,000.0" in text
        assert "all of the -3,000.0 gap is reviewed strays" in text
        assert "UNEXPLAINED" not in text

    def test_an_unlisted_gap_is_flagged_unexplained(self):
        lines = pvf.account_view(self._fills(), {K("PY", "ADANIPORTS"): 1333.8}, exempt={})
        assert any("UNEXPLAINED" in line for line in lines)

    def test_positions_and_dashboard_are_shown_with_their_gaps(self):
        pages = {K("PY", "ADANIPORTS"): 1333.8}
        lines = pvf.account_view(
            self._fills(),
            pages,
            exempt={("PY", "ADANIPORTS"): "x"},
            positions_total=-1666.2,
            funds_total=-1941.4,
        )
        text = "\n".join(lines)
        assert "Positions page" in text
        assert "drifts from its own positions" in text  # funds is 275.2 below positions

    def test_positions_corrected_for_strays_is_recognised(self):
        pages = {K("PY", "ADANIPORTS"): 1333.8}
        lines = pvf.account_view(
            self._fills(), pages, exempt={("PY", "ADANIPORTS"): "x"}, positions_total=1333.8
        )
        assert any("corrected for the reviewed strays" in line for line in lines)

    def test_agreeing_account_has_no_stray_lines(self):
        fills = [F("A", "X", "BUY", 1, 10.0), F("A", "X", "SELL", 1, 12.0)]
        lines = pvf.account_view(fills, {K("A", "X"): 2.0})
        assert not any("stray" in line.lower() or "UNEXPLAINED" in line for line in lines)


class TestChargeLines:
    def _trade(self, strategy, pnl, entry=100.0, exit_=101.0, qty=100.0):
        return {
            "strategy": strategy, "symbol": "X", "exchange": "NSE", "product": "MIS", "mode": "analyze",
            "direction": "LONG", "closed_quantity": qty, "entry_price": entry, "exit_price": exit_,
            "realized_pnl": pnl, "trade_date": DAY, "closed_at": f"{DAY}T10:00:00",
        }

    def test_each_strategy_and_the_total_get_a_line_with_the_profit_share(self):
        lines, headline = pvf.charge_lines([self._trade("A", 100.0), self._trade("B", -20.0)])
        text = "\n".join(lines)
        assert "A " in text and "B " in text and "ALL STRATEGIES" in text
        assert "% of profit" in text
        assert "no gross profit" in lines[1]  # B lost money
        assert headline.startswith("est. charges")
        assert "% of gross profit" in headline

    def test_unmodelled_instrument_says_so_instead_of_inventing_a_number(self):
        t = self._trade("A", 50.0)
        t["exchange"] = "MCX"
        lines, headline = pvf.charge_lines([t])
        assert "unknown" in lines[0] and headline == ""

    def test_summary_carries_the_charge_headline_when_the_check_passes(self):
        res = compare(DAY, "analyze", {}, {}, {})
        res.charges_headline = "est. charges 5,528 = 80.6% of gross profit 6,862"
        res.by_strategy["A"] = (1.0, 1.0)
        assert res.summary().endswith("est. charges 5,528 = 80.6% of gross profit 6,862")


class TestChargesFollowTheBroker:
    def _t(self, broker):
        return {
            "strategy": "A", "symbol": "X", "exchange": "NSE", "product": "MIS", "mode": "analyze",
            "direction": "LONG", "closed_quantity": 100.0, "entry_price": 1000.0, "exit_price": 1010.0,
            "realized_pnl": 1000.0, "trade_date": DAY, "closed_at": f"{DAY}T10:00:00", "broker": broker,
        }

    def test_flattrade_trades_cost_less_than_the_assumed_default(self):
        flat, _ = pvf.charge_lines([self._t("flattrade")])
        base, _ = pvf.charge_lines([self._t("unknown")])
        pick = lambda lines: float(next(x for x in lines if x.startswith("ALL")).split("charges")[1].split()[0].replace(",", ""))  # noqa: E731
        assert pick(flat) < pick(base)

    def test_a_line_per_broker_names_assumed_rates(self):
        lines, _ = pvf.charge_lines([self._t("flattrade"), self._t("unknown")])
        text = "\n".join(lines)
        assert "broker Flattrade:" in text
        assert "assumed rates" in text and "Generic discount broker" in text

    def test_loader_reads_the_broker_column_when_present_and_defaults_when_not(self, tmp_path):
        db = tmp_path / "core.db"
        con = sqlite3.connect(db)
        cols = (
            "strategy text, symbol text, exchange text, product text, mode text, direction text, "
            "closed_quantity real, entry_price real, exit_price real, realized_pnl real, "
            "trade_date text, closed_at text"
        )
        con.execute(f"create table strategy_closed_trades ({cols})")
        con.execute(
            "insert into strategy_closed_trades values "
            "('A','X','NSE','MIS','analyze','LONG',1,1,2,1,?,?)",
            (DAY, f"{DAY}T10:00:00"),
        )
        con.commit()
        assert pvf.load_closed_trades(DAY, "analyze", core_db=db)[0]["broker"] == "unknown"
        con.execute("alter table strategy_closed_trades add column broker text")
        con.execute("update strategy_closed_trades set broker='flattrade'")
        con.commit()
        con.close()
        assert pvf.load_closed_trades(DAY, "analyze", core_db=db)[0]["broker"] == "flattrade"
