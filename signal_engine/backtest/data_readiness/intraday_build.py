"""Intraday build steps 5-10: unify, convert, missing-minute policy, 5m, 5m validation.

Reads the frozen snapshot, the plan outputs and the recovery ledger and landing files; never the
live store. Writes, under the build folder:
    unified/<SYMBOL>.parquet       vendor-basis 1m bars, one origin per session
    provenance.parquet             one row per sampled legacy session: exact / scaled / mismatch
    session_ledger.parquet         one row per expected (symbol, session): origin, conversion,
                                   status
    policy.json                    the missing-minute threshold N and its distribution
    five/<SYMBOL>/<YEAR>.parquet   canonical 5m bars
    checks_5m.parquet              section 8.2 violation counts per symbol

Session-level replacement (plan 5.2): a session is either legacy or recovered, never both.

    uv run python -m signal_engine.backtest.data_readiness.intraday_build \
        --snapshot db/staging/intraday/legacy-1m-<stamp>-ist
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from signal_engine.backtest.data_readiness import intraday_5m as i5
from signal_engine.backtest.data_readiness import intraday_convert as ic
from signal_engine.backtest.data_readiness import intraday_freeze as fz
from signal_engine.backtest.data_readiness import intraday_plan as ip
from signal_engine.backtest.data_readiness import intraday_recovery as ir
from signal_engine.backtest.data_readiness import snapshot as _snapshot
from utils.logging import get_logger

logger = get_logger(__name__)

MISSING_PERCENTILE = 99
MISSING_STEP = 5
SCALED_RTOL = 1e-6
_BAR_COLS = ["symbol", "timestamp", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------- fetched bars

def _response_bars(path: str, symbol: str) -> pd.DataFrame:
    rows = ir.load_response(path)["response"].get("data") or []
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    return ic.with_clock(df.assign(symbol=symbol).astype({"timestamp": "int64"})[_BAR_COLS])


def fetched_sessions(symbol: str, ledger: pd.DataFrame, requests: pd.DataFrame) -> pd.DataFrame:
    """All fetched bars of one symbol, one source per session: a recovery response before a
    provenance-only response, then the earliest window (deterministic)."""
    last = ir.latest(ledger[ledger.symbol == symbol])
    final = last[last.outcome == "ok"].merge(
        requests[["request_id", "purpose"]], on="request_id")
    final = final.assign(rank=~final.purpose.str.contains("recovery")).sort_values(
        ["rank", "start", "request_id"])
    parts, taken = [], set()
    for row in final.itertuples():
        b = _response_bars(row.path, symbol)
        b = b[~b.session.isin(taken)]
        if len(b):
            parts.append(b.assign(request_id=row.request_id, requested_ist=row.requested_ist))
            taken |= set(b.session)
    if not parts:
        return pd.DataFrame(columns=[*_BAR_COLS, "session", "minute", "second", "request_id"])
    return pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------------- provenance (plan 5.5)

compare_session = ir.compare_session


def provenance_rows(symbol: str, legacy: pd.DataFrame, fetched: pd.DataFrame,
                    classes: pd.DataFrame, requests: pd.DataFrame) -> pd.DataFrame:
    """Score every legacy session inside a provenance window of this symbol."""
    prov = requests[(requests.symbol == symbol) & requests.purpose.str.contains("provenance")]
    mine = classes[(classes.symbol == symbol) & (classes.cls == ip.LEGACY)]
    rows = []
    for req in prov.itertuples():
        inside = mine[(mine.session >= pd.Timestamp(req.start))
                      & (mine.session <= pd.Timestamp(req.end))]
        for s in inside.session:
            rows.append({"symbol": symbol, "session": s, "request_id": req.request_id,
                         "stratum": req.stratum,
                         "result": compare_session(legacy[legacy.session == s],
                                                   fetched[fetched.session == s])})
    return pd.DataFrame(rows, columns=["symbol", "session", "request_id", "stratum", "result"])


# ---------------------------------------------------------------- one symbol

_NO_BARS = pd.DataFrame(columns=[*_BAR_COLS, "session", "minute", "second"])


def _regular_ledger(bars: pd.DataFrame, ref: pd.DataFrame, templates) -> pd.DataFrame:
    """Conversion ledger of a candidate source on its regular window, with a pass flag."""
    led = ic.convert_sessions(ic.summarise(bars, templates), ref)
    led["structure_ok"] = ic.structure_ok(led)
    led["passes"] = (led.status.isin(ic.PUBLISHABLE) & led.structure_ok
                     & led.edge_ok.fillna(False).astype(bool)
                     & led.volume_ok.fillna(False).astype(bool))
    return led.set_index("session")


def choose_sources(days, legacy, fetched, replaced: set, ref, templates) -> dict:
    """P3a: per post-3-Aug session, which source supplies the regular window, and why.

    Both are judged on the regular window alone, against NSE, with the unchanged conversion,
    structure, edge and volume-band gates. Both pass: the volume ratio closer to 1 wins, a tie
    goes to Historify. One passes: that one. Neither: Historify if it has bars (the session is
    then excluded by its own failing gate). A replaced symbol's legacy bars are another security
    and are never a candidate."""
    out = {}
    has_l = set(legacy.session) if len(legacy) else set()
    has_f = set(fetched.session) if len(fetched) else set()
    legacy_days = set(days) - set(replaced)
    led_l = _regular_ledger(legacy[legacy.session.isin(legacy_days)], ref, templates) if (
        has_l and legacy_days & has_l) else pd.DataFrame()
    led_f = _regular_ledger(fetched[fetched.session.isin(days)], ref, templates) if has_f else \
        pd.DataFrame()
    for d in days:
        l_ok = d not in replaced and d in has_l and d in led_l.index
        f_ok = d in has_f and d in led_f.index
        if l_ok and f_ok:
            pl, pf = bool(led_l.loc[d, "passes"]), bool(led_f.loc[d, "passes"])
            dl, df_ = abs(led_l.loc[d, "vol_ratio"] - 1), abs(led_f.loc[d, "vol_ratio"] - 1)
            if pl and pf:
                if dl < df_:
                    out[d] = ("legacy", "both_pass:legacy_closer")
                elif df_ < dl:
                    out[d] = ("recovered", "both_pass:recovered_closer")
                else:
                    out[d] = ("legacy", "both_pass:tie_legacy")
            elif pl:
                out[d] = ("legacy", "legacy_only_passes")
            elif pf:
                out[d] = ("recovered", "recovered_only_passes")
            else:
                out[d] = ("legacy", "neither_passes")
        elif l_ok:
            out[d] = ("legacy", "single_source:legacy")
        elif f_ok:
            out[d] = ("recovered", "single_source:recovered")
    return out


def _settled(fetched: pd.DataFrame) -> tuple[pd.DataFrame, set]:
    """AngelOne data fetched on the evening of its own session is provisional (measured
    2026-10-19: 370 bars with eight at 15:15-15:27, against 361 once settled). Only sessions
    requested on a later IST calendar day are used. Returns (settled bars, unsettled sessions)."""
    if not len(fetched) or "requested_ist" not in fetched:
        return fetched, set()
    asked = pd.to_datetime(fetched.requested_ist, utc=True).dt.tz_convert(
        "Asia/Kolkata").dt.tz_localize(None).dt.normalize()
    bad = asked <= fetched.session
    return fetched[~bad], set(fetched[bad].session) - set(fetched[~bad].session)


def unify_symbol(symbol: str, legacy_raw: pd.DataFrame, classes: pd.DataFrame,
                 fetched_raw: pd.DataFrame, requests: pd.DataFrame, ref: pd.DataFrame,
                 templates) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(unified vendor-basis bars with origin, session ledger, provenance rows) for one symbol.

    legacy_raw: snapshot bars; fetched_raw: `fetched_sessions` output."""
    bars, sessions, prov, _ = _unify(symbol, legacy_raw, classes, fetched_raw, requests, ref,
                                     templates)
    return bars, sessions, prov


def _unify(symbol: str, legacy_raw: pd.DataFrame, classes: pd.DataFrame,
           fetched_raw: pd.DataFrame, requests: pd.DataFrame, ref: pd.DataFrame,
           templates) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """`unify_symbol` plus the quarantined closing-window bars of both sources, with every
    original value and a `source` column (P3a: evidence is kept, nothing is deleted)."""
    cls = classes[classes.symbol == symbol]
    legacy_all = ic.clean(ic.with_clock(legacy_raw))[0] if len(legacy_raw) else legacy_raw
    legacy, legacy_q = (ic.split_closing(legacy_all, templates, keep_auction=False)
                        if len(legacy_all) else (legacy_all, _NO_BARS))
    aligned = pd.DataFrame(columns=["session", "applied", "second_min", "second_max"])
    if len(fetched_raw):
        fetched_raw, aligned = ic.align_timestamps(fetched_raw)
    fetched_all = ic.clean(fetched_raw)[0] if len(fetched_raw) else fetched_raw
    fetched_all, unsettled = _settled(fetched_all)
    fetched, fetched_q = (ic.split_closing(fetched_all, templates, keep_auction=True)
                          if len(fetched_all) else (fetched_all, _NO_BARS))
    quarantine = pd.concat([legacy_q.assign(source="legacy"), fetched_q.assign(source="recovered")],
                           ignore_index=True)
    prov = provenance_rows(symbol, legacy, fetched, classes, requests)
    replace = set(prov[prov.result.isin(["mismatch", "scaled"])].session)

    cas = pd.Timestamp(ip.CAS_START)
    cas_days = set(cls[(cls.session >= cas) & (cls.cls != ip.OUT_OF_SCOPE_MUHURAT)].session)
    replaced = set(cls[cls.cls == ip.RECOVER_REPLACED].session)
    choice = choose_sources(cas_days, legacy, fetched, replaced, ref[ref.symbol == symbol],
                            templates) if cas_days else {}
    keep_legacy = ((set(cls[cls.cls == ip.LEGACY].session) - replace - cas_days)
                   | {d for d, (src, _) in choice.items() if src == "legacy"})
    want_fresh = ((set(cls[cls.cls.str.startswith("recover:")].session) | replace) - cas_days
                  | {d for d, (src, _) in choice.items() if src == "recovered"})
    parts = []
    if len(legacy):
        parts.append(legacy[legacy.session.isin(keep_legacy)].assign(origin="legacy"))
    if len(fetched):
        f = fetched[fetched.session.isin(want_fresh)]
        parts.append(f.assign(origin=np.where(f.session.isin(replace), "recovered:provenance",
                                              "recovered")))
    parts = [p for p in parts if len(p)]
    bars = (pd.concat(parts, ignore_index=True).sort_values("timestamp", kind="stable")
            .reset_index(drop=True) if parts else pd.DataFrame(columns=[*_BAR_COLS, "session",
                                                                         "origin"]))

    sessions = cls[["symbol", "session", "cls", "defect_type", "contaminated"]].copy()
    if len(bars):
        led = ic.convert_sessions(ic.summarise(bars, templates), ref[ref.symbol == symbol])
        led["structure_ok"] = ic.structure_ok(led)
        origin = bars.groupby("session").origin.first().rename("origin").reset_index()
        sessions = sessions.merge(led.merge(origin, on="session", how="left"),
                                  on=["symbol", "session"], how="left")
    else:
        sessions = sessions.assign(status=None, origin=None, structure_ok=None,
                                   missing_minutes=np.nan)
    done = aligned[aligned.applied.astype(bool)]
    sessions["timestamp_aligned"] = (sessions.session.isin(set(done.session))
                                     & sessions.origin.fillna("").str.startswith("recovered"))
    basis = {d: f"vendor stamps {a}-{z} s past the previous minute; aligned to the minute start"
             for d, a, z in zip(done.session, done.second_min, done.second_max, strict=True)}
    sessions["timestamp_basis"] = sessions.session.map(basis).where(
        sessions.timestamp_aligned, None)
    sessions.loc[sessions.cls == ip.OUT_OF_SCOPE_MUHURAT, "status"] = ip.OUT_OF_SCOPE_MUHURAT
    sessions.loc[sessions.status.isna(), "status"] = "missing"
    sessions["source_choice"] = sessions.session.map({d: why for d, (_, why) in choice.items()})
    # a session whose only candidate was provisional AngelOne data is excluded for that reason
    unsettled_only = sessions.session.isin(unsettled) & sessions.status.eq("missing")
    sessions.loc[unsettled_only, "status"] = "excluded:unsettled"
    sessions["settled"] = ~unsettled_only
    sessions["session_hours_verification"] = sessions.session.map(
        lambda d: ic.template_for(pd.Timestamp(d).date(), templates).verification)
    return bars, sessions, prov, quarantine


# ---------------------------------------------------------------- missing-minute policy

def missing_minute_threshold(sessions: pd.DataFrame) -> dict:
    """Plan 7.3 / A5: N = the policy percentile of missing minutes over sessions whose prices
    and volume pass every rule except the threshold itself (volume completeness shows the
    session's trading is captured), rounded up to the policy step."""
    v = ic.field_validity(sessions)
    pop = sessions[v.volume_valid]
    p = float(np.percentile(pop.missing_minutes, MISSING_PERCENTILE))
    n = int(math.ceil(p / MISSING_STEP) * MISSING_STEP)
    by_year = pop.groupby(pop.session.dt.year).missing_minutes.describe(
        percentiles=[0.5, 0.9, 0.95, 0.99])
    return {"method": f"P{MISSING_PERCENTILE} of missing minutes over price- and volume-valid "
                      f"sessions, rounded up to a multiple of {MISSING_STEP}",
            "population": int(len(pop)), "percentile_value": p, "N": n,
            "by_year": json.loads(by_year.round(2).to_json(orient="index"))}


def quality_table(sessions: pd.DataFrame, n: int) -> pd.DataFrame:
    """Part A6: one row per expected session with its status, defect, treatment, origin and a
    validity flag per field family. The dataset makes no claim about any strategy."""
    s = sessions.copy()
    v = ic.field_validity(s, missing_threshold=n)
    s[v.columns] = v
    scope = s.cls.eq(ip.OUT_OF_SCOPE_MUHURAT)
    s.loc[scope, v.columns] = False
    price_ok = s.status.isin(ic.PUBLISHABLE)
    structure = s.structure_ok.fillna(False).astype(bool)
    s["price_status"] = s.status.where(~price_ok | ~structure | s.price_valid,
                                       "excluded:partial_session")
    s.loc[price_ok & ~structure, "price_status"] = "excluded:structure"
    s.loc[s.price_valid, "price_status"] = s.status
    defect = ip.defect_type(s.assign(final_bar_convention=s.final_bar_convention.fillna("none")))
    defect[~s.price_valid & defect.isna()] = "partial_session"
    defect[s.status.eq("missing")] = "missing_session"
    defect[s.status.eq("excluded:unsettled")] = "unsettled_session"
    defect[scope] = "out_of_scope_session"
    s["final_defect"] = defect
    origin = s.origin.fillna("")
    legacy_defect = origin.eq("legacy") & s.defect_type.notna()
    aligned = s.get("timestamp_aligned", pd.Series(False, index=s.index)).fillna(False)
    s["treatment"] = np.select(
        [scope, s.status.eq("missing"), aligned.astype(bool) & s.price_valid,
         origin.str.startswith("recovered"), legacy_defect,
         origin.eq("legacy") & s.contaminated.fillna(False).astype(bool)],
        ["expected:out_of_scope", "excluded:missing_session", ic.ALIGNED_TREATMENT,
         "recovered", "unresolved:irrecoverable_at_source", "repaired:foreign_bar_removal"],
        "accepted")
    # An excluded session never reads "accepted": it carries its exclusion.
    excluded = s.treatment.isin(["accepted", "recovered"]) & ~s.price_valid
    s.loc[excluded, "treatment"] = "excluded:" + s.loc[excluded, "final_defect"].astype(str)
    return _typed_quality(s)


#: Spec-2 quality columns and their types. A missing value is False / NaN, never object.
_BOOL_COLS = ("sparse_complete", "settled", "daily_reconciled", "fallback_conversion",
              "ca_demerger_approx", "non_unit_factor", "ca_unreconciled")
_NUM_COLS = ("closing_noise_bars", "closing_negvol_bars", "auction_price", "auction_volume")
_NEW_TEXT_COLS = ("source_choice", "session_hours_verification", "basis_class", "k_explanation")


def _typed_quality(s: pd.DataFrame) -> pd.DataFrame:
    """Add the spec-2 columns that the ledger lacks and give each its type. A session with a
    non-unit daily factor that is price-valid but does not reconcile with the daily reference is
    flagged `ca_unreconciled` (flagged, not excluded: the existing gates are unchanged)."""
    for c in _BOOL_COLS:
        if c not in s:
            s[c] = False
    for c in _NUM_COLS:
        if c not in s:
            s[c] = np.nan
    for c in _NEW_TEXT_COLS:
        if c not in s:
            s[c] = None
    s["ca_unreconciled"] = (s.non_unit_factor.fillna(False).astype(bool)
                            & s.price_valid.astype(bool)
                            & ~s.daily_reconciled.fillna(False).astype(bool))
    for c in _BOOL_COLS:
        s[c] = s[c].fillna(False).astype(bool)
    return s


# ---------------------------------------------------------------- run

def write_by_year(bars: pd.DataFrame, folder: Path) -> None:
    """One parquet file per calendar year of `bars.session` in `folder`."""
    for yr, part in bars.groupby(bars.session.dt.year):
        out = folder / f"{int(yr)}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        part.reset_index(drop=True).to_parquet(out, index=False)


def run(snapshot_folder: Path) -> Path:
    if fz.verify(snapshot_folder):
        raise fz.FreezeError("snapshot does not verify")
    manifest = fz.load(snapshot_folder)
    build = ip.build_folder(snapshot_folder, manifest)
    errors = ir.verify(build)
    if errors:
        raise RuntimeError(f"recovery is not complete or verifiable: {errors[:3]}")
    templates = ic.load_templates()
    ref = pd.read_parquet(build / "reference.parquet")
    classes = pd.read_parquet(build / "classification.parquet")
    if classes.cls.str.startswith(ip.CANDIDATE).any():
        raise RuntimeError("recovery candidates are undecided; run --decide first")
    requests = pd.read_parquet(build / "requests.parquet")
    ledger = ir.read_ledger(build)
    unified_dir = build / "unified"
    unified_dir.mkdir(exist_ok=True)
    ledgers, provs, quarantined = [], [], []
    symbols = sorted(classes.symbol.unique())
    plan_doc = json.loads((build / "plan.json").read_text())
    daily = _snapshot.resolve_version(plan_doc["daily_version"])
    events = pd.read_parquet(_snapshot._object_path(_snapshot.DEFAULT_ROOT, daily["events_sha256"]))
    for i, sym in enumerate(symbols):
        bars, sess, prov, quarantine = _unify(
            sym, ip.read_symbol(snapshot_folder, manifest, sym), classes,
            fetched_sessions(sym, ledger, requests), requests, ref, templates)
        bars[[*_BAR_COLS, "session", "origin"]].to_parquet(unified_dir / f"{sym}.parquet",
                                                           index=False)
        ledgers.append(sess)
        provs.append(prov)
        quarantined.append(quarantine)
        if i % 20 == 0:
            logger.info(f"build: unified {i + 1} of {len(symbols)} symbols")
    sessions = ic.ca_checks(pd.concat(ledgers, ignore_index=True), events)
    pd.concat([q for q in quarantined if len(q)], ignore_index=True)[
        ["symbol", "timestamp", "open", "high", "low", "close", "volume", "session", "minute",
         "source"]].to_parquet(build / "quarantine.parquet", index=False)
    policy = missing_minute_threshold(sessions)
    policy["data_policy_version"] = ic.policy_version()
    sessions = quality_table(sessions, policy["N"])
    (build / "policy.json").write_text(json.dumps(policy, indent=1) + "\n")
    pd.concat(provs, ignore_index=True).to_parquet(build / "provenance.parquet", index=False)
    sessions.to_parquet(build / "session_ledger.parquet", index=False)

    checks = []
    expected = classes[~classes.cls.str.startswith("out_of_scope")][["symbol", "session"]]
    for i, sym in enumerate(symbols):
        one = ic.convert_bars(pd.read_parquet(unified_dir / f"{sym}.parquet"),
                              sessions[sessions.symbol == sym])
        if one.empty:
            continue
        five = i5.aggregate(one)
        res = i5.validate(one, five, templates, expected[expected.symbol == sym])
        checks.append({"symbol": sym, "bars_1m": len(one), "bars_5m": len(five), **res})
        write_by_year(five, build / "five" / sym)
        # The canonical 1m bars the 5m bars are built from: the input of a 1m dataset version
        # (volume profiles and developing profiles need minute resolution).
        write_by_year(one, build / "one" / sym)
        if i % 20 == 0:
            logger.info(f"build: 5m {i + 1} of {len(symbols)} symbols")
    pd.DataFrame(checks).to_parquet(build / "checks_5m.parquet", index=False)
    (build / "build.json").write_text(json.dumps({
        "snapshot_root_hash": manifest["root_hash"],
        "template_sha256": _snapshot.sha256_file(ic.TEMPLATE_PATH),
        "conversion_spec_version": ic.CONVERSION_SPEC_VERSION,
        "missing_minute_N": policy["N"],
        "data_policy_version": ic.policy_version(),
        "status_counts": sessions.price_status.value_counts().to_dict()}, indent=1) + "\n")
    logger.info(f"build: finished {build}")
    return build


# ---------------------------------------------------------------- irrecoverability (A4)

def decide_cells(snapshot_folder: Path) -> dict:
    """Score the irrecoverability sample and turn every candidate into recover:<type> or legacy.

    A cell is irrecoverable at the source when at least `identical_share` of its sample comes
    back identical; its sessions stay legacy (flagged unresolved), except sampled sessions that
    came back different, which are replaced. Other cells are recovered in full."""
    manifest = fz.load(snapshot_folder)
    build = ip.build_folder(snapshot_folder, manifest)
    sample = pd.read_parquet(build / "irrecoverability_sample.parquet")
    classes = pd.read_parquet(build / "classification.parquet")
    requests = pd.read_parquet(build / "requests.parquet")
    ledger = ir.read_ledger(build)
    rows = []
    for sym, part in sample.groupby("symbol", sort=True):
        legacy = ic.clean(ic.with_clock(ip.read_symbol(snapshot_folder, manifest, sym)))[0]
        fetched = fetched_sessions(sym, ledger, requests)
        if len(fetched):
            fetched = ic.clean(fetched)[0]
        for r in part.itertuples():
            rows.append({"symbol": sym, "session": r.session, "cell": r.cell,
                         "result": compare_session(legacy[legacy.session == r.session],
                                                   fetched[fetched.session == r.session])})
    res = pd.DataFrame(rows, columns=["symbol", "session", "cell", "result"])
    share = res.assign(same=res.result.eq("exact")).groupby("cell").same.agg(["mean", "size"])
    limit = ip.IRRECOVERABILITY["identical_share"]
    cells = {cell: {"sampled": int(n), "identical_share": round(float(m), 4),
                    "decision": "irrecoverable_at_source" if m >= limit else "recover"}
             for cell, (m, n) in share.iterrows()}
    cell_of = classes.cls + ":" + classes.session.dt.year.astype(str)
    cand = classes.cls.str.startswith(ip.CANDIDATE)
    recover_cell = cell_of.map(lambda k: cells.get(k, {}).get("decision") == "recover")
    differs = set(zip(res[~res.result.eq("exact")].symbol, res[~res.result.eq("exact")].session,
                      strict=True))
    sampled_diff = pd.Series([(s, d) in differs for s, d in zip(classes.symbol, classes.session,
                                                                 strict=True)],
                             index=classes.index)
    to_recover = cand & (recover_cell | sampled_diff)
    classes.loc[to_recover, "cls"] = "recover:" + classes.loc[to_recover, "defect_type"]
    classes.loc[cand & ~to_recover, "cls"] = ip.LEGACY
    requests = ip.build_requests(classes, sample)
    res.to_parquet(build / "irrecoverability_results.parquet", index=False)
    classes.to_parquet(build / "classification.parquet", index=False)
    requests.to_parquet(build / "requests.parquet", index=False)
    out = {"policy": ip.IRRECOVERABILITY, "data_policy_version": ic.policy_version(),
           "cells": cells, "sessions_to_recover": int(to_recover.sum()),
           "requests_total": len(requests)}
    (build / "irrecoverability.json").write_text(json.dumps(out, indent=1) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.intraday_build")
    ap.add_argument("--snapshot", required=True, type=Path)
    ap.add_argument("--decide", action="store_true",
                    help="score the irrecoverability sample and finalise the recovery plan")
    args = ap.parse_args(argv)
    if args.decide:
        out = decide_cells(args.snapshot)
        print({k: v for k, v in out.items() if k != "cells"})
        return 0
    print(run(args.snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
