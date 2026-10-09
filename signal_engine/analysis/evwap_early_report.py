"""EVWAP-EARLY-PY paper behaviour report for one or more days. Read-only.

Joins the trail driver's per-trade diagnostics (logs/evwap-early-py_diagnostics_<day>.jsonl) with
the realised P&L in trades.db on sig_id, and prints what the behavioural check needs:

  * share of early entries that reached the EMA/VWAP crossover;
  * MAE and MFE before the crossover, MFE after it, entry advantage versus the crossover (R);
  * initial stop and entry-to-swing distance in ATR;
  * exit reasons, minutes in trade;
  * results by symbol and by entry half-hour.

  A does the early entry work: crossover conversion, stopped before crossover, entry advantage
    versus the crossover (R), MAE and MFE before the crossover;
  B does the move continue: MFE after the crossover, post_cross_extension_r (MFE after the crossover
    minus the entry advantage, i.e. the move beyond the crossover price), early_entry_capture
    (entry advantage / MFE after the crossover: the share of the best excursion already earned by
    entering early), trail_capture (realized R / MFE R on trailing exits);
  C does the structural stop make sense: stop and entry-to-swing distance in ATR, swing age, MAE in
    R and ATR, initial-stop hit rate, and SWING vs FALLBACK_EXTREME side by side;
  D where it works: by symbol, entry half-hour, exit reason and stop basis;
  plus excursion timing (MFE/MAE reached by 5..90 minutes) and slippage (entry vs the signal close,
  exits vs the decided price, in rupees, ATR and R).

Each record also keeps the raw facts for later research: the bar path of the trade (OHLC, EMA9,
EMA20, VWAP, ATR, displacement, ROC, stop in force and after, running MFE/MAE), the displacement
path into the signal (pre_signal_path), displacement at the crossover / max / min / last bar, the
ATR regime, and signal clustering (earlier signals and trades on the symbol that day).

R figures are from the fills against the initial stop distance, before charges. The report
describes behaviour. It is not an edge test and must not drive tuning on its own.

    uv run python -m signal_engine.analysis.evwap_early_report --day 2026-10-09
    uv run python -m signal_engine.analysis.evwap_early_report --day 2026-10-09 --day 2026-10-12
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from signal_engine.pipeline.trail_driver import DEFAULT_DIAGNOSTICS_DIR

TAG = "EVWAP-EARLY-PY"


def load_records(days: list[str], directory: Path = DEFAULT_DIAGNOSTICS_DIR) -> pd.DataFrame:
    rows = []
    for day in days:
        path = Path(directory) / f"{TAG.lower()}_diagnostics_{day}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return pd.DataFrame(rows)


def attach_pnl(df: pd.DataFrame, closes: dict[str, dict]) -> pd.DataFrame:
    """Add pnl, exit_price (the broker fill) and realized_r (exit fill against the initial risk)."""
    out = df.copy()
    out["pnl"] = out["sig_id"].map(lambda s: closes.get(s, {}).get("pnl")).astype(float)
    out["exit_price"] = (
        out["sig_id"].map(lambda s: closes.get(s, {}).get("exit_fill")).astype(float)
    )
    qty = out["sig_id"].map(lambda s: closes.get(s, {}).get("quantity")).astype(float)
    sign = out["direction"].map({"LONG": 1, "SHORT": -1})
    risk = (out["fill_price"] - out["initial_stop"]).abs()
    from_fill = sign * (out["exit_price"] - out["fill_price"]) / risk
    # A close row without a fill (seen on a tracker time exit) still has its booked P&L.
    from_pnl = out["pnl"] / (qty * risk)
    out["realized_r"] = from_fill.fillna(from_pnl)
    # the close as trades.db booked it, to cross-check the driver's own exit reason
    out["db_exit_type"] = out["sig_id"].map(
        lambda s: (closes.get(s, {}).get("exit_types") or [None])[-1]
    )
    out["closed_at"] = out["sig_id"].map(lambda s: closes.get(s, {}).get("closed_at"))
    out["r_source"] = np.where(
        from_fill.notna(), "fill", np.where(from_pnl.notna(), "pnl", "missing")
    )
    # entry -> crossover vs crossover -> best excursion (R from the fill)
    adv = pd.to_numeric(out["entry_advantage_vs_cross_r"], errors="coerce")
    mfe_post = pd.to_numeric(out["mfe_post_cross_r"], errors="coerce")
    out["post_cross_extension_r"] = mfe_post - adv
    out["early_entry_capture"] = (adv / mfe_post).where(mfe_post > 0)
    out["mae_atr"] = pd.to_numeric(out["mae_r"], errors="coerce") * pd.to_numeric(
        out.get("stop_atr"), errors="coerce"
    )
    out["trail_capture"] = (out["realized_r"] / pd.to_numeric(out["mfe_r"], errors="coerce")).where(
        (out["exit_reason"] == "TRAILING_STOP") & (pd.to_numeric(out["mfe_r"], errors="coerce") > 0)
    )
    return out


#: driver exit reason -> the trades.db exit labels that agree with it
_AGREEING_DB_EXITS = {
    "TRAILING_STOP": {"TRAILING_STOP"},
    "INITIAL_STOP": {"SL", "INITIAL_STOP_SAFETY"},
    "INITIAL_STOP_SAFETY": {"INITIAL_STOP_SAFETY"},
    "FORCE_EXIT": {"TIME", "TIME_EXIT"},
}


#: trades.db close label -> how a touched initial stop was executed
_STOP_EXECUTION = {
    "SL": "broker_executed",
    "INITIAL_STOP_SAFETY": "safety_market_close",
    "TIME": "missed_until_time_exit",
    "TIME_EXIT": "missed_until_time_exit",
}


def stop_execution(df: pd.DataFrame) -> pd.DataFrame:
    """Execution reliability of touched initial stops, kept apart from strategy performance.

    The touch is a bar fact (the 5m bar reached the stop, `stop_touch_bar`); the execution is how
    trades.db booked the close: broker_executed (the SL-M filled), safety_market_close (the runner
    closed it after the stop did not execute), missed_until_time_exit, or other.
    """
    touch = df["stop_touch_bar"] if "stop_touch_bar" in df else pd.Series(None, index=df.index)
    # records written before the field existed (2026-10-09): the touch is the decided bar
    older = df["exit_reason"].eq("INITIAL_STOP") & touch.isna()
    touch = touch.where(~older, df.get("exit_decided_bar"))
    t = df.assign(stop_touch_bar=touch)
    t = t[t["stop_touch_bar"].notna()].copy()
    if t.empty:
        return pd.DataFrame()
    t["stop_execution"] = t["db_exit_type"].map(_STOP_EXECUTION).fillna("other")
    touch_open = pd.to_datetime(t["stop_touch_bar"], utc=True)
    closed = pd.to_datetime(t["closed_at"], utc=False, errors="coerce")
    if closed.dt.tz is None:
        closed = closed.dt.tz_localize("Asia/Kolkata")
    t["minutes_from_touch_bar_open"] = (
        closed.dt.tz_convert("UTC") - touch_open
    ).dt.total_seconds() / 60
    return (
        t.groupby("stop_execution")
        .agg(
            trades=("sig_id", "count"),
            median_minutes_to_booked=("minutes_from_touch_bar_open", "median"),
            max_minutes_to_booked=("minutes_from_touch_bar_open", "max"),
            realized_r=("realized_r", "sum"),
        )
        .round(2)
    )


def exit_mismatches(df: pd.DataFrame) -> pd.DataFrame:
    """Trades whose driver exit reason disagrees with how trades.db booked the close (for example an
    initial stop the sandbox did not execute, later closed by the time exit)."""
    if "db_exit_type" not in df:
        return df.iloc[0:0]
    ok = [
        db in _AGREEING_DB_EXITS.get(reason, {db})
        for reason, db in zip(df["exit_reason"], df["db_exit_type"], strict=True)
    ]
    cols = ["symbol", "sig_id", "exit_reason", "db_exit_type", "exit_decided_bar", "realized_r"]
    return df.loc[[not x for x in ok], [c for c in cols if c in df]]


def _num(s) -> pd.Series:
    """Numeric, non-null values of a column; empty for a column the records do not carry."""
    if s is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(pd.Series(s), errors="coerce").dropna()


def _median(s) -> float | None:
    s = _num(s)
    return None if s.empty else round(float(s.median()), 3)


def _mean(s) -> float | None:
    s = _num(s)
    return None if s.empty else round(float(s.mean()), 3)


def _pct(mask: pd.Series) -> float | None:
    return None if mask.empty else round(100 * float(mask.mean()), 1)


def summarise(df: pd.DataFrame) -> dict:
    """The four questions of the behavioural check, A to D (D is the grouped tables)."""
    crossed = df["crossover_occurred"].astype(bool)
    c = df[crossed]
    reasons = df["exit_reason"]
    return {
        "trades": len(df),
        "exit_reasons": reasons.value_counts().to_dict(),
        "trades_without_pnl": int(df["pnl"].isna().sum()),
        "exit_reason_mismatches": len(exit_mismatches(df)),
        "r_source": df["r_source"].value_counts().to_dict() if "r_source" in df else {},
        "realized_r_sum": round(float(df["realized_r"].sum()), 2),
        "pnl_sum": round(float(df["pnl"].sum()), 2),
        # A. does the early entry work?
        "A_reached_crossover_pct": _pct(crossed),
        "A_stopped_before_crossover_pct": _pct((reasons == "INITIAL_STOP") & ~crossed),
        "A_closed_before_crossover_pct": _pct(~crossed),
        "A_entry_advantage_vs_cross_r_mean": _mean(c["entry_advantage_vs_cross_r"]),
        "A_entry_advantage_vs_cross_r_median": _median(c["entry_advantage_vs_cross_r"]),
        "A_mae_pre_cross_r_mean": _mean(df["mae_pre_cross_r"]),
        "A_mae_pre_cross_r_median": _median(df["mae_pre_cross_r"]),
        "A_mfe_pre_cross_r_mean": _mean(df["mfe_pre_cross_r"]),
        "A_mfe_pre_cross_r_median": _median(df["mfe_pre_cross_r"]),
        # B. does the move continue after the crossover?
        "B_mfe_post_cross_r_mean": _mean(c["mfe_post_cross_r"]),
        "B_mfe_post_cross_r_median": _median(c["mfe_post_cross_r"]),
        "B_mae_post_cross_r_median": _median(c["mae_post_cross_r"]),
        "B_post_cross_extension_r_mean": _mean(c["post_cross_extension_r"]),
        "B_post_cross_extension_r_median": _median(c["post_cross_extension_r"]),
        "B_early_entry_capture_median": _median(c["early_entry_capture"]),
        "B_trail_capture_median": _median(df["trail_capture"]),
        # C. does the structural stop make sense?
        "C_stop_atr_mean": _mean(df.get("stop_atr")),
        "C_stop_atr_median": _median(df.get("stop_atr")),
        "C_entry_to_swing_atr_median": _median(df.get("entry_to_swing_atr")),
        "C_swing_age_bars_median": _median(df.get("swing_age_bars")),
        "C_mae_r_median": _median(df["mae_r"]),
        "C_mae_atr_median": _median(df["mae_atr"]),
        "C_initial_stop_hit_pct": _pct(reasons == "INITIAL_STOP"),
        "minutes_in_trade_median": _median(df["minutes_in_trade"]),
    }


def by(df: pd.DataFrame, key: str) -> pd.DataFrame:
    return (
        df.assign(crossed=df["crossover_occurred"].astype(float))
        .groupby(key)
        .agg(
            trades=("sig_id", "count"),
            reached_cross=("crossed", "mean"),
            realized_r=("realized_r", "sum"),
            pnl=("pnl", "sum"),
            mfe_r=("mfe_r", "median"),
            mae_r=("mae_r", "median"),
            minutes=("minutes_in_trade", "median"),
        )
        .round(3)
        .sort_values("trades", ascending=False)
    )


def by_stop_basis(df: pd.DataFrame) -> pd.DataFrame:
    """SWING vs FALLBACK_EXTREME, side by side, on the measures that decide whether the fallback
    stays valid. R is from the fills against the initial stop distance, before charges."""
    rows = {}
    for basis, g in df.groupby("stop_basis"):
        n = len(g)
        reasons = g["exit_reason"]
        rows[basis] = {
            "trades": n,
            "net_r": round(float(g["realized_r"].sum()), 3),
            "avg_r_per_trade": _mean(g["realized_r"]),
            "pnl": round(float(g["pnl"].sum()), 2),
            "median_mae_r": _median(g["mae_r"]),
            "median_mfe_r": _median(g["mfe_r"]),
            "median_mae_pre_cross_r": _median(g["mae_pre_cross_r"]),
            "stop_out_rate": round(float((reasons == "INITIAL_STOP").mean()), 3),
            "crossover_conversion": round(float(g["crossover_occurred"].astype(bool).mean()), 3),
            "trailing_exits": int((reasons == "TRAILING_STOP").sum()),
            "time_exits": int((reasons == "FORCE_EXIT").sum()),
            "median_stop_atr": _median(g.get("stop_atr")),
            "median_entry_advantage_r": _median(
                g.loc[g["crossover_occurred"].astype(bool), "entry_advantage_vs_cross_r"]
            ),
        }
    return pd.DataFrame(rows)


TIME_BUCKETS_MIN = (5, 10, 15, 30, 60, 90)


def excursion_by_time(df: pd.DataFrame, interval_min: int = 5) -> pd.DataFrame:
    """Median MFE and MAE (R) reached by each time since entry, from each record's bar path.

    A trade that closed earlier keeps its final values (what it had reached while open)."""
    rows = {}
    for minutes in TIME_BUCKETS_MIN:
        k = minutes // interval_min
        mfe, mae = [], []
        for path in df.get("path", pd.Series(dtype=object)).dropna():
            if not path:
                continue
            bar = path[min(k, len(path)) - 1]
            mfe.append(bar.get("mfe_r"))
            mae.append(bar.get("mae_r"))
        rows[f"{minutes}m"] = {
            "median_mfe_r": _median(mfe),
            "median_mae_r": _median(mae),
            "n": len(mfe),
        }
    return pd.DataFrame(rows)


def slippage(df: pd.DataFrame) -> pd.DataFrame:
    """Execution friction, positive = worse than the reference price, in rupees, ATR and R.

    Entry: actual fill vs the signal bar close (only fills confirmed by the broker). Exit: actual
    fill (trades.db) vs the price the exit was decided at (the stop level, or the last close for a
    time exit), per exit reason."""
    sign = df["direction"].map({"LONG": 1, "SHORT": -1})
    atr = pd.to_numeric(df.get("atr"), errors="coerce")
    risk = pd.to_numeric(df.get("initial_risk"), errors="coerce")
    entry = sign * (df["fill_price"] - pd.to_numeric(df.get("signal_price"), errors="coerce"))
    entry = entry.where(df.get("fill_price_source", "broker") == "broker")
    exit_ = sign * (
        pd.to_numeric(df.get("exit_price_reference"), errors="coerce") - df["exit_price"]
    )

    def stats(e: pd.Series, a: pd.Series, r: pd.Series) -> dict:
        return {
            "n": int(e.notna().sum()),
            "worse": int((e > 0).sum()),
            "better": int((e < 0).sum()),
            "median_inr": _median(e),
            "mean_inr": _mean(e),
            "median_atr": _median(e / a),
            "median_r": _median(e / r),
            "mean_r": _mean(e / r),
        }

    rows = {"ENTRY": stats(entry, atr, risk)}
    for reason in sorted(df["exit_reason"].dropna().unique()):
        m = df["exit_reason"] == reason
        rows[f"EXIT {reason}"] = stats(exit_[m], atr[m], risk[m])
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_engine.analysis.evwap_early_report")
    ap.add_argument("--day", action="append", required=True, help="YYYY-MM-DD (repeatable)")
    ap.add_argument("--mode", default="analyze", help="trades.db trade_mode (default analyze)")
    args = ap.parse_args(argv)
    df = load_records(args.day)
    if df.empty:
        print(f"no {TAG} diagnostics records for {', '.join(args.day)}")
        return 0
    from signal_engine import db

    closes: dict[str, dict] = {}
    for day in args.day:
        closes.update(db.fetch_strategy_closes(TAG, args.mode, day))
    df = attach_pnl(df, closes)
    for k, v in summarise(df).items():
        print(f"{k}: {v}")
    with pd.option_context("display.width", 160, "display.max_rows", 200):
        print("\nby symbol\n", by(df, "symbol"))
        print("\nby entry half-hour\n", by(df, "entry_half_hour"))
        bad = exit_mismatches(df)
        if len(bad):
            print("\nEXIT REASON MISMATCHES (driver vs trades.db) - investigate\n", bad)
        se = stop_execution(df)
        if len(se):
            print(
                "\ninitial-stop touches by how they were executed (bar touch vs trades.db close)\n",
                se,
            )
        print("\nMFE / MAE by time since entry (R)\n", excursion_by_time(df))
        print("\nslippage (positive = worse than reference)\n", slippage(df))
        print("\nby exit reason\n", by(df, "exit_reason"))
        print("\nby stop basis\n", by(df, "stop_basis"))
        if "stop_basis" in df:
            print(
                "\nSWING (confirmed swing) vs FALLBACK_EXTREME (preceding 6-bar extreme); "
                "R from fills before charges\n",
                by_stop_basis(df),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
