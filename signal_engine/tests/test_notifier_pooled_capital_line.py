"""M7: the consolidated day summary reported a capital figure no account holds.

In ANALYZE each strategy sizes off its OWN cached day-start capital (per-strategy risk
isolation), so total_last_known_capital() sums four independent Rs 1L sandbox pools into a
Rs 4L figure — and the header divided net P&L by it, understating the day fourfold. The
per-strategy summaries are correct and keep their capital line; the consolidated one must
either say what the number is or not show it.
"""

from signal_engine import notifier


class TestPerStrategyHeaderKeepsItsCapitalLine:
    def test_capital_trajectory_is_shown(self):
        lines = notifier._day_summary_header(
            "11-Sep-2026", trades=2, wins=1, losses=1, net_pnl=500.0,
            capital=100_000, time_exits=0, trade_records=[], title="ORB DAY SUMMARY",
        )
        assert any("Capital:" in ln for ln in lines)
        assert any("100,000" in ln and "100,500" in ln for ln in lines)


class TestConsolidatedHeaderLabelsAPooledFigure:
    def test_pooled_capital_is_labelled_not_presented_as_an_account(self):
        lines = notifier._day_summary_header(
            "11-Sep-2026", trades=4, wins=2, losses=2, net_pnl=1000.0,
            capital=400_000, time_exits=0, trade_records=[], pooled_strategies=4,
        )
        capital_line = next(ln for ln in lines if "Capital:" in ln)
        assert "4 strategy pools" in capital_line

    def test_the_percentage_is_dropped_when_capital_is_pooled(self):
        """Net over a sum of independent pools is not a return on anything."""
        lines = notifier._day_summary_header(
            "11-Sep-2026", trades=4, wins=2, losses=2, net_pnl=1000.0,
            capital=400_000, time_exits=0, trade_records=[], pooled_strategies=4,
        )
        net_line = next(ln for ln in lines if ln.startswith("Net:"))
        assert "%" not in net_line

    def test_a_single_strategy_day_is_not_pooled_and_keeps_its_percentage(self):
        lines = notifier._day_summary_header(
            "11-Sep-2026", trades=2, wins=1, losses=1, net_pnl=1000.0,
            capital=100_000, time_exits=0, trade_records=[], pooled_strategies=1,
        )
        net_line = next(ln for ln in lines if ln.startswith("Net:"))
        capital_line = next(ln for ln in lines if "Capital:" in ln)
        assert "%" in net_line
        assert "pools" not in capital_line
