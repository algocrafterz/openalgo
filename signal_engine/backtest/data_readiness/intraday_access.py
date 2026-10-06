"""Read path and preflight for published intraday versions (plan Part A6, criterion 20).

The dataset states quality per field family; the strategy's data contract says which families it
uses (`DataContract.field_families`; none declared = every family is required). This module joins
the two. A session is available to a run only when every required family is valid and the
session lies inside each required family's usable range. Volume of a served session whose volume
is invalid (and not required) is withheld as NaN, so it cannot be used by accident.

A published intraday contract may add two checks (`min_n_minutes`, `complete_bins`): a session
is also unavailable when a bin from `complete_bins.first` to `complete_bins.last` (inclusive, on
the bar grid) is missing, or when a bar in that window was built from fewer than `min_n_minutes`
source minutes. `apply_data_exclusions` is the single implementation, used by the preflight and
by the read path. Nothing is filled or repaired and bars are never trimmed: whole sessions drop.

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
from utils.logging import get_logger

logger = get_logger(__name__)


DATA_MISSING_BIN = "DATA-MISSING-BIN"
DATA_MIN_N_MINUTES = "DATA-MIN-N-MINUTES"
DATA_NO_N_MINUTES = "DATA-NO-N-MINUTES"


class NMinutesUnavailable(RuntimeError):
    """The contract declares min_n_minutes but the dataset version holds no n_minutes column."""


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


def _clock(text: str) -> int:
    return int(text[:2]) * 60 + int(text[3:])


def _enforcement(contract) -> tuple[int | None, int, int, int, int | None] | None:
    """(grid anchor, window start, window end, step, min_n_minutes), in minutes of the day, or
    None when the contract declares neither check. Without bins the anchor is None: completeness
    is not checked and the whole day is the n_minutes window."""
    bins = getattr(contract, "complete_bins", None)
    min_n = getattr(contract, "min_n_minutes", None)
    if not bins and min_n is None:
        return None
    step = int(str(contract.frequency).removesuffix("m"))
    if not bins:
        return None, 0, 1439, 0, min_n
    first, last = _clock(bins[0]), _clock(bins[1])
    return first, first, last, step, min_n


def _window_stats(con, symbols, interval: str, lo: int, hi: int, step: int, anchor: int,
                  has_n: bool) -> pd.DataFrame:
    """Per symbol-session: bins present on the grid inside [lo, hi] and the least n_minutes."""
    local = "timezone('Asia/Kolkata', to_timestamp(timestamp))"
    n_expr = "min(n_minutes)" if has_n else "NULL::INTEGER"
    grid = f"AND (minute - {anchor}) % {step} = 0" if step else ""
    sql = (f"SELECT symbol, session, count(DISTINCT minute) FILTER (WHERE true {grid}) AS n_bins, "
           f"{n_expr} AS min_n FROM (SELECT symbol, CAST({local} AS DATE) AS session, "
           f"hour({local}) * 60 + minute({local}) AS minute"
           f"{', n_minutes' if has_n else ''} FROM market_data "
           "WHERE interval = ? AND list_contains(?, symbol)) "
           f"WHERE minute BETWEEN {lo} AND {hi} GROUP BY symbol, session")
    out = con.execute(sql, [interval, list(symbols)]).df()
    out["session"] = pd.to_datetime(out.session)
    return out


def data_exclusions(con, contract, candidates: pd.DataFrame, symbols) -> pd.DataFrame:
    """The candidate symbol-sessions the data contract excludes, with a reason code each:
    DATA-MISSING-BIN (a bin of the window is absent) takes precedence over DATA-MIN-N-MINUTES
    (a bar of the window was built from fewer than min_n_minutes source minutes)."""
    cols = ["symbol", "session", "reason"]
    rule = _enforcement(contract)
    if rule is None or candidates.empty:
        return pd.DataFrame(columns=cols)
    anchor, lo, hi, step, min_n = rule
    has_n = "n_minutes" in {r[0] for r in con.execute("DESCRIBE market_data").fetchall()}
    if min_n is not None and not has_n:
        raise NMinutesUnavailable("the dataset version has no n_minutes column but the contract "
                                  f"declares min_n_minutes {min_n}")
    stats = _window_stats(con, sorted(set(symbols)), str(contract.frequency), lo, hi, step,
                          anchor if anchor is not None else 0, has_n)
    m = candidates[["symbol", "session"]].merge(stats, how="left", on=["symbol", "session"])
    expected = (hi - lo) // step + 1 if anchor is not None else 0
    missing = (m.n_bins.fillna(0) != expected) if anchor is not None else pd.Series(False, m.index)
    low = (m.min_n < min_n) if min_n is not None else pd.Series(False, m.index)
    m["reason"] = np.where(missing, DATA_MISSING_BIN, np.where(low, DATA_MIN_N_MINUTES, None))
    return m[m.reason.notna()][cols].reset_index(drop=True)


def apply_data_exclusions(con, contract, avail: pd.DataFrame, symbols) -> pd.DataFrame:
    """`avail` with the sessions the data contract excludes marked unavailable and named."""
    bad = data_exclusions(con, contract, avail[avail.available], symbols)
    if bad.empty:
        return avail
    key = pd.MultiIndex.from_frame(bad[["symbol", "session"]])
    hit = pd.MultiIndex.from_frame(avail[["symbol", "session"]]).isin(key)
    reasons = dict(zip(zip(bad.symbol, bad.session, strict=True), bad.reason, strict=True))
    out = avail.copy()
    out.loc[hit, "available"] = False
    out.loc[hit, "reason"] = [reasons[k] for k in zip(out.symbol[hit], out.session[hit],
                                                      strict=True)]
    return out


def _session_dates(con) -> set[date]:
    rows = con.execute("SELECT DISTINCT CAST(timezone('Asia/Kolkata', to_timestamp(timestamp)) "
                       "AS DATE) FROM market_data").fetchall()
    return {r[0] for r in rows}


def run_preflight(report, *, manifest: dict, contract, calendar, stop: date, start: date,
                  root, git_dir, require_ready: bool = False):
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
            return _decide(report, con, manifest=manifest, contract=contract,
                           calendar=calendar, stop=stop, start=start, root=root, have=have,
                           symbols=symbols, require_ready=require_ready)
    except snapshot.SnapshotError as exc:
        report.dataset["integrity_error"] = str(exc)
        return _finish(report, st.NOT_READY, ["dataset_integrity"])


def _decide(report, con, *, manifest, contract, calendar, stop, start, root, have, symbols,
            require_ready):
    from signal_engine.backtest.data_readiness.preflight import _finish

    # The version covers a session when its quality table has it (valid or recorded as
    # excluded); a day on which every symbol was excluded has no bars but is still covered.
    quality = load_quality(manifest, root)
    covered = {d.date() for d in quality[~quality.cls.str.startswith("out_of_scope")].session
               if start <= d.date() <= stop}
    out_of_scope = {date.fromisoformat(d) for d in manifest.get("out_of_scope_sessions") or []}
    expected = set(calendar.expected_sessions(start, stop)) - out_of_scope
    compatible = covered == expected and have <= covered
    report.calendar_check = {"range": [str(start), str(stop)],
                             "current_version": calendar.version,
                             "out_of_scope_sessions": sorted(str(d) for d in out_of_scope),
                             "compatible": compatible,
                             "dataset_only": sorted(str(d) for d in covered - expected),
                             "calendar_only": sorted(str(d) for d in expected - covered),
                             "bars_outside_covered": sorted(str(d) for d in have - covered),
                             "sessions_without_valid_bars": sorted(str(d) for d in covered - have)}
    if not compatible:
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
    avail = availability(quality, families, manifest.get("usable_range_removals") or [],
                         start, stop)
    avail = avail[avail.symbol.isin(requested)]
    try:
        avail = apply_data_exclusions(con, contract, avail, requested)
    except NMinutesUnavailable as exc:
        logger.error(f"preflight refused: {exc}")
        return _finish(report, st.NOT_READY, [DATA_NO_N_MINUTES])
    report.exclusions = [
        {"symbol": s, "class": "session", "sessions": int((~g.available).sum()),
         "reasons": g[~g.available].reason.value_counts().to_dict(),
         "years": g[~g.available].session.dt.year.astype(str).value_counts().sort_index()
         .to_dict()}
        for s, g in avail.groupby("symbol") if (~g.available).any()]
    report.universe["session_exclusions_by_year"] = _by_year(avail)
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
        if require_ready:
            return _finish(report, st.NOT_READY, ["require_ready"])
        return _finish(report, st.READY_WITH_WARNINGS, [])
    return _finish(report, st.READY, [])


def _by_year(avail: pd.DataFrame) -> dict[str, dict[str, int]]:
    """Excluded symbol-sessions counted by year and reason code."""
    bad = avail[~avail.available]
    years = bad.session.dt.year.astype(str)
    return {y: g.reason.value_counts().sort_index().to_dict()
            for y, g in bad.groupby(years) if len(g)}


def read_bars(con, manifest: dict, contract, symbols, interval: str, end,
              root=snapshot.DEFAULT_ROOT, collector=None) -> dict[str, pd.DataFrame]:
    """Bars of the available sessions, one frame per symbol (IST index; Open..Volume, plus
    n_minutes when the version holds it). A session the data contract excludes is dropped whole."""
    if manifest.get("frequency") != interval:
        raise ValueError(f"{manifest['dataset_version']} holds {manifest.get('frequency')} bars, "
                         f"not {interval}")
    if collector is not None:
        collector.interval = interval
    stop = None if not end else date.fromisoformat(str(end)[:10])
    avail = availability(load_quality(manifest, root), required_families(contract),
                         manifest.get("usable_range_removals") or [], end=stop)
    avail = apply_data_exclusions(con, contract, avail, symbols)
    has_n = "n_minutes" in {r[0] for r in con.execute("DESCRIBE market_data").fetchall()}
    names = ["Open", "High", "Low", "Close", "Volume"] + (["n_minutes"] if has_n else [])
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        a = avail[avail.symbol == s]
        ok = set(a[a.available].session.dt.date)
        no_volume = set(a[a.available & ~a.volume_valid].session.dt.date)
        q = (f"SELECT timestamp, open, high, low, close, volume{', n_minutes' if has_n else ''} "
             "FROM market_data "
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
        raw.columns = names
        day = pd.Series(raw.index.date, index=raw.index)
        keep = day.isin(ok).to_numpy()
        raw, day = raw[keep], day[keep]
        if raw.empty:
            continue
        raw["Volume"] = raw.Volume.astype(float)
        if has_n:
            raw["n_minutes"] = raw.n_minutes.astype("int64")
        raw.loc[day.isin(no_volume).to_numpy(), "Volume"] = np.nan
        out[s] = raw
    if collector is not None:
        collector.symbols_loaded = len(out)
    return out
