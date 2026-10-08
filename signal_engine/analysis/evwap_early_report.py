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
    sign = out["direction"].map({"LONG": 1, "SHORT": -1})
    risk = (out["fill_price"] - out["initial_stop"]).abs()
    out["realized_r"] = sign * (out["exit_price"] - out["fill_price"]) / risk
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
    rows = {
        "ENTRY": {
            "n": int(entry.notna().sum()),
            "median_inr": _median(entry),
            "median_atr": _median(entry / atr),
            "median_r": _median(entry / risk),
        }
    }
    for reason in sorted(df["exit_reason"].dropna().unique()):
        m = df["exit_reason"] == reason
        e = exit_[m]
        rows[f"EXIT {reason}"] = {
            "n": int(e.notna().sum()),
            "median_inr": _median(e),
            "median_atr": _median(e / atr[m]),
            "median_r": _median(e / risk[m]),
        }
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
