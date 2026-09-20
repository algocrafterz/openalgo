"""End-of-day Telegram summary: what actually happened to today's calls.

WHY THIS EXISTS

A watchlist notice is a claim about the future ("this looks worth watching"), sent the moment
it fires and never revisited. Nothing currently closes the loop - a trader has to remember
every symbol flagged that day and go check prices themselves to know whether the calls were any
good, and nothing is left behind for later analysis beyond the raw alerts table. This sends one
message per day, after the close, scoring every call against what the stock actually did -
purely informational, exactly like the watchlist notices themselves. It never places, closes or
implies a trade.

TWO SEPARATE REPORTS, TWO SEPARATE QUESTIONS

- Intraday watchlist: "if a trader had acted on every notice at the price it fired, where would
  they be now?" - entry is the price at the moment each symbol was first flagged, exit is the
  same-day closing read. Scored purely on direction (did the move go the called way), since
  most of these were never real trades - see alerts.py's docstring on WATCHLIST vs trade_signal.
- BTST: "what did the positions that settled today actually return?" - reads directly from
  paper_trades (paper.py), which already computed real entry/exit/return_pct against the
  vendor's own snapshot data. No live-quote fetch needed; the number is already final.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from signal_engine.analysis.breakingtrade import alerts, store

_QUOTES_TIMEOUT = 10


def fetch_ltp(symbol: str, exchange: str = "NSE") -> float | None:
    """Best-effort last traded price via OpenAlgo. None on any failure - a summary missing one
    price is still worth sending; failing the whole report over one bad fetch is not."""
    from signal_engine.config import settings

    try:
        response = httpx.post(
            f"{settings.openalgo_base_url}/api/v1/quotes",
            json={"apikey": settings.openalgo_api_key, "symbol": symbol, "exchange": exchange},
            timeout=_QUOTES_TIMEOUT,
        )
        response.raise_for_status()
        ltp = response.json().get("data", {}).get("ltp")
        return float(ltp) if ltp else None
    except Exception:  # noqa: BLE001 - a summary must never crash the poller
        return None


def _side_for(scan_name: str) -> str:
    """Same heuristic alert_transitions() uses to color a scan - kept here rather than shared
    so this module has no import-time dependency on __main__.py."""
    return (
        "short"
        if scan_name.rstrip().endswith(("Down", "Dn", "Trap", "Breakdown", "PDL"))
        else "long"
    )


def _first_seen_watchlist_calls(day: str) -> list[dict]:
    """One row per symbol: its FIRST watchlist alert today, with the scan that fired it and the
    price at that moment (read from the matching snapshot, joined on symbol + HH:MM - alerts
    and snapshots are stamped by different clocks a few seconds apart, so matching to the
    minute is the reliable join, not an exact timestamp).

    Uses alerts._connect() rather than a bare sqlite3.connect() - that one also runs the
    alerts table's schema/migration, which a fresh database won't have yet.
    """
    with alerts._connect() as conn:
        alert_rows = conn.execute(
            "SELECT symbol, scan, MIN(created_at) FROM alerts "
            "WHERE kind = 'intraday_transition' AND date(created_at) = ? "
            "GROUP BY symbol ORDER BY MIN(created_at)",
            (day,),
        ).fetchall()

        calls = []
        for symbol, scan, first_seen in alert_rows:
            minute = first_seen[:16]  # "YYYY-MM-DD HH:MM"
            price_row = conn.execute(
                "SELECT price FROM snapshots WHERE kind = 'market_profile' AND symbol = ? "
                "AND substr(captured_at, 1, 16) = ?",
                (symbol, minute),
            ).fetchone()
            if not price_row or not price_row[0]:
                continue
            calls.append(
                {
                    "symbol": symbol,
                    "scan": scan,
                    "side": _side_for(scan),
                    "entry": float(price_row[0]),
                    "first_seen": first_seen,
                }
            )
    return calls


def _already_sent_today(day: str, scan: str) -> bool:
    """One eod_summary per report per day - the late-day block can run on more than one poll
    (14:50 and 15:05), and a second identical summary is noise, not new information."""
    with alerts._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE kind = 'eod_summary' AND scan = ? "
            "AND date(created_at) = ? LIMIT 1",
            (scan, day),
        ).fetchone()
    return row is not None


def _row(c: dict, width: int) -> str:
    if c["pct"] is None:
        return f"  {c['symbol']:<{width}} price unavailable ({c['scan']})"
    return (
        f"  {c['symbol']:<{width}} {c['entry']:>9,.2f} -> {c['ltp']:>9,.2f}  "
        f"{c['pct']:+.2f}%  {c['side']}"
    )


def alert_intraday_eod_summary(day: str, captured_at: datetime) -> bool:
    """Score every symbol first flagged today against its closing price. Sends once per day.

    Grouped by outcome (right/wrong/no price) rather than one interleaved list sorted by
    return, since "how many of today's calls actually worked" is the question this answers and
    a reader shouldn't have to tally right/wrong marks down a mixed list to see it. Sent as a
    monospace block (see alerts.send(monospace=True)) so the column alignment actually renders
    on the phone instead of collapsing under Telegram's default proportional font.
    """
    if _already_sent_today(day, "INTRADAY"):
        return False
    calls = _first_seen_watchlist_calls(day)
    if not calls:
        return False

    scored = []
    for call in calls:
        ltp = fetch_ltp(call["symbol"])
        if ltp is None:
            scored.append({**call, "ltp": None, "pct": None, "worked": None})
            continue
        pct = (ltp - call["entry"]) / call["entry"] * 100
        worked = (pct > 0) == (call["side"] == "long")
        scored.append({**call, "ltp": ltp, "pct": pct, "worked": worked})

    right = sorted((c for c in scored if c["worked"] is True), key=lambda c: -c["pct"])
    wrong = sorted((c for c in scored if c["worked"] is False), key=lambda c: c["pct"])
    unresolved = [c for c in scored if c["worked"] is None]
    width = max((len(c["symbol"]) for c in scored), default=8)

    date_label = datetime.strptime(day, "%Y-%m-%d").strftime("%d-%b-%Y")
    # "SCORECARD", not "EOD SUMMARY" - this measures direction-only right/wrong on calls that
    # were mostly never real trades, a different thing from notifier.py's day_summary (real
    # P&L on real fills) and eod_summary.py's own BTST summary below (real basket P&L). All
    # three used to share the generic "EOD SUMMARY" title, which let a trader conflate a
    # watchlist hit-rate with an actual trading result.
    lines = [f"BT WATCHLIST SCORECARD {date_label}"]
    if right or wrong:
        lines.append(f"{len(scored)} called | {len(right)} right, {len(wrong)} wrong")
    else:
        lines.append(f"{len(scored)} called | no closing prices available")

    if right:
        lines += ["", f"RIGHT ({len(right)})"] + [_row(c, width) for c in right]
    if wrong:
        lines += ["", f"WRONG ({len(wrong)})"] + [_row(c, width) for c in wrong]
    if unresolved:
        lines += ["", f"NO PRICE ({len(unresolved)})"] + [_row(c, width) for c in unresolved]

    lines += ["", "Informational only - most of these were never real trades."]
    message = "\n".join(lines)

    delivered, message_id = alerts.send(message, "intraday_transition", monospace=True)
    alerts.record(
        "eod_summary",
        message,
        scan="INTRADAY",
        deliver=False,
        delivered=delivered,
        message_id=message_id,
    )
    return True


def _btst_settled_today(day: str) -> list[dict]:
    from signal_engine.analysis.breakingtrade import paper

    with paper._connect() as conn:
        rows = conn.execute(
            "SELECT symbol, entry_at, entry_price, exit_price, return_pct FROM paper_trades "
            "WHERE strategy = 'BTST' AND status = 'closed' AND date(exit_at) = ? "
            "ORDER BY return_pct DESC",
            (day,),
        ).fetchall()
    return [
        {"symbol": s, "recommended_on": a[:10], "entry": e, "exit": x, "pct": r}
        for s, a, e, x, r in rows
    ]


def btst_metrics(settled: list, strategy_capital: float) -> dict:
    """Basket economics for a day's settled BTST positions.

    ONE CAPITAL FOR THE STRATEGY, split equally across the day's names - not a fixed amount
    per stock. Per-stock notional made the deployed figure grow with the size of the
    watchlist (six names meant Rs 6,00,000 at work), which is not how the account behaves:
    a trader allocates BTST a pot and divides it across whatever the scan produced that day.
    Ten names means smaller positions, not more money.

    BTST has no position size of its own - it is a manual call the engine never places - so
    the basket is the honest framing, and it turns a column of percentages into the number
    the report exists to give: whether the day made or lost money.

    `payoff` (average winner / average loser) is the figure a percentage list hides: a 60%
    hit rate with payoff 0.5 loses money, and nothing in the old summary would have shown it.
    A flat 0.00% counts as a loss, matching how the winners/losers split already worked -
    after costs it is not a win.
    """
    if not settled:
        return {"count": 0, "wins": 0, "losses": 0, "hit_rate": 0.0, "net_pct": 0.0,
                "net_rupees": 0.0, "per_position": 0.0, "deployed": 0.0, "avg_win": None,
                "avg_loss": None, "payoff": None, "best": None, "worst": None, "rows": []}

    per_position = strategy_capital / len(settled)
    rows = [dict(t, rupees=t["pct"] / 100.0 * per_position) for t in settled]
    wins = [r for r in rows if r["pct"] > 0]
    losses = [r for r in rows if r["pct"] <= 0]
    avg_win = sum(r["pct"] for r in wins) / len(wins) if wins else None
    avg_loss = sum(r["pct"] for r in losses) / len(losses) if losses else None

    return {
        "count": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "hit_rate": len(wins) / len(rows) * 100.0,
        "net_pct": sum(r["pct"] for r in rows) / len(rows),
        "net_rupees": sum(r["rupees"] for r in rows),
        "per_position": per_position,
        "deployed": strategy_capital,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": (avg_win / abs(avg_loss)) if (avg_win and avg_loss) else None,
        "best": max(rows, key=lambda r: r["pct"]),
        "worst": min(rows, key=lambda r: r["pct"]),
        "rows": rows,
    }


def alert_btst_eod_summary(day: str) -> bool:
    """Summarize whatever BTST positions settled (closed against today's session) today.

    Reads directly from paper_trades - paper.settle_open_trades() already computed the final
    entry/exit/return_pct, so this is purely a formatting and delivery step, not a new
    calculation. Sends once per day; callers should call this right after
    paper.settle_open_trades() in the same poll that performed the settling.

    Every row states BOTH dates explicitly (recommended on / evaluated on) - a stock in a
    "settled today" report was picked on an EARLIER day (the evening before, per the BTST
    "buy today, sell tomorrow" design) and only closes out today, so "today's report" and
    "today's recommendation" are two different things about two different days. Leaving that
    implicit was confusing to read back later - see STRATEGY-LOG.md, 2026-09-08.
    """
    if _already_sent_today(day, "BTST"):
        return False
    settled = _btst_settled_today(day)
    if not settled:
        return False

    from signal_engine.config import settings

    capital = settings.btst_capital
    m = btst_metrics(settled, capital)
    width = max(len(t["symbol"]) for t in settled)
    date_label = datetime.strptime(day, "%Y-%m-%d").strftime("%d-%b-%Y")

    def _btst_row(t: dict) -> str:
        rec = datetime.strptime(t["recommended_on"], "%Y-%m-%d").strftime("%d-%b")
        return (
            f"  {t['symbol']:<{width}} (rec {rec})  {t['entry']:>9,.2f} -> {t['exit']:>9,.2f}  "
            f"{t['pct']:>+6.2f}%  {t['rupees']:>+10,.0f}"
        )

    # The headline is the question the report exists to answer - did this make or lose money -
    # not a column of percentages the reader has to total up. Equal-weight basket at a stated
    # notional, because BTST has no position size of its own (it is a manual call).
    lines = [
        f"BTST EOD SUMMARY {date_label}",
        f"{m['count']} positions settled | {m['wins']} won, {m['losses']} lost "
        f"| {m['hit_rate']:.0f}% hit rate",
        "",
        f"NET  {m['net_pct']:+.2f}%  =  Rs {m['net_rupees']:+,.0f} on Rs "
        f"{m['deployed']:,.0f} strategy capital",
        f"({m['count']} names, Rs {m['per_position']:,.0f} each - equal weight)",
    ]
    if m["best"] and m["worst"] and m["best"] is not m["worst"]:
        lines.append(
            f"Best  {m['best']['symbol']} {m['best']['pct']:+.2f}%   |   "
            f"Worst  {m['worst']['symbol']} {m['worst']['pct']:+.2f}%"
        )
    if m["avg_win"] is not None and m["avg_loss"] is not None:
        # Payoff is what a percentage list cannot show: a 60% hit rate at payoff 0.5 loses.
        lines.append(
            f"Avg winner {m['avg_win']:+.2f}%  |  Avg loser {m['avg_loss']:+.2f}%  |  "
            f"Payoff {m['payoff']:.2f}"
        )

    wins = [r for r in m["rows"] if r["pct"] > 0]
    losses = [r for r in m["rows"] if r["pct"] <= 0]
    if wins:
        lines += ["", f"WINNERS ({len(wins)})"] + [_btst_row(t) for t in sorted(wins, key=lambda t: -t["pct"])]
    if losses:
        lines += ["", f"LOSERS ({len(losses)})"] + [_btst_row(t) for t in sorted(losses, key=lambda t: t["pct"])]
    lines += [
        "",
        "Equal-weight basket, one position per stock. LONG only, bought on the recommended "
        "date and sold against this session.",
        "Paper only - no real capital was ever at risk. Kept for tracking, not action.",
    ]
    message = "\n".join(lines)

    delivered, message_id = alerts.send(message, "btst", monospace=True)
    alerts.record(
        "eod_summary", message, scan="BTST", deliver=False, delivered=delivered,
        message_id=message_id,
    )
    return True
