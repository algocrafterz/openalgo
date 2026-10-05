"""Intraday build steps 2-3: assess the frozen legacy sessions and plan the recovery requests.

Reads only the frozen snapshot (intraday_freeze) and the published daily version. Writes, under
`<staging>/build-<snapshot root[:12]>/`:
    reference.parquet         NSE values per expected (symbol, session), 2017-01-01 onward
    cleanup_ledger.parquet    every bar removed by C1/C2, with its rule
    legacy_ledger.parquet     conversion ledger of every legacy session after cleanup
    classification.parquet    one row per expected (symbol, session): its class
    requests.parquet          the deterministic request list (recovery and provenance)
    plan.json                 counts and the snapshot root hash this plan was made from

INTRADAY-5M-DATASET-PLAN.md sections 4, 5.3 and 5.5.

    uv run python -m signal_engine.backtest.data_readiness.intraday_plan \
        --snapshot db/staging/intraday/legacy-1m-<stamp>-ist --daily-version nse-daily-20261005-002
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

from signal_engine.backtest.data_readiness import intraday_convert as ic
from signal_engine.backtest.data_readiness import intraday_freeze as fz
from signal_engine.backtest.data_readiness import snapshot as _snapshot
from utils.logging import get_logger

logger = get_logger(__name__)

HISTORY_START = date(2017, 1, 1)
HISTORY_END = date(2026, 10, 1)
CAS_START = date(2026, 8, 3)
WINDOW_DAYS = 29                       # a 1m request covers at most 29 calendar days
REPLACED_SYMBOLS = ("APOLLOTYRE", "EMAMILTD", "EXIDEIND")      # C5
PROVENANCE_SEED = 29
REGIME_WINDOWS = ((date(2018, 10, 1), "2018-10 IL&FS stress, doubled-volume era"),
                  (date(2020, 3, 2), "2020-03 COVID crash"),
                  (date(2023, 9, 1), "2023-09 fallback-heavy period"),
                  (date(2025, 7, 1), "2025-07 mixed-vendor era"))

LEGACY = "legacy"
RECOVER_GAP = "recover:gap"
RECOVER_NEGVOL = "recover:negative_volume"
RECOVER_REPLACED = "recover:replaced_symbol"
RECOVER_CONTAMINATED = "recover:contaminated_failing"
OUT_OF_SCOPE_MUHURAT = "out_of_scope:muhurat"
CALENDAR_PATH = ic.TEMPLATE_PATH.parent / "nse_calendar.yaml"


def muhurat_dates(path: Path = CALENDAR_PATH) -> list[pd.Timestamp]:
    """Muhurat sessions of the NSE calendar: out of scope for the intraday dataset (plan 9).
    No mapping of misdated vendor bars to them is attempted."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [pd.Timestamp(s["date"]) for s in doc.get("special_sessions") or []
            if s.get("kind") == "muhurat"]


# ---------------------------------------------------------------- reference

def load_reference(snapshot_folder: Path, daily_version: str) -> pd.DataFrame:
    """NSE values for every expected (symbol, session) of the history range.

    The close reference is NSE `last` before the closing auction and the official close from it
    (plan 6.1). Expected sessions are the published daily rows."""
    m = _snapshot.resolve_version(daily_version)
    root = _snapshot.DEFAULT_ROOT
    index = pd.read_parquet(_snapshot._object_path(root, m["index_sha256"]))
    index = index[(index.interval == "D") & (index.period >= HISTORY_START.year)]
    cols = ["symbol", "session", "series", "raw_open", "raw_high", "raw_low", "raw_close",
            "raw_volume", "price_factor", "volume_factor", "open", "high", "low", "close",
            "volume"]
    daily = pd.concat([pd.read_parquet(_snapshot._object_path(root, s), columns=cols)
                       for s in index.object_sha256], ignore_index=True)
    daily["session"] = pd.to_datetime(daily.session).dt.normalize()
    daily = daily[(daily.session >= pd.Timestamp(HISTORY_START))
                  & (daily.session <= pd.Timestamp(HISTORY_END))]
    last = pd.read_parquet(snapshot_folder / "reference" / "nse_last.parquet")
    last["session"] = pd.to_datetime(last.session).dt.normalize()
    ref = daily.merge(last, on=["symbol", "session"], how="left", validate="one_to_one")
    ref["ref_close"] = ref["last"].where(ref.session < pd.Timestamp(CAS_START), ref.raw_close)
    return ref.sort_values(["symbol", "session"]).reset_index(drop=True)


# ---------------------------------------------------------------- legacy assessment

def read_symbol(snapshot_folder: Path, manifest: dict, symbol: str) -> pd.DataFrame:
    paths = [snapshot_folder / f["path"] for f in manifest["files"] if f["symbol"] == symbol]
    if not paths:
        return pd.DataFrame(columns=["symbol", "timestamp", "open", "high", "low", "close",
                                     "volume"])
    return pd.concat([pd.read_parquet(p) for p in sorted(paths)], ignore_index=True)


def assess_symbol(bars: pd.DataFrame, ref: pd.DataFrame,
                  templates) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(conversion ledger with cleanup flags, removed bars) for one symbol's legacy sessions in
    the history range. Sessions without an NSE row are kept with status excluded:no_nse_row
    (C4: out of scope)."""
    b = ic.with_clock(bars)
    b = b[(b.session >= pd.Timestamp(HISTORY_START)) & (b.session <= pd.Timestamp(HISTORY_END))]
    kept, removed, flags = ic.clean(b)
    summary = ic.summarise(kept, templates)
    ledger = ic.convert_sessions(summary, ref)
    ledger["structure_ok"] = ic.structure_ok(ledger)
    ledger = ledger.merge(flags, on=["symbol", "session"], how="left")
    ledger[["contaminated", "negative_volume"]] = ledger[
        ["contaminated", "negative_volume"]].fillna(False).astype(bool)
    return ledger, removed[["symbol", "timestamp", "rule"]]


def classify(ref: pd.DataFrame, legacy: pd.DataFrame) -> pd.DataFrame:
    """One row per expected (symbol, session) with its class (plan 4 and 5.3)."""
    cols = ["symbol", "session", "status", "structure_ok", "contaminated", "negative_volume"]
    d = ref[["symbol", "session"]].merge(legacy[cols], on=["symbol", "session"], how="left")
    publishable = d.status.isin(ic.PUBLISHABLE) & d.structure_ok.fillna(False).astype(bool)
    cls = pd.Series(LEGACY, index=d.index, dtype=object)
    cls[d.contaminated.fillna(False).astype(bool) & ~publishable] = RECOVER_CONTAMINATED
    cls[d.negative_volume.fillna(False).astype(bool)] = RECOVER_NEGVOL
    cls[d.status.isna()] = RECOVER_GAP
    cls[d.symbol.isin(REPLACED_SYMBOLS)] = RECOVER_REPLACED
    cls[d.session.isin(muhurat_dates())] = OUT_OF_SCOPE_MUHURAT
    d["cls"] = cls
    return d


# ---------------------------------------------------------------- requests

def block(day: date) -> tuple[date, date]:
    """The fixed 29-day request block containing `day`, on a grid anchored at HISTORY_START.

    A fixed grid keeps request ids stable whatever else the plan contains, so a re-plan after a
    policy change reuses every landing file already fetched."""
    k = (day - HISTORY_START).days // WINDOW_DAYS
    start = HISTORY_START + timedelta(days=k * WINDOW_DAYS)
    return start, start + timedelta(days=WINDOW_DAYS - 1)


def windows(sessions: Iterable[pd.Timestamp]) -> list[tuple[date, date]]:
    """The grid blocks that cover `sessions`, in order."""
    return sorted({block(pd.Timestamp(x).date()) for x in sessions})


def request_id(symbol: str, start: date, end: date) -> str:
    return hashlib.sha256(f"NSE|1m|{symbol}|{start}|{end}".encode()).hexdigest()[:16]


def recovery_requests(classes: pd.DataFrame) -> pd.DataFrame:
    need = classes[classes.cls.str.startswith("recover:")]
    rows = []
    for sym, part in need.groupby("symbol", sort=True):
        for a, b in windows(part.session):
            inside = part[(part.session >= pd.Timestamp(a)) & (part.session <= pd.Timestamp(b))]
            rows.append({"symbol": sym, "start": a, "end": b, "purpose": "recovery",
                         "stratum": ",".join(sorted(inside.cls.unique()))})
    return pd.DataFrame(rows)


def provenance_requests(classes: pd.DataFrame) -> pd.DataFrame:
    """Stratified sample (plan 5.5): per symbol and year, one grid block drawn with a fixed seed
    from the blocks that start in that year and hold an expected session of the symbol; plus the
    four regime and problem windows for every symbol. The draw does not depend on how sessions
    are classified; only legacy sessions inside the windows are scored (intraday_build)."""
    scope = classes[~classes.cls.str.startswith("out_of_scope")]
    rows = []
    for sym, part in scope.groupby("symbol", sort=True):
        blocks = sorted({block(d) for d in part.session.dt.date})
        for yr in sorted({a.year for a, _ in blocks}):
            choices = [b for b in blocks if b[0].year == yr]
            start, end = random.Random(f"{PROVENANCE_SEED}:{sym}:{yr}").choice(choices)
            rows.append({"symbol": sym, "start": start, "end": end, "purpose": "provenance",
                         "stratum": f"year:{yr}"})
        for start, label in REGIME_WINDOWS:
            end = start + timedelta(days=WINDOW_DAYS - 1)
            if ((part.session >= pd.Timestamp(start)) & (part.session <= pd.Timestamp(end))).any():
                rows.append({"symbol": sym, "start": start, "end": end, "purpose": "provenance",
                             "stratum": f"regime:{label}"})
    return pd.DataFrame(rows)


def build_requests(classes: pd.DataFrame) -> pd.DataFrame:
    req = pd.concat([recovery_requests(classes), provenance_requests(classes)], ignore_index=True)
    req["request_id"] = [request_id(s, a, b) for s, a, b in zip(req.symbol, req.start, req.end, strict=False)]
    # The same window can serve recovery and provenance; it is fetched once.
    req = req.groupby(["request_id", "symbol", "start", "end"], as_index=False, sort=True).agg(
        purpose=("purpose", lambda x: "+".join(sorted(set(x)))),
        stratum=("stratum", lambda x: ";".join(sorted(set(x)))))
    return req.sort_values(["symbol", "start"]).reset_index(drop=True)


# ---------------------------------------------------------------- run

def replan(out: Path, snapshot_root: str, daily_version: str) -> pd.DataFrame:
    """Classify and plan from the saved legacy ledger and reference (no re-assessment)."""
    ref = pd.read_parquet(out / "reference.parquet")
    legacy = pd.read_parquet(out / "legacy_ledger.parquet")
    classes = classify(ref, legacy)
    requests = build_requests(classes)
    classes.to_parquet(out / "classification.parquet", index=False)
    requests.to_parquet(out / "requests.parquet", index=False)
    summary = {
        "snapshot_root_hash": snapshot_root,
        "daily_version": daily_version,
        "template_sha256": _snapshot.sha256_file(ic.TEMPLATE_PATH),
        "conversion_spec_version": ic.CONVERSION_SPEC_VERSION,
        "expected_sessions": len(classes),
        "classes": classes.cls.value_counts().to_dict(),
        "legacy_out_of_scope_sessions": int((legacy.status == "excluded:no_nse_row").sum()),
        "legacy_status": legacy.status.value_counts().to_dict(),
        "requests": requests.purpose.value_counts().to_dict(),
        "requests_total": len(requests),
    }
    (out / "plan.json").write_text(json.dumps(summary, indent=1, default=str) + "\n")
    return requests


def run(snapshot_folder: Path, daily_version: str) -> Path:
    errors = fz.verify(snapshot_folder)
    if errors:
        raise fz.FreezeError(f"snapshot does not verify: {errors[:3]}")
    manifest = fz.load(snapshot_folder)
    out = snapshot_folder.parent / f"build-{manifest['root_hash'][:12]}"
    out.mkdir(exist_ok=True)
    templates = ic.load_templates()
    ref = load_reference(snapshot_folder, daily_version)
    ledgers, removed = [], []
    symbols = manifest["scope"]["symbols"]
    for i, sym in enumerate(symbols):
        bars = read_symbol(snapshot_folder, manifest, sym)
        if bars.empty:
            continue
        led, rem = assess_symbol(bars, ref[ref.symbol == sym], templates)
        ledgers.append(led)
        removed.append(rem)
        if i % 20 == 0:
            logger.info(f"plan: assessed {i + 1} of {len(symbols)} symbols")
    legacy = pd.concat(ledgers, ignore_index=True)
    ref.to_parquet(out / "reference.parquet", index=False)
    pd.concat(removed, ignore_index=True).to_parquet(out / "cleanup_ledger.parquet", index=False)
    legacy.to_parquet(out / "legacy_ledger.parquet", index=False)
    requests = replan(out, manifest["root_hash"], daily_version)
    logger.info(f"plan: {len(requests)} requests written to {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.intraday_plan")
    ap.add_argument("--snapshot", required=True, type=Path)
    ap.add_argument("--daily-version", required=True)
    ap.add_argument("--replan", action="store_true",
                    help="re-classify and re-plan from the saved ledger only")
    args = ap.parse_args(argv)
    if args.replan:
        root = fz.load(args.snapshot)["root_hash"]
        out = args.snapshot.parent / f"build-{root[:12]}"
        print(len(replan(out, root, args.daily_version)), "requests")
        return 0
    print(run(args.snapshot, args.daily_version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
