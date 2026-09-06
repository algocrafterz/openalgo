"""Post-trade report: what the strategy asked for vs what the broker delivered.

    # after the close, capture the broker's tradebook - it is WIPED daily and a past
    # session can never be re-queried, so this must run every trading evening
    PYTHONPATH=. uv run python -m signal_engine.analysis --snapshot

    # the report, over every snapshot captured so far
    PYTHONPATH=. uv run python -m signal_engine.analysis
    PYTHONPATH=. uv run python -m signal_engine.analysis --since 2026-08-01 --positions

Read the reconciliation block FIRST. Expectancy computed over a ledger with unresolved
flags is expectancy over a fiction.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
from datetime import date, datetime

from collections import defaultdict

from signal_engine.analysis.ledger import (
    FLAG_NO_FILL,
    FLAG_OPEN_AT_EOD,
    FLAG_UNMATCHED_FILL,
    build_ledger,
    load_declined,
    load_engine_events,
    load_fills,
)

SNAP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tradebook"
)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

async def _fetch_tradebook() -> list[dict]:
    from signal_engine.api_client import _post_tolerant

    data = await _post_tolerant("tradebook", {})
    if isinstance(data, tuple):
        data = data[0] if data else {}
    if not isinstance(data, dict) or data.get("status") != "success":
        raise RuntimeError(f"tradebook call failed: {data}")
    return list(data.get("data") or [])


def snapshot() -> str:
    """Save today's broker tradebook. Idempotent - re-running overwrites the same day."""
    os.makedirs(SNAP_DIR, exist_ok=True)
    trades = asyncio.run(_fetch_tradebook())
    path = os.path.join(SNAP_DIR, f"tradebook_{date.today().isoformat()}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(trades, fh, indent=1)
    print(f"saved {len(trades)} broker trades -> {path}")
    return path


def load_snapshots() -> list[dict]:
    out: list[dict] = []
    for f in sorted(glob.glob(os.path.join(SNAP_DIR, "tradebook_*.json"))):
        with open(f, encoding="utf-8") as fh:
            try:
                out += json.load(fh)
            except json.JSONDecodeError:
                print(f"  (skipped unreadable snapshot {os.path.basename(f)})")
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt(v, spec=".2f", dash="-"):
    return dash if v is None else format(v, spec)


def report(positions: list, show_positions: bool = False) -> None:
    import statistics as st

    real = [p for p in positions if FLAG_UNMATCHED_FILL not in p.flags]
    print(f"\n{'=' * 78}\nRECONCILIATION  ({len(real)} positions from the engine's audit trail)")
    print("=" * 78)
    if not real:
        print("nothing to report")
        return

    n_filled = sum(1 for p in real if p.entry.filled)
    counts = {
        "entry order sent, never filled": sum(1 for p in real if FLAG_NO_FILL in p.flags),
        "still open at end of session": sum(1 for p in real if FLAG_OPEN_AT_EOD in p.flags),
        "no exit event was ever sent": sum(1 for p in real if not p.exits),
        "broker fills the engine never sent": sum(
            1 for p in positions if FLAG_UNMATCHED_FILL in p.flags),
    }
    print(f"  entries with a confirmed broker fill : {n_filled}/{len(real)}")
    for k, v in counts.items():
        mark = "   <-- investigate" if v else ""
        print(f"  {k:<36}: {v}{mark}")
    if not n_filled:
        print("\n  No broker fills are joined to any order. Either no tradebook snapshot has")
        print("  been captured yet, or the order ids do not match. Run --snapshot after the")
        print("  close; without it only signal-level fields below are meaningful.")

    closed = [p for p in real if p.realised_r is not None]
    print(f"\n{'-' * 78}\nEXECUTION QUALITY  ({len(closed)} round trips with real fills)\n{'-' * 78}")
    if closed:
        ent = [p.entry_slippage_bps for p in closed if p.entry_slippage_bps is not None]
        ext = [p.exit_slippage_bps for p in closed if p.exit_slippage_bps is not None]
        rs = [p.realised_r for p in closed]
        pnl = [p.gross_pnl for p in closed if p.gross_pnl is not None]
        print(f"  entry slippage   median {_fmt(st.median(ent) if ent else None)} bps"
              f"   worst {_fmt(max(ent) if ent else None)} bps   (n={len(ent)})")
        print(f"  exit slippage    median {_fmt(st.median(ext) if ext else None)} bps"
              f"   worst {_fmt(max(ext) if ext else None)} bps   (n={len(ext)}, SL legs excluded)")
        if ent and ext:
            print(f"  round-trip execution cost, median: {st.median(ent) + st.median(ext):.2f} bps")
        print(f"\n  realised R       mean {st.mean(rs):+.3f}   median {st.median(rs):+.3f}"
              f"   win {100 * sum(1 for r in rs if r > 0) / len(rs):.1f}%")
        print(f"  gross P&L        {sum(pnl):+,.0f} INR over {len(pnl)} closed positions")
        hold = [p.hold_minutes for p in closed if p.hold_minutes is not None]
        if hold:
            print(f"  hold time        median {st.median(hold):.0f} min")
    else:
        print("  none - needs a tradebook snapshot to compute anything here")

    mix: dict[str, list] = {}
    for p in real:
        mix.setdefault(p.exit_reasons, []).append(p)
    print(f"\n{'-' * 78}\nEXIT PATH\n{'-' * 78}")
    for k, v in sorted(mix.items(), key=lambda kv: -len(kv[1])):
        rr = [x.realised_r for x in v if x.realised_r is not None]
        print(f"  {k:<28} n={len(v):<5} mean R {_fmt(st.mean(rr) if rr else None, '+.3f')}")

    if show_positions:
        print(f"\n{'-' * 78}\nPOSITIONS\n{'-' * 78}")
        hdr = (f"{'day':<11}{'symbol':<14}{'dir':<5}{'signal':>9}{'fill':>9}"
               f"{'slip_bps':>9}{'exit':>9}{'R':>7}  path")
        print(hdr)
        for p in sorted(real, key=lambda x: (x.day, x.symbol)):
            print(f"{str(p.day):<11}{p.symbol:<14}{'LONG' if p.direction > 0 else 'SHORT':<5}"
                  f"{p.entry.signal_price:>9.2f}{_fmt(p.entry.fill_price, '9.2f'):>9}"
                  f"{_fmt(p.entry_slippage_bps, '9.1f'):>9}"
                  f"{_fmt(p.avg_exit, '9.2f'):>9}{_fmt(p.realised_r, '7.2f'):>7}  "
                  f"{p.exit_reasons}{' [' + ','.join(p.flags) + ']' if p.flags else ''}")


def report_declined(declined: list, show_all: bool = False) -> None:
    """Signals the engine refused before sending an order, grouped by which gate stopped them.

    Reported next to the ledger rather than inside it, because a decline is not a broken trade
    -- it is a trade that never started, and mixing the two makes every decline look like an
    unfilled order. The pair of numbers is the point: a thin week reads completely differently
    depending on whether the strategy found nothing or the risk limits refused what it found.
    """
    if not declined:
        print("\ndeclined signals: none")
        return

    by_stage: dict[str, list] = defaultdict(list)
    for row in declined:
        stage = (row.get("message") or "").split("]")[0].lstrip("[") or "unknown"
        by_stage[stage].append(row)

    print(f"\ndeclined signals: {len(declined)} (no order was sent for these)")
    for stage, rows in sorted(by_stage.items(), key=lambda kv: -len(kv[1])):
        symbols = ", ".join(sorted({r["symbol"] for r in rows})[:6])
        more = "" if len({r["symbol"] for r in rows}) <= 6 else ", ..."
        print(f"  {len(rows):3}  {stage:24} {symbols}{more}")

    if show_all:
        print()
        for r in declined:
            print(f"  {r['_ts']:%Y-%m-%d %H:%M}  {r['symbol']:12} {r['direction']:5} "
                  f"entry={r['entry']:<10.2f} {r.get('message') or ''}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="signal_engine.analysis")
    ap.add_argument("--snapshot", action="store_true",
                    help="capture today's broker tradebook and exit (run after the close)")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD, filter the ledger")
    ap.add_argument("--positions", action="store_true", help="print every position")
    ap.add_argument("--db", default=None, help="path to trades.db")
    ap.add_argument("--declined", action="store_true",
                    help="list every declined signal, not just the per-reason counts")
    args = ap.parse_args()

    if args.snapshot:
        snapshot()
        return

    since = datetime.strptime(args.since, "%Y-%m-%d").date() if args.since else None
    events = load_engine_events(db_path=args.db, since=since)
    fills = load_fills(load_snapshots())
    print(f"engine events: {len(events)}   broker orders with fills: {len(fills)}")
    report(build_ledger(events, fills), show_positions=args.positions)
    report_declined(load_declined(db_path=args.db, since=since), show_all=args.declined)


if __name__ == "__main__":
    main()
