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
    final = ledger[(ledger.symbol == symbol) & (ledger.outcome == "ok")]
    final = final.drop_duplicates("request_id", keep="last").merge(
        requests[["request_id", "purpose"]], on="request_id")
    final = final.assign(rank=~final.purpose.str.contains("recovery")).sort_values(
        ["rank", "start", "request_id"])
    parts, taken = [], set()
    for row in final.itertuples():
        b = _response_bars(row.path, symbol)
        b = b[~b.session.isin(taken)]
        if len(b):
            parts.append(b.assign(request_id=row.request_id))
            taken |= set(b.session)
    if not parts:
        return pd.DataFrame(columns=[*_BAR_COLS, "session", "minute", "second", "request_id"])
    return pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------------- provenance (plan 5.5)

def compare_session(legacy: pd.DataFrame, fresh: pd.DataFrame) -> str:
    """exact | scaled (one price multiplier) | mismatch | fresh_missing."""
    if fresh.empty:
        return "fresh_missing"
    a = legacy.sort_values("timestamp")
    b = fresh.sort_values("timestamp")
    if len(a) != len(b) or not np.array_equal(a.timestamp.to_numpy(), b.timestamp.to_numpy()):
        return "mismatch"
    fields = ["open", "high", "low", "close"]
    pa, pb = a[fields].to_numpy(float), b[fields].to_numpy(float)
    if np.array_equal(pa, pb) and np.array_equal(a.volume.to_numpy(), b.volume.to_numpy()):
        return "exact"
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = pb / pa
        r = float(ratio.mean())
        vol = b.volume.to_numpy(float) / a.volume.to_numpy(float)
    one_multiplier = (np.isfinite(ratio).all() and abs(r - 1) > SCALED_RTOL
                      and np.ptp(ratio) <= SCALED_RTOL * abs(r))
    volume_follows = np.array_equal(a.volume.to_numpy(), b.volume.to_numpy()) or (
        np.isfinite(vol).all() and np.allclose(vol, 1 / r, rtol=SCALED_RTOL))
    return "scaled" if one_multiplier and volume_follows else "mismatch"


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

def unify_symbol(symbol: str, legacy_raw: pd.DataFrame, classes: pd.DataFrame,
                 fetched_raw: pd.DataFrame, requests: pd.DataFrame, ref: pd.DataFrame,
                 templates) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(unified vendor-basis bars with origin, session ledger, provenance rows) for one symbol.

    legacy_raw: snapshot bars; fetched_raw: `fetched_sessions` output."""
    cls = classes[classes.symbol == symbol]
    legacy = ic.clean(ic.with_clock(legacy_raw))[0] if len(legacy_raw) else legacy_raw
    fetched = ic.clean(fetched_raw)[0] if len(fetched_raw) else fetched_raw
    prov = provenance_rows(symbol, legacy, fetched, classes, requests)
    replace = set(prov[prov.result.isin(["mismatch", "scaled"])].session)

    keep_legacy = set(cls[cls.cls == ip.LEGACY].session) - replace
    want_fresh = set(cls[cls.cls.str.startswith("recover:")].session) | replace
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

    sessions = cls[["symbol", "session", "cls"]].copy()
    if len(bars):
        led = ic.convert_sessions(ic.summarise(bars, templates), ref[ref.symbol == symbol])
        led["structure_ok"] = ic.structure_ok(led)
        origin = bars.groupby("session").origin.first().rename("origin").reset_index()
        sessions = sessions.merge(led.merge(origin, on="session", how="left"),
                                  on=["symbol", "session"], how="left")
    else:
        sessions = sessions.assign(status=None, origin=None, structure_ok=None,
                                   missing_minutes=np.nan)
    sessions.loc[sessions.cls == ip.OUT_OF_SCOPE_MUHURAT, "status"] = ip.OUT_OF_SCOPE_MUHURAT
    sessions.loc[sessions.status.isna(), "status"] = "missing"
    bad = sessions.status.isin(ic.PUBLISHABLE) & ~sessions.structure_ok.fillna(False).astype(bool)
    sessions.loc[bad, "status"] = "excluded:structure"
    return bars, sessions, prov


# ---------------------------------------------------------------- missing-minute policy

def missing_minute_threshold(sessions: pd.DataFrame) -> dict:
    """Plan 7.3: N = the 99th percentile of missing minutes over sessions that pass every
    blocking 7.1 and 7.2 rule, rounded up to the next multiple of 5."""
    pop = sessions[sessions.status.isin(ic.PUBLISHABLE)]
    p = float(np.percentile(pop.missing_minutes, MISSING_PERCENTILE))
    n = int(math.ceil(p / MISSING_STEP) * MISSING_STEP)
    by_year = pop.groupby(pop.session.dt.year).missing_minutes.describe(
        percentiles=[0.5, 0.9, 0.95, 0.99])
    return {"method": f"P{MISSING_PERCENTILE} of missing minutes over sessions passing 7.1 and "
                      f"7.2, rounded up to a multiple of {MISSING_STEP}",
            "population": int(len(pop)), "percentile_value": p, "N": n,
            "by_year": json.loads(by_year.round(2).to_json(orient="index"))}


def apply_threshold(sessions: pd.DataFrame, n: int) -> pd.DataFrame:
    over = sessions.status.isin(ic.PUBLISHABLE) & (sessions.missing_minutes > n)
    return sessions.assign(status=sessions.status.where(~over, "excluded:missing_minutes"))


# ---------------------------------------------------------------- run

def run(snapshot_folder: Path) -> Path:
    if fz.verify(snapshot_folder):
        raise fz.FreezeError("snapshot does not verify")
    manifest = fz.load(snapshot_folder)
    build = snapshot_folder.parent / f"build-{manifest['root_hash'][:12]}"
    errors = ir.verify(build)
    if errors:
        raise RuntimeError(f"recovery is not complete or verifiable: {errors[:3]}")
    templates = ic.load_templates()
    ref = pd.read_parquet(build / "reference.parquet")
    classes = pd.read_parquet(build / "classification.parquet")
    requests = pd.read_parquet(build / "requests.parquet")
    ledger = ir.read_ledger(build)
    unified_dir = build / "unified"
    unified_dir.mkdir(exist_ok=True)
    ledgers, provs = [], []
    symbols = sorted(classes.symbol.unique())
    for i, sym in enumerate(symbols):
        bars, sess, prov = unify_symbol(sym, ip.read_symbol(snapshot_folder, manifest, sym),
                                        classes, fetched_sessions(sym, ledger, requests),
                                        requests, ref, templates)
        bars[[*_BAR_COLS, "session", "origin"]].to_parquet(unified_dir / f"{sym}.parquet",
                                                           index=False)
        ledgers.append(sess)
        provs.append(prov)
        if i % 20 == 0:
            logger.info(f"build: unified {i + 1} of {len(symbols)} symbols")
    sessions = pd.concat(ledgers, ignore_index=True)
    policy = missing_minute_threshold(sessions)
    sessions = apply_threshold(sessions, policy["N"])
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
        for yr, part in five.groupby(five.session.dt.year):
            out = build / "five" / sym / f"{int(yr)}.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            part.reset_index(drop=True).to_parquet(out, index=False)
        if i % 20 == 0:
            logger.info(f"build: 5m {i + 1} of {len(symbols)} symbols")
    pd.DataFrame(checks).to_parquet(build / "checks_5m.parquet", index=False)
    (build / "build.json").write_text(json.dumps({
        "snapshot_root_hash": manifest["root_hash"],
        "template_sha256": _snapshot.sha256_file(ic.TEMPLATE_PATH),
        "conversion_spec_version": ic.CONVERSION_SPEC_VERSION,
        "missing_minute_N": policy["N"],
        "status_counts": sessions.status.value_counts().to_dict()}, indent=1) + "\n")
    logger.info(f"build: finished {build}")
    return build


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.intraday_build")
    ap.add_argument("--snapshot", required=True, type=Path)
    args = ap.parse_args(argv)
    print(run(args.snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
