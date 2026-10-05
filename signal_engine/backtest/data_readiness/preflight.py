"""Pre-run data readiness (plan Phase 4): before a backtest starts, decide whether it may run.

Order of checks (the first NOT_READY ends the preflight; nothing is executed or counted):

1. interval: the version holds the contract frequency (only daily versions exist today);
2. dataset integrity: every hash of the resolved published version verifies;
3. calendar compatibility for the requested range only: the version's sessions in
   [coverage start, end] must equal the current official calendar's sessions there. A calendar
   update that only adds later sessions changes nothing; a correction inside the range needs a
   new published version;
4. universe: strategy universe -> dataset availability (unavailable symbols are a recorded
   limitation) -> requested symbols;
5. history: the contract's largest history requirement must fit the sessions in the range;
6. the data-readiness rule pack on the requested scope (D15 thresholds, D16 session rule);
7. eligibility: requested symbols under the loader minimum are eligibility exclusions (they do
   not count toward the data-defect threshold);
8. state: NOT_READY, READY_WITH_WARNINGS (warnings or exclusions) or READY; `require_ready`
   turns warnings into NOT_READY.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from signal_engine.backtest.data_readiness import audit as _audit
from signal_engine.backtest.data_readiness import policy as pol
from signal_engine.backtest.data_readiness import snapshot
from signal_engine.backtest.data_readiness import state as st
from signal_engine.backtest.data_readiness.calendar import CalendarError, NseCalendar
from signal_engine.backtest.data_readiness.contract import DataContract
from signal_engine.research.intake.fingerprints import canonical_json
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

SCHEMA = 1


@dataclass
class PreflightReport:
    state: str
    reasons: list[str]
    warnings: list[str]
    dataset: dict[str, Any]
    contract: dict[str, Any]
    universe: dict[str, Any] = field(default_factory=dict)
    exclusions: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    calendar_check: dict[str, Any] = field(default_factory=dict)
    policy_version: str = ""
    end: str | None = None
    stage: str = "preflight"
    created_ist: str = ""

    def body(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("created_ist")
        return {"schema": SCHEMA, **d}

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.body()).encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "created_ist": self.created_ist, "sha256": self.sha256}


class DataNotReadyError(RuntimeError):
    """The run may not start: no run id, no run folder, no trial."""

    def __init__(self, report: PreflightReport, path: Path | None = None):
        self.report, self.path = report, path
        super().__init__(f"data NOT_READY at {report.stage}: {', '.join(report.reasons)}"
                         + (f" (record: {path})" if path else ""))


def _dataset_block(m: dict) -> dict[str, Any]:
    return {"version_id": m.get("dataset_version"), "root_hash": m.get("root_hash"),
            "status": m.get("status"), "source": m.get("source"), "as_of": m.get("as_of"),
            "frequency": m.get("frequency"), "price_basis": m.get("price_basis"),
            "calendar_version": m.get("calendar_version"),
            "corporate_action_version": m.get("corporate_action_version"),
            "overrides_source": m.get("overrides_source"),
            "series_rule_version": m.get("series_rule_version"),
            "coverage": m.get("coverage"), "known_limitations": m.get("known_limitations")}


def _session_dates(con) -> set[date]:
    # Daily bars are stamped 00:00 UTC of the session date (05:30 IST).
    return {pd.Timestamp(r[0], unit="s").date() for r in con.execute(
        "SELECT DISTINCT timestamp FROM market_data").fetchall()}


def _calendar_check(con, calendar: NseCalendar, start: date, end: date) -> dict[str, Any]:
    have = {d for d in _session_dates(con) if start <= d <= end}
    base = {"range": [str(start), str(end)], "current_version": calendar.version}
    try:
        expected = set(calendar.expected_sessions(start, end))
    except CalendarError as exc:
        return {**base, "compatible": False, "error": str(exc)}
    return {**base, "compatible": have == expected,
            "dataset_only": sorted(d.isoformat() for d in have - expected),
            "calendar_only": sorted(d.isoformat() for d in expected - have)}


def _sessions_per_symbol(con) -> dict[str, int]:
    return dict(con.execute("SELECT symbol, COUNT(*) FROM market_data GROUP BY symbol").fetchall())


def _finish(report: PreflightReport, state: str, reasons: list[str]) -> PreflightReport:
    report.state, report.reasons = state, reasons
    report.created_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    return report


def _exclusions(proj: dict, manifest: dict, requested: list[str], counts: dict[str, int],
                loader_min: int) -> tuple[list[dict], set[str], list[str]]:
    published = {e["symbol"]: e for e in manifest.get("excluded_symbols") or []}
    defect: dict[str, list[str]] = {}
    for rule, syms in proj["defect_excluded_by_rule"].items():
        for s in syms:
            defect.setdefault(s, []).append(rule)
    out = []
    for s in sorted(defect):
        known = published.get(s, {})
        out.append({"symbol": s, "class": st.DEFECT, "rules": sorted(defect[s]),
                    "reason": known.get("reason", "data-readiness rule finding"),
                    "ranges": known.get("ranges")})
    short = sorted(s for s in requested if s not in defect and counts.get(s, 0) < loader_min)
    for s in short:
        out.append({"symbol": s, "class": st.ELIGIBILITY, "rules": ["loader_min_sessions"],
                    "reason": f"{counts.get(s, 0)} sessions up to the end, fewer than the loader "
                              f"minimum {loader_min}", "ranges": None})
    return out, set(defect), short


def run_preflight(*, manifest: dict, contract: DataContract, calendar: NseCalendar,
                  policy: pol.Policy, end: str | None = None, require_ready: bool = False,
                  root: str | Path = snapshot.DEFAULT_ROOT,
                  git_dir: str | Path = snapshot.DEFAULT_GIT_DIR) -> PreflightReport:
    """The readiness decision for one run over one published version. A data problem never
    raises: it is a NOT_READY reason in the report."""
    vid = manifest["dataset_version"]
    as_of = date.fromisoformat(manifest["as_of"])
    stop = min(date.fromisoformat(end), as_of) if end else as_of
    start = date.fromisoformat(manifest["coverage"]["start"])
    report = PreflightReport(state=st.NOT_READY, reasons=[], warnings=[],
                             dataset=_dataset_block(manifest), contract=contract.to_dict(),
                             policy_version=policy.version, end=stop.isoformat())
    if manifest.get("frequency") != contract.frequency:
        return _finish(report, st.NOT_READY, ["no_published_interval"])
    end_ts = int(pd.Timestamp(stop).timestamp())
    try:
        with snapshot.open_version(vid, root=root, git_dir=git_dir, end_ts=end_ts) as con:
            report.calendar_check = _calendar_check(con, calendar, start, stop)
            available = set(_sessions_per_symbol(con))
    except snapshot.SnapshotError as exc:
        report.dataset["integrity_error"] = str(exc)
        return _finish(report, st.NOT_READY, ["dataset_integrity"])
    if not report.calendar_check["compatible"]:
        return _finish(report, st.NOT_READY, ["CAL-INCOMPATIBLE"])

    universe = list(contract.universe_symbols)
    requested = sorted(s for s in universe if s in available)
    report.universe = {"name": contract.universe_name, "source": contract.universe_source,
                       "version": contract.universe_version,
                       "strategy_universe_count": len(universe),
                       "unavailable": sorted(s for s in universe if s not in available),
                       "requested": requested}
    sessions = calendar.expected_sessions(start, stop)
    if contract.history_sessions > len(sessions):
        report.counts = {"sessions_in_range": len(sessions)}
        return _finish(report, st.NOT_READY, ["insufficient_history"])
    if not requested:
        return _finish(report, st.NOT_READY, ["no_requested_symbols"])

    with snapshot.open_version(vid, root=root, git_dir=git_dir, symbols=requested,
                               end_ts=end_ts) as con, tempfile.TemporaryDirectory() as tmp:
        cal = _audit.AuditCalendar(tuple(sessions), f"nse_calendar: {calendar.source}",
                                   calendar.version)
        block, _ = _audit._audit_interval(con, "D", policy, Path(tmp), cal)
        counts = _sessions_per_symbol(con)
    proj = block["projection"]
    exclusions, defect, short = _exclusions(proj, manifest, requested, counts,
                                            contract.loader_min_sessions)
    excluded = {e["symbol"] for e in exclusions}
    eligible = [s for s in requested if s not in excluded]
    report.exclusions = exclusions
    report.universe["eligible"] = eligible
    report.counts = {"strategy_universe": len(universe), "available": len(requested),
                     "unavailable": len(report.universe["unavailable"]),
                     "requested": len(requested), "defect_excluded": len(defect),
                     "eligibility_excluded": len(short), "eligible": len(eligible),
                     "sessions_in_range": len(sessions)}
    report.warnings = list(proj["warnings"]) + (
        ["COV-UNAVAILABLE"] if report.universe["unavailable"] else [])
    if proj["state"] == st.NOT_READY:
        return _finish(report, st.NOT_READY, list(proj["reasons"]))
    if not eligible:
        return _finish(report, st.NOT_READY, ["no_eligible_symbols"])
    if report.warnings or exclusions:
        if require_ready:
            return _finish(report, st.NOT_READY, ["require_ready"])
        return _finish(report, st.READY_WITH_WARNINGS, [])
    return _finish(report, st.READY, [])


def write_record(report: PreflightReport, idea_folder: str | Path) -> Path:
    """Write the report under `<idea>/preflight/`, never overwriting an earlier record."""
    folder = Path(idea_folder) / "preflight"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y-%m-%d-%H%M%S")
    path = folder / f"{stamp}-{report.stage}.json"
    n = 1
    while path.exists():
        n += 1
        path = folder / f"{stamp}-{report.stage}-{n}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    logger.info(f"preflight {report.state} recorded at {path}")
    return path


@dataclass(frozen=True)
class Resolution:
    """The one published version a run uses, with its preflight result. Created only by
    `prepare_data` (or a test fixture); a run path without one is refused."""

    manifest: dict
    report: PreflightReport
    contract: DataContract
    calendar: NseCalendar
    root: Path = snapshot.DEFAULT_ROOT
    git_dir: Path = snapshot.DEFAULT_GIT_DIR

    @property
    def version_id(self) -> str:
        return self.manifest["dataset_version"]

    @property
    def eligible(self) -> list[str]:
        return list(self.report.universe.get("eligible") or [])

    def run_fingerprint(self, symbols, end: str | None) -> str:
        return snapshot.run_fingerprint(self.manifest, symbols, end, root=self.root)


def prepare_data(idea_folder: str | Path, frozen, *, dataset_version: str | None = None,
                 end: str | None = None, require_ready: bool = False,
                 root: str | Path = snapshot.DEFAULT_ROOT,
                 git_dir: str | Path = snapshot.DEFAULT_GIT_DIR,
                 calendar: NseCalendar | None = None,
                 policy: pol.Policy | None = None) -> Resolution:
    """Resolve the published version, derive the contract, run the preflight. A NOT_READY result
    is recorded under `<idea>/preflight/` and raised as DataNotReadyError."""
    from signal_engine.backtest.data_readiness.calendar import load_calendar
    from signal_engine.backtest.data_readiness.contract import ContractError, resolve_contract

    calendar = calendar or load_calendar()
    policy = policy or pol.load_policy()
    try:
        contract = resolve_contract(frozen)
        manifest = snapshot.resolve_version(dataset_version, root=root, git_dir=git_dir)
    except (snapshot.SnapshotError, ContractError) as exc:
        report = PreflightReport(state=st.NOT_READY, reasons=["dataset_resolution"], warnings=[],
                                 dataset={"requested_version": dataset_version, "error": str(exc)},
                                 contract={}, policy_version=policy.version, end=end)
        report.created_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        raise DataNotReadyError(report, write_record(report, idea_folder)) from exc
    report = run_preflight(manifest=manifest, contract=contract, calendar=calendar, policy=policy,
                           end=end, require_ready=require_ready, root=root, git_dir=git_dir)
    if report.state == st.NOT_READY:
        raise DataNotReadyError(report, write_record(report, idea_folder))
    return Resolution(manifest, report, contract, calendar, Path(root), Path(git_dir))


class DataUnavailable(RuntimeError):
    """The resolved dataset version does not hold the bars a run asked for."""


class PublishedSource:
    """The only way a recorded run reads bars: one verified published version connection,
    limited to the preflight's eligible symbols. Daily bars only, until a 1m version exists."""

    def __init__(self, con, resolution: Resolution):
        self._con, self._resolution = con, resolution

    def daily(self, symbols, end, min_sessions, collector=None):
        from signal_engine.backtest import data

        return data.from_published_daily(self._con, symbols=symbols, end=end,
                                         min_sessions=min_sessions, collector=collector)

    def intraday(self, symbols, interval, end, collector=None):
        raise DataUnavailable(
            f"no published intraday ({interval}) dataset version; "
            f"{self._resolution.version_id} holds {self._resolution.manifest.get('frequency')} bars")


def _open_source(self: Resolution):
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        end = self.report.end
        end_ts = None if not end else int(pd.Timestamp(end, tz=IST).timestamp()) + 86399
        with snapshot.open_version(self.version_id, root=self.root, git_dir=self.git_dir,
                                   symbols=self.eligible, end_ts=end_ts,
                                   expected_root=self.manifest["root_hash"]) as con:
            yield PublishedSource(con, self)

    return _cm()


Resolution.open = _open_source
