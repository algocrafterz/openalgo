"""Look-ahead check: a run on data cut at a session boundary must reproduce the earlier trades.

The engine is run on a symbol's full history and again on the history truncated to the
sessions before a cut. strategy.prepare() runs on the truncated data too, so a look-ahead
inside an indicator (a centred window, a whole-series statistic, a negative shift) changes
the earlier signals and shows up as a mismatch. A causal strategy reproduces every trade
that closed before the cut exactly.
"""

from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass, field

import pandas as pd

from signal_engine.backtest.engine import simulate
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.ledgers import LedgerRecorder

MIN_SESSIONS = 4
MAX_CUTS_PER_SYMBOL = 4
COVERAGE = (
    "Covers single-symbol inputs derived from the symbol's own frame only. Index or regime "
    "data (NIFTY, VIX), cross-sectional and universe-level features are NOT truncated."
)
COVERAGE_PANEL = (
    "Covers inputs derived from the symbol's own frame and, because the strategy's cross-sectional "
    "panel step is recomputed on the truncated universe, its ranking across symbols. Index or "
    "regime data (NIFTY, VIX) are NOT truncated."
)
MIN_SESSION_BARS = 4
MID_SESSION_FRACTION = 0.6
END_OF_DATA_REASON = "EOD_DATA"
_COMPARED = (
    "symbol",
    "direction",
    "entry_time",
    "exit_time",
    "entry",
    "exit",
    "reason",
    "r_net",
    "sl",
    "tp",
    "legs",
    "gross_ret",
    "net_ret",
)


@dataclass
class TruncationResult:
    ok: bool
    checked: list = field(default_factory=list)
    mismatches: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    elapsed_s: float = 0.0
    coverage: str = COVERAGE

    @property
    def compared_total(self) -> int:
        return sum(
            c.get("n_trades_compared", 0) + c.get("n_signals_compared", 0) for c in self.checked
        )

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked": [dict(c) for c in self.checked],
            "mismatches": [dict(m) for m in self.mismatches],
            "skipped": [dict(s) for s in self.skipped],
            "compared_total": self.compared_total,
            "elapsed_s": self.elapsed_s,
            "coverage": self.coverage,
        }


def _pick_symbols(keys: list[str], n: int) -> list[str]:
    """`n` keys evenly spaced over the sorted list (all of them when there are fewer)."""
    keys = sorted(keys)
    if n <= 0 or not keys:
        return []
    if len(keys) <= n:
        return keys
    if n == 1:
        return [keys[0]]
    step = (len(keys) - 1) / (n - 1)
    return [keys[round(i * step)] for i in range(n)]


def _run(strategy, params, symbol, df, run, engine):
    rec = LedgerRecorder()
    bt = Backtest(copy.deepcopy(strategy), {symbol: df}, run, engine=engine, release_raw=False)
    trades = bt.trades(params, "all", recorder=rec)
    return trades, rec.to_frames()


def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            x, y = float(a), float(b)
        except (TypeError, ValueError):
            return False
        if x == y:
            return True  # equal numbers, and identical infinities (a trade with no target)
        if math.isnan(x) and math.isnan(y):
            return True  # a trade with no stop has NaN risk and R on both runs
        if not (math.isfinite(x) and math.isfinite(y)):
            return False
        return abs(x - y) <= 1e-9 * max(1.0, abs(x))
    return a == b


def _compare(full, trunc, cut_ts, symbol, cut_s, cut_kind) -> list[dict]:
    out: list[dict] = []

    def add(kind, detail):
        out.append(
            {"symbol": symbol, "cut": cut_s, "cut_kind": cut_kind, "kind": kind, "detail": detail}
        )

    full_by = {(pd.Timestamp(t.entry_time), t.direction): t for t in full}
    trunc_by = {(pd.Timestamp(t.entry_time), t.direction): t for t in trunc}
    n = 0
    for key in sorted(full_by):
        t = full_by[key]
        if pd.Timestamp(t.exit_time) >= cut_ts:
            continue
        u = trunc_by.get(key)
        if u is None:
            n += 1
            add("trade_missing", f"trade entered {key[0]} is absent when data stops at {cut_s}")
        elif u.reason == END_OF_DATA_REASON:
            continue
        else:
            n += 1
            bad = [f for f in _COMPARED if not _same(getattr(t, f), getattr(u, f))]
            if bad:
                add("trade_differs", f"trade entered {key[0]} differs on {', '.join(bad)}")
    for key in sorted(trunc_by):
        u = trunc_by[key]
        if u.reason != END_OF_DATA_REASON and key not in full_by:
            add("trade_extra", f"trade entered {key[0]} exists only when data stops at {cut_s}")
    return out, n


def _signal_mismatch(full_led, trunc_led, cut_ts, last_ts, symbol, cut_s, cut_kind):
    """Accepted signals strictly before the truncated data's last bar must be identical.

    On the last bar the truncated engine force-closes an open position and then evaluates a
    signal, which the full run (position still open) does not, so there only the one-sided
    check "every signal the full run accepted is also accepted" is made. A look-ahead that
    reads exactly one bar ahead from the last bar is therefore caught only when the full run
    acts on it; one that reads one bar ahead from any earlier bar sees real data in both runs
    and is a blind spot of every truncation check.
    """

    def accepted(led):
        sig = led.get("signals")
        if sig is None or len(sig) == 0:
            return {}
        sig = sig[sig["accepted"].astype(bool)]
        return dict(zip(sig["signal_id"], pd.to_datetime(sig["bar_time"]), strict=True))

    a, b = accepted(full_led), accepted(trunc_led)
    early_a = {k for k, t in a.items() if t < last_ts}
    early_b = {k for k, t in b.items() if t < last_ts}
    last_a = {k for k, t in a.items() if last_ts <= t < cut_ts}
    last_b = {k for k, t in b.items() if last_ts <= t < cut_ts}
    n = len(early_a) + len(last_a)
    bad = sorted(early_a ^ early_b) or sorted(last_a - last_b)
    if not bad:
        return [], n
    return [
        {
            "symbol": symbol,
            "cut": cut_s,
            "cut_kind": cut_kind,
            "kind": "signal_differs",
            "detail": f"accepted signal {bad[0]} differs when data stops at {cut_s}",
        }
    ], n


def _cut_points(df, days, cuts, mid_session) -> list[tuple]:
    """(kind, label, cut timestamp) for each fraction: a session boundary, and a mid-session bar."""
    points = []
    dates = pd.Series(df.index.date, index=df.index)
    for frac in cuts:
        cut = days[min(max(int(len(days) * frac), 1), len(days) - 1)]
        points.append(("session", str(cut), df.index[dates.to_numpy() == cut][0]))
        if mid_session:
            bars = df.index[dates.to_numpy() == cut]
            if len(bars) >= MIN_SESSION_BARS:
                at = bars[int(len(bars) * MID_SESSION_FRACTION)]
                points.append(("mid_session", str(at), at))
    unique: dict = {}
    for kind, label, ts in points:
        unique.setdefault(ts, (kind, label, ts))
    return list(unique.values())[:MAX_CUTS_PER_SYMBOL]


def truncation_check(
    strategy,
    params,
    frames,
    run,
    *,
    engine=None,
    symbols: int = 3,
    cuts=(0.5, 0.8),
    mid_session: bool = True,
    panel=None,
) -> TruncationResult:
    """Run the engine on full and truncated data and compare the earlier trades.

    `frames` is never mutated: each selected symbol's frame is copied.

    `panel` is the strategy's cross-sectional step, a callable taking a mapping of symbol to frame
    and returning the prepared mapping. `frames` must then hold every symbol: the step is run on
    the whole universe, and again on the whole universe truncated at each cut, so a ranking that
    uses data after the cut shows up as a trade that does not reproduce.
    """
    engine = engine or simulate
    started = time.perf_counter()
    checked, mismatches, skipped = [], [], []
    prepared_full = panel({s: f.copy() for s, f in frames.items()}) if panel else None
    for sym in _pick_symbols(list(frames), symbols):
        df = frames[sym]
        run_df = prepared_full[sym] if panel else df
        days = sorted(set(df.index.date))
        if len(days) < MIN_SESSIONS:
            skipped.append({"symbol": sym, "reason": f"only {len(days)} sessions"})
            continue
        full, full_led = _run(strategy, params, sym, run_df.copy(), run, engine)
        for kind, label, cut_ts in _cut_points(df, days, cuts, mid_session):
            if panel:
                universe = {
                    s: f.loc[f.index < cut_ts].copy()
                    for s, f in frames.items()
                    if (f.index < cut_ts).any()
                }
                part = panel(universe)[sym]
            else:
                part = df.loc[df.index < cut_ts].copy()
            trunc, trunc_led = _run(strategy, params, sym, part, run, engine)
            bad_t, n_t = _compare(full, trunc, cut_ts, sym, label, kind)
            bad_s, n_s = _signal_mismatch(
                full_led, trunc_led, cut_ts, part.index[-1], sym, label, kind
            )
            if n_t + n_s == 0:
                skipped.append({"symbol": sym, "cut": label, "reason": "nothing to compare"})
                continue
            checked.append(
                {
                    "symbol": sym,
                    "cut": label,
                    "cut_kind": kind,
                    "n_trades_compared": n_t,
                    "n_signals_compared": n_s,
                }
            )
            mismatches += bad_t + bad_s
    elapsed = round(time.perf_counter() - started, 3)
    return TruncationResult(
        not mismatches,
        checked,
        mismatches,
        skipped,
        elapsed,
        COVERAGE_PANEL if panel else COVERAGE,
    )
