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
#   14:50/15:05  The BTST decision window. Since 2026-08-03 the NSE runs a Closing Auction
#                Session and continuous trading in F&O stocks ENDS AT 15:15, so a delivery
#                order has to be placed before then. The K session (14:15-14:45) is complete at
#                14:45 and is reported for 100% of names, which leaves roughly 25 minutes to
#                act. Polling at 15:20 - as this schedule first did - produces a list that
#                can no longer be traded that day.
# The volume scanner's columns are half-hour buckets plus a cumulative Surge x, so polling it
# every five minutes re-reads numbers that have not changed. These marks straddle each bucket
# close, and 15:05/15:20 carry the K/L/M accumulation read the BTST list needs.
VOLUME_FETCH_MINUTES = (5, 16, 46, 50)

POLL_WINDOWS = (
    (time(9, 20), time(10, 30), (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)),
    (time(10, 30), time(13, 0), (1, 16, 31, 46)),
    (time(14, 50), time(15, 10), (50, 5)),
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


def is_due(now: time) -> bool:
    """True when `now` falls on a scheduled poll mark inside a POLL_WINDOWS window."""
    return any(
        start <= now <= end and now.minute in minutes for start, end, minutes in POLL_WINDOWS
    )


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

    new_by_scan = {}
    if store_it:
        store.save_snapshot(mp_snapshot)
        if vol_snapshot is not None:
            store.save_snapshot(vol_snapshot)
        new_by_scan = store.record_hits(results, captured_at)

    if btst_mode:
        print()
        _print_btst(mp_snapshot.frame, vol_snapshot.frame if vol_snapshot else None, captured_at)
        return

    # The BTST decision window (14:50/15:05) alerts the carry list; every other poll alerts only
    # names that ENTERED a scan on this poll.
    if store_it and vol_snapshot is not None and captured_at.time() >= time(14, 45):
        from signal_engine.analysis.breakingtrade import btst

        try:
            watchlist = btst.candidates(mp_snapshot.frame, vol_snapshot.frame)
            alerts.alert_btst(watchlist, captured_at)
        except Exception as exc:
            print(f"  BTST alert failed: {type(exc).__name__}: {exc}")
    elif store_it and new_by_scan:
        alerts.alert_transitions(new_by_scan, captured_at, mp_snapshot.frame)

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

    logger = _setup_logging()
    logger.info("poller starting; schedule:")
    for start_t, end_t, minutes in POLL_WINDOWS:
        logger.info(f"  {start_t:%H:%M}-{end_t:%H:%M} on {', '.join(f':{m:02d}' for m in minutes)}")

    already = _stored_polls(_dt.today().date())
    if already:
        logger.info(f"resuming - {len(already)} polls already stored today: {sorted(already)}")

    last_success = None
    last_heartbeat_alert = None
    session = None

    try:
        while True:
            now = _dt.now()
            current = now.time()

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

            # Heartbeat: silence during market hours is the failure mode that cost 04-Sep.
            if time(9, 20) <= current <= time(15, 15) and last_success is not None:
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
        logger.info("poller stopped by user")
        return 0
    finally:
        if session is not None:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass


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
    args = parser.parse_args()

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
