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
#   15:05/15:20  Two polls only, and NOT for entries: the K/L/M periods are where a closing
#                ramp and the Del% accumulation read show up, which the guide frames as a
#                "watchlist for tomorrow's open more than a same-day trade".
# The volume scanner's columns are half-hour buckets plus a cumulative Surge x, so polling it
# every five minutes re-reads numbers that have not changed. These marks straddle each bucket
# close, and 15:05/15:20 carry the K/L/M accumulation read the BTST list needs.
VOLUME_FETCH_MINUTES = (5, 16, 20, 46)

POLL_WINDOWS = (
    (time(9, 20), time(10, 30), (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)),
    (time(10, 30), time(13, 0), (1, 16, 31, 46)),
    (time(15, 0), time(15, 25), (5, 20)),
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
    from signal_engine.analysis.breakingtrade import fetcher, store

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


def _watch(args) -> int:
    """Poll on the documented schedule until interrupted, on ONE browser for the whole day."""
    from datetime import datetime as _dt

    from signal_engine.analysis.breakingtrade import fetcher

    print("watching on this schedule (Ctrl-C to stop):")
    for start, end, minutes in POLL_WINDOWS:
        marks = ", ".join(f":{m:02d}" for m in minutes)
        print(f"  {start:%H:%M}-{end:%H:%M} on {marks}")
    print(
        f"  volume scanner only on {VOLUME_FETCH_MINUTES} past the hour "
        "(its buckets are half-hourly, so fetching it more often reads the same numbers)"
    )

    last_polled = None
    with fetcher.ScannerSession(debug_dir=args.debug_dir) as session:
        while True:
            now = _dt.now()
            marker = (now.hour, now.minute)

            if is_due(now.time()) and marker != last_polled:
                last_polled = marker
                want_volume = args.volume and now.minute in VOLUME_FETCH_MINUTES
                try:
                    _fetch_and_report(args.store, want_volume, args.debug_dir, session=session)
                except Exception as exc:
                    # One bad poll (session lapse, slow render) must not end the day's watch.
                    print(f"  poll failed at {now:%H:%M}: {type(exc).__name__}: {exc}")
            sleep(20)


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
