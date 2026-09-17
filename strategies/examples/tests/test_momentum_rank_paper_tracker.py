"""Tests for the momentum-rank paper-trading ledger's pure math.

Run: uv run pytest strategies/examples/tests/ -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import momentum_rank_paper_tracker as tracker


class TestInitialBuy:
    def test_splits_capital_equally_across_target(self):
        ledger = tracker.new_ledger(capital=12000.0)
        prices = {"A": 100.0, "B": 200.0, "C": 300.0}

        out = tracker.initial_buy(ledger, "2026-01-01", ["A", "B", "C"], prices,
                                   top_n=3)

        # per_slot = 4000: A -> 40 shares, B -> 20 shares, C -> 13 shares
        assert out["positions"]["A"]["qty"] == 40
        assert out["positions"]["B"]["qty"] == 20
        assert out["positions"]["C"]["qty"] == 13
        spent = 40 * 100.0 + 20 * 200.0 + 13 * 300.0
        assert out["cash"] == 12000.0 - spent

    def test_a_stock_priced_above_its_slot_is_skipped_not_zero_qty(self, capsys):
        """2026-09-17 regression: POWERINDIA @ Rs30,435 against an Rs8,333 slot used to
        open a qty=0 "position" - unfunded, but still counted as an open position in
        every later report. It must not appear in positions at all."""
        ledger = tracker.new_ledger(capital=10000.0)
        prices = {"CHEAP": 50.0, "EXPENSIVE": 99999.0}

        out = tracker.initial_buy(ledger, "2026-01-01", ["CHEAP", "EXPENSIVE"],
                                   prices, top_n=2)

        assert "EXPENSIVE" not in out["positions"]
        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "EXPENSIVE" in captured.out
        assert "skipped" in captured.out

    def test_the_skipped_slot_s_capital_is_redistributed_to_the_other_buy(self):
        """EXPENSIVE's Rs5,000 slot is unaffordable even alone - CHEAP absorbs the whole
        Rs10,000 pool instead of just its own Rs5,000 slot."""
        ledger = tracker.new_ledger(capital=10000.0)
        prices = {"CHEAP": 50.0, "EXPENSIVE": 99999.0}

        out = tracker.initial_buy(ledger, "2026-01-01", ["CHEAP", "EXPENSIVE"],
                                   prices, top_n=2)

        assert out["positions"]["CHEAP"]["qty"] == 200  # floor(10000 / 50)
        assert out["cash"] == 0.0

    def test_redistribution_drops_every_candidate_unaffordable_at_the_current_target(self):
        """A and C are BOTH unaffordable at the initial 3-way target (3000) - each pass
        drops everyone priced out at that pass's target, not just one at a time. Only B
        survives, and absorbs the entire pool."""
        ledger = tracker.new_ledger(capital=9000.0)  # per_slot = 3000
        prices = {"A": 50000.0, "B": 100.0, "C": 4500.0}

        out = tracker.initial_buy(ledger, "2026-01-01", ["A", "B", "C"], prices, top_n=3)

        assert "A" not in out["positions"]
        assert "C" not in out["positions"]
        assert out["positions"]["B"]["qty"] == 90  # floor(9000/100)
        assert out["cash"] == 0.0

    def test_all_candidates_unaffordable_skips_every_one_and_keeps_the_cash(self):
        ledger = tracker.new_ledger(capital=1000.0)
        prices = {"A": 50000.0, "B": 60000.0}

        out = tracker.initial_buy(ledger, "2026-01-01", ["A", "B"], prices, top_n=2)

        assert out["positions"] == {}
        assert out["cash"] == 1000.0

    def test_no_warning_when_all_positions_funded(self, capsys):
        ledger = tracker.new_ledger(capital=10000.0)
        prices = {"A": 100.0, "B": 200.0}

        tracker.initial_buy(ledger, "2026-01-01", ["A", "B"], prices, top_n=2)

        assert "WARNING" not in capsys.readouterr().out

    def test_empty_target_list_is_a_no_op(self):
        ledger = tracker.new_ledger(capital=10000.0)
        out = tracker.initial_buy(ledger, "2026-01-01", [], {}, top_n=2)
        assert out["positions"] == {}
        assert out["cash"] == 10000.0

    def test_a_missing_fill_price_is_treated_as_unaffordable_not_a_crash(self, capsys):
        ledger = tracker.new_ledger(capital=10000.0)
        prices = {"HASPRICE": 100.0}  # "NOPRICE" deliberately absent

        out = tracker.initial_buy(ledger, "2026-01-01", ["HASPRICE", "NOPRICE"],
                                   prices, top_n=2)

        assert "NOPRICE" not in out["positions"]
        assert out["positions"]["HASPRICE"]["qty"] == 100  # floor(10000/100), full pool
        assert "NOPRICE" in capsys.readouterr().out


class TestApplyRebalance:
    def _ledger_with(self, positions: dict, cash: float = 0.0) -> dict:
        ledger = tracker.new_ledger(capital=100000.0)
        ledger["cash"] = cash
        ledger["positions"] = positions
        return ledger

    def test_sell_computes_realized_pnl(self):
        ledger = self._ledger_with(
            {"OLD": {"qty": 10, "entry_price": 100.0, "entry_date": "2026-01-01"}},
            cash=0.0)

        out = tracker.apply_rebalance(
            ledger, "2026-02-01", sells=["OLD"], buys=[], target=[],
            fill_prices={"OLD": 150.0}, top_n=1)

        assert "OLD" not in out["positions"]
        assert out["cash"] == 1500.0  # 10 shares * 150
        trade = out["closed_trades"][0]
        assert trade["pnl"] == 500.0  # (150-100)*10
        assert trade["pnl_pct"] == 50.0

    def test_hold_positions_are_left_untouched(self):
        ledger = self._ledger_with(
            {"KEEP": {"qty": 5, "entry_price": 200.0, "entry_date": "2026-01-01"}},
            cash=1000.0)

        out = tracker.apply_rebalance(
            ledger, "2026-02-01", sells=[], buys=[], target=["KEEP"],
            fill_prices={"KEEP": 999.0}, top_n=1)

        # Untouched: same qty and entry_price despite a very different
        # current fill_price - HOLD means no re-weighting trade at all.
        assert out["positions"]["KEEP"] == {
            "qty": 5, "entry_price": 200.0, "entry_date": "2026-01-01"}
        assert out["cash"] == 1000.0

    def test_buy_sized_off_current_total_value_not_original_capital(self):
        # cash=0, one HOLD position worth 5*300=1500 at today's price ->
        # total book value = 1500, top_n=1 -> new buy gets the whole 1500,
        # not the ledger's unrelated "capital" field.
        ledger = self._ledger_with(
            {"KEEP": {"qty": 5, "entry_price": 100.0, "entry_date": "2026-01-01"}},
            cash=0.0)
        ledger["capital"] = 999999.0  # must NOT be used for sizing

        out = tracker.apply_rebalance(
            ledger, "2026-02-01", sells=[], buys=["NEW"], target=["KEEP", "NEW"],
            fill_prices={"KEEP": 300.0, "NEW": 150.0}, top_n=1)

        # per_slot = (0 cash + 5*300 remaining) / top_n(1) = 1500
        assert out["positions"]["NEW"]["qty"] == 10  # 1500 // 150

    def test_sell_and_buy_in_same_rebalance(self):
        ledger = self._ledger_with(
            {"OLD": {"qty": 10, "entry_price": 100.0, "entry_date": "2026-01-01"},
             "KEEP": {"qty": 2, "entry_price": 50.0, "entry_date": "2026-01-01"}},
            cash=0.0)

        out = tracker.apply_rebalance(
            ledger, "2026-02-01", sells=["OLD"], buys=["NEW"],
            target=["KEEP", "NEW"],
            fill_prices={"OLD": 100.0, "KEEP": 50.0, "NEW": 200.0}, top_n=2)

        assert "OLD" not in out["positions"]
        assert out["positions"]["KEEP"]["qty"] == 2  # untouched
        # total_value after sell = 1000 (OLD proceeds) + 100 (KEEP mkt value) = 1100
        # per_slot = 1100/2 = 550 -> NEW qty = 550//200 = 2
        assert out["positions"]["NEW"]["qty"] == 2


class TestPortfolioValueAndSummary:
    def test_portfolio_value_falls_back_to_entry_price_if_missing(self):
        ledger = tracker.new_ledger(capital=1000.0)
        ledger["cash"] = 100.0
        ledger["positions"] = {
            "A": {"qty": 3, "entry_price": 10.0, "entry_date": "2026-01-01"}}

        value = tracker.portfolio_value(ledger, current_prices={})

        assert value == 100.0 + 3 * 10.0

    def test_summary_reports_realized_unrealized_and_return_pct(self):
        ledger = tracker.new_ledger(capital=1000.0)
        ledger["cash"] = 400.0
        ledger["positions"] = {
            "A": {"qty": 10, "entry_price": 50.0, "entry_date": "2026-01-01"}}
        ledger["closed_trades"] = [{"pnl": 25.0}]

        s = tracker.summary(ledger, current_prices={"A": 60.0})

        assert s["realized_pnl"] == 25.0
        assert s["unrealized_pnl"] == 100.0  # (60-50)*10
        assert s["total_value"] == 400.0 + 10 * 60.0
        expected_return = (s["total_value"] - 1000.0) / 1000.0 * 100
        assert s["total_return_pct"] == round(expected_return, 2)
