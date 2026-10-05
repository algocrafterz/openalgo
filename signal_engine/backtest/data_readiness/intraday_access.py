"""Read path and preflight for published intraday versions (plan Part A6, criterion 20).

The dataset states quality per field family; the strategy's data contract says which families it
uses (`DataContract.field_families`; none declared = every family is required). This module joins
the two. A session is available to a run only when every required family is valid and the
session lies inside each required family's usable range. Volume of a served session whose volume
is invalid (and not required) is withheld as NaN, so it cannot be used by accident.

Daily versions never come here; `preflight.run_preflight` and `PublishedSource.intraday` call in
only for intraday frequencies.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from signal_engine.backtest.data_readiness import intraday_convert as ic
from signal_engine.backtest.data_readiness import snapshot
from signal_engine.backtest.data_readiness import state as st
from signal_engine.timeutils import IST


def required_families(contract) -> tuple[str, ...]:
    declared = getattr(contract, "field_families", None)
    if not declared:
        return ic.FIELD_FAMILIES
    unknown = sorted(set(declared) - set(ic.FIELD_FAMILIES))
    if unknown:
        raise ValueError(f"unknown field families {unknown}; known {list(ic.FIELD_FAMILIES)}")
    return tuple(declared)


def load_quality(manifest: dict, root=snapshot.DEFAULT_ROOT) -> pd.DataFrame:
    sha = (manifest.get("extra_objects") or {}).get("quality_table")
    if not sha:
        raise snapshot.SnapshotError(f"{manifest['dataset_version']} has no quality table")
    q = pd.read_parquet(snapshot._object_path(Path(root), sha))
    q["session"] = pd.to_datetime(q.session)
    return q


def availability(quality: pd.DataFrame, families: tuple[str, ...], removals: list[dict],
                 start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Per requested (symbol, session): available, and the reason when not."""
    q = quality[~quality.cls.str.startswith("out_of_scope")].copy()
    if start is not None:
        q = q[q.session >= pd.Timestamp(start)]
    if end is not None:
        q = q[q.session <= pd.Timestamp(end)]
    reason = pd.Series(None, index=q.index, dtype=object)
    for fam in reversed(families):
        reason[~q[f"{fam}_valid"].astype(bool)] = f"{fam}_invalid"
    for r in removals:
        if r["family"] not in families:
            continue
        hit = ((q.session.dt.year.astype(str) == r["year"]) if "year" in r
               else (q.symbol == r["symbol"]))
        reason[hit & reason.isna()] = f"usable_range_removed:{r['family']}"
    reason = reason.astype(object).where(reason.notna(), None)
    return pd.DataFrame({"symbol": q.symbol, "session": q.session, "available": reason.isna(),
                         "reason": reason, "volume_valid": q.volume_valid.astype(bool)})


def _session_dates(con) -> set[date]:
    rows = con.execute("SELECT DISTINCT CAST(timezone('Asia/Kolkata', to_timestamp(timestamp)) "
                       "AS DATE) FROM market_data").fetchall()
    return {r[0] for r in rows}


def run_preflight(report, *, manifest: dict, contract, calendar, stop: date, start: date,
                  root, git_dir):
    """Intraday preflight: integrity, calendar (minus declared out-of-scope sessions), universe,
    history, and the contract's field families against the quality table."""
    from signal_engine.backtest.data_readiness.preflight import _finish

    end_ts = int(pd.Timestamp(stop, tz=IST).timestamp()) + 86399
    try:
        with snapshot.open_version(manifest["dataset_version"], root=root, git_dir=git_dir,
                                   end_ts=end_ts) as con:
            have = {d for d in _session_dates(con) if start <= d <= stop}
            symbols = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM market_data")
                       .fetchall()}
    except snapshot.SnapshotError as exc:
        report.dataset["integrity_error"] = str(exc)
        return _finish(report, st.NOT_READY, ["dataset_integrity"])
    out_of_scope = {date.fromisoformat(d) for d in manifest.get("out_of_scope_sessions") or []}
    expected = set(calendar.expected_sessions(start, stop)) - out_of_scope
    report.calendar_check = {"range": [str(start), str(stop)],
                             "current_version": calendar.version,
                             "out_of_scope_sessions": sorted(str(d) for d in out_of_scope),
                             "compatible": have == expected,
                             "dataset_only": sorted(str(d) for d in have - expected),
                             "calendar_only": sorted(str(d) for d in expected - have)}
    if have != expected:
        return _finish(report, st.NOT_READY, ["CAL-INCOMPATIBLE"])
    universe = list(contract.universe_symbols)
    requested = sorted(s for s in universe if s in symbols)
    report.universe = {"name": contract.universe_name, "source": contract.universe_source,
                       "version": contract.universe_version,
                       "strategy_universe_count": len(universe),
                       "unavailable": sorted(s for s in universe if s not in symbols),
                       "requested": requested}
    if contract.history_sessions > len(expected):
        return _finish(report, st.NOT_READY, ["insufficient_history"])
    if not requested:
        return _finish(report, st.NOT_READY, ["no_requested_symbols"])
    families = required_families(contract)
    avail = availability(load_quality(manifest, root), families,
                         manifest.get("usable_range_removals") or [], start, stop)
    avail = avail[avail.symbol.isin(requested)]
    report.exclusions = [
        {"symbol": s, "class": "session", "sessions": int((~g.available).sum()),
         "reasons": g[~g.available].reason.value_counts().to_dict()}
        for s, g in avail.groupby("symbol") if (~g.available).any()]
    eligible = sorted(avail[avail.available].symbol.unique())
    report.universe["eligible"] = eligible
    report.universe["field_families"] = list(families)
    report.counts = {"strategy_universe": len(universe), "requested": len(requested),
                     "eligible": len(eligible), "sessions_in_range": len(expected),
                     "symbol_sessions": len(avail),
                     "symbol_sessions_available": int(avail.available.sum())}
    report.warnings = ["COV-UNAVAILABLE"] if report.universe["unavailable"] else []
    report.warnings += [w for w in manifest.get("warnings") or [] if w.startswith("alert")]
    if not eligible:
        return _finish(report, st.NOT_READY, ["no_eligible_symbols"])
    if report.warnings or report.exclusions:
        return _finish(report, st.READY_WITH_WARNINGS, [])
    return _finish(report, st.READY, [])


def read_bars(con, manifest: dict, contract, symbols, interval: str, end,
              root=snapshot.DEFAULT_ROOT, collector=None) -> dict[str, pd.DataFrame]:
    """Bars of the available sessions, one frame per symbol (IST index; Open..Volume)."""
    if manifest.get("frequency") != interval:
        raise ValueError(f"{manifest['dataset_version']} holds {manifest.get('frequency')} bars, "
                         f"not {interval}")
    if collector is not None:
        collector.interval = interval
    stop = None if not end else date.fromisoformat(str(end)[:10])
    avail = availability(load_quality(manifest, root), required_families(contract),
                         manifest.get("usable_range_removals") or [], end=stop)
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        a = avail[avail.symbol == s]
        ok = set(a[a.available].session.dt.date)
        no_volume = set(a[a.available & ~a.volume_valid].session.dt.date)
        q = ("SELECT timestamp, open, high, low, close, volume FROM market_data "
             "WHERE symbol = ? AND interval = ?")
        args: list = [s, interval]
        if stop is not None:
            q += " AND timestamp <= ?"
            args.append(int(pd.Timestamp(stop, tz=IST).timestamp()) + 86399)
        raw = con.execute(q + " ORDER BY timestamp", args).df()
        if raw.empty:
            continue
        idx = pd.to_datetime(raw.pop("timestamp"), unit="s", utc=True).dt.tz_convert(IST)
        raw.index = pd.DatetimeIndex(idx)
        raw.columns = ["Open", "High", "Low", "Close", "Volume"]
        day = pd.Series(raw.index.date, index=raw.index)
        keep = day.isin(ok).to_numpy()
        raw, day = raw[keep], day[keep]
        if raw.empty:
            continue
        raw["Volume"] = raw.Volume.astype(float)
        raw.loc[day.isin(no_volume).to_numpy(), "Volume"] = np.nan
        out[s] = raw
    if collector is not None:
        collector.symbols_loaded = len(out)
    return out
