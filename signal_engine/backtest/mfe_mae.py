"""MFE/MAE diagnostic for early-displacement signals (IDEA-032 phase 2). Exploratory only.

For EVERY executable signal of a strategy (no per-day or per-position cap) this measures the path
from the executable fill, the OPEN of the bar after the signal bar, to the engine time exit:

  - MFE / MAE over the whole window, in ATR units (ATR14 at the signal bar) and in bps of entry.
    One ATR is a unit of measure here, not a risk: Benchmark A has no stop.
  - the zero bar m (first bar at or after the fill whose displacement reaches zero), which is when
    the Benchmark A exit order is placed, and the benchmark exit price that the engine would fill.
  - excursion before and after the zero bar, and first-hit bars for a reach table.

Bars are one unit: a bar that touches both a favourable and an adverse level counts the adverse
level as hit first (conservative). The signal rule is the adapter's own `signal()`; the window
rules are the engine's (warm-up, same-session previous bar, a next bar in the session, signal bar
open time below the entry cutoff and the time exit, valid ATR).

A placebo draws bars on the same symbol-days as the real signals and measures them the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

ADVERSE_LEVELS = (0.5, 0.75, 1.0, 1.25, 1.5)
FAVOURABLE_LEVELS = (0.5, 1.0, 1.5, 2.0, 2.5)
NO_HIT = 10**6
DIST_KEYS = ("n", "mean", "median", "p10", "p25", "p75", "p90")


def effective_time_exit_min(run: Any, bar_min: int = 5) -> int:
    """The engine's time exit stamp: the standing exit, capped so the exit bar closes in time."""
    return int(min(run.time_exit_min, run.mis_squareoff_min - run.squareoff_margin_min - bar_min))


@dataclass(frozen=True)
class WindowRules:
    """Which bars can carry a signal and where the measurement window ends (engine rules)."""

    warmup_bars: int
    entry_cutoff_min: int
    skip_open_minutes: int
    time_exit_min: int

    @classmethod
    def from_run(cls, run: Any, bar_min: int = 5) -> WindowRules:
        return cls(
            warmup_bars=int(run.warmup_bars),
            entry_cutoff_min=int(run.entry_cutoff_min),
            skip_open_minutes=int(run.skip_open_minutes),
            time_exit_min=effective_time_exit_min(run, bar_min),
        )


def _session_ends(day: np.ndarray, mins: np.ndarray, time_exit_min: int) -> np.ndarray:
    """Per bar, the index k where its session window ends: the first bar at or after the time
    exit stamp, or the session's last bar when there is none."""
    n = len(day)
    k = np.empty(n, dtype=np.int64)
    start = 0
    while start < n:
        end = start
        while end + 1 < n and day[end + 1] == day[start]:
            end += 1
        late = np.nonzero(mins[start : end + 1] >= time_exit_min)[0]
        k[start : end + 1] = start + int(late[0]) if len(late) else end
        start = end + 1
    return k


def executable_mask(c: Any, rules: WindowRules) -> np.ndarray:
    """Bars that pass the engine's gates for a new entry at the next bar's open."""
    n = c.n
    day, mins, from_open = c["day"], c["mins"], c["from_open"]
    ok = np.ones(n, dtype=bool)
    ok[: min(rules.warmup_bars, n)] = False
    if n:
        ok[0] = False
        ok[-1] = False
    if n > 1:
        same_prev = np.zeros(n, dtype=bool)
        same_prev[1:] = day[1:] == day[:-1]
        same_next = np.zeros(n, dtype=bool)
        same_next[:-1] = day[:-1] == day[1:]
        ok &= same_prev & same_next
    ok &= from_open >= rules.skip_open_minutes
    ok &= mins < rules.entry_cutoff_min
    ok &= mins < rules.time_exit_min
    atr = np.asarray(c["atr"], dtype=float)
    ok &= np.isfinite(atr) & (atr > 0)
    return ok


def find_signals(c: Any, strategy: Any, params: Any, rules: WindowRules) -> list[tuple[int, int]]:
    """Every (bar, direction) the strategy's own `signal()` fires on an executable bar."""
    out: list[tuple[int, int]] = []
    for i in np.nonzero(executable_mask(c, rules))[0]:
        for d in (1, -1):
            if strategy.signal(c, int(i), params, d):
                out.append((int(i), d))
    return out


def _first_hit(values: np.ndarray, level: float) -> int:
    hit = np.nonzero(values >= level)[0]
    return int(hit[0]) if len(hit) else NO_HIT


def measure(
    c: Any,
    picks: list[tuple[int, int]],
    rules: WindowRules,
    cost_bps: dict[int, float],
    levels: bool = True,
) -> pd.DataFrame:
    """One row per (bar, direction) pick: excursions from the open of bar i+1 to the window end.
    `levels=False` skips the first-hit columns of the reach table (placebo draws)."""
    o, h, low, close = c["Open"], c["High"], c["Low"], c["Close"]
    disp, atr, mins = c["disp"], c["atr"], c["mins"]
    times = c.index
    kend = _session_ends(c["day"], mins, rules.time_exit_min)
    rows = []
    for i, d in picks:
        k = int(kend[i])
        a = float(atr[i])
        e = float(o[i + 1])
        hi, lo = h[i + 1 : k + 1], low[i + 1 : k + 1]
        fav = (hi - e) if d == 1 else (e - lo)
        adv = (e - lo) if d == 1 else (hi - e)
        ds = disp[i + 1 : k + 1]
        zero = (ds >= 0) if d == 1 else (ds <= 0)
        m_off = int(np.argmax(zero)) if zero.any() else -1
        reached = m_off >= 0
        m = i + 1 + m_off if reached else -1
        mfe, mae = float(fav.max()), float(adv.max())
        if reached:
            mfe_pre, mae_pre = float(fav[: m_off + 1].max()), float(adv[: m_off + 1].max())
            post = fav[m_off + 1 :]
            post_abs = float(post.max()) if len(post) else np.nan
            if m < k:
                exit_px, reason = float(o[m + 1]), "ZERO_CROSS"
            else:
                exit_px = float(close[k])
                reason = "TIME_EXIT" if mins[k] >= rules.time_exit_min else "EOD"
        else:
            mfe_pre, mae_pre, post_abs = mfe, mae, np.nan
            exit_px = float(close[k])
            reason = "TIME_EXIT" if mins[k] >= rules.time_exit_min else "EOD"
        b_mfe = int(np.argmax(fav))
        t_sig = times[i]
        gross = d * (exit_px - e) / e
        row = {
            "day": c["day"][i],
            "signal_time": t_sig,
            "entry_time": times[i + 1],
            "direction": d,
            "signal_bar": i,
            "window_end": k,
            "atr": a,
            "entry": e,
            "disp_signal": float(disp[i]),
            "jump": bool(disp[i] >= 0) if d == 1 else bool(disp[i] <= 0),
            "mfe_atr": mfe / a,
            "mae_atr": mae / a,
            "mfe_bps": mfe / e * 1e4,
            "mae_bps": mae / e * 1e4,
            "reached_zero": reached,
            "reached_time_exit_without_zero": not reached,
            "zero_bar": m,
            "bars_to_zero": (m - i) if reached else np.nan,
            "minutes_to_zero": (times[m] - t_sig).total_seconds() / 60 if reached else np.nan,
            "zero_cross_price": float(close[m]) if reached else np.nan,
            "mfe_pre_atr": mfe_pre / a,
            "mae_pre_atr": mae_pre / a,
            "mfe_post_abs_atr": post_abs / a if np.isfinite(post_abs) else np.nan,
            "mfe_post_incr_atr": max(0.0, mfe - mfe_pre) / a,
            "bars_to_mfe": b_mfe + 1,
            "minutes_to_mfe": (times[i + 1 + b_mfe] - t_sig).total_seconds() / 60,
            "exit_price": exit_px,
            "exit_reason": reason,
            "gross_ret": gross,
            "net_ret": gross - cost_bps[d] / 1e4,
        }
        if levels:
            for lv in ADVERSE_LEVELS:
                row[f"adv_{lv}"] = _first_hit(adv / a, lv)
            for lv in FAVOURABLE_LEVELS:
                row[f"fav_{lv}"] = _first_hit(fav / a, lv)
        rows.append(row)
    return pd.DataFrame(rows)


def mark_engine_traded(frame: pd.DataFrame, symbol: str, trades: list) -> pd.DataFrame:
    """Set `engine_traded` in place: True where the engine opened a trade for this symbol at this
    entry time (the signal passed the position and per-day caps)."""
    taken = {pd.Timestamp(t.entry_time) for t in trades if t.symbol == symbol}
    frame["engine_traded"] = [pd.Timestamp(t) in taken for t in frame["entry_time"]]
    return frame


def placebo_picks(
    c: Any, real: list[tuple[int, int]], rules: WindowRules, seed: int, mode: str = "day"
) -> list[tuple[int, int]]:
    """One placebo bar per real signal, same symbol and same session date, same direction.

    Only bars that pass the real-signal gates are drawn (`executable_mask`), and never a bar that
    is itself a real signal (either direction). Per-direction counts therefore match the real
    signals exactly.
    mode "day":  any such bar of that session.
    mode "zone": additionally the displacement already sits on the signal's side of zero within
                 0.5 ATR (long -0.5..0, short 0..0.5): the same state without the crossing
                 event. A session with no such bar is skipped.
    """
    if mode not in ("day", "zone"):
        raise ValueError(f"mode must be day or zone, got {mode!r}")
    rng = np.random.default_rng(seed)
    ok = np.nonzero(executable_mask(c, rules))[0]
    day, disp = c["day"], np.asarray(c["disp"], dtype=float)
    real_bars = {i for i, _ in real}
    by_day: dict[Any, list[int]] = {}
    for j in ok:
        if int(j) not in real_bars:
            by_day.setdefault(day[j], []).append(int(j))
    pools = {k: np.asarray(v) for k, v in by_day.items()}
    out: list[tuple[int, int]] = []
    for i, d in real:
        pool = pools.get(day[i], np.empty(0, dtype=int))
        if mode == "zone":
            vals = disp[pool]
            pool = (
                pool[(vals > -0.5) & (vals < 0.0)] if d == 1 else pool[(vals > 0.0) & (vals < 0.5)]
            )
        if len(pool):
            out.append((int(rng.choice(pool)), d))
    return out


# ---- summaries -----------------------------------------------------------------------------


def dist(s: pd.Series) -> dict[str, float]:
    """n, mean, median, p10, p25, p75, p90 of the finite values."""
    x = pd.to_numeric(s, errors="coerce").dropna()
    x = x[np.isfinite(x)]
    if x.empty:
        return {"n": 0, **{k: float("nan") for k in DIST_KEYS[1:]}}
    q = x.quantile([0.1, 0.25, 0.75, 0.9])
    return {
        "n": int(len(x)),
        "mean": float(x.mean()),
        "median": float(x.median()),
        "p10": float(q[0.1]),
        "p25": float(q[0.25]),
        "p75": float(q[0.75]),
        "p90": float(q[0.9]),
    }


def reach_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Share of signals whose favourable level is hit strictly before the adverse level.

    Rows are adverse levels, columns favourable levels (ATR units). Equal first-hit bars count as
    adverse first. Reporting only, not an optimisation grid.
    """
    out = pd.DataFrame(index=list(ADVERSE_LEVELS), columns=list(FAVOURABLE_LEVELS), dtype=float)
    for a in ADVERSE_LEVELS:
        for f in FAVOURABLE_LEVELS:
            fa, ad = frame[f"fav_{f}"], frame[f"adv_{a}"]
            out.loc[a, f] = float(((fa < NO_HIT) & (fa < ad)).mean()) if len(frame) else np.nan
    return out


def main(argv: list[str] | None = None) -> None:
    """CLI: run the published-data study (see mfe_mae_report)."""
    from signal_engine.backtest import mfe_mae_report

    mfe_mae_report.main(argv)


if __name__ == "__main__":
    main()
