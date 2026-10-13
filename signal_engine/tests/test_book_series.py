"""Daily mark-to-market book of a fixed-notional trade ledger (book_series)."""

import math

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import book_series as bs

IST = "Asia/Kolkata"


def _ts(day: str) -> pd.Timestamp:
    return pd.Timestamp(day, tz=IST)


def _trades(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["entry_time"] = frame["entry_time"].map(_ts)
    frame["exit_time"] = frame["exit_time"].map(_ts)
    frame["gross_bps"] = (frame["exit_price"] / frame["entry_price"] - 1) * 1e4
    frame["net_bps"] = frame["gross_bps"] - frame["cost_bps"]
    return frame


SESSIONS = [
    pd.Timestamp(d).date()
    for d in ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05")
]


def _closes(values: dict[str, list[float | None]]) -> pd.DataFrame:
    return pd.DataFrame(values, index=SESSIONS, dtype=float)


def test_marks_open_position_to_each_close_and_books_cost_half_at_each_end():
    trades = _trades(
        [
            {
                "symbol": "A",
                "side": "BUY",
                "entry_time": "2024-01-02",
                "exit_time": "2024-01-04",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "cost_bps": 20.0,
            }
        ]
    )
    closes = _closes({"A": [99, 102, 105, 111, 112]})
    out = bs.mark_to_market(trades, closes, notional=10_000.0, capital=10_000.0, sessions=SESSIONS)
    pnl = out.daily["pnl"].tolist()
    # 100 shares; entry day 100 -> 102 (+200) less 10 cost; day 2 102 -> 105; exit 105 -> 110.
    assert pnl == pytest.approx([0.0, 190.0, 300.0, 490.0, 0.0])
    assert out.daily["open_positions"].tolist() == [0, 1, 1, 0, 0]
    assert out.reconciliation["difference_rs"] == pytest.approx(0.0, abs=1e-9)


def test_same_day_round_trip_and_short_side():
    trades = _trades(
        [
            {
                "symbol": "A",
                "side": "SELL",
                "entry_time": "2024-01-03",
                "exit_time": "2024-01-03",
                "entry_price": 100.0,
                "exit_price": 95.0,
                "cost_bps": 0.0,
            }
        ]
    )
    trades["gross_bps"] = 500.0
    trades["net_bps"] = 500.0
    out = bs.mark_to_market(
        trades, _closes({"A": [100] * 5}), notional=10_000.0, capital=10_000.0, sessions=SESSIONS
    )
    assert out.daily["pnl"].tolist() == pytest.approx([0, 0, 500.0, 0, 0])
    assert out.reconciliation["difference_rs"] == pytest.approx(0.0, abs=1e-9)


def test_missing_close_carries_the_last_mark_and_is_counted():
    trades = _trades(
        [
            {
                "symbol": "A",
                "side": "BUY",
                "entry_time": "2024-01-01",
                "exit_time": "2024-01-05",
                "entry_price": 100.0,
                "exit_price": 104.0,
                "cost_bps": 0.0,
            }
        ]
    )
    closes = _closes({"A": [101, None, None, 103, 120]})
    out = bs.mark_to_market(trades, closes, notional=10_000.0, capital=10_000.0, sessions=SESSIONS)
    assert out.daily["pnl"].tolist() == pytest.approx([100, 0, 0, 200, 100])
    assert out.carried_marks == 2
    assert out.reconciliation["difference_rs"] == pytest.approx(0.0, abs=1e-9)


def test_statistics_count_every_session_including_zero_pnl():
    trades = _trades(
        [
            {
                "symbol": "A",
                "side": "BUY",
                "entry_time": "2024-01-02",
                "exit_time": "2024-01-03",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "cost_bps": 0.0,
            }
        ]
    )
    closes = _closes({"A": [100, 90, 110, 110, 110]})
    out = bs.mark_to_market(trades, closes, notional=1_000.0, capital=2_000.0, sessions=SESSIONS)
    s = out.stats
    assert s["sessions"] == 5
    assert s["sessions_with_pnl"] == 2
    assert s["total_return_pct"] == pytest.approx(5.0)
    # Day 2: -100 on 2000 (-5%); day 3: +200 on 1900; the trough is -5% below the start.
    assert s["max_drawdown_pct"] == pytest.approx(-5.0)
    returns = np.array([0, -100 / 2000, 200 / 1900, 0, 0])
    expected = returns.mean() / returns.std(ddof=1) * math.sqrt(252)
    assert s["sharpe"] == pytest.approx(expected, rel=1e-6)
    assert s["annual_return_pct"] == pytest.approx((1.05 ** (252 / 5) - 1) * 100, rel=1e-6)
    assert s["max_open_positions"] == 1


def test_trade_outside_the_session_axis_is_refused():
    trades = _trades(
        [
            {
                "symbol": "A",
                "side": "BUY",
                "entry_time": "2023-12-29",
                "exit_time": "2024-01-03",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "cost_bps": 0.0,
            }
        ]
    )
    with pytest.raises(bs.BookError, match="outside"):
        bs.mark_to_market(
            trades, _closes({"A": [100] * 5}), notional=1_000.0, capital=1_000.0, sessions=SESSIONS
        )


def test_symbol_without_closes_is_refused():
    trades = _trades(
        [
            {
                "symbol": "B",
                "side": "BUY",
                "entry_time": "2024-01-02",
                "exit_time": "2024-01-03",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "cost_bps": 0.0,
            }
        ]
    )
    with pytest.raises(bs.BookError, match="no closes"):
        bs.mark_to_market(
            trades, _closes({"A": [100] * 5}), notional=1_000.0, capital=1_000.0, sessions=SESSIONS
        )


def test_series_payload_is_compact_and_ordered():
    trades = _trades(
        [
            {
                "symbol": "A",
                "side": "BUY",
                "entry_time": "2024-01-02",
                "exit_time": "2024-01-04",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "cost_bps": 0.0,
            }
        ]
    )
    out = bs.mark_to_market(
        trades,
        _closes({"A": [99, 102, 105, 111, 112]}),
        notional=10_000.0,
        capital=20_000.0,
        sessions=SESSIONS,
    )
    payload = bs.series_payload(out)
    assert [row[0] for row in payload["equity_pct"]] == [d.isoformat() for d in SESSIONS]
    assert payload["equity_pct"][-1][1] == pytest.approx(5.0)
    assert payload["drawdown_pct"][0][1] == 0.0
    assert set(payload["monthly_pct"][0]) == {"month", "return_pct"}


def test_portfolio_block_names_both_cost_bases_and_returns_daily_returns():
    trades = _trades(
        [
            {
                "symbol": "A",
                "side": "BUY",
                "entry_time": "2024-01-02",
                "exit_time": "2024-01-04",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "cost_bps": 20.0,
            }
        ]
    )
    trades["modeled_cost_bps"] = 30.0
    closes = bs.closes_frame({"A": dict(zip(SESSIONS, [99, 102, 105, 111, 112], strict=True))})
    block, returns = bs.portfolio_block(
        trades,
        closes,
        sessions=SESSIONS,
        notional=10_000.0,
        slots=2,
        alternate_cost_column="modeled_cost_bps",
    )
    assert block["capital_rs"] == 20_000.0
    assert block["cost_column"] == "cost_bps"
    assert block["alternate_cost"]["cost_column"] == "modeled_cost_bps"
    assert block["alternate_cost"]["stats"]["total_return_pct"] < block["stats"]["total_return_pct"]
    assert len(returns) == len(SESSIONS)


def test_portfolio_block_omits_an_identical_alternate_cost():
    trades = _trades(
        [
            {
                "symbol": "A",
                "side": "BUY",
                "entry_time": "2024-01-02",
                "exit_time": "2024-01-04",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "cost_bps": 20.0,
            }
        ]
    )
    trades["modeled_cost_bps"] = 20.0
    block, _ = bs.portfolio_block(
        trades,
        _closes({"A": [99, 102, 105, 111, 112]}),
        sessions=SESSIONS,
        notional=10_000.0,
        slots=1,
        alternate_cost_column="modeled_cost_bps",
    )
    assert "alternate_cost" not in block
    with pytest.raises(bs.BookError, match="slots"):
        bs.portfolio_block(
            trades, _closes({"A": [100] * 5}), sessions=SESSIONS, notional=10_000.0, slots=0
        )
