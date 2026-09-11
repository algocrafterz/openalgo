"""CLI: breakingtrade.com's own documented trend scans, run against the latest snapshot.

    # zero-argument daily use: auto-discovers the freshest market_profile/volume .xlsx
    # already sitting in signal_engine/pinescripts/intraday/breaking-trade/excel/
    PYTHONPATH=. uv run python -m signal_engine.analysis.breakingtrade

    # or point it at specific files / a different folder
    PYTHONPATH=. uv run python -m signal_engine.analysis.breakingtrade file1.xlsx file2.xlsx
    PYTHONPATH=. uv run python -m signal_engine.analysis.breakingtrade --dir path/to/exports

    # the broader composite-score watchlist (scorer.py) instead of the documented scans
    PYTHONPATH=. uv run python -m signal_engine.analysis.breakingtrade --broad

Default output is scans.py: breakingtrade.com's own named "Runaway / Breakdown / Breakaway /
Value Migration / Live Print" scans, trend-only (fade scans are excluded - see scans.py's
docstring), each labeled with whether its documented time window is currently open. This is a
manual pre-trade research step - review the output, do not wire it to auto-fire orders.
"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
from datetime import time
from time import sleep

import pandas as pd

from signal_engine.analysis.breakingtrade.extractor import (
    DEFAULT_EXCEL_DIR,
    SnapshotFormatError,
    discover_latest,
    load_snapshot,
)
from signal_engine.analysis.breakingtrade.scans import run_scans
from signal_engine.analysis.breakingtrade.scorer import rank

# Poll schedule: (window start, window end, minutes-of-hour to poll on).
#
# Derived from the profile's own structure rather than picked round. A TPO letter is a
# 30-minute period (A 09:15-09:45, B 09:45-10:15, C 10:15-10:45 ...), and Day Type / TPO Pos /
# IB% are all functions of the COMPLETED profile, so new structural information only exists
# just after a period closes - hence the :16/:46 phasing.
#
#   09:20-10:30  The vendor's own earliest windows start here (Runaway 09:20, Gap-Down Rescue
#                09:25, Gap-Up Trap 09:30), and the volume scanner's O bucket - the first five
#                minutes against the 7-day average of the same five minutes - is the earliest
#                read there is on unusual participation. Day Type is still the vendor's "~"
#                provisional estimate until 10:15 and an open-type read this early can still
#                flip, so this stretch is for RECORDING: polling every 5 minutes makes
#                persistence measurable, and a name that holds a scan across several
#                consecutive polls is worth far more than one that appears once.
#   10:16-13:00  Day Type is now valid. The guide calls the C period (10:15-10:45) the "first
#                breakout window - decision point"; Breakaway, Live Print and Value Migration
#                all live here. This is where the confirmed trend entries actually appear.
#   13:00-15:00  Off. Lunch trap, and the guide warns against chasing a breakout once the
#                day's range is already spent.
#   14:50/15:05/15:10  The BTST decision window. Since 2026-08-03 the NSE runs a Closing
#                Auction Session and continuous trading in F&O stocks ENDS AT 15:15, so a
#                delivery order has to be placed before then. The K session (14:15-14:45) is
#                complete at 14:45 and, per the vendor's own reporting, is EVENTUALLY published
#                for 100% of names - but not immediately: 2026-09-08/09-09 both found K barely
#                populated (2-4%) at a live 14:50 poll, minutes after the session closed, and L
#                (14:45-15:15) even sparser that early. A 15:10 mark was added so the vendor has
#                a full 25 minutes of L-session time to publish before the read is taken, versus
#                5 minutes at 14:50 - still 5 minutes clear of the 15:15 cutoff. Polling at
#                15:20 - as this schedule first did - produces a list that can no longer be
#                traded that day.
# The volume scanner's columns are half-hour buckets plus a cumulative Surge x, so polling it
# every five minutes re-reads numbers that have not changed. These marks straddle each bucket
# close, and 15:05/15:10 carry the K/L/M accumulation read the BTST list needs.
VOLUME_FETCH_MINUTES = (5, 10, 16, 46, 50)

POLL_WINDOWS = (
    (time(9, 20), time(10, 30), (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)),
    (time(10, 30), time(13, 0), (1, 16, 31, 46)),
    (time(14, 50), time(15, 10), (50, 5, 10)),
)


def _load_snapshots(args) -> tuple:
    if args.files:
        market_profile = None
        volume = None
        captured_at = None
        for path in args.files:
            try:
                snapshot = load_snapshot(path)
            except SnapshotFormatError as exc:
                print(f"ERROR reading {path}: {exc}", file=sys.stderr)
                sys.exit(1)
            captured_at = captured_at or snapshot.captured_at
            if snapshot.kind == "market_profile":
                market_profile = snapshot.frame
            else:
                volume = snapshot.frame
        return market_profile, volume, captured_at

    found = discover_latest(args.dir)
    mp_snapshot = found.get("market_profile")
    vol_snapshot = found.get("volume")
    if mp_snapshot is None:
        print(f"ERROR: no market_profile .xlsx export found in {args.dir}", file=sys.stderr)
        sys.exit(1)
    captured_at = mp_snapshot.captured_at
    print(f"auto-discovered market_profile snapshot captured_at {captured_at}")
    if vol_snapshot is not None:
        print(f"auto-discovered volume snapshot captured_at {vol_snapshot.captured_at}")
    else:
        print("no volume snapshot found - scans will run without volume confirmation")
    return mp_snapshot.frame, (vol_snapshot.frame if vol_snapshot else None), captured_at


def _print_scans(market_profile, volume, captured_at) -> None:
    results = run_scans(market_profile, volume=volume, captured_at=captured_at)
    hit_count = 0
    for result in results:
        scan = result.scan
        header = f"=== {scan.name} ({scan.direction}) [{result.status}] - {scan.description} ==="
        print(header)
        if scan.mirrored:
            print("(mirrored from the vendor's bullish-only listing - see scans.py docstring)")
        if result.matches.empty:
            print("(no matches)")
        else:
            hit_count += 1
            print(result.matches.to_string(index=False))
        print()

    if hit_count == 0:
        print(
            "No scan has a match yet. Day Type and TPO Pos are LIVE and provisional until the "
            "close (cheat sheet: never treat pre-close reads as final) - these are strict, "
            "multi-condition filters by design, and an empty result is itself a valid answer "
            "('today's market isn't offering that setup [yet]'). Re-run later in the session "
            "as Day Type firms up, especially after 11:00-12:00 IST."
        )


def _print_btst(market_profile, volume, captured_at) -> None:
    from signal_engine.analysis.breakingtrade import btst

    if volume is None:
        print("ERROR: --btst needs a volume snapshot (Del% and the K/L/M sessions).")
        return

    if not btst.is_snapshot_late_enough(captured_at):
        print(
            f"WARNING: snapshot is from {captured_at} - the closing ramp reads the K/L/M "
            f"sessions (14:15 onward), so anything before {btst.EARLIEST_USEFUL_TIME} is "
            "incomplete. Treat the list below as provisional."
        )

    watchlist = btst.candidates(market_profile, volume)
    print(f"=== BTST / carry candidates for the next session ({len(watchlist)}) ===")
    print(watchlist.to_string(index=False) if not watchlist.empty else "(none)")
    print(
        "\nWatchlist, not a signal. Long only by design (no cash-segment BTST short). "
        "An overnight position has no stop - size for a gap, not for an intraday stop."
    )


def _print_broad(market_profile, volume, top_n, min_legs) -> None:
    watchlist = rank(market_profile, volume=volume, top_n=top_n, min_confirming_legs=min_legs)
    print(
        f"universe: {len(market_profile)} symbols, "
        f"{watchlist.excluded_non_trend} excluded (non-tradeable day type)"
    )
    print()
    print(f"=== BULLISH candidates ({len(watchlist.bullish)}) ===")
    print(watchlist.bullish.to_string(index=False) if not watchlist.bullish.empty else "(none)")
    print()
    print(f"=== BEARISH candidates ({len(watchlist.bearish)}) ===")
    print(watchlist.bearish.to_string(index=False) if not watchlist.bearish.empty else "(none)")


def is_trading_day(day) -> bool:
    """Mon-Fri only.

    Without this the poller happily ran all weekend and stored 32 snapshots of FRIDAY'S closing
    data stamped with Saturday and Sunday timestamps - the vendor keeps serving the last
    session's numbers when the market is shut. That is not merely useless, it is corrupting:
    anything computing a "next session" return would see a fabricated 0% day between Friday and
    Monday. Exchange holidays are not handled here; the duplicate-data guard below catches those.
    """
    return day.weekday() < 5


def is_due(now: time, day=None) -> bool:
    """True when `now` falls on a scheduled poll mark inside a POLL_WINDOWS window."""
    if day is not None and not is_trading_day(day):
        return False
    return any(
        start <= now <= end and now.minute in minutes for start, end, minutes in POLL_WINDOWS
    )


# Trailing slack added to each POLL_WINDOWS end when deciding whether the heartbeat should be
# watching at all - just enough to still catch a failure that hits right at a window's close.
HEARTBEAT_WINDOW_BUFFER_MINUTES = 5


def _within_poll_hours(now: time) -> bool:
    """True while `now` sits inside a scheduled POLL_WINDOWS window (plus a short trailing
    buffer) - NOT the same thing as "market hours".

    POLL_WINDOWS has a deliberate ~2-hour gap between the 10:30-13:00 window and 14:50-15:10
    (see its own "13:00-15:00 Off" comment - lunch trap, nothing worth polling for). Silence
    there is the SCHEDULE working, not a failure. Treating "no poll in the last
    HEARTBEAT_STALE_MINUTES" as failure regardless of the schedule fired a false "no successful
    poll" alert every single trading day at ~13:21 (and again at ~14:21, hourly, until the 14:50
    window resumed) - see 2026-09-06 and 2026-09-07's breakingtrade_poller.log. A watchdog that
    cries wolf on a known, intentional gap trains the reader to ignore it exactly on the day it
    is right.
    """
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    for start, end, _marks in POLL_WINDOWS:
        buffered_end = (
            _dt.combine(_dt.today(), end) + _td(minutes=HEARTBEAT_WINDOW_BUFFER_MINUTES)
        ).time()
        if start <= now <= buffered_end:
            return True
    return False


def _fetch_and_report(
    store_it: bool, want_volume: bool, debug_dir, btst_mode: bool = False, session=None
) -> None:
    """One poll: fetch, store, run the scans, and report only what is NEW since last poll."""
    from signal_engine.analysis.breakingtrade import alerts, fetcher, store

    if session is None:
        with fetcher.ScannerSession(debug_dir=debug_dir) as owned:
            _fetch_and_report(store_it, want_volume, debug_dir, btst_mode, session=owned)
        return

    mp_snapshot = session.fetch("market_profile")
    vol_snapshot = session.fetch("volume") if want_volume else None

    captured_at = mp_snapshot.captured_at
    if vol_snapshot is not None:
        # ONE poll, one timestamp. Each fetch stamps itself with its own wall clock, and the
        # second scanner lands 30-60s after the first, so left alone the two halves of the
        # same poll are stored a minute apart and can never be paired back up by time.
        vol_snapshot.captured_at = captured_at
    print(f"fetched {len(mp_snapshot.frame)} market_profile rows at {captured_at}")
    if vol_snapshot is not None:
        print(f"fetched {len(vol_snapshot.frame)} volume rows")

    results = run_scans(
        mp_snapshot.frame,
        volume=vol_snapshot.frame if vol_snapshot else None,
        captured_at=captured_at,
    )

    # A market holiday looks exactly like a trading day to a clock: the vendor keeps serving the
    # last session's table. If this snapshot is identical to the previous stored one, it carries
    # no new information and storing it would fabricate a session.
    if store_it and store.is_duplicate_of_last(mp_snapshot):
        print("  identical to the previous snapshot - not stored (market likely closed)")
        return

    new_by_scan = {}
    if store_it:
        store.save_snapshot(mp_snapshot)
        if vol_snapshot is not None:
            store.save_snapshot(vol_snapshot)
        new_by_scan = store.record_hits(results, captured_at)

        # Alert-only: does any currently-open BREAKINGTRADE position's own setup now read the
        # other way? Runs every real poll, independent of whether this poll produced any new
        # scan matches - a flip is a property of an EXISTING position, not a new one.
        try:
            from signal_engine.analysis.breakingtrade import flip_watch

            flipped = flip_watch.check(mp_snapshot.frame, captured_at)
            if flipped:
                print(f"  {flipped} structure-flip warning(s) sent")
        except Exception as exc:
            print(f"  flip watch failed: {type(exc).__name__}: {exc}")

    if btst_mode:
        print()
        _print_btst(mp_snapshot.frame, vol_snapshot.frame if vol_snapshot else None, captured_at)
        return

    # The BTST decision window (14:50/15:05) alerts the carry list; every other poll alerts only
    # names that ENTERED a scan on this poll.
    if store_it and vol_snapshot is not None and captured_at.time() >= time(14, 45):
        from signal_engine.analysis.breakingtrade import btst

        try:
            from signal_engine.analysis.breakingtrade import paper

            watchlist = btst.candidates(mp_snapshot.frame, vol_snapshot.frame)
            alerts.alert_btst(watchlist, captured_at)
            # Paper-trade it rather than risking capital on an unproven list.
            opened = paper.record_entries(watchlist, captured_at)
            print(f"  paper: opened {opened} hypothetical positions")

            # Save both reads for later comparison - "live" is what was actually actionable,
            # "retrospective" is the K+L+M measurement-only read (never tradeable same-day, M
            # has not traded yet at this poll time either - this captures whatever of it exists
            # so far; a --backfill re-run the next day, once M has fully settled, overwrites
            # this with the complete picture). See btst.retrospective_candidates()'s docstring.
            trade_day = captured_at.strftime("%Y-%m-%d")
            store.save_btst_candidates(trade_day, "live", watchlist)
            retrospective = btst.retrospective_candidates(mp_snapshot.frame, vol_snapshot.frame)
            store.save_btst_candidates(trade_day, "retrospective", retrospective)
            # Settle anything still open from an earlier day now that this poll's snapshot
            # gives settle_open_trades() a next-session close to settle against. Only called
            # from the 14:45+ block (not every poll) so an early-morning read of the new day
            # never gets locked in as if it were the close.
            settled = paper.settle_open_trades()
            if settled:
                print(f"  paper: settled {settled} previously open position(s)")

            from signal_engine.analysis.breakingtrade import eod_summary

            day = captured_at.strftime("%Y-%m-%d")
            if eod_summary.alert_btst_eod_summary(day):
                print("  paper: BTST EOD summary sent")
            if eod_summary.alert_intraday_eod_summary(day, captured_at):
                print("  paper: intraday EOD summary sent")
        except Exception as exc:
            print(f"  BTST alert failed: {type(exc).__name__}: {exc}")
    elif store_it:
        if new_by_scan:
            alerts.alert_transitions(new_by_scan, captured_at, mp_snapshot.frame)
            _emit_trade_signals(new_by_scan, mp_snapshot.frame, captured_at)

        # Re-check EVERY still-pending scan pick from earlier polls, not just symbols new to
        # this one - entry_trigger() waits for a later bar to close beyond the signal bar's
        # level, which almost never exists yet on the same poll a symbol is first flagged. See
        # entry_watch.py's docstring for why this was the reason real signals almost never fired.
        try:
            from signal_engine.analysis.breakingtrade import entry_watch

            confirmed = entry_watch.check(captured_at, mp_snapshot.frame)
            if confirmed:
                print(f"  entry_watch confirmed {confirmed} pending pick(s)")
        except Exception as exc:
            print(f"  entry watch failed: {type(exc).__name__}: {exc}")

    for result in results:
        if result.matches.empty:
            continue
        fresh = set(new_by_scan.get(result.scan.name, []))
        marks = " ".join(f"*{s}" if s in fresh else s for s in result.matches["symbol"].tolist())
        print(f"  [{result.status}] {result.scan.name} ({result.scan.direction}): {marks}")

    if store_it:
        total_new = sum(len(v) for v in new_by_scan.values())
        print(f"  {total_new} NEW (marked *) since the previous stored poll")


def _backfill(args) -> int:
    """Walk the scanner's own day navigation backwards, storing each completed session.

    IMPORTANT LIMITATION: the day navigation yields ONE snapshot per day - the state at the
    CLOSE. That is exactly what the BTST/carry thesis needs (an end-of-day read judged against
    the next session) and it CANNOT validate the intraday scans, whose whole claim is about
    the state at 10:30. Those still need live polling to accumulate.

    Each scanner is walked once (not re-walked per day) - see ScannerSession.iter_history.
    """
    from signal_engine.analysis.breakingtrade import fetcher, store

    by_date = {}
    with fetcher.ScannerSession(debug_dir=args.debug_dir) as session:
        for scanner in ("market_profile", "volume"):
            print(f"walking {scanner} back {args.backfill} sessions...")
            try:
                for snapshot in session.iter_history(scanner, args.backfill):
                    store.save_snapshot(snapshot)
                    by_date.setdefault(snapshot.captured_at, {})[scanner] = snapshot
            except Exception as exc:
                print(f"  {scanner} walk stopped: {type(exc).__name__}: {exc}")

    for captured_at in sorted(by_date):
        pair = by_date[captured_at]
        if "market_profile" not in pair:
            continue
        volume = pair.get("volume")
        results = run_scans(
            pair["market_profile"].frame,
            volume=volume.frame if volume else None,
            captured_at=captured_at,
        )
        store.record_hits(results, captured_at)
        hits = sum(len(r.matches) for r in results)
        print(f"  {captured_at:%Y-%m-%d}: {hits} scan matches")

    print(f"backfilled {len(by_date)} sessions into {store._DB_PATH}")
    return 0


LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "logs",
    "breakingtrade_poller.log",
)
# How long a gap during market hours counts as "something is wrong" rather than "between polls".
HEARTBEAT_STALE_MINUTES = 35
# A poll may still be taken this many minutes after its scheduled mark - covers a restart that
# lands just after a due time, so a bounced process does not silently skip the slot.
CATCH_UP_GRACE_MINUTES = 4

# The process self-terminates at this time rather than idling forever waiting for something
# external (a cron job, poller.sh stop, a manually-typed Ctrl-C) to notice the day is over.
# Nothing is ever scheduled this late - POLL_WINDOWS' last mark is 15:10, continuous F&O
# trading itself ends at 15:15 - so this is pure safety margin, not a real cutoff. Without it,
# a forgotten `--watch` process holds a browser session open indefinitely; it would not poll or
# alert again (nothing matches _due_marks past 15:10), but there is no reason to leave a headless
# Chromium and a login session running unattended overnight when the day's work is done.
AUTO_STOP_TIME = time(15, 20)


def _setup_logging():
    """File logging with rotation, so a failure at 11:31 is still diagnosable next week."""
    from loguru import logger

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    logger.remove()
    fmt = "{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {message}"
    logger.add(sys.stderr, level="INFO", format=fmt)
    logger.add(LOG_PATH, level="DEBUG", format=fmt, rotation="1 week", retention="8 weeks")
    return logger


def _due_marks(day) -> list:
    """Every scheduled poll time for one day, as datetimes."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    if not is_trading_day(day):
        return []
    marks, cursor = [], _dt.combine(day, time(9, 0))
    end = _dt.combine(day, time(15, 40))
    while cursor < end:
        if is_due(cursor.time()):
            marks.append(cursor)
        cursor += _td(minutes=1)
    return marks


def _stored_polls(day) -> set:
    from signal_engine.analysis.breakingtrade import store

    with sqlite3.connect(store._DB_PATH) as conn:
        rows = conn.execute(
            "SELECT DISTINCT captured_at FROM snapshots WHERE captured_at LIKE ? AND kind = ?",
            (f"{day:%Y-%m-%d}%", "market_profile"),
        ).fetchall()
    return {r[0][11:16] for r in rows}


def _audit(args) -> int:
    """Did the poller actually collect what the schedule promised?"""
    from datetime import datetime as _dt

    day = _dt.strptime(args.audit, "%Y-%m-%d").date() if args.audit else _dt.today().date()
    due = _due_marks(day)
    got = _stored_polls(day)
    missed = [m for m in due if f"{m:%H:%M}" not in got]

    print(f"COLLECTION AUDIT for {day}")
    print(f"  scheduled : {len(due)}")
    print(f"  collected : {len(due) - len(missed)}")
    print(
        f"  missed    : {len(missed)}" + (f"  ({len(missed) / len(due) * 100:.0f}%)" if due else "")
    )
    if missed:
        print("  missed at : " + ", ".join(f"{m:%H:%M}" for m in missed[:20]))
        print(f"  log       : {LOG_PATH}")
    return 0 if not missed else 1


def _signals_sent_today() -> int:
    from datetime import datetime as _dt

    from signal_engine.analysis.breakingtrade import store

    try:
        with sqlite3.connect(store._DB_PATH) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE kind = 'trade_signal' AND created_at LIKE ?",
                (f"{_dt.today():%Y-%m-%d}%",),
            ).fetchone()[0]
    except Exception:
        return 0


def _emit_trade_signals(new_by_scan: dict, snapshot, captured_at) -> int:
    """Turn each newly-selected name into up to TWO signals the engine can act on - the two
    outcomes this scanner now trades side by side:

      CONFIRMED   (strategy BREAKINGTRADE, channel intraday-breakingtrade) - only once
                  trigger.plan_trade()'s entry_trigger has seen a bar CLOSE beyond the signal
                  bar's extreme. Real signals are rarer, but each one has already been proven
                  right once before money moves.
      WATCHLIST   (strategy BREAKINGTRADE-WATCHLIST, channel intraday-breakingtrade-watchlist)
                  - trigger.plan_trade_watchlist(), entered at the scan-hit price itself, no
                  confirmation wait. Trades the scanner's own call on the belief the selection
                  alone is already decent quality.

    Both plans are built from the SAME bars/ATR fetch per symbol (one OpenAlgo history call,
    not two). The scan says WHICH symbol; entry, stop and targets come from trigger.py. If the
    bars are unavailable, neither signal is emitted for that symbol - a selection without
    levels is not a trade, and inventing levels to fill the gap would be worse than staying
    silent. See config.yaml's BREAKINGTRADE / BREAKINGTRADE-WATCHLIST strategy_profiles blocks
    for why the two are kept on separate risk slots rather than sharing one.
    """
    from signal_engine.analysis.breakingtrade import alerts, trigger, validate

    # First scan to match a symbol this poll wins both its direction and the reason quoted in
    # the trade-signal alert - a symbol matching two scans at once is rare, and the trade needs
    # exactly one direction regardless.
    directions = {}
    scan_names = {}
    for result_scan, symbols in new_by_scan.items():
        for symbol in symbols:
            directions.setdefault(symbol, "down" if _is_bearish(result_scan) else "up")
            scan_names.setdefault(symbol, result_scan)

    day_types = {}
    prices = {}
    if snapshot is not None:
        if "day_type" in snapshot.columns:
            day_types = dict(zip(snapshot["symbol"], snapshot["day_type"], strict=False))
        if "price" in snapshot.columns:
            prices = dict(zip(snapshot["symbol"], snapshot["price"], strict=False))

    confirmed_emitted = 0
    watchlist_emitted = 0
    for symbol, direction in directions.items():
        scan_name = scan_names.get(symbol)
        try:
            bars = validate.fetch_bars(symbol, captured_at)
            if bars.empty:
                continue
            before = bars[pd.to_datetime(bars["timestamp"]) <= captured_at]
            atr = validate.average_true_range(before) if not before.empty else None
            day_type = day_types.get(symbol)

            confirmed_plan = trigger.plan_trade(symbol, direction, day_type, bars, captured_at, atr)
            if confirmed_plan is not None:
                alerts.alert_trade_signal(confirmed_plan, scan_name=scan_name)
                confirmed_emitted += 1

            price = prices.get(symbol)
            if price:
                watchlist_plan = trigger.plan_trade_watchlist(
                    symbol, direction, day_type, bars, captured_at, atr, price
                )
                if watchlist_plan is not None:
                    alerts.alert_trade_signal(
                        watchlist_plan,
                        strategy="BREAKINGTRADE-WATCHLIST",
                        scan_name=scan_name,
                        kind="trade_signal_watchlist",
                    )
                    watchlist_emitted += 1
        except Exception as exc:
            print(f"  signal for {symbol} skipped: {type(exc).__name__}: {exc}")
    if confirmed_emitted:
        print(f"  emitted {confirmed_emitted} confirmed trade signal(s)")
    if watchlist_emitted:
        print(f"  emitted {watchlist_emitted} watchlist trade signal(s)")
    return confirmed_emitted + watchlist_emitted


def _is_bearish(scan_name: str) -> bool:
    return scan_name.rstrip().endswith(("Down", "Dn", "Trap", "Breakdown", "PDL"))


def _watch(args) -> int:
    """Poll on the documented schedule until interrupted, on ONE browser for the whole day.

    Hardened after 2026-09-04, when 25 of 27 scheduled polls were lost and nothing said so:
      - every attempt is logged to file, with duration and row counts
      - a failed poll is retried by the fetcher, then logged and skipped, never fatal
      - a restart CATCHES UP a mark it landed just after, instead of skipping the slot
      - the browser is rebuilt if it dies, so one bad session does not end the day
      - a stale heartbeat during market hours raises an alert rather than failing silently
    """
    from datetime import datetime as _dt

    from signal_engine.analysis.breakingtrade import alerts, fetcher

    # poller.sh stop sends SIGTERM; without this the process dies without running the shutdown
    # path, so the channel never learns the poller went away.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    logger = _setup_logging()
    logger.info("poller starting; schedule:")
    for start_t, end_t, minutes in POLL_WINDOWS:
        logger.info(f"  {start_t:%H:%M}-{end_t:%H:%M} on {', '.join(f':{m:02d}' for m in minutes)}")

    already_polls = _stored_polls(_dt.today().date())

    # The paper phase is only paper if OpenAlgo is in analyze mode. Check at startup and say so
    # in the log AND on Telegram, so a mis-set mode is discovered on day one, not in the P&L.
    try:
        from signal_engine.analysis.breakingtrade import review as _review

        mode, is_analyze = _review.trading_mode()
        # Seed alerts.py's own mode cache from the check we just made, so alert_started()'s
        # send below doesn't immediately repeat the identical OpenAlgo API call from a cold
        # cache.
        alerts.prime_mode_cache(mode, is_analyze)
        windows = " | ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b, _ in POLL_WINDOWS)
        alerts.alert_started(mode, is_analyze, windows, len(already_polls))
        if is_analyze:
            logger.info(f"OpenAlgo mode: {mode} (analyze - orders are sandboxed)")
        elif mode == "unknown":
            logger.warning("OpenAlgo unreachable - cannot confirm analyze mode")
        else:
            logger.error(f"OpenAlgo mode is {mode.upper()}, NOT analyze - orders would be REAL")
            alerts.alert_health(f"mode is {mode}, NOT analyze - orders would be REAL")
    except Exception as exc:
        logger.warning(f"could not check trading mode: {type(exc).__name__}: {exc}")

    already = already_polls
    if already:
        logger.info(f"resuming - {len(already)} polls already stored today: {sorted(already)}")

    last_success = None
    last_heartbeat_alert = None
    session = None

    try:
        while True:
            now = _dt.now()
            current = now.time()

            if current >= AUTO_STOP_TIME:
                logger.info(f"auto-stop: {AUTO_STOP_TIME:%H:%M} reached, nothing left on today's schedule")
                raise KeyboardInterrupt

            # Staged TP + runner-SL trailing for open BREAKINGTRADE positions, checked on THIS
            # loop's own ~20s cadence rather than the scan schedule's 5-15 minute one. It needs
            # nothing from a scan poll - only OpenAlgo's own live quote (already used, see
            # eod_summary.fetch_ltp) - so tying it to the scanner's slower cadence was leaving
            # real accuracy on the table for no reason. Market hours only, so a poller left
            # running before the open or after the close isn't spending OpenAlgo API calls on
            # nothing.
            if time(9, 15) <= current <= time(15, 30):
                try:
                    from signal_engine.analysis.breakingtrade import tp_watch

                    tp_hits = tp_watch.check(now)
                    if tp_hits:
                        logger.info(f"{tp_hits} TP-hit exit signal(s) sent")
                except Exception as exc:
                    logger.warning(f"tp watch failed: {type(exc).__name__}: {exc}")

            # A mark is due if it is this minute, or was up to a few minutes ago and nothing was
            # stored for it - which is what makes a restart resume rather than skip.
            pending = None
            for mark in _due_marks(now.date()):
                age = (now - mark).total_seconds() / 60
                if 0 <= age <= CATCH_UP_GRACE_MINUTES and f"{mark:%H:%M}" not in _stored_polls(
                    now.date()
                ):
                    pending = mark
                    break

            if pending is not None:
                if session is None:
                    logger.info("opening browser session")
                    session = fetcher.ScannerSession(debug_dir=args.debug_dir).__enter__()

                want_volume = args.volume and pending.minute in VOLUME_FETCH_MINUTES
                started = _dt.now()
                try:
                    _fetch_and_report(args.store, want_volume, args.debug_dir, session=session)
                    took = (_dt.now() - started).total_seconds()
                    last_success = _dt.now()
                    logger.info(f"poll {pending:%H:%M} ok in {took:.0f}s (volume={want_volume})")
                except Exception as exc:
                    logger.exception(f"poll {pending:%H:%M} FAILED: {type(exc).__name__}: {exc}")
                    # A dead browser poisons every later poll, so drop it and rebuild next time.
                    try:
                        if session is not None:
                            session.__exit__(None, None, None)
                    except Exception:
                        logger.warning("could not close the browser session cleanly")
                    session = None

            # Heartbeat: silence during a SCHEDULED poll window is the failure mode that cost
            # 04-Sep - silence during the 13:00-14:50 gap between windows is just the schedule.
            if _within_poll_hours(current) and last_success is not None:
                stale = (_dt.now() - last_success).total_seconds() / 60
                if stale > HEARTBEAT_STALE_MINUTES and (
                    last_heartbeat_alert is None
                    or (_dt.now() - last_heartbeat_alert).total_seconds() > 3600
                ):
                    logger.error(f"no successful poll for {stale:.0f} minutes")
                    alerts.alert_health(f"no successful poll for {stale:.0f} minutes")
                    last_heartbeat_alert = _dt.now()

            sleep(20)
    except KeyboardInterrupt:
        logger.info("poller stopped")
        try:
            polls = len(_stored_polls(_dt.today().date()))
            alerts.alert_stopped(polls, _signals_sent_today())
        except Exception:
            logger.warning("could not send the shutdown notification")
        return 0
    finally:
        if session is not None:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass


#: Mirrors maintenance.DEFAULT_RETENTION_DAYS for the --help text without importing the
#: module (and the browser stack behind it) just to build the parser.
_DEFAULT_RETENTION_DAYS = 120


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "files", nargs="*", help="Specific .xlsx exports (omit to auto-discover the latest)"
    )
    parser.add_argument(
        "--dir", default=DEFAULT_EXCEL_DIR, help="Folder to auto-discover exports from"
    )
    parser.add_argument(
        "--broad",
        action="store_true",
        help="Also print the broad composite-score watchlist (scorer.py) alongside the scans",
    )
    parser.add_argument("--top", type=int, default=20, help="--broad candidates per side")
    parser.add_argument("--min-legs", type=int, default=3, help="--broad min confirming legs")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch a live snapshot headlessly instead of reading .xlsx",
    )
    parser.add_argument(
        "--watch", action="store_true", help="Fetch repeatedly on the documented poll schedule"
    )
    parser.add_argument(
        "--volume", action="store_true", help="Also fetch the volume scanner (--fetch/--watch)"
    )
    parser.add_argument(
        "--no-store", dest="store", action="store_false", help="Do not write to breakingtrade.db"
    )
    parser.add_argument("--debug-dir", default=None, help="Dump page HTML+screenshot on failure")
    parser.add_argument(
        "--set-analyze",
        choices=("on", "off"),
        default=None,
        help="Switch OpenAlgo analyze (paper) mode on or off via its API",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="End-of-day paper review: mode check, signals emitted, trades taken",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Settle and report the BTST paper-trade ledger",
    )
    parser.add_argument(
        "--audit",
        nargs="?",
        const="",
        default=None,
        metavar="YYYY-MM-DD",
        help="Report scheduled vs collected polls for a day (default today)",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        default=0,
        metavar="N",
        help="Store the last N completed sessions using the scanner's own day navigation",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="MFE/MAE forward test over recorded scan hits (needs OpenAlgo running)",
    )
    parser.add_argument(
        "--emerging",
        action="store_true",
        help="Directional setups that have NOT already made their move (room left)",
    )
    parser.add_argument(
        "--btst",
        action="store_true",
        help="Closing-hour accumulation watchlist for the next session (needs a volume snapshot)",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Trim old snapshot history and clear the browser cache (see maintenance.py)",
    )
    parser.add_argument(
        "--prune-days",
        type=int,
        default=None,
        help=f"Retention window for --prune (default {_DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --prune: report what would be removed, remove nothing",
    )
    args = parser.parse_args()

    if args.prune:
        from signal_engine.analysis.breakingtrade import maintenance

        print(maintenance.run(
            retention_days=args.prune_days or maintenance.DEFAULT_RETENTION_DAYS,
            dry_run=args.dry_run,
        ))
        return 0

    if args.set_analyze is not None:
        from signal_engine.analysis.breakingtrade import review

        mode, is_analyze = review.set_analyze_mode(args.set_analyze == "on")
        print(f"OpenAlgo mode is now: {mode} (analyze={is_analyze})")
        return 0 if is_analyze == (args.set_analyze == "on") else 1

    if args.review:
        from signal_engine.analysis.breakingtrade import review

        review.report()
        return 0

    if args.paper:
        from signal_engine.analysis.breakingtrade import paper

        paper.report()
        return 0

    if args.audit is not None:
        return _audit(args)

    if args.backfill:
        return _backfill(args)

    if args.validate:
        from signal_engine.analysis.breakingtrade import validate

        validate.run()
        return 0

    if args.watch:
        return _watch(args)
    if args.fetch:
        want_volume = (
            args.volume or args.btst or args.emerging
        )  # BTST cannot run without Del% and K/L/M
        if args.emerging:
            from signal_engine.analysis.breakingtrade import emerging, fetcher

            with fetcher.ScannerSession(debug_dir=args.debug_dir) as session:
                mp = session.fetch("market_profile")
                vol = session.fetch("volume")
                vol.captured_at = mp.captured_at
            picks = emerging.candidates(mp.frame, vol.frame, mp.captured_at)
            print(f"snapshot {mp.captured_at}")
            print(f"=== EMERGING: directional structure, move not yet made ({len(picks)}) ===")
            print(picks.to_string(index=False) if not picks.empty else "(none)")
            return 0
        _fetch_and_report(args.store, want_volume, args.debug_dir, btst_mode=args.btst)
        return 0

    market_profile, volume, captured_at = _load_snapshots(args)

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 160)

    print(f"snapshot captured_at: {captured_at}")
    print()

    if args.emerging:
        from signal_engine.analysis.breakingtrade import emerging

        picks = emerging.candidates(market_profile, volume, captured_at)
        print(f"=== EMERGING: directional structure, move not yet made ({len(picks)}) ===")
        print(picks.to_string(index=False) if not picks.empty else "(none)")
        return 0

    if args.btst:
        _print_btst(market_profile, volume, captured_at)
        return 0

    _print_scans(market_profile, volume, captured_at)

    if args.broad:
        print()
        _print_broad(market_profile, volume, args.top, args.min_legs)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
