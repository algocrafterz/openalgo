# Query recipes (read-only)

Run from the repo root. The script opens every database with `mode=ro` (it also forces the
enrichment helper to open `trades.db` read-only). It writes nothing in the repo. It writes one JSON
file to `$TMPDIR` and ends with a `RO_AUDIT` list of every database it opened.

Save it to `$TMPDIR/sepa.py` and run:

```
PYTHONPATH=. uv run python $TMPDIR/sepa.py --mode paper|live|both [--cutoff YYYY-MM-DD] [--n 5] [--active 10] [--strategy NAME]
```

- `--mode` is required in practice. `paper` reads rows stored as `analyze`. `live` reads rows stored as `live`. `both` runs the two modes one after the other and prints a rate-only comparison. The modes are never merged.
- `--n` (or `PSA_N`): sessions per window, default 5. `--active` (or `PSA_ACTIVE`): look-back for "active", default 10.
- `--strategy NAME`: one strategy only. An unknown name prints `n/a - insufficient data` and exits 0.

```python
"""Strategy performance analyst - read-only data script. Modes: paper, live, both."""
import argparse, datetime as dt, json, os, re, sqlite3, sys
from collections import Counter, defaultdict
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--mode", choices=("paper", "live", "both"), default="paper")
ap.add_argument("--cutoff", default=None, help="YYYY-MM-DD; default = last session with data in the mode")
ap.add_argument("--n", type=int, default=int(os.environ.get("PSA_N", "5")), help="sessions per window")
ap.add_argument("--active", type=int, default=int(os.environ.get("PSA_ACTIVE", "10")), help="look-back sessions")
ap.add_argument("--strategy", default=None, help="analyse only this strategy name")
args = ap.parse_args()

NA = "n/a - insufficient data"
DBMODE = {"paper": "analyze", "live": "live"}   # value stored in the ledger and in trades.db
LOGTAG = {"paper": "analyze", "live": "live"}   # signal_engine/logs/signal_engine_<tag>_<day>.log
RO_AUDIT = []


def ro(path):
    uri = f"file:{path}?mode=ro"
    RO_AUDIT.append(uri)
    c = sqlite3.connect(uri, uri=True)
    c.row_factory = sqlite3.Row
    return c


# The enrichment helper opens trades.db with a plain sqlite URL. Replace only its engine factory
# in this process so that it opens the file read-only too. No repo file changes.
import services.strategy_trade_enrichment as ste
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool


def _ro_engine_for(path):
    key = Path(path).resolve()
    RO_AUDIT.append(f"sqlalchemy:file:{key}?mode=ro")
    return create_engine(f"sqlite:///file:{key}?mode=ro&uri=true", poolclass=NullPool)


ste._engine_for = _ro_engine_for
from services.strategy_metrics_service import compute_metrics, LEDGER_RELIABLE_SINCE
from services.strategy_trade_enrichment import (enrich_trades, summarize_enrichment,
                                                symbol_breakdown, entry_hour_breakdown)

# Data-reliability start dates are per source AND per mode.
# Ledger floor is one constant in code for every mode. For live, no reliability test exists: UNKNOWN.
FLOOR = LEDGER_RELIABLE_SINCE.isoformat()
RELIABLE = {
    "paper": {"strategy_closed_trades": FLOOR, "trades.db": FLOOR},
    "live": {"strategy_closed_trades": FLOOR + " (code floor only; live reliability not verified, no live rows)",
             "trades.db": "unknown"},
}

led = ro("db/openalgo.db")
trd = ro("signal_engine/data/trades.db")
rst = ro("signal_engine/data/runner_state.db")
sbx = ro("db/sandbox.db")
HOL = {r[0] for r in led.execute("select holiday_date from market_holidays where holiday_type='TRADING_HOLIDAY'")}


def log_state(mode, day):
    p = Path(f"signal_engine/logs/signal_engine_{LOGTAG[mode]}_{day}.log")
    if not p.exists():
        return "missing"
    return "non-empty" if p.stat().st_size > 0 else "empty"


def pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def run(mode):
    dbm = DBMODE[mode]
    print(f"\n===== MODE {mode.upper()} (stored as '{dbm}') =====")
    print("reliable-since:", RELIABLE[mode])
    floor = FLOOR
    closed = [dict(r) for r in led.execute(
        "select * from strategy_closed_trades where mode=? and trade_date>=? order by trade_date,id", (dbm, floor))]
    sig = [dict(r) for r in trd.execute(
        "select strategy,direction,status,message,symbol,substr(coalesce(executed_at,received_at),1,10) d,context "
        "from trades where trade_mode=?", (dbm,)) if (r["d"] or "") >= floor]
    if args.cutoff:
        closed = [r for r in closed if r["trade_date"] <= args.cutoff]
        sig = [r for r in sig if r["d"] <= args.cutoff]
    sessions = sorted({r["trade_date"] for r in closed} | {r["d"] for r in sig})
    res = {"mode": mode, "db_mode": dbm, "reliable_since": RELIABLE[mode],
           "closed_rows": len(closed), "signal_rows": len(sig)}
    if not sessions:
        print(f"no {mode} data: 0 closed trades and 0 signal rows with mode '{dbm}' since {floor}"
              f"{' up to ' + args.cutoff if args.cutoff else ''}. Nothing to analyse. No P&L is invented.")
        res["status"] = "no_data"
        return res
    cut = sessions[-1]
    N = args.n
    if len(sessions) >= 2 * N:
        recent, prev = sessions[-N:], sessions[-2 * N:-N]
        wnote = ""
    else:  # fewer than 2N sessions: recent = last half, previous = first half
        h = len(sessions) // 2
        recent, prev = sessions[h:], sessions[:h]
        wnote = f"fewer than {2 * N} sessions with data: recent = last half, previous = first half"
    fmt = lambda d: f"{d[0]}..{d[-1]} ({len(d)})" if d else "none"
    print("cutoff", cut, "| sessions with data", len(sessions), "| recent", fmt(recent), "| previous", fmt(prev), wnote)
    act_set = set(sessions[-args.active:])
    active = sorted({r["strategy"] for r in closed if r["trade_date"] in act_set}
                    | {r["strategy"] for r in sig if r["d"] in act_set and r["status"] in ("SUCCESS", "DECLINED", "TIMEOUT")})
    if args.strategy:
        if args.strategy not in active:
            print(f"strategy {args.strategy!r}: no {mode} rows in the last {args.active} sessions. {NA}")
            res.update(status="strategy_not_found", strategies={})
            return res
        active = [args.strategy]
    print("active", active)

    # --- gaps: classify weekday non-holiday days without rows
    other = "live" if mode == "paper" else "paper"
    gaps = {"MISSING_SESSION": [], "NO_ROWS_ENGINE_RAN": [], "OTHER_MODE_DAY": []}
    d0, d1, have = dt.date.fromisoformat(sessions[0]), dt.date.fromisoformat(cut), set(sessions)
    while d0 <= d1:
        iso = d0.isoformat()
        if d0.weekday() < 5 and iso not in HOL and iso not in have:   # weekends and NSE holidays are skipped
            if log_state(other, iso) == "non-empty":
                gaps["OTHER_MODE_DAY"].append(iso)          # engine ran in the other mode: not a session of this mode
            elif log_state(mode, iso) == "non-empty":
                gaps["NO_ROWS_ENGINE_RAN"].append(iso)      # engine ran, nothing recorded: quiet day, not missing
            else:
                gaps["MISSING_SESSION"].append(iso)         # trading weekday, no rows, no log evidence of a run
        d0 += dt.timedelta(days=1)
    print("MISSING sessions (weekday, no rows, engine log missing or empty):", gaps["MISSING_SESSION"])
    print("No-rows days where engine log is non-empty (legitimate quiet day, not missing):", gaps["NO_ROWS_ENGINE_RAN"])
    print(f"Days the engine ran in {other} mode instead (not {mode} sessions):", gaps["OTHER_MODE_DAY"])
    res["gaps"] = gaps
    res.update(cutoff=cut, sessions_global=len(sessions), windows={"recent": recent, "previous": prev})

    def stats(strat, days, label):
        ds = set(days)
        t = [r for r in closed if r["strategy"] == strat and r["trade_date"] in ds]
        s = [r for r in sig if r["strategy"] == strat and r["d"] in ds]
        own_days = sorted({r["trade_date"] for r in t})
        out = {"window": label, "global_sessions": len(days), "from": min(days) if days else None,
               "to": max(days) if days else None, "trades": len(t),
               "strategy_trading_days_in_window": len(own_days)}
        ent = [r for r in s if r["direction"] in ("LONG", "SHORT")]
        out["signals_success"] = sum(1 for r in ent if r["status"] == "SUCCESS")
        out["signals_declined"] = sum(1 for r in ent if r["status"] == "DECLINED")
        out["timeouts"] = sum(1 for r in s if r["status"] == "TIMEOUT")
        out["decline_rate_pct"] = pct(out["signals_declined"], out["signals_declined"] + out["signals_success"])
        out["decline_reasons"] = Counter(re.sub(r"[\d.]+", "#", (r["message"] or "")[:60])
                                         for r in ent if r["status"] == "DECLINED").most_common(3)
        all_days = sorted({r["trade_date"] for r in closed if r["strategy"] == strat})
        out["strategy_sessions_total"] = len(all_days)   # strategy's OWN trading days in this mode, to cut-off
        out["strategy_first_trade"] = all_days[0] if all_days else None
        out["trades_per_strategy_session"] = round(len(t) / len(own_days), 2) if own_days else None
        out["trades_per_global_session"] = round(len(t) / len(days), 2) if days else None
        # no-trade days of this strategy inside the window, engine ran (other rows exist that day)
        out["no_trade_sessions_in_window"] = len([d for d in days if d not in own_days and out["strategy_first_trade"] and d >= out["strategy_first_trade"]])  # engine ran (rows exist that day); only days since the strategy first traded
        if mode == "paper" and days:
            q = [r for r in sbx.execute(
                "select order_status,count(*) n from sandbox_orders where strategy=? and substr(order_timestamp,1,10) between ? and ? group by 1",
                (strat, min(days), max(days)))]
            out["paper_order_status"] = {r["order_status"]: r["n"] for r in q}
        if len(t) == 0:
            out["status"] = NA
            return out
        try:
            m = compute_metrics(t, min(days), max(days))
            e = enrich_trades(t, min(days), max(days))
            en = summarize_enrichment(e)
        except Exception as ex:  # never raise out of the report
            out["status"] = f"{NA} (metrics error: {type(ex).__name__})"
            return out
        wins = [x["r_multiple"] for x in e if x["r_multiple"] is not None and x["r_multiple"] > 0]
        loss = [x["r_multiple"] for x in e if x["r_multiple"] is not None and x["r_multiple"] < 0]
        out.update({
            "win_rate": m.get("win_rate"), "profit_factor": m.get("profit_factor"), "gross": m.get("net_profit"),
            "est_charges": en.get("est_costs"), "net_after_charges": en.get("net_after_costs"),
            "charges_basis": "estimated (Performance page schedule); no actual-charge source exists in the repo data",
            "expectancy_inr": m.get("expectancy"), "avg_win_inr": m.get("avg_win"), "avg_loss_inr": m.get("avg_loss"),
            "expectancy_r": en.get("expectancy_r"), "expectancy_net_r": en.get("expectancy_net_r"),
            "r_covered": en.get("r_covered"),
            "avg_win_r": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss_r": round(sum(loss) / len(loss), 2) if loss else None,
            "slip_bps": en.get("entry_slippage_bps"), "slip_covered": en.get("slippage_covered"),
            "max_dd_inr": m.get("max_drawdown"), "loss_streak_days": m.get("best_loss_streak"),
            "worst_day": m.get("worst_day"), "best_day": m.get("best_day"),
            "long_short": dict(Counter(x["direction"] for x in t)),
        })
        if label in ("recent", "all"):
            sb = symbol_breakdown(t)
            out["by_symbol_best"], out["by_symbol_worst"] = sb[:2], sb[-2:]
            out["by_entry_hour_ist"] = entry_hour_breakdown(t)
            ex = Counter()
            for r in s:
                if r["direction"] == "EXIT" and r["status"] == "SUCCESS":
                    try:
                        ex["+".join(json.loads(r["context"]).get("exit_types", ["?"]))] += 1
                    except Exception:
                        ex["?"] += 1
            out["exit_mix"] = dict(ex)
            pd_ = defaultdict(float)
            for r in t:
                pd_[r["trade_date"][5:]] += r["realized_pnl"]
            out["pnl_by_day"] = {k: round(v) for k, v in sorted(pd_.items())}
            if len(pd_) >= 3:   # concentration check: drop best and worst day, look at the sign
                v = sorted(pd_.values())
                out["net_gross_without_best_worst_day"] = round(sum(v[1:-1]))
        return out

    strategies = {}
    for st in active:
        ws = [stats(st, recent, "recent"), stats(st, prev, "previous"), stats(st, sessions, "all")]
        tot = ws[2]["strategy_sessions_total"]
        n_all = ws[2]["trades"]
        conf = []
        for w in ws[:2]:
            n = w["trades"]
            w["confidence"] = "NOT ASSESSED (fewer than 10 trades)" if n < 10 else (
                "LOW CONFIDENCE (10-19 trades)" if n < 20 else "OK (20+ trades)")
        strategies[st] = {
            "strategy_sessions_total": tot, "trades_all": n_all, "global_sessions": len(sessions),
            "tuning_rule": (f"fewer than 20 {mode} sessions for this strategy ({tot}): no tuning recommendation"
                            if tot < 20 else
                            f"{tot} {mode} sessions: sample size alone does not block; owner approval still required"),
            "windows": ws}
        print(f"\n{st}: own {mode} trading sessions {tot} (global sessions {len(sessions)}); {strategies[st]['tuning_rule']}")
        for w in ws:
            print(" ", json.dumps(w, default=str))
    res["strategies"] = strategies
    res["status"] = "ok"
    return res


modes = ["paper", "live"] if args.mode == "both" else [args.mode]
results = {m: run(m) for m in modes}

if args.mode == "both":
    print("\n===== COMPARISON (rates and execution only; no P&L or trade counts are summed across modes) =====")
    pa, li = results["paper"], results["live"]
    if pa.get("status") != "ok" or li.get("status") != "ok":
        print("comparison n/a - insufficient data: paper status =", pa.get("status"), "| live status =", li.get("status"))
    else:
        for st in sorted(set(pa["strategies"]) & set(li["strategies"])):
            a, b = pa["strategies"][st]["windows"][2], li["strategies"][st]["windows"][2]
            print(st, {k: (a.get(k), b.get(k)) for k in ("win_rate", "profit_factor", "slip_bps", "decline_rate_pct")}, "(paper, live)")
    print("separate counts:", {m: {"closed_rows": results[m]["closed_rows"], "signal_rows": results[m]["signal_rows"]} for m in modes})

print("\nRO_AUDIT (every DB opened):")
for u in sorted(set(RO_AUDIT)):
    print(" ", u)
assert all("mode=ro" in u for u in RO_AUDIT)
out = os.path.join(os.environ.get("TMPDIR", "."), f"sepa_out_{args.mode}.json")
json.dump(results, open(out, "w"), default=str)
print("saved", out)
```

## Notes on the output

- `strategy_sessions_total` = the strategy's OWN trading sessions in that mode: distinct days with at least one closed trade for that strategy, from the reliable-since date to the cut-off. Use it for the confidence label and for the 20-session rule. Do not use `global_sessions`.
- `strategy_trading_days_in_window` is the same count inside one window. `no_trade_sessions_in_window` counts global sessions (rows exist, so the engine ran) on or after the strategy's first trade where this strategy had no closed trade. These are legitimate no-trade sessions.
- `tuning_rule` gives the 20-session text from the strategy's own count. A count of 20 or more does NOT allow a change. Owner approval is always needed.
- Gap classes (weekends and NSE holidays are never listed):
  - `MISSING_SESSION`: weekday, no rows in the mode, and the engine log of that mode is missing or empty. The engine should have run and left no proof.
  - `NO_ROWS_ENGINE_RAN`: weekday, no rows, but the engine log of that mode is non-empty. A quiet day. Not missing.
  - `OTHER_MODE_DAY`: the engine ran in the other mode that day. It is not a session of this mode.
- A window with 0 trades gives `status: "n/a - insufficient data"`. A mode with no rows prints `no <mode> data` and exits 0. `confidence` per window: NOT ASSESSED (under 10 trades), LOW CONFIDENCE (10 to 19), OK (20 or more).
- `window` is `recent`, `previous` or `all`. Windows count sessions that have data, not calendar days. With fewer than 2 x N sessions the script uses last half and first half.
- `exit_mix` joins `context.exit_types` of EXIT rows (`NO-PROGRESS`, `SL`, `TRAILING_STOP`, `TP1`, `TIME`).
- `slip_bps` is entry slippage in basis points. A negative value means a better fill than the signal price.
- `r_covered` is how many trades have a matched signal stop. R figures use only those trades.
- `est_charges` and `net_after_charges` use the Performance page schedule (`estimate_trade_costs`). They are estimates in every mode.
- `paper_order_status` (paper only) counts `sandbox_orders` by status for the strategy in the window. `cancelled` includes stop orders cancelled after an exit, so it is not a failure count. `rejected` is.
- `net_gross_without_best_worst_day` is the gross P&L after removing the best and worst day. If its sign differs from the window gross, say "driven by one day".
- Reliable-since dates are printed at the top of each mode block. The ledger date comes from `LEDGER_RELIABLE_SINCE`. For live the `trades.db` date is `unknown`.
- Live-only data the script cannot read (no DB table): broker rejections, actual charges, broker fills. See `data-source-map.md`.
