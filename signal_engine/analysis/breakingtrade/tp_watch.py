"""Wires trigger.py's already-computed staged target ladder through to the SAME staged-TP and
runner-SL-trailing mechanism ORB/BREAKOUT already use, instead of a separate one.

WHY THIS EXISTS

trigger.plan_trade() computes three target levels (1.0R/1.5R/2.0R of the IB range) and a
day-type-aware split, but alert_trade_signal() only ever sent the first level as a flat TP -
the rest was computed and discarded, so every BreakingTrade position exited 100% at TP1 with
no partial booking and no SL trailing as price ran. The engine already has real, hardened
infrastructure for exactly this - staged partial exits via ExitQtyPct, and a runner stop that
ratchets to each TP level hit (main.py's _resolve_exit_qty / compute_next_tp / TP_LEVEL_R
multipliers) - built for ORB/BREAKOUT, which are PineScript strategies watching every tick on
their own chart and firing a "TP HIT" alert the instant a level crosses. This module is the
BreakingTrade-side equivalent of that PineScript alert, driven from Python instead.

CADENCE: NOT TIED TO THE SCANNER'S 5-15 MINUTE SCHEDULE
check() needs nothing from a scan poll - only an open position (trades.db) and a live quote
(OpenAlgo's own /api/v1/quotes, via eod_summary.fetch_ltp - never the scanner's own scraped
price, which can be a poll cycle old). __main__.py's _watch() calls it on the poller's own
outer loop, which already runs every ~20s all day regardless of whether a scan poll is due -
so this checks roughly as often as that loop iterates, not once per 5-15 minute scan window.
Still not tick-level like PineScript watching its own chart - a level can still be crossed and
run past, or reverse, within a ~20s gap - but the gap is the loop's own cadence, not the much
coarser scanner schedule it happened to inherit at first.

WHY THE SPLIT IS A FIXED DEFAULT, NOT DAY-TYPE-AWARE (YET)
trigger.py computes a different split for trend days (ride more of the position) vs normal
days (book early) - a real, deliberate distinction. Reproducing it here would need the split
persisted somewhere at signal time and correlated back to the right open position later, across
two different processes (the poller, which computes it, and the trading engine, which executes
independently and doesn't share memory or a clock). That correlation problem is solvable but is
its own separate piece of work with its own failure modes; shipping a single fixed default
first, on a mechanism nothing has exercised with a real signal yet, is the smaller thing to get
right before adding a second dimension of complexity. See DEFAULT_SPLIT below.
"""

from __future__ import annotations

from datetime import datetime

from signal_engine.analysis.breakingtrade import alerts, eod_summary, flip_watch

STRATEGY = "BREAKINGTRADE"

# Same R-multiples as main.py's _TP_LEVEL_R_MULTIPLIERS (TP1/TP1.5/TP2 subset - BreakingTrade's
# own ladder, trigger.TARGET_IB_MULTIPLES, is exactly (1.0, 1.5, 2.0), so it lines up with the
# engine's EXISTING hard-coded TP1/TP1.5/TP2 labels with no changes needed there.
_LEVEL_SEQUENCE = ("TP1", "TP1.5", "TP2")
_LEVEL_MULTIPLIER = {"TP1": 1.0, "TP1.5": 1.5, "TP2": 2.0}

# trigger.DEFAULT_SPLIT (0.50, 0.30, 0.20) - fractions of the ORIGINAL position. See the module
# docstring for why this doesn't yet vary by day type the way trigger.py's own computation does.
DEFAULT_SPLIT = (0.50, 0.30, 0.20)


def _next_level(last_level: str | None) -> str | None:
    """The level after `last_level` in the sequence, or the first level if None, or None if
    `last_level` is already the final one."""
    if last_level is None:
        return _LEVEL_SEQUENCE[0]
    if last_level not in _LEVEL_SEQUENCE:
        return None
    idx = _LEVEL_SEQUENCE.index(last_level)
    return _LEVEL_SEQUENCE[idx + 1] if idx + 1 < len(_LEVEL_SEQUENCE) else None


def _expected_price(entry: float, tp1: float, level: str, direction: str) -> float:
    """Price of `level`, derived from TP1's R-distance - identical formula to main.py's
    _tp_level_price()/compute_next_tp(), so the level this module reports crossed is the exact
    same price the engine will independently derive when it processes the exit."""
    r_distance = abs(tp1 - entry)
    sign = 1 if direction == "LONG" else -1
    return entry + sign * _LEVEL_MULTIPLIER[level] * r_distance


def _exit_qty_pct_of_remaining(split: tuple, level_index: int) -> float:
    """Convert a split expressed as fractions of the ORIGINAL position into the ExitQtyPct the
    engine expects - a fraction of what is currently OPEN, since main.py reduces the tracked
    quantity after each partial exit and applies the next ExitQtyPct against that smaller
    figure (the same convention PineScript's own ExitQtyPct already follows). The final level
    always closes 100% of whatever remains, matching "no next TP level = full exit"."""
    if level_index >= len(split) - 1:
        return 100.0
    already_exited = sum(split[:level_index])
    remaining_before = 1.0 - already_exited
    if remaining_before <= 0:
        return 100.0
    return min(100.0, (split[level_index] / remaining_before) * 100.0)


def _last_level_hit(symbol: str, since: str) -> str | None:
    """Highest TP level already fired for this position's current entry - read from our own
    delivery record (alerts table), not trades.db, since trades.db has no dedicated tp_level
    column to query against.

    Only DELIVERED rows count as "hit". If Telegram delivery ever fails, the engine never
    received the exit instruction - counting the attempt as done anyway would dedup out any
    retry and leave that position silently stuck at the previous level forever. An undelivered
    attempt is retried on the next poll instead, exactly like a poll that found nothing to send.
    """
    # datetime() on BOTH sides, not a raw string compare. trades.db writes executed_at with
    # datetime.isoformat() ("2026-09-11T11:46:43.327851") while this database writes
    # created_at with a space ("2026-09-11 14:52:16"), and at index 10 ' ' (0x20) sorts
    # BELOW 'T' (0x54) - so a LATER alert always compared as smaller, the filter matched
    # nothing, and this function returned None every time. _next_level(None) is "TP1", so
    # the watcher re-sent TP1 on every poll for as long as the position stayed open: five
    # times for AXISBANK on 2026-09-11 between 14:52 and 14:54. Each carries ExitQtyPct 50,
    # so against a live position that is half the remainder exited, five times over.
    with alerts._connect() as conn:
        rows = conn.execute(
            f"SELECT scan FROM alerts WHERE kind = 'tp_hit' AND symbol = ? "
            f"AND {alerts.SINCE_CLAUSE} AND delivered = 1",
            (symbol, since),
        ).fetchall()
    hit = {r[0] for r in rows}
    last = None
    for level in _LEVEL_SEQUENCE:
        if level in hit:
            last = level
    return last


def _send_tp_hit(symbol: str, direction: str, price: float, level: str, exit_qty_pct: float) -> None:
    """Canonical EXIT alert, the exact shape normalizer.py already produces for ORB's PineScript
    TP-HIT alerts (Entry/SL as 0.0 placeholders - the engine looks up the real position from its
    own tracker, not from this message) - so no special case is needed anywhere downstream.

    Always records the attempt (delivered or not) - see _last_level_hit for why the delivered
    flag, not the record itself, is what gates a retry.
    """
    message = "\n".join(
        [
            f"{STRATEGY} EXIT",
            f"Symbol: {symbol}",
            "Entry: 0.0",
            "SL: 0.0",
            f"TP: {price}",
            f"TPLevel: {level}",
            f"ExitQtyPct: {exit_qty_pct:.1f}",
        ]
    )
    delivered, message_id = alerts.send(message, "trade_signal")
    alerts.record(
        "tp_hit", message, symbol=symbol, direction=direction, scan=level,
        deliver=False, delivered=delivered, message_id=message_id,
    )


def check(captured_at: datetime) -> int:
    """For every open BreakingTrade position, check whether price has reached its next staged
    target; if so, send the exit alert that drives a real partial (or final) exit through the
    engine's existing multi-TP pipeline. Returns how many exit alerts were sent this poll."""
    positions = flip_watch._open_positions(captured_at.strftime("%Y-%m-%d"))
    if not positions:
        return 0

    sent = 0
    for symbol, pos in positions.items():
        last_level = _last_level_hit(symbol, pos["executed_at"])
        level = _next_level(last_level)
        if level is None:
            continue  # already booked out at the final target

        ltp = eod_summary.fetch_ltp(symbol)
        if ltp is None:
            continue

        expected = _expected_price(pos["entry"], pos["tp"], level, pos["direction"])
        crossed = ltp >= expected if pos["direction"] == "LONG" else ltp <= expected
        if not crossed:
            continue

        level_index = _LEVEL_SEQUENCE.index(level)
        exit_qty_pct = _exit_qty_pct_of_remaining(DEFAULT_SPLIT, level_index)
        _send_tp_hit(symbol, pos["direction"], ltp, level, exit_qty_pct)
        sent += 1
    return sent
