"""Session coverage of a run: which official sessions each symbol is missing, and what the run does.

Two different things go wrong with stored history, and this module names them apart:

  MISSING FULL SESSION   An official NSE trading day on which a symbol has no bar at all. The loaded
                         frame silently bridges it: the next bar follows a bar from an earlier day.
  MISSING BARS           Bars absent inside a session the symbol does have (a partial session).
                         Counted by the data-quality audit (`dq_run`), reused here, never recounted.

Three safeguards, all warnings (a run is never rejected only because sessions are missing):

  1. `build_block` writes the optional run-record block `session_coverage`: per-symbol rows (the
     worst few in full, aggregates for all), the session basis, the effective period and a
     `coverage_class` (COMPLETE, MINOR_GAPS, MATERIAL_GAPS, SEVERE_GAPS) and, for the last two,
     a plain-language `disclosure`.
  2. `attach_after_gap` adds the boolean bar column `session_after_gap` (True on every bar of the
     first loaded session that follows a missing official session of that symbol). The engines
     skip NEW entries on such sessions (ledger reason SESSION_AFTER_GAP), because previous-session
     context (prior day high, low, close, gap, value area) then refers to a stale day.
  3. `apply_common` keeps only the sessions present in every symbol of the basket (session basis
     `common`); basis `own` leaves every frame as loaded.

Thresholds and the basket-size rule come from the data policy (`research/data_policy.yaml`,
section `session_coverage`); nothing here has a default for them. Pure: no I/O.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

SCHEMA = 1
AFTER_GAP_COLUMN = "session_after_gap"

#: Four classes by the share of missing FULL sessions over the expected sessions after exclusions.
#: COMPLETE: no missing full session and no partial session. MINOR_GAPS: share up to and including
#: the minor limit (the policy's `incomplete_session_share`, 1%). MATERIAL_GAPS: above it, up to and
#: including the material limit (`severe_session_share`, 5%). SEVERE_GAPS: above that. Reporting
#: classes only: no promotion or verdict gate reads them.
COMPLETE = "COMPLETE"
MINOR_GAPS = "MINOR_GAPS"
MATERIAL_GAPS = "MATERIAL_GAPS"
SEVERE_GAPS = "SEVERE_GAPS"
CLASSES = (COMPLETE, MINOR_GAPS, MATERIAL_GAPS, SEVERE_GAPS)

#: Shown verbatim to the trader for a MATERIAL or SEVERE run (no code, no threshold names).
DISCLOSURES = {
    MATERIAL_GAPS: (
        "Some trading sessions are missing from the stored history for part of this basket; "
        "results may be affected."
    ),
    SEVERE_GAPS: (
        "Many trading sessions are missing for part of this basket. Treat this result as "
        "indicative only, not as reliable historical validation."
    ),
}

#: A share compared against a limit is equal to it within this slack (inclusive boundaries).
_EPS = 1e-12

COMMON = "common"
OWN = "own"
BASES = (COMMON, OWN)

#: Dates listed per symbol and per list in the record (counts are always exact).
MAX_LISTED_DATES = 20

CONTINUITY_NOTE = (
    "Only previous-session context is guarded: the first loaded session after a missing official "
    "session takes no new entries (SESSION_AFTER_GAP). Rolling indicators that span sessions (EMAs, "
    "multi-day ATR, rolling highs) are NOT reset at a gap and keep averaging across the missing "
    "days. Positions are not affected: an intraday position never crosses a session, and a swing "
    "position held across a missing day is exited on the next bar that exists."
)

MISSING_FULL_DEF = (
    "official trading days (calendar minus out-of-scope sessions) inside the symbol's own first to "
    "last loaded session with no bar at all, after subtracting the sessions the loader excluded as "
    "probable splits"
)
EXPECTED_DEF = (
    "expected_sessions_after_exclusions: the official trading days inside the symbol's own first "
    "to last loaded session, minus the declared out-of-scope sessions (Muhurat or special) and "
    "minus the sessions the loader excluded as probable splits. missing_share is "
    "missing_full_sessions divided by this count"
)
PARTIAL_DEF = (
    "sessions the symbol has but with fewer than the data-quality completeness floor of the "
    "expected bars (dq_run.min_session_completeness); the bar shortfall is the expected bars "
    "minus the bars present, summed over those sessions"
)


class CoverageError(ValueError):
    """A session-coverage setting or input is invalid."""


def _iso(d: date) -> str:
    return d.isoformat()


def compress_sessions(dates: Iterable[date], calendar_days: Sequence[date]) -> list[str]:
    """Dates as ranges of consecutive official sessions: 'YYYY-MM-DD..YYYY-MM-DD' or one date.

    Consecutive means adjacent in `calendar_days`, so a Friday and the next Monday form one range.
    A date outside the calendar is its own range.
    """
    order = {d: i for i, d in enumerate(calendar_days)}
    out: list[str] = []
    run: list[date] = []

    def flush() -> None:
        if run:
            out.append(_iso(run[0]) if len(run) == 1 else f"{_iso(run[0])}..{_iso(run[-1])}")

    for d in sorted(set(dates)):
        if run and d in order and run[-1] in order and order[d] == order[run[-1]] + 1:
            run.append(d)
            continue
        flush()
        run = [d]
    flush()
    return out


def resolve_basis(requested: str | None, n_symbols: int, max_common_symbols: int) -> tuple[str, str]:
    """The session basis of the run and where it came from ('setting' or 'default').

    Without a setting: `common` for a basket of at most `max_common_symbols` symbols, `own` above
    it (one missing session in one of 200 symbols would otherwise delete that day for all). A
    single-symbol run is `own` whatever the setting says: there is nothing to intersect with.
    """
    if requested is not None and requested not in BASES:
        raise CoverageError(f"session_basis must be one of {', '.join(BASES)}, got {requested!r}")
    if n_symbols <= 1:
        return OWN, "single symbol"
    if requested is not None:
        return requested, "setting"
    return (COMMON if n_symbols <= max_common_symbols else OWN), "default"


def loaded_dates(frames: Mapping[str, pd.DataFrame | None]) -> dict[str, set[date]]:
    """Per symbol with bars, the set of session dates (IST wall dates of the bar index)."""
    out: dict[str, set[date]] = {}
    for sym, df in frames.items():
        if df is None or len(df) == 0:
            continue
        out[sym] = set(pd.unique(df.index.date).tolist())
    return out


def common_sessions(loaded: Mapping[str, set[date]]) -> set[date]:
    """Sessions present in every symbol (empty when there are no symbols)."""
    sets = list(loaded.values())
    if not sets:
        return set()
    keep = set(sets[0])
    for s in sets[1:]:
        keep &= s
    return keep


def apply_common(
    frames: Mapping[str, pd.DataFrame], keep: set[date]
) -> dict[str, pd.DataFrame]:
    """Each frame without the bars of sessions outside `keep`. A frame that loses nothing is kept as is."""
    out: dict[str, pd.DataFrame] = {}
    keep_arr = np.array(sorted(keep), dtype=object)
    for sym, df in frames.items():
        if df is None or len(df) == 0:
            out[sym] = df
            continue
        mask = np.isin(np.asarray(df.index.date, dtype=object), keep_arr)
        out[sym] = df if bool(mask.all()) else df[mask].copy()
    return out


def after_gap_flags(index: pd.DatetimeIndex, calendar_days: Sequence[date]) -> np.ndarray:
    """Per bar: True when the bar's session follows a missing official session of this frame.

    A session is flagged when the previous loaded session is not the immediately preceding
    official session. The first loaded session is never flagged (nothing before it to be stale).
    A date outside the calendar is never flagged: nothing can be said about it.
    """
    n = len(index)
    flags = np.zeros(n, dtype=bool)
    if n == 0:
        return flags
    days = np.asarray(index.date, dtype=object)
    uniq, first, inverse = np.unique(days, return_index=True, return_inverse=True)
    order = {d: i for i, d in enumerate(calendar_days)}
    flagged = np.zeros(len(uniq), dtype=bool)
    # np.unique sorts by date, so uniq[k - 1] is the previous loaded session
    for k in range(1, len(uniq)):
        a, b = order.get(uniq[k - 1]), order.get(uniq[k])
        if a is not None and b is not None and b - a > 1:
            flagged[k] = True
    return flagged[inverse]


def attach_after_gap(
    frames: Mapping[str, pd.DataFrame], calendar_days: Sequence[date]
) -> tuple[dict[str, pd.DataFrame], dict[str, list[date]]]:
    """Frames with the `session_after_gap` column set, and per symbol the flagged session dates.

    The frames are modified in place (the run owns them); the same dict contents are returned.
    """
    flagged: dict[str, list[date]] = {}
    out: dict[str, pd.DataFrame] = {}
    for sym, df in frames.items():
        if df is None or len(df) == 0:
            out[sym] = df
            continue
        flags = after_gap_flags(df.index, calendar_days)
        # the run owns these frames; a frame cut from another by a boolean mask is ours to extend
        with pd.option_context("mode.chained_assignment", None):
            df[AFTER_GAP_COLUMN] = flags
        out[sym] = df
        if flags.any():
            flagged[sym] = sorted(set(pd.unique(df.index.date[flags]).tolist()))
    return out, flagged


def carry_column(
    prepared: pd.DataFrame, source: pd.Series | None, name: str = AFTER_GAP_COLUMN
) -> pd.DataFrame:
    """`prepared` with the input frame's bool column `name`, which a strategy's `prepare` may drop.

    The column is put back by bar time. None (the input had no such column) or a prepared frame
    that kept it changes nothing. A prepared bar the input does not have gets False.
    """
    if source is None or name in prepared.columns:
        return prepared
    carried = source.reindex(prepared.index)
    return prepared.assign(**{name: carried.fillna(False).to_numpy(dtype=bool)})


def symbol_coverage(
    symbol: str,
    loaded: set[date],
    calendar_days: Sequence[date],
    split_excluded: Iterable[date] = (),
    partial: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One symbol's row: full-session coverage, and (separately) the partial-session numbers.

    `partial` is the data-quality audit's entry for the symbol (`sessions`, `incomplete_sessions`,
    `missing_bars`), or None when the symbol was not audited.
    """
    cal = set(calendar_days)
    split_all = sorted(set(split_excluded))
    split_in = [d for d in split_all if d in cal]
    seen = sorted((loaded & cal) | set(split_in))
    row: dict[str, Any] = {
        "symbol": symbol,
        "calendar_sessions": len(calendar_days),
        "loaded_sessions": len(loaded & cal),
        "split_excluded": [_iso(d) for d in split_all],
    }
    if not seen:
        row.update(
            first_session=None,
            last_session=None,
            span_sessions=0,
            outside_span_sessions=len(calendar_days),
            expected_sessions_after_exclusions=0,
            missing_share=0.0,
            missing_full_sessions={"count": 0, "ranges": [], "share": 0.0},
            coverage_share=None,
        )
    else:
        lo, hi = seen[0], seen[-1]
        span = [d for d in calendar_days if lo <= d <= hi]
        split_set = set(split_in)
        missing = [d for d in span if d not in loaded and d not in split_set]
        expected = len(span) - len(split_in)
        row.update(
            first_session=_iso(lo),
            last_session=_iso(hi),
            span_sessions=len(span),
            outside_span_sessions=len(calendar_days) - len(span),
            expected_sessions_after_exclusions=expected,
            missing_share=round(len(missing) / expected, 6) if expected > 0 else 0.0,
            missing_full_sessions={
                "count": len(missing),
                "ranges": compress_sessions(missing, calendar_days),
                "share": round(len(missing) / expected, 6) if expected > 0 else 0.0,
            },
            coverage_share=round(len(loaded & cal) / expected, 6) if expected > 0 else None,
        )
    if partial is None:
        row["partial_sessions"] = {"audited": False, "count": None, "missing_bars": None}
    else:
        row["partial_sessions"] = {
            "audited": True,
            "count": int(partial.get("incomplete_sessions", 0)),
            "missing_bars": int(partial.get("missing_bars", 0)),
        }
    return row


def symbol_class(row: Mapping[str, Any], minor_share: float, material_share: float) -> str:
    """The class of one symbol row by its missing share over the expected sessions after exclusions.

    COMPLETE (nothing missing, no partial session), MINOR_GAPS (share up to and including
    `minor_share`; a partial session alone also lands here), MATERIAL_GAPS (above `minor_share`, up
    to and including `material_share`), SEVERE_GAPS (above `material_share`).
    """
    count = row["missing_full_sessions"]["count"]
    partial = row["partial_sessions"]["count"] or 0
    expected = row["expected_sessions_after_exclusions"]
    if count == 0 and partial == 0:
        return COMPLETE
    share = count / expected if expected > 0 else 0.0
    if share > material_share + _EPS:
        return SEVERE_GAPS
    if share > minor_share + _EPS:
        return MATERIAL_GAPS
    return MINOR_GAPS


def _worst_first(row: Mapping[str, Any]) -> tuple:
    return (
        -CLASSES.index(row["class"]),
        -row["missing_full_sessions"]["share"],
        -(row["partial_sessions"]["count"] or 0),
        row["symbol"],
    )


def build_block(
    *,
    loaded: Mapping[str, set[date]],
    calendar: Any,
    out_of_scope: Sequence[str],
    split_excluded: Mapping[str, Iterable[date]],
    partial: Mapping[str, Mapping[str, Any]] | None,
    policy: Any,
    basis: str,
    basis_source: str,
    kept_sessions: set[date],
    after_gap: Mapping[str, Sequence[date]],
    requested_basis: str | None = None,
) -> dict[str, Any]:
    """The `session_coverage` run-record block (JSON-serialisable, stable key order).

    `loaded` is per symbol the sessions present BEFORE the basis was applied. `kept_sessions` are
    the sessions that reach the engine (for `own`, the union over the symbols; for `common`, the
    intersection). `policy` supplies the thresholds (`incomplete_session_share`,
    `severe_session_share`, `common_basis_max_symbols`, `listed_symbols`, `version`).
    """
    days = list(calendar.trading_days)
    rows = []
    for sym in sorted(loaded):
        row = symbol_coverage(
            sym, loaded[sym], days, split_excluded.get(sym, ()), (partial or {}).get(sym)
        )
        row["class"] = symbol_class(
            row, policy.incomplete_session_share, policy.severe_session_share
        )
        flagged = list(after_gap.get(sym, ()))
        row["after_gap_sessions"] = {
            "count": len(flagged),
            "dates": [_iso(d) for d in flagged[:MAX_LISTED_DATES]],
        }
        rows.append(row)

    classes = [r["class"] for r in rows]
    # the run class is the worst class among its symbols; the per-class counts always accompany it
    run_class = max(classes, key=CLASSES.index) if classes else COMPLETE
    ordered = sorted(rows, key=_worst_first)
    listed = ordered[: policy.listed_symbols]
    audited = [r for r in rows if r["partial_sessions"]["audited"]]
    union = sorted(set().union(*loaded.values())) if loaded else []
    inter = common_sessions(loaded)

    def excluded(keep: set[date]) -> dict[str, Any]:
        dropped = [d for d in union if d not in keep]
        return {"count": len(dropped), "ranges": compress_sessions(dropped, days)}

    kept_sorted = sorted(kept_sessions)
    expected_total = sum(r["expected_sessions_after_exclusions"] for r in rows)
    missing_total = sum(r["missing_full_sessions"]["count"] for r in rows)
    shares = [r["coverage_share"] for r in rows if r["coverage_share"] is not None]
    block: dict[str, Any] = {
        "schema": SCHEMA,
        "coverage_class": run_class,
        "warning_only": "A run is never rejected only because sessions are missing.",
        "calendar": {
            "source": calendar.source,
            "version": calendar.version,
            "sessions": len(days),
            "out_of_scope_sessions": list(out_of_scope),
        },
        "thresholds": {
            # inclusive upper limits: MINOR up to incomplete_session_share, MATERIAL up to
            # severe_session_share, SEVERE above it
            "incomplete_session_share": policy.incomplete_session_share,
            "severe_session_share": policy.severe_session_share,
            "common_basis_max_symbols": policy.common_basis_max_symbols,
            "policy_version": policy.version,
        },
        "session_basis": {
            "used": basis,
            "source": basis_source,
            "requested": requested_basis,
            "symbols": len(loaded),
            "rule": f"common for at most {policy.common_basis_max_symbols} symbols, own above",
        },
        "effective": {
            "period": {
                "start": _iso(kept_sorted[0]) if kept_sorted else None,
                "end": _iso(kept_sorted[-1]) if kept_sorted else None,
            },
            "sessions_kept": len(kept_sorted),
            "sessions_sha256": hashlib.sha256(
                ",".join(_iso(d) for d in kept_sorted).encode("ascii")
            ).hexdigest(),
            "sessions_excluded_by_basis": excluded(kept_sessions),
            "common_would_drop": excluded(inter) if basis == OWN else None,
        },
        "universe": {
            "symbols": len(rows),
            "symbols_complete": classes.count(COMPLETE),
            "symbols_minor_gaps": classes.count(MINOR_GAPS),
            "symbols_material_gaps": classes.count(MATERIAL_GAPS),
            "symbols_severe_gaps": classes.count(SEVERE_GAPS),
            "expected_sessions_after_exclusions_total": expected_total,
            "missing_full_sessions_total": missing_total,
            "missing_share_total": (
                round(missing_total / expected_total, 6) if expected_total else 0.0
            ),
            "mean_coverage_share": round(float(np.mean(shares)), 6) if shares else None,
            "min_coverage_share": round(float(min(shares)), 6) if shares else None,
            "after_gap_sessions_total": sum(r["after_gap_sessions"]["count"] for r in rows),
            "partial_sessions_total": (
                sum(r["partial_sessions"]["count"] for r in audited) if audited else None
            ),
            "missing_bars_total": (
                sum(r["partial_sessions"]["missing_bars"] for r in audited) if audited else None
            ),
            "partial_audited_symbols": len(audited),
        },
        "definitions": {
            "missing_full_sessions": MISSING_FULL_DEF,
            "expected_sessions_after_exclusions": EXPECTED_DEF,
            "partial_sessions": PARTIAL_DEF,
        },
        "symbols_listed": len(listed),
        "symbols_not_listed": len(rows) - len(listed),
        "symbols": listed,
        "continuity_note": CONTINUITY_NOTE,
        "entries_skipped_session_after_gap": None,
    }
    if run_class in DISCLOSURES:
        block["disclosure"] = DISCLOSURES[run_class]
    warnings: list[str] = []
    for cls, floor in (
        (MATERIAL_GAPS, policy.incomplete_session_share),
        (SEVERE_GAPS, policy.severe_session_share),
    ):
        if classes.count(cls):
            warnings.append(
                f"{classes.count(cls)} of {len(rows)} symbols miss more than {floor:.1%} of "
                "their expected sessions"
            )
    if basis == OWN and len(loaded) > 1 and block["effective"]["common_would_drop"]["count"]:
        warnings.append(
            f"basis own: the symbols trade different session sets; common would drop "
            f"{block['effective']['common_would_drop']['count']} sessions"
        )
    dropped_share = block["effective"]["sessions_excluded_by_basis"]["count"] / max(len(union), 1)
    if basis == COMMON and dropped_share > policy.severe_session_share + 1e-12:
        warnings.append(
            f"basis common removed {dropped_share:.1%} of the sessions any symbol has "
            f"({block['effective']['sessions_excluded_by_basis']['count']} of {len(union)}); "
            "symbols with different listing or data ranges shrink the shared period"
        )
    block["warnings"] = warnings
    return block
