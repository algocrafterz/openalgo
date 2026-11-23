"""Additive behaviour keys of services/discretionary_analytics (winning-day %, net drawdown
timing, trades per day, re-entry after loss, quantity dispersion).

``EXISTING_SNAPSHOT`` was produced by the code BEFORE these keys existed, from the same
fixture. The invariance test proves every pre-existing key keeps its value and type.
"""

from __future__ import annotations

import pytest

from services import discretionary_analytics as an


def _p(i, day, o, c, pnl, charges, qty, sym="SBIN", direction="LONG"):
    return {
        "id": i,
        "broker": "flattrade",
        "account": "A",
        "product": "MIS",
        "trade_date": day,
        "opened_at": f"{day} {o}:00",
        "closed_at": f"{day} {c}:00",
        "symbol": sym,
        "exchange": "NSE",
        "direction": direction,
        "closed_quantity": qty,
        "realized_pnl": pnl,
        "charges": charges,
        "net_pnl": None if charges is None else round(pnl - charges, 4),
        "hold_minutes": 30.0,
    }


def fixture_trades():
    return [
        _p(1, "2026-11-19", "09:20", "09:50", -300.0, 20.0, 10),
        _p(2, "2026-11-19", "09:55", "10:30", 200.0, 20.0, 20),
        _p(3, "2026-11-20", "10:00", "10:40", 500.0, 20.0, 30, direction="SHORT"),
        _p(4, "2026-11-20", "11:00", "11:20", -150.0, 20.0, 10),
        _p(5, "2026-11-20", "13:00", "14:00", 100.0, 20.0, 50, sym="TCS"),
        _p(6, "2026-11-23", "09:30", "09:40", -80.0, 20.0, 40),
    ]


NEW_CONSISTENCY_KEYS = {"net_days", "trades_per_day", "reentry_after_loss", "quantity_by_symbol"}

_REF = {
    "build_id": None,
    "broker": "flattrade",
    "account": "A",
    "exchange": "NSE",
    "product": "MIS",
    "symbol": "SBIN",
    "charges": 20.0,
    "charges_status": None,
}
EXISTING_SNAPSHOT = {
    "consistency": {
        "avg_daily_net": 50.0,
        "avg_daily_net_complete": True,
        "avg_daily_pnl": 90.0,
        "avg_daily_unavailable_reason": None,
        "avg_hold_minutes": 30.0,
        "basis": (
            "trade-level items (largest win / loss, streaks) are net of estimated charges; "
            "daily figures and drawdown are gross realized P&L (before charges) unless the "
            "name says net"
        ),
        "completed_trades": 6,
        "drawdown_basis": (
            "gross realized P&L by close date, equity peak starts at 0; "
            "excludes charges and unrealized"
        ),
        "flat_days": 0,
        "largest_loss": {
            **_REF,
            "closed_at": "2026-11-19 09:50:00",
            "direction": "LONG",
            "id": 1,
            "net_pnl": -320.0,
            "realized_pnl": -300.0,
            "trade_date": "2026-11-19",
        },
        "largest_win": {
            **_REF,
            "closed_at": "2026-11-20 10:40:00",
            "direction": "SHORT",
            "id": 3,
            "net_pnl": 480.0,
            "realized_pnl": 500.0,
            "trade_date": "2026-11-20",
        },
        "losing_days": 2,
        "max_consecutive_losses": 1,
        "max_consecutive_wins": 2,
        "max_drawdown": -100.0,
        "max_drawdown_unavailable_reason": None,
        "median_hold_minutes": 30.0,
        "profitable_days": 1,
        "streak_rule": (
            "completed trades ordered by close time (ties by id); a break-even trade ends "
            "both streaks"
        ),
        "trading_days": 3,
    },
    "risk": {
        "basis": "net realised equity by position close time, peak starts at 0",
        "current_drawdown": 190.0,
        "current_drawdown_pct": None,
        "current_streak": {"length": 1, "type": "loss"},
        "latest_equity": 150.0,
        "max_consecutive_losses": 1,
        "max_consecutive_wins": 2,
        "max_drawdown": 320.0,
        "max_drawdown_at": "2026-11-19 09:50:00",
        "max_drawdown_pct": None,
        "net_unavailable_positions": 0,
        "peak_equity": 340.0,
        "percent_unavailable_reason": "no capital basis",
        "positions": 6,
    },
    "stats": {
        "avg_hold_minutes": 30.0,
        "avg_loss": 196.67,
        "avg_win": 246.67,
        "charges": 120.0,
        "charges_unavailable_trades": 0,
        "expectancy": 25.0,
        "losses": 3,
        "median_hold_minutes": 30.0,
        "net_basis_trades": 6,
        "net_charges_complete": True,
        "net_of_charges": 150.0,
        "net_unavailable_trades": 0,
        "profit_factor": 1.25,
        "realized_gross": 270.0,
        "stats_unavailable_reason": None,
        "trades": 6,
        "win_rate": 50.0,
        "wins": 3,
    },
}


def _blocks(trades):
    daily = an.period_breakdown(trades, "daily")
    return {
        "consistency": an.consistency(trades, daily, an.hold_summary(trades)),
        "risk": an.risk_block(trades),
        "stats": an.summary_stats(trades),
    }


def test_existing_keys_are_unchanged_versus_pre_change_snapshot():
    now = _blocks(fixture_trades())
    for block, old in EXISTING_SNAPSHOT.items():
        for key, value in old.items():
            assert now[block][key] == value, (block, key)
            assert type(now[block][key]) is type(value), (block, key)
    # only the named keys are new
    assert set(now["consistency"]) - set(EXISTING_SNAPSHOT["consistency"]) == NEW_CONSISTENCY_KEYS
    assert set(now["risk"]) - set(EXISTING_SNAPSHOT["risk"]) == {"max_drawdown_detail"}
    assert set(now["stats"]) == set(EXISTING_SNAPSHOT["stats"])


def test_new_keys_on_fixture():
    now = _blocks(fixture_trades())
    c = now["consistency"]
    assert c["net_days"]["total_days"] == 3
    assert (c["net_days"]["winning_days"], c["net_days"]["losing_days"]) == (1, 2)
    assert c["net_days"]["winning_day_pct"] == 33.33
    assert c["trades_per_day"]["avg"] == 2.0
    assert (c["trades_per_day"]["max"], c["trades_per_day"]["max_day"]) == (3, "2026-11-20")
    assert c["reentry_after_loss"] == {
        "window_minutes": an.REENTRY_WINDOW_MINUTES,
        "losses_followed_by_entry": 2,
        "reentries_within_window": 1,
        "shortest_gap_minutes": 5.0,
    }
    sbin = next(r for r in c["quantity_by_symbol"] if r["symbol"] == "SBIN")
    assert (sbin["min_quantity"], sbin["median_quantity"], sbin["max_quantity"]) == (10, 20, 40)
    d = now["risk"]["max_drawdown_detail"]
    assert d["depth"] == 320.0 and d["trough_at"] == "2026-11-19 09:50:00"
    assert d["recovered"] is True and d["recovered_at"] == "2026-11-20 10:40:00"
    assert d["start_at"] == "2026-11-19 09:20:00"
    assert d["duration_minutes"] == 1520.0


def test_reentry_window_is_a_named_constant():
    assert an.REENTRY_WINDOW_MINUTES == 15


def test_drawdown_never_recovered_has_no_recovery_keys():
    trades = [
        _p(1, "2026-11-19", "09:20", "09:40", 100.0, 0.0, 10),
        _p(2, "2026-11-19", "10:00", "10:30", -300.0, 0.0, 10),
        _p(3, "2026-11-19", "11:00", "11:10", 50.0, 0.0, 10),
    ]
    d = an.drawdown(trades)["max_drawdown_detail"]
    assert d["recovered"] is False
    assert "recovered_at" not in d
    assert d["depth"] == 300.0
    assert d["start_at"] == "2026-11-19 09:40:00"
    assert d["duration_minutes"] == 90.0  # peak close to latest close
    assert d["duration_basis"] == "start to latest close"


def test_no_drawdown_means_no_detail_key():
    trades = [_p(1, "2026-11-19", "09:20", "09:40", 100.0, 0.0, 10)]
    assert "max_drawdown_detail" not in an.drawdown(trades)
    assert "max_drawdown_detail" not in an.drawdown([])


def test_empty_input_adds_no_keys():
    out = an.consistency([], [], {})
    assert not (NEW_CONSISTENCY_KEYS & set(out))
    assert an.drawdown([])["max_drawdown"] == 0.0


def test_single_losing_trade():
    trades = [_p(1, "2026-11-19", "09:20", "09:40", -100.0, 10.0, 5)]
    c = _blocks(trades)["consistency"]
    assert c["net_days"]["winning_day_pct"] == 0.0
    assert c["trades_per_day"]["max"] == 1
    assert "reentry_after_loss" not in c  # no later entry
    assert c["quantity_by_symbol"][0]["min_quantity"] == 5
    d = an.drawdown(trades)["max_drawdown_detail"]
    assert d["recovered"] is False and d["start_basis"] == "first position opened"
    assert d["start_at"] == "2026-11-19 09:20:00"


def test_day_with_unavailable_charges_is_not_counted_as_win_or_loss():
    days = [
        {"net_of_charges": 100.0, "net_charges_complete": True},
        {"net_of_charges": 50.0, "net_charges_complete": False},
    ]
    out = an.net_day_stats(days)
    assert (out["total_days"], out["winning_days"], out["net_unavailable_days"]) == (1, 1, 1)
    assert an.net_day_stats([{"net_of_charges": 5.0, "net_charges_complete": False}]) == {}


def test_gross_positive_net_negative_day_is_a_losing_day():
    trades = [_p(1, "2026-11-19", "09:20", "09:40", 10.0, 25.0, 5)]
    c = _blocks(trades)["consistency"]
    assert c["profitable_days"] == 1  # existing gross key unchanged
    assert c["net_days"]["losing_days"] == 1 and c["net_days"]["winning_days"] == 0


def test_long_short_and_partial_exit_positions_count_once():
    # three closing legs of one SHORT position grouped into one completed position
    legs = []
    for i, (qty, hh) in enumerate([(10, 10), (10, 11), (20, 12)], start=1):
        legs.append(
            {
                "id": i,
                "broker": "flattrade",
                "account": "A",
                "exchange": "NSE",
                "symbol": "SBIN",
                "product": "MIS",
                "position_seq": 1,
                "direction": "SHORT",
                "closed_quantity": qty,
                "entry_price": 100.0,
                "exit_price": 99.0,
                "opened_at": "2026-11-19 09:30:00",
                "closed_at": f"2026-11-19 {hh}:00:00",
                "trade_date": "2026-11-19",
                "overnight": False,
                "hold_minutes": 60.0,
                "realized_pnl": qty * 1.0,
                "charges": 1.0,
                "charges_status": "ok",
                "charges_breakdown": {},
            }
        )
    pos = [
        {
            "id": 7,
            "build_id": "b",
            "broker": "flattrade",
            "account": "A",
            "exchange": "NSE",
            "symbol": "SBIN",
            "product": "MIS",
            "position_seq": 1,
            "direction": "SHORT",
            "opened_at": "2026-11-19 09:30:00",
            "status": "FLAT",
        }
    ]
    trades = an.group_completed(legs, pos)
    assert len(trades) == 1
    c = an.consistency(trades, an.period_breakdown(trades, "daily", legs), an.hold_summary(trades))
    assert c["trades_per_day"]["max"] == 1
    assert c["quantity_by_symbol"][0]["max_quantity"] == 40  # whole position, not per leg
    assert c["net_days"]["winning_days"] == 1
    assert an.drawdown(trades).get("max_drawdown_detail") is None


@pytest.mark.parametrize("gap_min,expected", [(15, 1), (16, 0)])
def test_reentry_window_boundary(gap_min, expected):
    opened = f"09:{40 + gap_min}"  # the loss closes at 09:40
    trades = [
        _p(1, "2026-11-19", "09:20", "09:40", -100.0, 0.0, 10),
        _p(2, "2026-11-19", opened, "11:00", 5.0, 0.0, 10),
    ]
    out = an.reentry_after_loss(trades)
    assert out["reentries_within_window"] == expected


def test_drawdown_recovery_is_float_safe():
    # 0.1 + 0.2 = 0.30000000000000004 is the peak; -2.5 then +2.5 returns to
    # 0.2999999999999998, 2e-16 below the peak: float noise, so it is a recovery.
    trades = [
        _p(1, "2026-11-19", "09:20", "09:30", 0.1, 0.0, 1),
        _p(2, "2026-11-19", "09:31", "09:40", 0.2, 0.0, 1),
        _p(3, "2026-11-19", "10:00", "10:30", -2.5, 0.0, 1),
        _p(4, "2026-11-19", "10:40", "11:00", 2.5, 0.0, 1),
    ]
    peak = 0.1 + 0.2
    back = peak - 2.5 + 2.5
    assert back < peak  # the noise this test exists for
    d = an.drawdown(trades)["max_drawdown_detail"]
    assert d["recovered"] is True
    assert d["recovered_at"] == "2026-11-19 11:00:00"


def test_drawdown_start_is_earliest_opening_with_overlapping_positions():
    # Position 1 closes first but position 2 (still open then) was opened earlier.
    trades = [
        _p(1, "2026-11-19", "09:30", "09:40", -100.0, 0.0, 1),
        _p(2, "2026-11-19", "09:10", "10:00", -50.0, 0.0, 1),
    ]
    d = an.drawdown(trades)["max_drawdown_detail"]
    assert d["start_basis"] == "first position opened"
    assert d["start_at"] == "2026-11-19 09:10:00"
    assert d["depth"] == 150.0


def test_reentry_same_timestamp_tie_and_reversal_count_with_zero_gap():
    # Reversal: the loss closes at 09:40 and the opposite position opens at 09:40.
    # Two positions open at that same instant; the loss itself never counts.
    trades = [
        _p(1, "2026-11-19", "09:20", "09:40", -100.0, 0.0, 10),
        _p(2, "2026-11-19", "09:40", "10:00", 5.0, 0.0, 10, direction="SHORT"),
        _p(3, "2026-11-19", "09:40", "10:10", 5.0, 0.0, 10, sym="TCS"),
    ]
    out = an.reentry_after_loss(trades)
    assert out["losses_followed_by_entry"] == 1
    assert out["reentries_within_window"] == 1
    assert out["shortest_gap_minutes"] == 0.0


def test_reentry_excludes_position_opened_before_the_loss_closed():
    trades = [
        _p(1, "2026-11-19", "09:20", "09:40", -100.0, 0.0, 10),
        _p(2, "2026-11-19", "09:30", "11:00", 5.0, 0.0, 10, sym="TCS"),  # overlaps
        _p(3, "2026-11-19", "10:30", "10:50", 5.0, 0.0, 10, sym="INFY"),
    ]
    out = an.reentry_after_loss(trades)
    assert out["losses_followed_by_entry"] == 1
    assert out["reentries_within_window"] == 0
    assert out["shortest_gap_minutes"] == 50.0
