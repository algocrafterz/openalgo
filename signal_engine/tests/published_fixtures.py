"""Small published dataset versions for the Phase 4 tests (preflight, loader, record_run).

`publish_fixture` builds NSE raw staging rows, the adjusted build and a published immutable
version under `tmp_path`, with a synthetic official calendar covering exactly the given days.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path

import pandas as pd

from signal_engine.backtest.data_readiness import nse_dataset, nse_eod, snapshot
from signal_engine.backtest.data_readiness import policy as pol
from signal_engine.backtest.data_readiness.calendar import NseCalendar
from signal_engine.tests.dq_fixtures import weekdays


def calendar_for(days: list[date], version: str = "cal-test") -> NseCalendar:
    """An official calendar whose sessions are exactly `days` (other weekdays are holidays)."""
    lo, hi = days[0], days[-1]
    every = [date.fromisoformat(d) for d in weekdays(f"{lo.year}-01-01", f"{hi.year}-12-31")]
    keep = set(days)
    hol = frozenset(d for d in every if lo <= d <= hi and d not in keep)
    return NseCalendar(holidays=hol, specials={}, mocks=frozenset(), unconfirmed=frozenset(),
                       coverage=(lo.year, hi.year), regular_session=("09:15", "15:30"),
                       version=version, source="synthetic")


def lenient_policy(max_symbols: int = 5, share: float = 1.0) -> pol.Policy:
    return dataclasses.replace(pol.load_policy(), max_defect_excluded_symbols=max_symbols,
                               max_defect_excluded_symbol_share=share)


def price(symbol: str, i: int) -> float:
    return 100.0 + (sum(map(ord, symbol)) % 50) + i * 0.1


def publish_fixture(tmp_path: Path, symbols, days, *, drop=(), extra_rows=(), anns=(),
                    version_id="nse-daily-test-001", accepted=(), policy=None, rows=None):
    """Publish a version; returns (root, git_dir, version_id, calendar)."""
    raw = nse_eod.open_staging(tmp_path / "nse.duckdb")
    out = list(extra_rows)
    drop = set(drop)
    for s in symbols:
        for i, d in enumerate(days):
            if (s, d) in drop:
                continue
            px = rows(s, i, d) if rows else price(s, i)
            out.append((s, "INE0", "EQ", d, px, px * 1.01, px * 0.99, px, px, px,
                        1000 + i, 1.0, 5, "f.zip", "sha"))
    df = pd.DataFrame(out, columns=[*nse_eod.COLUMNS, "source_file", "source_sha256"])
    raw.register("_r", df)
    raw.execute("INSERT INTO nse_daily_raw SELECT * FROM _r")
    raw.close()
    staging = tmp_path / "adjusted.duckdb"
    nse_dataset.build(tmp_path / "nse.duckdb", staging, symbols=tuple(symbols),
                      announcements=list(anns), overrides=(), calendar_version="cal-test")
    cal = calendar_for(list(days))
    root, git_dir = tmp_path / "datasets", tmp_path / "data_versions"
    snapshot.publish(staging, version_id=version_id, root=root, git_dir=git_dir, calendar=cal,
                     accepted_exclusions=accepted, cause="test", policy=policy)
    return root, git_dir, version_id, cal


def fake_resolution(version_id: str = "nse-daily-fake-001"):
    """A READY resolution for record_run tests whose `execute` never reads data. The default
    autouse fixture in conftest.py installs it for every test that does not ask for the real
    preflight; it never touches a store."""
    from signal_engine.backtest.data_readiness import preflight as pf
    from signal_engine.backtest.data_readiness import state as st
    from signal_engine.backtest.data_readiness.contract import DataContract

    contract = DataContract("D", 0, {}, 0, False, "spec", "test", ("A.NS",), "u" * 64)
    manifest = {"dataset_version": version_id, "root_hash": "r" * 64, "status": "published",
                "source": "NSE CM bhavcopy (test)", "as_of": "2026-02-20", "frequency": "D",
                "price_basis": "test", "calendar_version": "cal-test",
                "corporate_action_version": "ca-test", "overrides_source": "none",
                "series_rule_version": "1", "fingerprint_algo": "snapshot-v1",
                "coverage": {"start": "2026-01-01", "end": "2026-02-20", "symbols": 1},
                "known_limitations": "test", "excluded_symbols": []}
    report = pf.PreflightReport(state=st.READY, reasons=[], warnings=[],
                                dataset={"version_id": version_id}, contract=contract.to_dict(),
                                universe={"eligible": ["A.NS"], "unavailable": []},
                                counts={"strategy_universe": 1, "available": 1, "unavailable": 0,
                                        "requested": 1, "defect_excluded": 0,
                                        "eligibility_excluded": 0, "eligible": 1},
                                created_ist="2026-10-05 00:00:00")

    class _Fake(pf.Resolution):
        def run_fingerprint(self, symbols, end):
            return "d" * 64

    return _Fake(manifest, report, contract, calendar_for([date(2026, 1, 1)]))


class FrameSource:
    """In-memory bars with the `PublishedSource` interface, for engine-wiring tests. It filters
    by symbols, `end` and (daily) `min_sessions` the way the published loader does."""

    def __init__(self, daily=None, intraday=None):
        self._daily, self._intraday = daily, intraday
        self.calls: list[tuple] = []

    @staticmethod
    def _cut(frames, symbols, end):
        out = {}
        for s, f in frames.items():
            if symbols is not None and s not in set(symbols):
                continue
            if end is not None:
                stop = pd.Timestamp(end[:10]).date()
                f = f[[ts.date() <= stop for ts in f.index]]
            out[s] = f.copy()
        return out

    def daily(self, symbols, end, min_sessions, collector=None):
        from signal_engine.backtest.data_readiness.preflight import DataUnavailable

        self.calls.append(("daily", tuple(symbols or ()), end))
        if self._daily is None:
            raise DataUnavailable("no daily frames in this test source")
        return {s: f for s, f in self._cut(self._daily, symbols, end).items()
                if len(f) >= min_sessions}

    def intraday(self, symbols, interval, end, collector=None):
        from signal_engine.backtest.data_readiness.preflight import DataUnavailable

        self.calls.append(("intraday", tuple(symbols or ()), end))
        if self._intraday is None:
            raise DataUnavailable("no intraday frames in this test source")
        return self._cut(self._intraday, symbols, end)


def frames_resolution(daily=None, intraday=None, eligible=None, loader_min_sessions=0):
    """A READY resolution whose data source is the given frames (no store at all). The official
    calendar is the frames' own session dates, so the load-time calendar check passes for
    consistent frames."""
    from contextlib import contextmanager

    days = sorted({ts.date() for frames in (daily or {}, intraday or {})
                   for f in frames.values() for ts in f.index})
    base = fake_resolution()
    syms = list(eligible) if eligible is not None else sorted(set(daily or {}) | set(intraday or {}))
    contract = dataclasses.replace(base.contract, loader_min_sessions=loader_min_sessions,
                                   universe_symbols=tuple(syms))
    report = dataclasses.replace(base.report, universe={"eligible": syms, "unavailable": []})
    weekend = {d: None for d in days if d.weekday() >= 5}
    cal = calendar_for([d for d in days if d.weekday() < 5] or days)
    cal = dataclasses.replace(cal, specials={**dict(cal.specials), **weekend})
    source = FrameSource(daily, intraday)

    class _Frames(type(base)):
        def open(self):
            @contextmanager
            def _cm():
                yield source
            return _cm()

    res = _Frames(base.manifest, report, contract, cal)
    res.source = source
    return res
