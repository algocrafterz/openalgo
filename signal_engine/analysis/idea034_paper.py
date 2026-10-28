"""IDEA-034 v3 forward paper recorder: every eligible signal, its modelled limit fill and exits.

    uv run python -m signal_engine.analysis.idea034_paper --day YYYY-MM-DD
    uv run python -m signal_engine.analysis.idea034_paper --report
    uv run python -m signal_engine.analysis.idea034_paper --day YYYY-MM-DD --unfrozen-dry-run
    uv run python -m signal_engine.analysis.idea034_paper --freeze      (once, at the freeze)

A thin paper adapter (G0.1): the rules, the limit fill and the exits are
strategy_core.balance_breakout, the same code the backtest adapter calls. This module only loads
the day's Historify bars, prices the result with the cost model and writes its own ledger. It
places no orders, touches no engine state and shares no risk slots with the running strategies.

The frozen rules, the acceptance criteria and how to read the report are in
research/ideas/idea-034-multi-day-balance-breakout/PAPER-PLAN.md. paper/FROZEN.json holds the
fingerprint of this file, the core, the parameters, the universe, the criteria and the backtest
reference; a mismatch refuses to record, so the rules cannot drift during the test.

Run it after the evening Historify refresh, once per session. The ledger
(data/idea034_paper.db) holds one `days` row per session (OK or DATA_GAP) and one `signals` row
per eligible signal: FILLED, UNFILLED or CAPACITY_SKIP. Unfilled and skipped signals count as 0
per signal, so the result is measured per signal as well as per filled trade.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.tick_reference import tick_for_price
from signal_engine.features.ib_width import ib_pct_at
from signal_engine.strategy_core.balance_breakout import (
    BAR_MIN,
    LIMIT_WINDOW_MIN,
    BalanceParams,
    Outcome,
    daily_table,
    mark_signals,
    round_to_tick,
    simulate_limit,
    simulate_market,
)
from signal_engine.timeutils import IST

__all__ = ["round_to_tick"]

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "strategy_core" / "balance_breakout.py"
PAPER = ROOT / "research" / "ideas" / "idea-034-multi-day-balance-breakout" / "paper"
UNIVERSE = PAPER / "universe.txt"
REFERENCE = PAPER / "reference.json"
FROZEN = PAPER / "FROZEN.json"
LEDGER = ROOT / "data" / "idea034_paper.db"
LOGS = ROOT / "logs"

#: The v2 primary rules (BalanceParams defaults) with the v3 limit entry.
PARAMS = BalanceParams()
HOLDOUT_START = dt.date(2026, 10, 29)
SLIP_BPS = 4.0
STRESS_SLIP_BPS = 8.0
NOTIONAL = 1_00_000.0
MAX_CONCURRENT = 10
#: Calendar days of history loaded before the session: 20 completed sessions are needed
#: (5 balance + 14 ATR + 1), 45 calendar days leave room for holidays.
HISTORY_DAYS = 45
#: A session is usable when this share of the universe has bars through the last 5m bar
#: before the 14:45 time exit.
MIN_COVERAGE = 0.90
LAST_BAR = dt.time(14, 40)

CRITERIA = {
    "min_sessions": 40,
    "min_signals": 100,
    "min_fills": 40,
    "fill_rate_min": 0.30,
    "fill_rate_max": 0.55,
    "kill_after_signals": 50,
    "max_data_gap_share": 0.10,
}

_SIGNAL_COLUMNS = (
    "day TEXT, symbol TEXT, direction INTEGER, signal_time TEXT, edge REAL, h REAL, h_pct REAL,"
    " atr REAL, limit_price REAL, stop REAL, target REAL, status TEXT, fill_time TEXT,"
    " fill_price REAL, exit_time TEXT, exit_price REAL, exit_reason TEXT, gross_bps REAL,"
    " stat_bps REAL, cost_bps REAL, net_bps REAL, net_bps_stress REAL, per_signal_bps REAL,"
    " mkt_gross_bps REAL, mkt_stat_bps REAL, mkt_net_bps REAL, mkt_reason TEXT,"
    " ib_pct_info REAL, data_hash TEXT, code_fingerprint TEXT, PRIMARY KEY (day, symbol)"
)
_DAY_COLUMNS = (
    "day TEXT PRIMARY KEY, status TEXT, symbols_total INTEGER, symbols_with_data INTEGER,"
    " signals INTEGER, recorded_at TEXT, forced INTEGER, data_hash TEXT, code_fingerprint TEXT"
)


class FrozenMismatch(RuntimeError):
    """The code, parameters or criteria differ from the frozen fingerprint."""


@dataclass(frozen=True)
class DayResult:
    day: dt.date
    status: str  # OK | DATA_GAP | ALREADY_RECORDED
    signals: int = 0
    fills: int = 0
    rows: tuple = ()


# --- freeze ----------------------------------------------------------------------------------


def _text(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def fingerprint() -> str:
    """sha256 of the core, this recorder, the parameters, the criteria, universe and reference."""
    h = hashlib.sha256()
    for part in (
        _text(CORE),
        _text(Path(__file__)),
        json.dumps(dataclasses.asdict(PARAMS), sort_keys=True).encode(),
        json.dumps(
            {
                "criteria": CRITERIA,
                "holdout_start": str(HOLDOUT_START),
                "slip": SLIP_BPS,
                "notional": NOTIONAL,
                "max_concurrent": MAX_CONCURRENT,
                "window": LIMIT_WINDOW_MIN,
            },
            sort_keys=True,
        ).encode(),
        _text(UNIVERSE),
        _text(REFERENCE),
    ):
        h.update(hashlib.sha256(part).digest())
    return h.hexdigest()


def check_frozen(path: Path = FROZEN) -> dict:
    if not path.exists():
        raise FrozenMismatch(f"{path.name} is missing: the rules are not frozen")
    frozen = json.loads(path.read_text(encoding="utf-8"))
    now = fingerprint()
    if frozen.get("fingerprint") != now:
        raise FrozenMismatch(
            f"code fingerprint {now[:12]} differs from frozen {str(frozen.get('fingerprint'))[:12]}:"
            " a rule, parameter or criterion changed; that is a new version, not this test"
        )
    return frozen


def freeze(path: Path = FROZEN) -> dict:
    if path.exists():
        raise FrozenMismatch(f"{path.name} already exists; a new freeze is a new version")
    body = {
        "idea": "IDEA-034",
        "version": "v3",
        "frozen_ist": dt.datetime.now(IST).isoformat(timespec="seconds"),
        "holdout_start": str(HOLDOUT_START),
        "fingerprint": fingerprint(),
        "params": dataclasses.asdict(PARAMS),
        "entry": {"type": "limit at the broken edge", "window_min": LIMIT_WINDOW_MIN},
        "costs": {"statutory": "india_intraday NSE flattrade", "slip_bps_market_leg": SLIP_BPS},
        "notional": NOTIONAL,
        "max_concurrent": MAX_CONCURRENT,
        "criteria": CRITERIA,
        "universe_sha256": hashlib.sha256(_text(UNIVERSE)).hexdigest(),
        "reference": json.loads(REFERENCE.read_text(encoding="utf-8")),
    }
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return body


# --- data ------------------------------------------------------------------------------------


def _historify5(symbols: list[str], start: dt.date, end: dt.date) -> dict[str, pd.DataFrame]:
    from signal_engine.backtest.data import from_historify

    try:
        return from_historify(symbols, "5m", start=str(start), end=str(end), min_sessions=1)
    except RuntimeError:
        logger.exception("idea034 paper: no 5m bars loaded")
        return {}


def _historify1(symbol: str, day: dt.date) -> pd.DataFrame | None:
    from signal_engine.backtest.data import from_historify

    try:
        got = from_historify(
            [symbol], "1m", start=str(day), end=str(day), min_sessions=1, drop_split_days=False
        )
    except RuntimeError:
        return None
    return got.get(symbol)


def _stat_bps(direction: int, entry: float, exit_: float) -> float:
    """Statutory cost of a Rs 1,00,000 round trip in bps of the entry notional."""
    from portfolio.costs import india_intraday

    other = NOTIONAL * exit_ / entry
    buy, sell = (NOTIONAL, other) if direction > 0 else (other, NOTIONAL)
    charge = india_intraday("NSE", broker="flattrade").charge(
        buy_value=buy, sell_value=sell, orders=2
    )
    return float(charge) / NOTIONAL * 1e4


def _session_frame(df5: pd.DataFrame) -> pd.DataFrame:
    d = ind.add_session_columns(df5)
    d["sig_dir"] = np.zeros(len(d), dtype=np.int8)
    d["sig_sl"] = np.nan
    d["sig_tp"] = np.nan
    return d


def _signals(symbol: str, df5: pd.DataFrame, day: dt.date) -> pd.DataFrame:
    d = _session_frame(df5)
    tab = daily_table(d, PARAMS)
    if len(tab) <= PARAMS.balance_days + PARAMS.atr_len:
        return d.iloc[0:0]
    d = mark_signals(d, tab, PARAMS, symbol)
    return d[(d["day"] == day) & (d["sig_dir"] != 0)]


def _data_hash(frames: dict[str, pd.DataFrame], day: dt.date) -> str:
    h = hashlib.sha256()
    for sym in sorted(frames):
        f = frames[sym]
        part = f[f.index.date == day][["Open", "High", "Low", "Close", "Volume"]]
        h.update(sym.encode())
        h.update(part.to_csv(float_format="%.4f").encode())
    return h.hexdigest()


# --- one session -----------------------------------------------------------------------------


def _row(day, symbol, sig, df5, df1, code_fp, data_fp) -> dict:
    s = int(sig["sig_dir"])
    edge = float(sig["bal_hi"] if s > 0 else sig["bal_lo"])
    t_close = sig.name + pd.Timedelta(minutes=BAR_MIN)
    tick = tick_for_price(edge, day)
    row = {
        "day": str(day),
        "symbol": symbol,
        "direction": s,
        "signal_time": t_close.isoformat(),
        "edge": edge,
        "h": float(sig["bal_h"]),
        "h_pct": float(sig["bal_h"]) / edge * 100.0,
        "atr": float(sig["atr_d"]),
        "limit_price": round_to_tick(edge, tick),
        "stop": float(sig["sig_sl"]),
        "target": float(sig["sig_tp"]),
        "ib_pct_info": ib_pct_at(df5, t_close, BAR_MIN),
        "data_hash": data_fp,
        "code_fingerprint": code_fp,
        "_tick": tick,
        "_t_close": t_close,
    }
    if df1 is not None and len(df1):
        mkt = simulate_market(df1, t_close.time(), s, row["stop"], row["target"])
        if mkt.filled:
            stat = _stat_bps(s, mkt.fill_price, mkt.exit_price)
            row |= {
                "mkt_gross_bps": mkt.gross_bps,
                "mkt_stat_bps": stat,
                "mkt_net_bps": mkt.gross_bps - stat - 2 * SLIP_BPS,
                "mkt_reason": mkt.reason,
            }
    return row


def _apply(row: dict, out: Outcome) -> dict:
    if not out.filled:
        return row | {"status": "UNFILLED", "per_signal_bps": 0.0}
    stat = _stat_bps(row["direction"], out.fill_price, out.exit_price)
    net = out.gross_bps - stat - SLIP_BPS  # the limit entry pays no slippage; the exit does
    return row | {
        "status": "FILLED",
        "fill_time": out.fill_time.isoformat(),
        "fill_price": out.fill_price,
        "exit_time": out.exit_time.isoformat(),
        "exit_price": out.exit_price,
        "exit_reason": out.reason,
        "gross_bps": out.gross_bps,
        "stat_bps": stat,
        "cost_bps": stat + SLIP_BPS,
        "net_bps": net,
        "net_bps_stress": out.gross_bps - stat - STRESS_SLIP_BPS,
        "per_signal_bps": net,
        "_release": out.exit_time,
    }


def _occupied(held: list[tuple[pd.Timestamp, pd.Timestamp]], t: pd.Timestamp) -> int:
    return sum(1 for a, b in held if a <= t < b)


def record_day(
    day: dt.date,
    *,
    ledger: Path = LEDGER,
    symbols: list[str] | None = None,
    load5: Callable[[list[str], dt.date, dt.date], dict[str, pd.DataFrame]] = _historify5,
    load1: Callable[[str, dt.date], pd.DataFrame | None] = _historify1,
    force: bool = False,
    dry_run: bool = False,
    min_last_bar: dt.time = LAST_BAR,
) -> DayResult:
    """Compute and (unless dry_run) record one session. Capacity: a slot is held from the signal
    close, by the live limit until it fills or expires, then by the position until it exits;
    a signal arriving with MAX_CONCURRENT slots held is CAPACITY_SKIP."""
    if day < HOLDOUT_START and not dry_run:
        raise ValueError(f"{day} is before the hold-out start {HOLDOUT_START}")
    symbols = symbols if symbols is not None else UNIVERSE.read_text().split()
    if not dry_run and _recorded(ledger, day) == "OK" and not force:
        logger.info(f"idea034 paper: {day} already recorded; pass --force to replace it")
        return DayResult(day, "ALREADY_RECORDED")
    frames = load5(symbols, day - dt.timedelta(days=HISTORY_DAYS), day)
    complete = {
        s: f for s, f in frames.items()
        if len(f) and (f.index.date == day).any() and f[f.index.date == day].index[-1].time() >= min_last_bar
    }  # fmt: skip
    code_fp, data_fp = fingerprint(), _data_hash(complete, day)
    day_row = {
        "day": str(day),
        "symbols_total": len(symbols),
        "symbols_with_data": len(complete),
        "recorded_at": dt.datetime.now(IST).isoformat(timespec="seconds"),
        "forced": int(force),
        "data_hash": data_fp,
        "code_fingerprint": code_fp,
    }
    if len(complete) < MIN_COVERAGE * len(symbols):
        logger.warning(
            f"idea034 paper: {day} DATA_GAP, {len(complete)} of {len(symbols)} symbols complete"
        )
        if not dry_run:
            _write(ledger, day_row | {"status": "DATA_GAP", "signals": 0}, [])
        return DayResult(day, "DATA_GAP")
    pending = []
    for sym in sorted(complete):
        sigs = _signals(sym, complete[sym], day)
        df1 = load1(sym, day) if len(sigs) else None
        for _, sig in sigs.iterrows():
            pending.append(
                _row(day, sym, sig, complete[sym], df1, code_fp, data_fp) | {"_df1": df1}
            )
    pending.sort(key=lambda r: (r["_t_close"], r["symbol"]))
    held: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    rows = []
    for r in pending:
        t0 = r["_t_close"]
        if _occupied(held, t0) >= MAX_CONCURRENT:
            rows.append(r | {"status": "CAPACITY_SKIP", "per_signal_bps": 0.0})
            continue
        df1 = r["_df1"]
        out = (
            simulate_limit(
                df1, t0.time(), r["direction"], r["edge"], r["stop"], r["target"], r["_tick"]
            )
            if df1 is not None and len(df1)
            else Outcome(False)
        )
        done = _apply(r, out)
        held.append((t0, done.get("_release", t0 + pd.Timedelta(minutes=LIMIT_WINDOW_MIN))))
        rows.append(done)
    rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    fills = sum(r["status"] == "FILLED" for r in rows)
    logger.info(f"idea034 paper: {day} OK, {len(rows)} signals, {fills} filled, dry_run={dry_run}")
    if force and not dry_run:
        logger.warning(f"idea034 paper: {day} re-recorded with --force")
    if not dry_run:
        _write(ledger, day_row | {"status": "OK", "signals": len(rows)}, rows)
    return DayResult(day, "OK", len(rows), fills, tuple(rows))


# --- ledger ----------------------------------------------------------------------------------


def _connect(ledger: Path) -> sqlite3.Connection:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ledger)
    con.execute(f"CREATE TABLE IF NOT EXISTS signals ({_SIGNAL_COLUMNS})")
    con.execute(f"CREATE TABLE IF NOT EXISTS days ({_DAY_COLUMNS})")
    return con


def _recorded(ledger: Path, day: dt.date) -> str | None:
    """The day's recorded status (OK or DATA_GAP), or None. A DATA_GAP day holds no result, so
    it may be recorded again without --force once the data is complete."""
    if not ledger.exists():
        return None
    with closing(_connect(ledger)) as con:
        row = con.execute("SELECT status FROM days WHERE day = ?", (str(day),)).fetchone()
    return row[0] if row else None


def _write(ledger: Path, day_row: dict, rows: list[dict]) -> None:
    with closing(_connect(ledger)) as con, con:
        con.execute("DELETE FROM signals WHERE day = ?", (day_row["day"],))
        con.execute("DELETE FROM days WHERE day = ?", (day_row["day"],))
        con.execute(
            f"INSERT INTO days ({', '.join(day_row)}) VALUES ({', '.join('?' * len(day_row))})",
            tuple(day_row.values()),
        )
        for r in rows:
            con.execute(
                f"INSERT INTO signals ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                tuple(r.values()),
            )


def load_ledger(ledger: Path = LEDGER) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not ledger.exists():
        return pd.DataFrame(), pd.DataFrame()
    with closing(_connect(ledger)) as con:
        return (
            pd.read_sql_query("SELECT * FROM signals ORDER BY signal_time, symbol", con),
            pd.read_sql_query("SELECT * FROM days ORDER BY day", con),
        )


# --- verdict ---------------------------------------------------------------------------------


def _band(reference: dict, n: int) -> dict | None:
    grid = sorted(int(k) for k in reference["bands"])
    below = [g for g in grid if g <= n]
    return reference["bands"][str(below[-1])] if below else None


def max_drawdown(per_signal: pd.Series) -> float:
    cum = per_signal.cumsum()
    return float((cum.cummax().clip(lower=0) - cum).max()) if len(cum) else 0.0


def metrics(signals: pd.DataFrame, days: pd.DataFrame) -> dict:
    n = len(signals)
    filled = signals["status"] == "FILLED" if n else pd.Series(dtype=bool)
    per = signals["per_signal_bps"].astype(float) if n else pd.Series(dtype=float)
    mkt = signals["mkt_net_bps"].astype(float) if n else pd.Series(dtype=float)
    return {
        "sessions": len(days),
        "data_gap_sessions": int((days["status"] == "DATA_GAP").sum()) if len(days) else 0,
        "signals": n,
        "fills": int(filled.sum()),
        "capacity_skips": int((signals["status"] == "CAPACITY_SKIP").sum()) if n else 0,
        "fill_rate": float(filled.mean()) if n else 0.0,
        "net_per_signal": float(per.mean()) if n else 0.0,
        "net_per_fill": float(signals.loc[filled, "net_bps"].mean()) if filled.any() else 0.0,
        "mkt_net_per_signal": float(mkt.mean()) if n and mkt.notna().any() else 0.0,
        "mkt_net_filled": float(mkt[filled].mean()) if filled.any() else float("nan"),
        "mkt_net_missed": float(mkt[~filled].mean()) if n and (~filled).any() else float("nan"),
        "stress_net_per_signal": (
            float(signals["net_bps_stress"].where(filled, 0.0).mean()) if n else 0.0
        ),
        "cum_net": float(per.sum()) if n else 0.0,
        "max_drawdown": max_drawdown(per),
    }


def evaluate(m: dict, reference: dict) -> dict:
    """The frozen acceptance criteria applied to the ledger metrics (PAPER-PLAN.md)."""
    c = CRITERIA
    reasons: list[str] = []
    if m["sessions"] and m["data_gap_sessions"] / m["sessions"] > c["max_data_gap_share"]:
        return {
            "status": "PAUSED_DATA",
            "reasons": ["DATA_GAP on more than 10 percent of sessions"],
        }
    band = _band(reference, m["signals"])
    if m["signals"] >= c["kill_after_signals"] and band is not None:
        if m["cum_net"] < m["signals"] * band["mean_p5"]:
            reasons.append("cumulative per-signal net below the backtest 5th percentile path")
        if m["max_drawdown"] > band["dd_p99"]:
            reasons.append("drawdown beyond the backtest 99th percentile")
        if reasons:
            return {"status": "KILL", "reasons": reasons}
    if (
        m["sessions"] < c["min_sessions"]
        or m["signals"] < c["min_signals"]
        or m["fills"] < c["min_fills"]
    ):
        status = "ON_TRACK" if m["signals"] >= c["kill_after_signals"] else "INSUFFICIENT_SAMPLE"
        return {"status": status, "reasons": ["minimum sample not reached"]}
    checks = {
        "net per signal > 0": m["net_per_signal"] > 0,
        "net per filled trade > 0": m["net_per_fill"] > 0,
        "fill rate 30 to 55 percent": c["fill_rate_min"] <= m["fill_rate"] <= c["fill_rate_max"],
        "limit beats market per signal": m["net_per_signal"] >= m["mkt_net_per_signal"],
        "drawdown within backtest 95th percentile": m["max_drawdown"] <= band["dd_p95"],
        "expectancy inside backtest 5th-95th band": (
            band["mean_p5"] <= m["net_per_signal"] <= band["mean_p95"]
        ),
    }
    failed = [k for k, ok in checks.items() if not ok]
    return {"status": "FAIL" if failed else "PASS", "reasons": failed or list(checks)}


# --- report ----------------------------------------------------------------------------------


def _sessions_since(start: dt.date, end: dt.date) -> list[dt.date]:
    """NSE sessions in Historify between start and end, read from a liquid reference symbol."""
    from signal_engine.backtest.data import from_historify_daily

    try:
        got = from_historify_daily(["RELIANCE"], start=str(start), end=str(end), min_sessions=1)
    except RuntimeError:
        return []
    df = got.get("RELIANCE")
    return sorted({ts.date() for ts in df.index}) if df is not None else []


def report(ledger: Path = LEDGER, reference_path: Path = REFERENCE) -> str:
    signals, days = load_ledger(ledger)
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    m = metrics(signals, days)
    verdict = evaluate(m, reference)
    rupee = NOTIONAL / 1e4
    lines = [
        "IDEA-034 v3 forward paper report (modelled limit fills, no orders)",
        f"hold-out from {HOLDOUT_START}; sessions {m['sessions']} (DATA_GAP {m['data_gap_sessions']});"
        f" signals {m['signals']}; filled {m['fills']}; capacity skips {m['capacity_skips']}",
        f"fill rate {m['fill_rate']:.1%} (frozen band 30% to 55%)",
        f"net per signal {m['net_per_signal']:+.2f} bps (Rs {m['net_per_signal'] * rupee:+.0f} at Rs 1L);"
        f" per filled trade {m['net_per_fill']:+.2f} bps (Rs {m['net_per_fill'] * rupee:+.0f})",
        f"stress (8 bps per market leg) net per signal {m['stress_net_per_signal']:+.2f} bps",
        f"missed-trade impact: market entry on the same signals {m['mkt_net_per_signal']:+.2f} bps per"
        f" signal; market net of filled {m['mkt_net_filled']:+.2f}, of missed {m['mkt_net_missed']:+.2f}",
        f"cumulative Rs {m['cum_net'] * rupee:+,.0f}; max drawdown Rs {m['max_drawdown'] * rupee:,.0f}",
    ]
    if len(signals):
        ib = pd.cut(signals["ib_pct_info"], [-np.inf, 30, 60, 90, np.inf], right=False)
        lines.append("IB% (information only, never a filter): " + "; ".join(
            f"{b}: n {len(g)}, {g['per_signal_bps'].mean():+.1f} bps/signal"
            for b, g in signals.groupby(ib, observed=True)
        ))  # fmt: skip
    today = dt.datetime.now(IST).date()
    recorded = set(pd.to_datetime(days["day"]).dt.date) if len(days) else set()
    missing = sorted(set(_sessions_since(HOLDOUT_START, today)) - recorded)
    lines.append(
        f"sessions with no ledger entry (missed runs): {[str(d) for d in missing] or 'none'}"
    )
    lines.append(f"STATUS {verdict['status']}: {'; '.join(verdict['reasons'])}")
    return "\n".join(lines)


# --- CLI -------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--day", type=dt.date.fromisoformat)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--unfrozen-dry-run", action="store_true")
    ap.add_argument("--freeze", action="store_true")
    a = ap.parse_args(argv)
    if a.freeze:
        body = freeze()
        logger.info(f"idea034 paper: frozen, fingerprint {body['fingerprint']}")
        return 0
    if a.report:
        logger.info("\n" + report())
        return 0
    if a.day is None:
        ap.error("--day, --report or --freeze is required")
    if a.day > dt.datetime.now(IST).date():
        ap.error(f"{a.day} is in the future; record a session after its data is in Historify")
    LOGS.mkdir(exist_ok=True)
    logger.add(LOGS / f"idea034_paper_{a.day}.log", level="INFO")
    if a.unfrozen_dry_run:
        res = _dry(a.day)
    else:
        check_frozen()
        res = record_day(a.day, force=a.force)
    logger.info(f"idea034 paper: {res.day} {res.status} signals {res.signals} fills {res.fills}")
    return 0


def _dry(day: dt.date) -> DayResult:
    """A dry run computes any day, including one before the hold-out, and writes nothing."""
    res = record_day(day, dry_run=True)
    for r in res.rows:
        logger.info(
            f"  {r['symbol']} {'LONG' if r['direction'] > 0 else 'SHORT'} {r['signal_time'][11:16]}"
            f" edge {r['edge']:.2f} H% {r['h_pct']:.2f} {r['status']}"
            f" net {r.get('net_bps') or 0:+.1f} mkt {r.get('mkt_net_bps') or 0:+.1f}"
        )
    return res


if __name__ == "__main__":
    sys.exit(main())
