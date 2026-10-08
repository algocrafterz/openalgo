"""Published-data runner for the IDEA-032 MFE/MAE study (exploratory, never a recorded run).

Reads ONLY the published intraday dataset version, builds the engine data layer the way a recorded
run does, runs Benchmark A through the real engine (statutory Flattrade MIS cost at a fixed
notional) and measures every executable signal (`mfe_mae`) plus two placebos. Files go to a
caller-chosen output directory only: nothing is written under research/ or into an idea folder, no
run artifact is created and no trial is counted.

    python -m signal_engine.backtest.mfe_mae_report collect --out DIR
    python -m signal_engine.backtest.mfe_mae_report analyze --out DIR
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import yaml

from signal_engine.backtest import mfe_mae as mm
from signal_engine.backtest.metrics import clustered_t
from signal_engine.backtest.types import RunConfig, Trade

DATASET = "nse-intraday-5m-20261006-001"
IDEA_SPEC = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "ideas"
    / "idea-032-ema-vwap-atr-early-displacement"
    / "spec.yaml"
)
PROFILE = "flattrade-nse-cash"
BROKER = "flattrade"
NOTIONAL = 100_000.0
SEED = 20261008
#: Last bin that must be present for a session to count. 15:10 is the platform precedent
#: (IDEA-029, IDEA-031); 15:25 would drop every session since the 2026-08-03 73-bin template.
COMPLETE_LAST = "15:10"
ELIGIBILITY_DEFAULTS = {
    "eligibility_min_prior_sessions": 60,
    "eligibility_min_volume": 1,
    "eligibility_min_price": 10.0,
}
PLACEBO_MODES = ("day", "zone")
N_SEEDS = 20
PLACEBO_COLS = (
    "symbol",
    "day",
    "direction",
    "reached_zero",
    "zero_bar",
    "window_end",
    "mfe_atr",
    "mae_atr",
    "mfe_post_incr_atr",
    "mfe_post_abs_atr",
    "net_ret",
)


# ---- data ----------------------------------------------------------------------------------


def stand_in_frozen(complete_last: str = COMPLETE_LAST) -> SimpleNamespace:
    """The unfrozen spec plus a data block, in memory only (spec.yaml is never edited)."""
    spec = yaml.safe_load(IDEA_SPEC.read_text(encoding="utf-8"))
    data = {
        "frequency": "5m",
        "field_families": ["price", "volume"],
        "min_n_minutes": 1,
        "complete_bins": {"first": "09:15", "last": complete_last},
    }
    return SimpleNamespace(intent={**spec, "data": data}, resolved={})


def load_published(out: Path, complete_last: str = COMPLETE_LAST):
    """(frames after the data layer, provenance dict, profile fields). Reads the published
    version only; any preflight record goes under `out`, never into the idea folder."""
    from signal_engine.backtest import record_run as rr
    from signal_engine.backtest.data_collect import DataCollector
    from signal_engine.backtest.data_readiness import intraday_access as ia
    from signal_engine.backtest.data_readiness import preflight
    from signal_engine.research.intake.profiles import load_profile

    res = preflight.prepare_data(
        out / "preflight_scratch", stand_in_frozen(complete_last), dataset_version=DATASET
    )
    eligible = res.eligible
    collector = DataCollector()
    with res.open() as source:
        frames = source.intraday(eligible, "5m", None, collector)
        factors = source.session_price_factors(sorted(frames), None)
        m = res.manifest
        avail = ia.availability(
            ia.load_quality(m, res.root),
            ia.required_families(res.contract),
            m.get("usable_range_removals") or [],
        )
        avail = avail[avail.symbol.isin(eligible)]
        after = ia.apply_data_exclusions(source._con, res.contract, avail, eligible)
    reasons = after.reason.value_counts().to_dict()
    pinned = res.manifest["contract"]
    profile = load_profile(PROFILE).run_config_fields("MIS")
    cfg = {**ELIGIBILITY_DEFAULTS, **profile}
    layer = rr._build_data_layer(
        frames,
        collector,
        cfg,
        as_of=None,
        adjusted_prices=False,
        price_factors=factors,
        price_floor_source={
            "daily_version": pinned["daily_version"],
            "daily_root_hash": pinned["daily_root_hash"],
            "field": "price_factor",
            "formula": "last-bar close / price_factor",
        },
        expected_bars_for=rr.intraday_session_shape(),
        official=res.calendar,
        out_of_scope=[pd.Timestamp(d).date() for d in m.get("out_of_scope_sessions") or []],
    )
    verdict = {"affects_validity": bool((layer.data_report or {}).get("affects_validity"))}
    rr._check_calendar_at_load(layer, res)
    dropped_elig = sum(len(v) for v in layer.dropped.values())
    prov = {
        "dataset_version": res.version_id,
        "content_hash": m.get("content_hash"),
        "root_hash": m.get("root_hash"),
        "readiness_state": res.report.state,
        "price_basis": m.get("price_basis"),
        "manifest_coverage": m.get("coverage"),
        "complete_bins": [res.contract.complete_bins[0], res.contract.complete_bins[1]],
        "universe": res.contract.universe_name,
        "strategy_universe": len(res.contract.universe_symbols),
        "preflight_counts": res.report.counts,
        "eligible_symbols": len(eligible),
        "preflight_excluded_symbols": [
            e["symbol"]
            for e in res.report.exclusions
            if e.get("class") in ("defect", "eligibility")
        ],
        "symbol_sessions_field_valid": int(avail.available.sum()),
        "symbol_sessions_dropped_by_complete_bins": int(
            avail.available.sum() - after.available.sum()
        ),
        "drop_reasons": {str(k): int(v) for k, v in reasons.items()},
        "symbols_loaded": len(frames),
        "eligibility_dropped_symbol_sessions": dropped_elig,
        "data_report_validity": verdict,
        "profile_fields": profile,
        "survivorship": "current F&O list applied to the past (disclosed)",
    }
    return layer.frames, prov, profile


# ---- collect -------------------------------------------------------------------------------


def cost_model() -> Any:
    """The primary cost: Flattrade statutory MIS schedule plus the house slippage, priced at a
    fixed notional per trade (the spec has no stop, so risk sizing cannot apply)."""
    from signal_engine.backtest.record_run import deployed_cost_model

    return dataclasses.replace(
        deployed_cost_model("intraday", broker=BROKER), fixed_notional=NOTIONAL
    )


def cost_by_direction(model: Any) -> dict[int, float]:
    def probe(d: int) -> Trade:
        return Trade(
            symbol="",
            day=None,
            direction=d,
            tag="",
            entry_time=None,
            entry=1.0,
            signal_price=1.0,
            sl=np.nan,
            tp=np.nan,
            risk=np.nan,
        )

    return {d: float(model.trade_cost_bps(probe(d))) for d in (1, -1)}


class Collector:
    """An engine wrapper: runs the real engine per symbol, then measures that symbol's signals."""

    def __init__(self, strategy, params, run, costs):
        self.strategy, self.params, self.run, self.costs = strategy, params, run, costs
        self.rules = mm.WindowRules.from_run(run, 5)
        self.real: list[pd.DataFrame] = []
        self.placebo: dict[tuple[str, int], list[pd.DataFrame]] = {
            (m, k): [] for m in PLACEBO_MODES for k in range(N_SEEDS)
        }
        self.no_zone_day = 0

    def __call__(self, ctx, strategy, p, run, **kw):
        from signal_engine.backtest.engine import simulate

        trades = simulate(ctx, strategy, p, run, **kw)
        sym = ctx.symbol
        picks = mm.find_signals(ctx, strategy, p, self.rules)
        if picks:
            df = mm.measure(ctx, picks, self.rules, self.costs)
            df.insert(0, "symbol", sym)
            self.real.append(mm.mark_engine_traded(df, sym, trades))
            for mode in PLACEBO_MODES:
                for k in range(N_SEEDS):
                    seed = SEED + 1000 * k + zlib.crc32(sym.encode()) % 1000
                    chosen = mm.placebo_picks(ctx, picks, self.rules, seed, mode)
                    if mode == "zone" and k == 0:
                        self.no_zone_day += len(picks) - len(chosen)
                    if chosen:
                        pdf = mm.measure(ctx, chosen, self.rules, self.costs, levels=False)
                        pdf.insert(0, "symbol", sym)
                        self.placebo[(mode, k)].append(pdf[list(PLACEBO_COLS)])
        return trades


def _to_store(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["day"] = out["day"].astype(str)
    return out


def collect(out: Path) -> dict[str, Any]:
    from signal_engine.backtest.costs import recost
    from signal_engine.backtest.harness import Backtest
    from signal_engine.backtest.record_run import _apply_profile_settings
    from signal_engine.backtest.strategies.ema_vwap_atr_displacement import (
        EmaVwapAtrDisplacement,
        EmaVwapAtrDisplacementParams,
    )

    out.mkdir(parents=True, exist_ok=True)
    frames, prov, profile = load_published(out)
    strategy, params = EmaVwapAtrDisplacement(), EmaVwapAtrDisplacementParams()
    run = RunConfig().with_(**strategy.run_overrides)
    run = _apply_profile_settings(run, profile)
    model = cost_model()
    costs = cost_by_direction(model)
    col = Collector(strategy, params, run, costs)
    bt = Backtest(strategy, frames, run, engine=col, release_raw=True, single_pass=True)
    prov["cut_date"] = str(bt.cut)
    prov["days"] = [str(bt.days[0]), str(bt.days[-1]), len(bt.days)]
    prov["run_config"] = {
        k: (v if not callable(v) else str(v))
        for k, v in dataclasses.asdict(run).items()
        if k != "exit_plan"
    }
    prov["window"] = {
        "entry_cutoff_min": run.entry_cutoff_min,
        "time_exit_min": run.time_exit_min,
        "mis_squareoff_min": run.mis_squareoff_min,
        "squareoff_margin_min": run.squareoff_margin_min,
        "effective_time_exit_min": col.rules.time_exit_min,
        "warmup_bars": run.warmup_bars,
        "timezone": "IST",
    }
    prov["cost"] = {
        "model": "statutory Flattrade MIS + 4 bps/side house slippage",
        "fixed_notional": NOTIONAL,
        "round_trip_bps_by_direction": costs,
    }
    trades = recost(bt.trades(params, "all"), model)
    rows = [
        {
            "symbol": t.symbol,
            "day": str(t.day),
            "direction": t.direction,
            "entry_time": t.entry_time,
            "exit_time": t.exit_time,
            "entry": t.entry,
            "exit": t.exit,
            "reason": t.reason,
            "gross_ret": t.gross_ret,
            "net_ret": t.net_ret,
        }
        for t in trades
    ]
    pd.DataFrame(rows).to_parquet(out / "trades.parquet")
    _to_store(pd.concat(col.real, ignore_index=True)).to_parquet(out / "signals.parquet")
    for mode in PLACEBO_MODES:
        parts = [
            pd.concat(col.placebo[(mode, k)], ignore_index=True).assign(seed=k)
            for k in range(N_SEEDS)
        ]
        _to_store(pd.concat(parts, ignore_index=True)).to_parquet(out / f"placebo_{mode}.parquet")
    prov["placebo_zone_signals_without_candidate"] = col.no_zone_day
    (out / "provenance.json").write_text(json.dumps(prov, indent=2, default=str))
    return prov


# ---- analysis ------------------------------------------------------------------------------


def _max_drawdown(pnl_in_exit_order: np.ndarray) -> float:
    if not len(pnl_in_exit_order):
        return 0.0
    curve = np.cumsum(pnl_in_exit_order)
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:]
    return float((peak - curve).max())


def max_open(trades: pd.DataFrame, inclusive: bool) -> int:
    """Most positions open at once across symbols. `inclusive` counts an exit and an entry at
    the same timestamp as overlapping (the conservative count)."""
    ev = [(t, 0, 1) for t in trades.entry_time] + [(t, 1, -1) for t in trades.exit_time]
    if not inclusive:
        ev = [(t, 1 - o, d) for t, o, d in ev]  # exits sort before entries
    ev.sort()
    cur = peak = 0
    for _, _, d in ev:
        cur += d
        peak = max(peak, cur)
    return peak


def phase1_row(t: pd.DataFrame) -> dict[str, Any]:
    if t.empty:
        return {"n": 0}
    net_bps, gross_bps = t.net_ret * 1e4, t.gross_ret * 1e4
    atr_ret = t.atr / t.entry
    net_atr = t.net_ret / atr_ret
    ordered = t.sort_values("exit_time")
    pnl = (ordered.net_ret * NOTIONAL).to_numpy()
    share = t.reason.value_counts(normalize=True).to_dict()
    return {
        "n": int(len(t)),
        "win_pct": float((t.net_ret > 0).mean() * 100),
        "mean_gross_bps": float(gross_bps.mean()),
        "mean_net_bps": float(net_bps.mean()),
        "mean_net_atr": float(net_atr.mean()),
        "total_net_atr": float(net_atr.sum()),
        "cum_pnl_rs": float(pnl.sum()),
        "max_dd_rs": _max_drawdown(pnl),
        "t_day_clustered": clustered_t(
            net_bps.to_numpy(), pd.to_datetime(t.exit_time).dt.date.to_numpy()
        ),
        "exit_share": {k: round(float(v), 4) for k, v in share.items()},
    }


def phase1(out: Path, cut: pd.Timestamp) -> dict[str, Any]:
    tr = pd.read_parquet(out / "trades.parquet")
    sg = pd.read_parquet(out / "signals.parquet")
    sg = sg[sg.engine_traded][["symbol", "entry_time", "atr", "jump"]]
    t = tr.merge(sg, on=["symbol", "entry_time"], how="left", validate="one_to_one")
    assert t.atr.notna().all(), "an engine trade has no matching measured signal"
    t["is_is"] = pd.to_datetime(t.day) < cut
    groups = {
        "overall": t,
        "long": t[t.direction == 1],
        "short": t[t.direction == -1],
        "IS": t[t.is_is],
        "OOS": t[~t.is_is],
    }
    res = {k: phase1_row(v) for k, v in groups.items()}
    res["max_open_inclusive"] = max_open(t, True)
    res["max_open_exclusive"] = max_open(t, False)
    jumps = t[t.jump]
    res["jump_trades"] = {
        "n": int(len(jumps)),
        "share_pct": float(len(jumps) / len(t) * 100),
        "exit_share": jumps.reason.value_counts(normalize=True).round(4).to_dict(),
        "mean_net_bps": float(jumps.net_ret.mean() * 1e4) if len(jumps) else None,
    }
    return res


def _by_year(s: pd.DataFrame) -> dict[str, dict[str, float]]:
    y = pd.to_datetime(s.day).dt.year
    g = s.assign(net_bps=s.net_ret * 1e4).groupby(y)
    return {
        str(k): {
            "n": int(len(v)),
            "mean_net_bps": float(v.net_bps.mean()),
            "mean_mfe_atr": float(v.mfe_atr.mean()),
        }
        for k, v in g
    }


def _group_dists(s: pd.DataFrame, cols: tuple[str, ...]) -> dict[str, dict]:
    return {c: mm.dist(s[c]) for c in cols}


def _placebo_stats(s: pd.DataFrame) -> dict[str, Any]:
    """The continuation statistics compared against the placebo."""
    post = s[s.reached_zero & (s.zero_bar < s.window_end)]
    net_bps = s.net_ret * 1e4
    return {
        "n": int(len(s)),
        "reached_zero_pct": float(s.reached_zero.mean() * 100) if len(s) else None,
        "mean_net_bps": float(net_bps.mean()) if len(s) else None,
        "t_day_clustered_net": clustered_t(net_bps.to_numpy(), s.day.to_numpy())
        if len(s)
        else None,
        "mfe_atr": mm.dist(s.mfe_atr),
        "mae_atr": mm.dist(s.mae_atr),
        "post_zero_n": int(len(post)),
        "mfe_post_incr_atr": mm.dist(post.mfe_post_incr_atr),
        "mfe_post_abs_atr": mm.dist(post.mfe_post_abs_atr),
        "post_incr_ge_0.5_pct": float((post.mfe_post_incr_atr >= 0.5).mean() * 100)
        if len(post)
        else None,
        "post_incr_ge_1.0_pct": float((post.mfe_post_incr_atr >= 1.0).mean() * 100)
        if len(post)
        else None,
    }


def _late_row(s: pd.DataFrame, late: pd.Series) -> dict[str, Any]:
    nb = s.net_ret * 1e4
    return {
        "n_late": int(late.sum()),
        "pct_late": float(late.mean() * 100),
        "mean_net_bps_late": float(nb[late].mean()) if late.any() else None,
        "mean_net_bps_rest": float(nb[~late].mean()),
        "n_rest": int((~late).sum()),
    }


def _placebo_spread(p: pd.DataFrame) -> dict[str, Any]:
    """Per-seed continuation statistics, summarised as mean and spread across seeds."""
    rows = []
    for _, g in p.groupby("seed"):
        st = _placebo_stats(g.assign(day=g.day))
        rows.append(
            {
                "n": st["n"],
                "reached_zero_pct": st["reached_zero_pct"],
                "mean_net_bps": st["mean_net_bps"],
                "mfe_mean": st["mfe_atr"]["mean"],
                "mfe_median": st["mfe_atr"]["median"],
                "mae_mean": st["mae_atr"]["mean"],
                "post_n": st["post_zero_n"],
                "post_incr_mean": st["mfe_post_incr_atr"]["mean"],
                "post_incr_median": st["mfe_post_incr_atr"]["median"],
                "post_abs_mean": st["mfe_post_abs_atr"]["mean"],
                "post_incr_ge_0.5_pct": st["post_incr_ge_0.5_pct"],
                "post_incr_ge_1.0_pct": st["post_incr_ge_1.0_pct"],
            }
        )
    df = pd.DataFrame(rows)
    return {
        "seeds": int(len(df)),
        **{
            k: {
                "mean": float(df[k].mean()),
                "sd": float(df[k].std(ddof=1)),
                "min": float(df[k].min()),
                "max": float(df[k].max()),
            }
            for k in df.columns
        },
    }


def phase2(out: Path, cut: pd.Timestamp) -> dict[str, Any]:
    s = pd.read_parquet(out / "signals.parquet")
    s["is_is"] = pd.to_datetime(s.day) < cut
    s["net_bps"] = s.net_ret * 1e4
    s["atr_bps"] = s.atr / s.entry * 1e4
    res: dict[str, Any] = {
        "population": {
            "signals": int(len(s)),
            "engine_traded": int(s.engine_traded.sum()),
            "long": int((s.direction == 1).sum()),
            "short": int((s.direction == -1).sum()),
            "jump_pct": float(s.jump.mean() * 100),
            "reached_zero_pct": float(s.reached_zero.mean() * 100),
        }
    }
    groups = {
        "all": s,
        "long": s[s.direction == 1],
        "short": s[s.direction == -1],
        "IS": s[s.is_is],
        "OOS": s[~s.is_is],
    }
    cols = (
        "mfe_atr",
        "mae_atr",
        "mfe_bps",
        "mae_bps",
        "atr_bps",
        "mfe_pre_atr",
        "mae_pre_atr",
        "bars_to_zero",
        "minutes_to_mfe",
        "net_bps",
    )
    res["distributions"] = {g: _group_dists(v, cols) for g, v in groups.items()}
    res["mfe_ge_pct"] = {
        g: {str(x): float((v.mfe_atr >= x).mean() * 100) for x in (0.5, 1, 1.5, 2)}
        for g, v in groups.items()
    }
    res["reach_table_all"] = mm.reach_table(s).round(4).to_dict()
    res["reach_table_long"] = mm.reach_table(s[s.direction == 1]).round(4).to_dict()
    res["reach_table_short"] = mm.reach_table(s[s.direction == -1]).round(4).to_dict()
    res["reach_table_IS"] = mm.reach_table(s[s.is_is]).round(4).to_dict()
    res["reach_table_OOS"] = mm.reach_table(s[~s.is_is]).round(4).to_dict()
    win = s.net_ret > 0
    res["mae_by_outcome"] = {
        "reached_zero": mm.dist(s[s.reached_zero].mae_atr),
        "not_reached_zero": mm.dist(s[~s.reached_zero].mae_atr),
        "winners_mae_before_exit": mm.dist(s[win].mae_pre_atr),
        "losers_mae_before_exit": mm.dist(s[~win].mae_pre_atr),
        "winners_mae_whole_window": mm.dist(s[win].mae_atr),
    }
    # Share of all signals / of winners whose adverse excursion before the benchmark exit reached a level.
    res["adverse_reach"] = {
        str(x): {
            "all_pct": float((s.mae_pre_atr >= x).mean() * 100),
            "winners_pct": float((s[win].mae_pre_atr >= x).mean() * 100),
        }
        for x in mm.ADVERSE_LEVELS
    }
    res["benchmark_all_signals"] = {
        g: {
            "n": int(len(v)),
            "mean_net_bps": float(v.net_bps.mean()),
            "mean_gross_bps": float(v.gross_ret.mean() * 1e4),
            "win_pct": float((v.net_ret > 0).mean() * 100),
            "t_day_clustered": clustered_t(v.net_bps.to_numpy(), v.day.to_numpy()),
        }
        for g, v in groups.items()
    }
    res["by_year_all_signals"] = _by_year(s)
    res["by_year_engine_traded"] = _by_year(s[s.engine_traded])
    per = s.groupby("symbol").net_bps.agg(["mean", "count"])
    tot = (
        s.assign(pnl=s.net_ret * NOTIONAL).groupby("symbol").pnl.sum().sort_values(ascending=False)
    )
    res["per_symbol"] = {
        "symbols": int(len(per)),
        "positive_mean_bps": int((per["mean"] > 0).sum()),
        "median_symbol_mean_bps": float(per["mean"].median()),
        "top10_share_of_total_net_pct": float(tot.head(10).sum() / tot.sum() * 100)
        if tot.sum() > 0
        else None,
        "total_net_rs": float(tot.sum()),
        "top10_net_rs": float(tot.head(10).sum()),
        "symbols_positive_total": int((tot > 0).sum()),
    }
    res["placebo"] = {
        "real": _placebo_stats(s),
        "real_long": _placebo_stats(s[s.direction == 1]),
        "real_short": _placebo_stats(s[s.direction == -1]),
    }
    for mode in PLACEBO_MODES:
        p = pd.read_parquet(out / f"placebo_{mode}.parquet")
        res["placebo"][mode] = _placebo_spread(p)
        res["placebo"][f"{mode}_long"] = _placebo_spread(p[p.direction == 1])
        res["placebo"][f"{mode}_short"] = _placebo_spread(p[p.direction == -1])
    late = s.entry_time.dt.hour * 60 + s.entry_time.dt.minute == 14 * 60 + 45
    eng = s[s.engine_traded]
    late_e = late[s.engine_traded]
    res["entries_at_1445_open"] = {
        "signals": _late_row(s, late),
        "engine_trades": _late_row(eng, late_e),
    }
    res["exit_reason_all_signals"] = s.exit_reason.value_counts(normalize=True).round(4).to_dict()
    return res


def analyze(out: Path) -> dict[str, Any]:
    prov = json.loads((out / "provenance.json").read_text())
    cut = pd.Timestamp(prov["cut_date"])
    result = {"provenance": prov, "phase1": phase1(out, cut), "phase2": phase2(out, cut)}
    (out / "results.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="mfe_mae_report")
    ap.add_argument("stage", choices=("collect", "analyze", "all"))
    ap.add_argument("--out", required=True, help="output directory (outside the repo)")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if args.stage in ("collect", "all"):
        collect(out)
    if args.stage in ("analyze", "all"):
        analyze(out)
    sys.stdout.write(f"done: {out}\n")


if __name__ == "__main__":
    main()
