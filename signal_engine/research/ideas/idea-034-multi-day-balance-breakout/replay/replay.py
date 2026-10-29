"""Historical replay of the FROZEN IDEA-034 v3 rules through the recorder's own code (read-only).

    PYTHONPATH=. uv run python <this dir>/replay.py --source historify|published
        [--start 2017-01-01] [--end 2026-10-28] [--workers 10] [--frames <published frames.pkl>]

Every session is run through `analysis.idea034_paper.record_day(day, dry_run=True, ...)`, the
same function the forward recorder calls, so signals, the 15-minute limit fill, stop, target,
14:45 exit, capacity cap, charges and slippage are the frozen code, not a re-implementation.
Only the bar loaders are injected:

  historify  5m bars from Historify raw 1m (the recorder's own `_historify5` call, loaded once
             per year and sliced per day exactly as the recorder's per-day call would be)
  published  5m bars from the adjusted published dataset (nse-intraday-5m-20261021-002, the
             backtest's source, read from the frames pickle built by
             experiments/2026-10-ib-reference-levels/inputs_frames.py)

Fills always use Historify raw 1m (`_historify1`, as the recorder does). For the published
source the signal prices (adjusted) and the fill prices (raw) can sit on different bases, so each
signal carries `basis_ratio` (published 5m close of the signal bar / raw 1m close of the same
minute) and `basis_ok` (within two ticks).

Nothing is written to the recorder's ledger (dry_run=True). Missing data is never read as "no
signal": per symbol-session the wrapper records NOT_LISTED (before the symbol's first bar),
DATA_GAP (listed but absent or incomplete that day, so the recorder skips it), HISTORY_GAP
(present but one of the 5 prior sessions short or missing), and per signal FILL_GAP (1m bars
missing in the limit window or before 14:45). Outputs go to results/<source>_signals.csv,
results/<source>_days.csv and results/<source>_symbol_days.csv.
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "results"
CACHE = OUT / "cache"
LOOKBACK_DAYS = 60  # > the recorder's HISTORY_DAYS (45), so every per-day slice is complete
REF_SHARE = 0.5  # a reference session: at least half the universe has bars that day
MIN_FILL_BARS = 12  # of the 15 one-minute bars in the limit window


def _universe() -> list[str]:
    from signal_engine.analysis import idea034_paper as m

    return m.UNIVERSE.read_text().split()


def _slicer(frames: dict[str, pd.DataFrame]):
    """load5 replacement: the same [start, end] whole-day slice the recorder asks Historify for."""
    dates = {s: f.index.date for s, f in frames.items()}

    def load5(symbols: list[str], start: dt.date, end: dt.date) -> dict[str, pd.DataFrame]:
        out = {}
        for s in symbols:
            f = frames.get(s)
            if f is None:
                continue
            d = dates[s]
            part = f[(d >= start) & (d <= end)]
            if len(part):
                out[s] = part
        return out

    return load5


def _bar_counts(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """5m bars per symbol-session before 15:30 (the daily-table session rule counts these)."""
    rows = []
    for s, f in frames.items():
        cont = f[f.index.time < dt.time(15, 30)]
        c = cont.groupby(cont.index.date).size()
        rows.append(pd.DataFrame({"symbol": s, "day": c.index, "bars": c.to_numpy()}))
    return (
        pd.concat(rows, ignore_index=True)
        if rows
        else pd.DataFrame(columns=["symbol", "day", "bars"])
    )


def _symbol_days(
    counts: pd.DataFrame,
    sessions: list[dt.date],
    universe: list[str],
    first_bar: dict[str, dt.date],
    days: list[dt.date],
) -> pd.DataFrame:
    from signal_engine.analysis import idea034_paper as m

    min_bars = m.PARAMS.min_session_bars
    bars = counts.set_index(["symbol", "day"])["bars"].to_dict()
    pos = {d: i for i, d in enumerate(sessions)}
    rows = []
    for day in days:
        i = pos[day]
        prior = sessions[max(0, i - m.PARAMS.balance_days) : i]
        for s in universe:
            fb = first_bar.get(s)
            if fb is None or day < fb:
                state = "NOT_LISTED"
            elif bars.get((s, day), 0) < min_bars:
                state = "DATA_GAP"
            elif any(bars.get((s, p), 0) < min_bars for p in prior if p >= fb):
                state = "HISTORY_GAP"
            else:
                state = "OK"
            rows.append((str(day), s, state))
    return pd.DataFrame(rows, columns=["day", "symbol", "state"])


def _fill_checks(row: dict, df1: pd.DataFrame | None, frames: dict[str, pd.DataFrame]) -> dict:
    from signal_engine.analysis import idea034_paper as m

    t0 = pd.Timestamp(row["signal_time"])
    out = {"fill_gap": False, "basis_ratio": float("nan"), "basis_ok": True}
    if df1 is None or not len(df1):
        return out | {"fill_gap": True}
    win = df1[(df1.index >= t0) & (df1.index < t0 + pd.Timedelta(minutes=m.LIMIT_WINDOW_MIN))]
    late = df1[df1.index.time >= dt.time(14, 40)]
    if len(win) < MIN_FILL_BARS or not len(late):
        out["fill_gap"] = True
    f5 = frames.get(row["symbol"])
    sig_bar = t0 - pd.Timedelta(minutes=5)
    raw = df1[df1.index < t0]
    if f5 is not None and sig_bar in f5.index and len(raw):
        ratio = float(f5.loc[sig_bar, "Close"]) / float(raw["Close"].iloc[-1])
        out["basis_ratio"] = ratio
        out["basis_ok"] = abs(ratio - 1.0) * float(raw["Close"].iloc[-1]) <= 2 * row["_tick_or"]
    return out


def _first_listed_historify() -> dict[str, dt.date]:
    """Each symbol's first 1m bar anywhere in Historify (read-only), as an IST date."""
    import duckdb

    from signal_engine.backtest.data import _DUCKDB_CONFIG, HISTORIFY_DB
    from signal_engine.timeutils import IST

    con = duckdb.connect(str(HISTORIFY_DB), read_only=True, config=_DUCKDB_CONFIG)
    try:
        rows = con.execute(
            "SELECT symbol, min(timestamp) FROM market_data WHERE interval = '1m' "
            "AND exchange = 'NSE' GROUP BY symbol"
        ).fetchall()
    finally:
        con.close()
    return {s: dt.datetime.fromtimestamp(t, IST).date() for s, t in rows}


def run_chunk(
    source: str,
    start: dt.date,
    end: dt.date,
    frames_path: str | None,
    first_listed: dict[str, dt.date] | None = None,
) -> tuple:
    """One year through record_day. With `first_listed`, each day passes the recorder only the
    universe symbols already listed that day (an input, not a rule: a symbol with no history
    cannot signal), so the recorder's 90% coverage rule counts missing bars, not later listings."""
    from loguru import logger

    from signal_engine.analysis import idea034_paper as m

    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    universe = _universe()
    lo = start - dt.timedelta(days=LOOKBACK_DAYS)
    if source == "historify":
        frames = m._historify5(universe, lo, end)
    else:
        frames = pickle.load(open(frames_path, "rb"))
    counts = _bar_counts(frames)
    first_bar = first_listed or {s: f.index[0].date() for s, f in frames.items() if len(f)}
    per_day = counts.groupby("day")["symbol"].nunique()
    sessions = sorted(d for d, n in per_day.items() if n >= REF_SHARE * len(universe))
    days = [d for d in sessions if start <= d <= end]

    load1 = functools.lru_cache(maxsize=64)(m._historify1)
    load5 = _slicer(frames)
    sig_rows, day_rows = [], []
    for day in days:
        syms = universe
        if first_listed is not None:
            syms = [s for s in universe if s in first_listed and first_listed[s] <= day]
        res = m.record_day(day, dry_run=True, symbols=syms, load5=load5, load1=load1)
        day_rows.append(
            {
                "day": str(day),
                "status": res.status,
                "signals": res.signals,
                "fills": res.fills,
                "symbols_passed": len(syms),
            }
        )
        for r in res.rows:
            tick = m.tick_for_price(r["edge"], day)
            chk = _fill_checks(r | {"_tick_or": tick}, load1(r["symbol"], day), frames)
            sig_rows.append(r | chk)
    sym_days = _symbol_days(counts, sessions, universe, first_bar, days)
    return pd.DataFrame(sig_rows), pd.DataFrame(day_rows), sym_days


def _chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    out = []
    for y in range(start.year, end.year + 1):
        a, b = max(start, dt.date(y, 1, 1)), min(end, dt.date(y, 12, 31))
        if a <= b:
            out.append((a, b))
    return out


def _split_published(path: Path, chunks: list[tuple[dt.date, dt.date]]) -> dict:
    """Write one pickle per year (plus look-back) so each worker loads only its slice."""
    CACHE.mkdir(parents=True, exist_ok=True)
    frames = pickle.load(open(path, "rb"))["frames"]
    paths = {}
    for a, b in chunks:
        lo = a - dt.timedelta(days=LOOKBACK_DAYS)
        part = {}
        for s, f in frames.items():
            d = f.index.date
            sel = f[(d >= lo) & (d <= b)]
            if len(sel):
                part[s] = sel[["Open", "High", "Low", "Close", "Volume"]]
        p = CACHE / f"published_{a.year}.pkl"
        pickle.dump(part, open(p, "wb"), protocol=4)
        paths[(a, b)] = str(p)
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", choices=("historify", "published"), required=True)
    ap.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2017, 1, 1))
    ap.add_argument("--end", type=dt.date.fromisoformat, default=dt.date(2026, 10, 28))
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--frames", type=Path, help="published frames.pkl (inputs_frames.py output)")
    ap.add_argument(
        "--listed-only",
        action="store_true",
        help="pass the recorder only symbols already listed that day (output tag _listed)",
    )
    a = ap.parse_args(argv)
    OUT.mkdir(exist_ok=True)
    chunks = _chunks(a.start, a.end)
    paths = dict.fromkeys(chunks)
    first_listed = None
    if a.source == "published":
        if a.frames is None:
            ap.error("--frames is required for --source published")
        paths = _split_published(a.frames, chunks)
        if a.listed_only:
            frames = pickle.load(open(a.frames, "rb"))["frames"]
            first_listed = {s: f.index[0].date() for s, f in frames.items() if len(f)}
    elif a.listed_only:
        first_listed = _first_listed_historify()
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(run_chunk, a.source, c[0], c[1], paths[c], first_listed) for c in chunks]
        parts = [f.result() for f in futs]
    tag = a.source + ("_listed" if a.listed_only else "")
    sigs = pd.concat([p[0] for p in parts], ignore_index=True)
    days = pd.concat([p[1] for p in parts], ignore_index=True)
    sym = pd.concat([p[2] for p in parts], ignore_index=True)
    sigs.to_csv(OUT / f"{tag}_signals.csv", index=False)
    days.to_csv(OUT / f"{tag}_days.csv", index=False)
    sym.to_csv(OUT / f"{tag}_symbol_days.csv", index=False)
    print(f"{tag}: {len(days)} sessions, {len(sigs)} signals -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
