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
from collections import defaultdict
from datetime import date, datetime

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


def report_by_strategy(positions: list, declined: list) -> None:
    """Per-strategy scorecard, split by mode. The answer to "which strategy is working".

    Split by MODE and never pooled: a paper fill carries no slippage, so averaging a paper
    row with a live one produces a number that describes neither. Rows with no mode are the
    221 trades that predate the trade_mode column and are shown as `unknown` rather than
    assumed live.

    Declines are reported beside the trades on purpose. A strategy that fired 20 signals and
    traded 4 is a different thing from one that fired 4, and the ratio is invisible if only
    the taken trades are counted.
    """
    if not positions and not declined:
        return

    buckets: dict[tuple, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "r": 0.0, "scored": 0, "declined": 0}
    )
    for p in positions:
        b = buckets[(p.strategy or "?", p.trade_mode or "unknown")]
        b["n"] += 1
        r = p.realised_r
        if r is not None:
            b["r"] += r
            b["scored"] += 1
            if r > 0:
                b["wins"] += 1
    for d in declined:
        buckets[(d.get("strategy") or "?", d.get("trade_mode") or "unknown")]["declined"] += 1

    print("\n" + "=" * 78)
    print("BY STRATEGY")
    print("=" * 78)
    print(f"{'strategy':<16}{'mode':<10}{'trades':>7}{'declined':>10}{'scored':>8}"
          f"{'win%':>7}{'sum R':>9}{'avg R':>8}")
    for (strategy, mode), b in sorted(buckets.items()):
        win = f"{b['wins'] / b['scored'] * 100:.0f}%" if b["scored"] else "-"
        avg = f"{b['r'] / b['scored']:+.2f}" if b["scored"] else "-"
        total = f"{b['r']:+.2f}" if b["scored"] else "-"
        print(f"{strategy:<16}{mode:<10}{b['n']:>7}{b['declined']:>10}{b['scored']:>8}"
              f"{win:>7}{total:>9}{avg:>8}")

    if any(m == "unknown" for _, m in buckets):
        print("\n  `unknown` = rows written before trades.db carried a mode column. Not")
        print("  assumed live: the engine has an analyze mode, so a guess would be wrong")
        print("  in a way nothing in the data could reveal.")


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
    positions = build_ledger(events, fills)
    declined = load_declined(db_path=args.db, since=since)
    report(positions, show_positions=args.positions)
    report_by_strategy(positions, declined)
    report_declined(declined, show_all=args.declined)


if __name__ == "__main__":
    main()
