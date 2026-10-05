"""Intraday build step 11: the validation report and the readiness state (plan sections 10, 12).

Reads only the build folder (and the corporate-action events of the daily version) and writes
`report.json` and `report.md` there. No strategy result is an input to anything here.

    uv run python -m signal_engine.backtest.data_readiness.intraday_report \
        --build db/staging/intraday/build-<root12> --daily-version nse-daily-20261005-002
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd

from signal_engine.backtest.data_readiness import intraday_convert as ic
from signal_engine.backtest.data_readiness import intraday_recovery as ir
from signal_engine.backtest.data_readiness import snapshot as _snapshot
from signal_engine.backtest.data_readiness import state as st

OVERALL_CEILING = 0.02
YEARLY_CEILING = 0.03
PROVENANCE_PASS = 0.99
WILSON_Z = 1.96
FALLBACK_PERIOD = ("2023-06-01", "2024-03-31")
BASIS_TOL = 0.001


def wilson_lower(k: int, n: int, z: float = WILSON_Z) -> float:
    """95% Wilson score lower bound of a proportion k/n."""
    if n == 0:
        return float("nan")
    p = k / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / (1 + z * z / n)


# ---------------------------------------------------------------- 10.1 conversion statistics

def outcome(sessions: pd.DataFrame) -> pd.Series:
    """primary | fallback | excluded:<reason> | unresolved | missing per requested session."""
    s = sessions.status.astype(str)
    return s.mask(s.eq(ic.CONVERTED), "primary").mask(s.eq(ic.CONVERTED_FALLBACK), "fallback")


def conversion_table(sessions: pd.DataFrame, by: pd.Series | None) -> pd.DataFrame:
    """Session count and the % of each outcome, per group (never one combined share)."""
    keys = by if by is not None else pd.Series("all", index=sessions.index)
    t = pd.crosstab(keys, outcome(sessions))
    total = t.sum(axis=1)
    pct = t.div(total, axis=0).mul(100).round(3)
    pct.insert(0, "sessions", total)
    return pct


def basis_class(sessions: pd.DataFrame) -> pd.Series:
    k, pf = sessions.k, sessions.price_factor
    cls = pd.Series("other k", index=sessions.index)
    cls[(k / pf - 1).abs() <= BASIS_TOL] = "k = NSE factor"
    cls[(k - 1).abs() <= BASIS_TOL] = "k = 1"
    cls[k.isna()] = "no k"
    return cls


def event_sessions(sessions: pd.DataFrame, events: pd.DataFrame) -> pd.Series:
    """'ex-date', 'session before' or 'no event' for each session (corporate-action events)."""
    tag = pd.Series("no event", index=sessions.index, dtype=object)
    ex = events.assign(ex_date=pd.to_datetime(events.ex_date))[["symbol", "ex_date"]]
    for sym, part in sessions.groupby("symbol"):
        dates = part.session.sort_values()
        for e in ex[ex.symbol == sym].ex_date:
            before = dates[dates < e]
            if len(before):
                tag[part.index[part.session == before.iloc[-1]]] = "session before"
            tag[part.index[part.session == e]] = "ex-date"
    return tag


def fallback_period_evidence(sessions: pd.DataFrame) -> dict:
    """The one-sided extreme-gap evidence for the fallback-heavy period (plan 6.2)."""
    a, b = (pd.Timestamp(x) for x in FALLBACK_PERIOD)
    inside = sessions.session.between(a, b)
    fb = sessions[inside & (sessions.status == ic.CONVERTED_FALLBACK)]
    h_gap = fb.H / fb.k / fb.raw_high - 1
    l_gap = fb.L / fb.k / fb.raw_low - 1
    published = sessions[inside & sessions.status.isin(ic.PUBLISHABLE)]
    return {"period": list(FALLBACK_PERIOD), "fallback_sessions": int(len(fb)),
            "fallback_share_of_published": round(len(fb) / max(len(published), 1), 4),
            "minute_high_at_or_below_nse_high": round(float((h_gap <= 1e-9).mean()), 4),
            "minute_low_at_or_above_nse_low": round(float((l_gap >= -1e-9).mean()), 4),
            "median_high_gap_pct": round(float(-h_gap.median() * 100), 4),
            "median_low_gap_pct": round(float(l_gap.median() * 100), 4)}


# ---------------------------------------------------------------- 10.2 exclusions

def exclusion_table(requested: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Excluded requested symbol-sessions / requested symbol-sessions, by year and overall."""
    excluded = ~requested.status.isin(ic.PUBLISHABLE)
    year = requested.session.dt.year
    t = pd.DataFrame({"requested": requested.groupby(year).size(),
                      "excluded": excluded.groupby(year).sum()})
    t["rate"] = (t.excluded / t.requested).round(5)
    t["within_ceiling"] = t.rate <= YEARLY_CEILING
    return t, float(excluded.mean())


# ---------------------------------------------------------------- provenance

def provenance_summary(prov: pd.DataFrame, sessions: pd.DataFrame) -> dict:
    p = prov.drop_duplicates(["symbol", "session"])
    exact = p.result.eq("exact")
    legacy = sessions[sessions.cls == "legacy"]
    year = p.session.dt.year
    by_year = pd.DataFrame({"sampled": p.groupby(year).size(), "exact": exact.groupby(year).sum(),
                            "legacy_sessions": legacy.groupby(legacy.session.dt.year).size()})
    by_year["exact_share"] = (by_year.exact / by_year.sampled).round(4)
    by_year["wilson_lower_95"] = [round(wilson_lower(int(k), int(n)), 4)
                                  for k, n in zip(by_year.exact, by_year.sampled, strict=False)]
    by_year["coverage"] = (by_year.sampled / by_year.legacy_sessions).round(4)
    strata = prov.assign(exact=prov.result.eq("exact"),
                         stratum=prov.stratum.str.split(";")).explode("stratum")
    by_stratum = strata.groupby("stratum").agg(sampled=("exact", "size"), exact=("exact", "sum"))
    by_stratum["exact_share"] = (by_stratum.exact / by_stratum.sampled).round(4)
    lowest = p.assign(exact=exact).groupby("symbol").exact.mean().sort_values().head(10)
    n, k = len(p), int(exact.sum())
    return {"definition": "a sampled legacy session matches exactly when a fresh AngelOne fetch "
                          "with the same request parameters has the same minute timestamps and "
                          "identical open, high, low, close and volume on every bar",
            "sampled_sessions": n, "exact": k, "results": p.result.value_counts().to_dict(),
            "exact_share": round(k / n, 5) if n else None,
            "wilson_lower_95": round(wilson_lower(k, n), 5) if n else None,
            "coverage": round(n / max(len(legacy), 1), 4),
            "pass_mark": PROVENANCE_PASS, "passed": bool(n and k / n >= PROVENANCE_PASS),
            "by_year": json.loads(by_year.to_json(orient="index")),
            "by_stratum": json.loads(by_stratum.to_json(orient="index")),
            "lowest_symbols": lowest.round(4).to_dict(),
            "statement": "legacy Angel store + stratified verification + targeted Angel recovery"}


# ---------------------------------------------------------------- report

def _frame_json(df: pd.DataFrame) -> dict:
    return json.loads(df.to_json(orient="index"))


def build_report(build: Path, daily_version: str) -> dict:
    plan = json.loads((build / "plan.json").read_text())
    built = json.loads((build / "build.json").read_text())
    policy = json.loads((build / "policy.json").read_text())
    sessions = pd.read_parquet(build / "session_ledger.parquet")
    sessions["session"] = pd.to_datetime(sessions.session)
    prov = pd.read_parquet(build / "provenance.parquet")
    prov["session"] = pd.to_datetime(prov.session)
    checks = pd.read_parquet(build / "checks_5m.parquet")
    m = _snapshot.resolve_version(daily_version)
    events = pd.read_parquet(_snapshot._object_path(_snapshot.DEFAULT_ROOT, m["events_sha256"]))

    req = sessions[~sessions.status.str.startswith("out_of_scope")].copy()
    pub = req[req.status.isin(ic.PUBLISHABLE)]
    excl_year, overall = exclusion_table(req)
    ledger = ir.read_ledger(build)
    final = ledger[ledger.outcome.isin(ir.FINAL)].drop_duplicates("request_id", keep="last")
    recovery_errors = ir.verify(build)
    five = {c: int(checks[c].sum()) for c in checks.columns if c[0].isdigit()}
    provenance = provenance_summary(prov, sessions)
    by_year = conversion_table(req, req.session.dt.year)

    criteria = {
        "1_snapshot_hashed": plan["snapshot_root_hash"] == built["snapshot_root_hash"],
        "2_recovery_completed": not any("no final outcome" in e for e in recovery_errors),
        "3_responses_hashed": not any("missing or changed" in e for e in recovery_errors),
        "4_mixed_vendor_handled": bool(pub.structure_ok.astype(bool).all()
                                       and (pub.negvol == 0).all()),
        "5_missing_sessions_handled": bool(req.status.notna().all()),
        "6_normalisation_completed": bool(pub.k.notna().all()),
        "7_conversion_statistics": len(by_year) > 0,
        "8_exclusions_within_ceilings": overall <= OVERALL_CEILING
        and bool(excl_year.within_ceiling.all()),
        "9_nse_crosscheck": bool((pub.close_ok & pub.volume_ok & pub.contained).all()),
        "10_missing_minute_policy": bool((pub.missing_minutes <= policy["N"]).all()),
        "11_1m_validation": bool(pub.structure_ok.astype(bool).all()),
        "12_5m_aggregation_validation": all(v == 0 for k, v in five.items()
                                            if not k.startswith("10_")),
        "13_5m_validation": five.get("10_daily_rollup", 1) == 0,
        "14_calendar_session_rules": five.get("8_unexpected_session", 1) == 0,
        "15_exclusions_documented": bool(req.status.notna().all()),
        "16_provenance_passed": provenance["passed"],
    }
    pending = ["17_fingerprint", "18_root_hash", "19_immutable_publication_verified",
               "20_published_source_reads_only_published"]
    warnings = len(req) > len(pub) or bool((~pub.open_ok.astype(bool)).any())
    if not all(criteria.values()):
        state = st.NOT_READY
    else:
        state = st.READY_WITH_WARNINGS if warnings else st.READY
    return {
        "kind": "intraday_validation_report",
        "snapshot_root_hash": built["snapshot_root_hash"],
        "template_sha256": built["template_sha256"],
        "conversion_spec_version": built["conversion_spec_version"],
        "daily_version": daily_version,
        "readiness_state": state,
        "criteria": criteria, "criteria_pending_publication": pending,
        "sessions": {"expected": len(sessions),
                     "out_of_scope_muhurat": int(len(sessions) - len(req)),
                     "requested": len(req), "published": len(pub),
                     "by_origin": pub.origin.value_counts().to_dict(),
                     "by_status": req.status.value_counts().to_dict()},
        "conversion": {
            "overall": _frame_json(conversion_table(req, None)),
            "by_year": _frame_json(by_year),
            "by_basis_class": _frame_json(conversion_table(req, basis_class(req))),
            "by_event": _frame_json(conversion_table(req, event_sessions(req, events))),
            "by_symbol": _frame_json(conversion_table(req, req.symbol)),
            "by_fallback_pair": pub[pub.status == ic.CONVERTED_FALLBACK].method
            .value_counts().to_dict(),
            "fallback_period": fallback_period_evidence(req),
            "open_field_outside_tolerance": int((~pub.open_ok.astype(bool)).sum()),
        },
        "exclusions": {"definition": "excluded requested symbol-sessions / requested "
                                     "symbol-sessions (Muhurat out of scope, not requested)",
                       "overall_rate": round(overall, 5), "overall_ceiling": OVERALL_CEILING,
                       "yearly_ceiling": YEARLY_CEILING, "by_year": _frame_json(excl_year)},
        "missing_minute_policy": policy,
        "provenance": provenance,
        "recovery": {"requests_planned": int(plan["requests_total"]),
                     "final": final.outcome.value_counts().to_dict(),
                     "errors": recovery_errors[:20]},
        "checks_5m": five,
        "bars": {"one_minute": int(checks.bars_1m.sum()),
                 "five_minute": int(checks.bars_5m.sum())},
    }


def _table(rows: dict) -> list[str]:
    df = pd.DataFrame(rows).T
    head = ["key", *map(str, df.columns)]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for idx, row in df.iterrows():
        out.append("| " + " | ".join([str(idx), *(str(v) for v in row)]) + " |")
    return out


def to_markdown(r: dict) -> str:
    lines = ["# Intraday 5m dataset validation report", "",
             f"Readiness: **{r['readiness_state']}**", "",
             f"Snapshot root: `{r['snapshot_root_hash']}`", "", "## Publication criteria", "",
             "| Criterion | Result |", "|---|---|"]
    lines += [f"| {k} | {'PASS' if v else 'FAIL'} |" for k, v in r["criteria"].items()]
    lines += [f"| {k} | pending publication |" for k in r["criteria_pending_publication"]]
    lines += ["", "## Sessions", "", "```", json.dumps(r["sessions"], indent=1), "```", "",
              "## Conversion by year (% of requested sessions)", "",
              *_table(r["conversion"]["by_year"]), "", "## Fallback period evidence", "", "```",
              json.dumps(r["conversion"]["fallback_period"], indent=1), "```", "",
              "## Exclusions by year", "", *_table(r["exclusions"]["by_year"]), "",
              f"Overall exclusion rate: {r['exclusions']['overall_rate']}", "",
              "## Missing-minute policy", "",
              f"N = {r['missing_minute_policy']['N']} ({r['missing_minute_policy']['method']})",
              "", "## Provenance", "", "```",
              json.dumps({k: r["provenance"][k] for k in (
                  "sampled_sessions", "exact", "results", "exact_share", "wilson_lower_95",
                  "coverage", "passed", "statement")}, indent=1), "```", "",
              "## 5m validation violations", "", "```", json.dumps(r["checks_5m"], indent=1),
              "```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.intraday_report")
    ap.add_argument("--build", required=True, type=Path)
    ap.add_argument("--daily-version", required=True)
    args = ap.parse_args(argv)
    report = build_report(args.build, args.daily_version)
    (args.build / "report.json").write_text(json.dumps(report, indent=1, default=str) + "\n")
    (args.build / "report.md").write_text(to_markdown(report))
    print(report["readiness_state"], [k for k, v in report["criteria"].items() if not v])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
