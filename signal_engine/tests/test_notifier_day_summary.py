"""Day summary arithmetic — RED phase first."""

from signal_engine.notifier import _day_summary_header


class _Rec:
    def __init__(self, r_multiple):
        self.r_multiple = r_multiple


class TestCapitalTrajectory:
    """`capital` reaching the summary is the DAY-START capital: sizing runs off
    get_sizing_capital(), which caches the first fetch of the day when
    use_day_start_capital is on, and calculate_quantity stamps that into
    _last_known_capital. Treating it as the closing balance shifted BOTH ends of
    the trajectory line down by the day's P&L."""

    def _line(self, **kw):
        args = dict(today="25-Aug-2026", trades=2, wins=2, losses=0,
                    net_pnl=500.0, capital=15000.0, time_exits=0, trade_records=None)
        args.update(kw)
        return [l for l in _day_summary_header(**args) if l.startswith("Capital:")][0]

    def test_profitable_day_counts_up_from_opening(self):
        assert self._line() == "Capital: ₹15,000 → ₹15,500"

    def test_losing_day_counts_down_from_opening(self):
        assert self._line(net_pnl=-450.0, wins=0, losses=2) == "Capital: ₹15,000 → ₹14,550"

    def test_flat_day_shows_no_movement(self):
        assert self._line(net_pnl=0.0, wins=1, losses=1) == "Capital: ₹15,000 → ₹15,000"


class TestReturnPct:
    def test_pct_is_measured_against_opening_capital(self):
        net = [l for l in _day_summary_header(
            today="25-Aug-2026", trades=1, wins=1, losses=0, net_pnl=750.0,
            capital=15000.0, time_exits=0, trade_records=None) if l.startswith("Net:")][0]
        assert "(+5.0%)" in net   # 750 / 15000, not 750 / 15750

    def test_zero_capital_does_not_divide_by_zero(self):
        assert _day_summary_header(
            today="25-Aug-2026", trades=1, wins=1, losses=0, net_pnl=100.0,
            capital=0.0, time_exits=0, trade_records=None)


class TestWinRate:
    def test_time_exits_excluded_from_win_rate_but_shown(self):
        head = _day_summary_header(
            today="25-Aug-2026", trades=3, wins=1, losses=1, net_pnl=10.0,
            capital=15000.0, time_exits=1, trade_records=None)[1]
        assert "W: 1" in head and "L: 1" in head and "T: 1" in head
        assert "Win Rate: 50%" in head

    def test_no_decided_trades_does_not_divide_by_zero(self):
        head = _day_summary_header(
            today="25-Aug-2026", trades=1, wins=0, losses=0, net_pnl=5.0,
            capital=15000.0, time_exits=1, trade_records=None)[1]
        assert "Win Rate: 0%" in head


class TestAverageR:
    def test_average_r_skips_records_without_one(self):
        line = _day_summary_header(
            today="25-Aug-2026", trades=3, wins=3, losses=0, net_pnl=300.0,
            capital=15000.0, time_exits=0,
            trade_records=[_Rec(2.0), _Rec(None), _Rec(1.0)])[2]
        assert "Avg R: +1.5R" in line
