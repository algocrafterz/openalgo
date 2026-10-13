"""Daily mark-to-market book of a fixed-notional trade ledger: portfolio return and drawdown.

A run record's `series.equity_bps` is the running sum of per-trade bps, keyed by entry day.
It is not a portfolio: a trade held for a year books its whole result on the day it opened,
and bps of one position do not add up to a percentage of an account. This module rebuilds
the account the ledger implies, one value per session:

  ACCOUNT.   `capital` is the declared book size (for a ranked book: the number of slots times
             the fixed notional per position). It is not compounded: every position is the
             same fixed notional, as the ledger was simulated, and profit accrues as cash.
  POSITION.  quantity = notional / entry price (fractional, as the ledger's fixed notional
             implies). Marked to each session's close from the entry session to the session
             before the exit; the exit session is marked from the prior close to the exit price.
  COST.      the trade's own `cost_column` (the run's governing cost) on the notional, half on
             the entry session and half on the exit session, so the book reconciles to the
             ledger's net result exactly.
  MISSING.   a held symbol without a close on a session keeps its last mark (no P&L that
             day, caught up at the next close); every carried mark is counted.
  SESSIONS.  the caller's official session list. Every session counts, including sessions
             with no open position, so Sharpe and track length use the right observations.

Read-only: nothing here writes a run record.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.timeutils import IST

#: Sessions per year used to annualise.
SESSIONS_PER_YEAR = 252


class BookError(ValueError):
    """The ledger and the closes cannot be turned into a book."""


@dataclass(frozen=True)
class Book:
    #: One row per session: pnl, equity, return, drawdown_pct, open_positions (held at the
    #: close) and deployed (their notional).
    daily: pd.DataFrame
    stats: dict[str, Any]
    reconciliation: dict[str, float]
    #: Holding sessions marked at a carried-forward close because the close was missing.
    carried_marks: int
    capital: float
    notional: float


def _ist_day(value: Any) -> date:
    ts = pd.Timestamp(value)
    ts = ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)
    return ts.date()


def _trade_pnl(
    trade: Any, closes: pd.Series, position: dict[date, int], notional: float, cost_column: str
) -> tuple[dict[date, float], int]:
    """P&L per session of one trade, and how many marks were carried forward."""
    entry_day, exit_day = _ist_day(trade.entry_time), _ist_day(trade.exit_time)
    if entry_day not in position or exit_day not in position:
        raise BookError(f"trade {getattr(trade, 'trade_id', '?')} lies outside the sessions")
    sign = -1.0 if str(trade.side).upper() == "SELL" else 1.0
    qty = notional / float(trade.entry_price)
    half_cost = notional * float(getattr(trade, cost_column)) / 1e4 / 2.0
    i, j = position[entry_day], position[exit_day]
    pnl: dict[date, float] = {}
    mark, carried = float(trade.entry_price), 0
    for day, close in zip(closes.index[i:j], closes.to_numpy()[i:j], strict=True):
        if math.isnan(close):
            carried += 1
            pnl[day] = 0.0
            continue
        pnl[day] = sign * qty * (close - mark)
        mark = close
    pnl[exit_day] = pnl.get(exit_day, 0.0) + sign * qty * (float(trade.exit_price) - mark)
    pnl[entry_day] = pnl.get(entry_day, 0.0) - half_cost
    pnl[exit_day] -= half_cost
    return pnl, carried


def _monthly(daily: pd.DataFrame) -> list[dict[str, Any]]:
    index = pd.PeriodIndex([pd.Timestamp(d).to_period("M") for d in daily.index])
    grown = (1.0 + daily["return"]).groupby(index).prod() - 1.0
    return [{"month": str(p), "return_pct": round(float(v) * 100, 4)} for p, v in grown.items()]


def _stats(daily: pd.DataFrame, capital: float) -> dict[str, Any]:
    ret = daily["return"].to_numpy(dtype=float)
    equity = daily["equity"].to_numpy(dtype=float)
    n = len(ret)
    sd = float(np.std(ret, ddof=1)) if n > 1 else float("nan")
    growth = equity[-1] / capital
    years = n / SESSIONS_PER_YEAR
    trough = int(np.argmin(daily["drawdown_pct"].to_numpy()))
    peak = int(np.argmax(equity[: trough + 1]))
    return {
        "sessions": n,
        "sessions_with_pnl": int((daily["pnl"] != 0).sum()),
        "sessions_with_position": int((daily["open_positions"] > 0).sum()),
        "first_session": daily.index[0].isoformat(),
        "last_session": daily.index[-1].isoformat(),
        "years": round(years, 3),
        "total_return_pct": round((growth - 1.0) * 100, 4),
        "annual_return_pct": (
            round((growth ** (1.0 / years) - 1.0) * 100, 4) if growth > 0 and years > 0 else None
        ),
        "annual_volatility_pct": (
            round(sd * math.sqrt(SESSIONS_PER_YEAR) * 100, 4) if math.isfinite(sd) else None
        ),
        "sharpe": (
            round(float(np.mean(ret)) / sd * math.sqrt(SESSIONS_PER_YEAR), 4)
            if math.isfinite(sd) and sd > 0
            else None
        ),
        "max_drawdown_pct": round(float(daily["drawdown_pct"].min()), 4),
        "max_drawdown_peak": daily.index[peak].isoformat(),
        "max_drawdown_trough": daily.index[trough].isoformat(),
        "max_open_positions": int(daily["open_positions"].max()),
        "mean_deployed_pct": round(float(daily["deployed"].mean()) / capital * 100, 4),
    }


def mark_to_market(
    trades: pd.DataFrame,
    closes: pd.DataFrame,
    *,
    notional: float,
    capital: float,
    sessions: Sequence[date],
    cost_column: str = "cost_bps",
) -> Book:
    """The daily book of `trades` (one row per trade) marked to `closes` (sessions x symbols).

    `trades` needs symbol, side, entry_time, exit_time, entry_price, exit_price, gross_bps
    and `cost_column`. Raises BookError when a trade falls outside `sessions` or its symbol
    has no closes.
    """
    if notional <= 0 or capital <= 0:
        raise BookError("notional and capital must be positive")
    axis = list(sessions)
    position = {d: k for k, d in enumerate(axis)}
    frame = closes.reindex(axis)
    pnl = np.zeros(len(axis))
    open_count = np.zeros(len(axis), dtype=int)
    carried = 0
    for trade in trades.itertuples(index=False):
        if trade.symbol not in frame.columns or frame[trade.symbol].isna().all():
            raise BookError(f"no closes for {trade.symbol}")
        per_day, n_carried = _trade_pnl(trade, frame[trade.symbol], position, notional, cost_column)
        carried += n_carried
        for day, value in per_day.items():
            pnl[position[day]] += value
        # Held at the close of every session from the entry to the one before the exit; a
        # same-session round trip counts on its session.
        i, j = position[_ist_day(trade.entry_time)], position[_ist_day(trade.exit_time)]
        open_count[i : max(j, i + 1)] += 1
    equity = capital + np.cumsum(pnl)
    previous = np.concatenate(([capital], equity[:-1]))
    peak = np.maximum.accumulate(np.concatenate(([capital], equity)))[1:]
    daily = pd.DataFrame(
        {
            "pnl": pnl,
            "equity": equity,
            "return": pnl / previous,
            "drawdown_pct": (equity / peak - 1.0) * 100,
            "open_positions": open_count,
            "deployed": open_count * notional,
        },
        index=pd.Index(axis, name="session"),
    )
    ledger = float((trades["gross_bps"] - trades[cost_column]).sum()) * notional / 1e4
    reconciliation = {
        "book_pnl_rs": round(float(pnl.sum()), 2),
        "ledger_net_rs": round(ledger, 2),
        "difference_rs": float(pnl.sum()) - ledger,
    }
    return Book(
        daily=daily,
        stats=_stats(daily, capital),
        reconciliation=reconciliation,
        carried_marks=carried,
        capital=capital,
        notional=notional,
    )


def series_payload(book: Book) -> dict[str, Any]:
    """Compact series for a record or a sidecar: equity and drawdown as % of capital."""
    days = [d.isoformat() for d in book.daily.index]
    equity = (book.daily["equity"] / book.capital - 1.0) * 100
    return {
        "equity_pct": [[d, round(float(v), 4)] for d, v in zip(days, equity, strict=True)],
        "drawdown_pct": [
            [d, round(float(v), 4)] for d, v in zip(days, book.daily["drawdown_pct"], strict=True)
        ],
        "monthly_pct": _monthly(book.daily),
    }


def closes_frame(closes: Mapping[str, Mapping[date, float]]) -> pd.DataFrame:
    """Sessions x symbols from `tickgrid.session_closes`-shaped closes."""
    return pd.DataFrame({s: pd.Series(v, dtype=float) for s, v in closes.items()}).sort_index()


def portfolio_block(
    trades: pd.DataFrame,
    closes: pd.DataFrame,
    *,
    sessions: Sequence[date],
    notional: float,
    slots: int,
    cost_column: str = "cost_bps",
    alternate_cost_column: str | None = None,
) -> tuple[dict[str, Any], np.ndarray]:
    """The `portfolio` block of a record or sidecar, and the daily returns the gates use.

    The book is `slots` positions of `notional` each. `cost_column` is the run's governing
    cost (the one its headline net uses); `alternate_cost_column`, when present and different,
    adds the same statistics at that cost so the two cost bases are never confused.
    """
    if slots <= 0:
        raise BookError("slots must be positive")
    capital = float(slots) * float(notional)
    book = mark_to_market(
        trades,
        closes,
        notional=notional,
        capital=capital,
        sessions=sessions,
        cost_column=cost_column,
    )
    block: dict[str, Any] = {
        "method": (
            "fixed-notional book marked to market at each session close "
            "(signal_engine.backtest.book_series); profits are not reinvested"
        ),
        "capital_rs": capital,
        "notional_rs": float(notional),
        "slots": int(slots),
        "cost_column": cost_column,
        "stats": book.stats,
        "reconciliation": book.reconciliation,
        "carried_marks": book.carried_marks,
        "series": series_payload(book),
    }
    if (
        alternate_cost_column
        and alternate_cost_column in trades.columns
        and not np.allclose(trades[alternate_cost_column], trades[cost_column])
    ):
        alt = mark_to_market(
            trades,
            closes,
            notional=notional,
            capital=capital,
            sessions=sessions,
            cost_column=alternate_cost_column,
        )
        block["alternate_cost"] = {
            "cost_column": alternate_cost_column,
            "stats": alt.stats,
            "reconciliation": alt.reconciliation,
        }
    return block, book.daily["return"].to_numpy(dtype=float)
