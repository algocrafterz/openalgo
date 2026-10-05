"""The authoritative NSE CM trading calendar, loaded from a versioned YAML file.

The calendar comes from NSE circulars (`signal_engine/research/reference/nse_calendar.yaml`) and is
never derived from observed market data. The audit compares the store against it to find missing
and unexpected sessions. Validation is fail-fast: unknown keys, bad dates, duplicates, a holiday on a
weekend, overlapping classifications and a missing source all raise `CalendarError`.

The version is the sha256 of the file bytes, so every edit, comments included, is a new version.
Dates are IST calendar dates (NSE trades only in IST).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "research" / "reference" / "nse_calendar.yaml"
SCHEMA = 1

TRADING = "trading"
HOLIDAY = "holiday"
WEEKEND = "weekend"
SPECIAL = "special"
MOCK = "mock"
UNKNOWN = "unknown"

#: Kinds of special session. Only a Muhurat session may fall on a listed weekday holiday.
SPECIAL_KINDS = frozenset({"muhurat", "budget", "live_saturday"})

_TOP_KEYS = frozenset({"schema", "exchange", "segment", "regular_session", "years", "holidays",
                       "special_sessions", "mock_sessions", "unconfirmed"})
_YEAR_KEYS = (frozenset({"source", "verified", "holiday_count"}), frozenset({"note"}))
_HOLIDAY_KEYS = (frozenset({"date", "name"}), frozenset({"source", "verified", "note"}))
_SPECIAL_KEYS = (frozenset({"date", "kind", "name", "windows", "source", "verified"}),
                 frozenset({"note"}))
_MOCK_KEYS = (frozenset({"date", "source"}), frozenset({"note"}))
_UNCONFIRMED_KEYS = (frozenset({"date", "note"}), frozenset())
_SESSION_KEYS = (frozenset({"open", "close"}), frozenset())
_HHMM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

Window = tuple[str, str]


class CalendarError(ValueError):
    """The calendar file is missing or malformed, or a query falls outside its coverage."""


@dataclass(frozen=True)
class NseCalendar:
    """A validated NSE CM calendar plus its content version."""

    holidays: frozenset[date]
    specials: Mapping[date, tuple[Window, ...] | None]
    mocks: frozenset[date]
    unconfirmed: frozenset[date]
    coverage: tuple[int, int]
    regular_session: Window
    version: str
    source: str

    def _check_covered(self, d: date) -> None:
        first, last = self.coverage
        if not first <= d.year <= last:
            raise CalendarError(f"{d} is outside the calendar coverage {first}-{last}")

    def classify(self, d: date) -> str:
        """Classify one IST date as trading, holiday, weekend, special, mock or unknown.

        Args:
            d: The calendar date.

        Returns:
            One of TRADING, HOLIDAY, WEEKEND, SPECIAL, MOCK or UNKNOWN. A Muhurat session on a
            weekday holiday is SPECIAL. An unconfirmed date is UNKNOWN.

        Raises:
            CalendarError: The date is outside the covered years.
        """
        self._check_covered(d)
        if d in self.unconfirmed:
            return UNKNOWN
        if d in self.specials:
            return SPECIAL
        if d in self.mocks:
            return MOCK
        if d in self.holidays:
            return HOLIDAY
        if d.weekday() >= 5:
            return WEEKEND
        return TRADING

    def expected_sessions(self, start: date, end: date) -> tuple[date, ...]:
        """Every live session in [start, end]: weekdays minus holidays, plus special sessions.

        Mock sessions and unconfirmed dates are never sessions.

        Args:
            start: First date, inclusive.
            end: Last date, inclusive.

        Returns:
            The session dates in ascending order.

        Raises:
            CalendarError: The range is reversed or leaves the covered years.
        """
        if end < start:
            raise CalendarError(f"reversed range {start}..{end}")
        self._check_covered(start)
        self._check_covered(end)
        out = []
        d = start
        while d <= end:
            if self.classify(d) in (TRADING, SPECIAL):
                out.append(d)
            d += timedelta(days=1)
        return tuple(out)

    def special_windows(self, d: date) -> tuple[Window, ...] | None:
        """The normal-market windows of a special session, or None when they are not known.

        Raises:
            CalendarError: The date is not a special session.
        """
        if d not in self.specials:
            raise CalendarError(f"{d} is not a special session")
        return self.specials[d]


def _keys(entry: Any, allowed: tuple[frozenset[str], frozenset[str]], where: str) -> dict:
    required, optional = allowed
    if not isinstance(entry, dict):
        raise CalendarError(f"{where}: expected a mapping")
    unknown = set(entry) - required - optional
    if unknown:
        raise CalendarError(f"{where}: unknown keys {sorted(unknown)}")
    missing = required - set(entry)
    if missing:
        raise CalendarError(f"{where}: missing keys {sorted(missing)}")
    return entry


def _date(value: Any, where: str) -> date:
    if isinstance(value, datetime):
        raise CalendarError(f"{where}: expected a date, got a datetime {value!r}")
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise CalendarError(f"{where}: bad date {value!r}") from exc
    raise CalendarError(f"{where}: bad date {value!r}")


def _source(entry: dict, where: str) -> None:
    src = entry.get("source")
    if not isinstance(src, str) or not src.startswith("https://"):
        raise CalendarError(f"{where}: source must be an https URL, got {src!r}")


def _verified(entry: dict, where: str) -> bool:
    flag = entry.get("verified", True)
    if not isinstance(flag, bool):
        raise CalendarError(f"{where}: verified must be true or false")
    return flag


def _hhmm(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _HHMM.fullmatch(value):
        raise CalendarError(f"{where}: bad window time {value!r}, expected HH:MM")
    return value


def _windows(entry: dict, verified: bool, where: str) -> tuple[Window, ...] | None:
    raw = entry["windows"]
    if raw is None:
        if verified:
            raise CalendarError(f"{where}: window null is only allowed with verified: false")
        return None
    if not isinstance(raw, list) or not raw:
        raise CalendarError(f"{where}: window list must be non-empty or null")
    out: list[Window] = []
    for pair in raw:
        if not isinstance(pair, list) or len(pair) != 2:
            raise CalendarError(f"{where}: each window must be [open, close]")
        lo, hi = (_hhmm(t, where) for t in pair)
        if lo >= hi or (out and lo < out[-1][1]):
            raise CalendarError(f"{where}: window {lo}-{hi} is empty or out of order")
        out.append((lo, hi))
    return tuple(out)


def _unique(dates: list[date], what: str) -> frozenset[date]:
    seen: set[date] = set()
    for d in dates:
        if d in seen:
            raise CalendarError(f"{what}: duplicate date {d}")
        seen.add(d)
    return frozenset(seen)


def _parse_years(raw: Any) -> tuple[tuple[int, int], dict[int, int]]:
    if not isinstance(raw, dict) or not raw:
        raise CalendarError("years: expected a non-empty mapping")
    for year, entry in raw.items():
        if not isinstance(year, int) or isinstance(year, bool):
            raise CalendarError(f"years: bad year {year!r}")
        _keys(entry, _YEAR_KEYS, f"years.{year}")
        _source(entry, f"years.{year}")
        _verified(entry, f"years.{year}")
        count = entry["holiday_count"]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise CalendarError(f"years.{year}.holiday_count must be a non-negative integer")
    years = sorted(raw)
    if years != list(range(years[0], years[-1] + 1)):
        raise CalendarError(f"years: coverage has gaps {years}")
    return (years[0], years[-1]), {y: raw[y]["holiday_count"] for y in years}


def _check_holiday_counts(holidays: frozenset[date], counts: dict[int, int]) -> None:
    """Each year's listed holidays must equal its declared count, so a truncated or partly
    deleted list fails to load instead of turning holidays into expected sessions."""
    for year, want in counts.items():
        have = sum(1 for d in holidays if d.year == year)
        if have != want:
            raise CalendarError(f"years.{year}.holiday_count is {want} but {have} holidays are "
                                "listed")


def _parse_holidays(raw: list) -> frozenset[date]:
    dates = []
    for i, entry in enumerate(raw):
        where = f"holidays[{i}]"
        _keys(entry, _HOLIDAY_KEYS, where)
        d = _date(entry["date"], where)
        if d.weekday() >= 5:
            raise CalendarError(f"{where}: {d} is a weekend; list weekday closures only")
        if "source" in entry:
            _source(entry, where)
        _verified(entry, where)
        dates.append(d)
    return _unique(dates, "holidays")


def _parse_specials(raw: list) -> tuple[dict[date, tuple[Window, ...] | None], dict[date, str]]:
    windows: dict[date, tuple[Window, ...] | None] = {}
    kinds: dict[date, str] = {}
    for i, entry in enumerate(raw):
        where = f"special_sessions[{i}]"
        _keys(entry, _SPECIAL_KEYS, where)
        d = _date(entry["date"], where)
        if entry["kind"] not in SPECIAL_KINDS:
            raise CalendarError(f"{where}: unknown kind {entry['kind']!r}")
        _source(entry, where)
        verified = _verified(entry, where)
        if d in windows:
            raise CalendarError(f"special_sessions: duplicate date {d}")
        windows[d] = _windows(entry, verified, where)
        kinds[d] = entry["kind"]
    return windows, kinds


def _parse_dated(raw: list, allowed, what: str, needs_source: bool) -> frozenset[date]:
    dates = []
    for i, entry in enumerate(raw):
        where = f"{what}[{i}]"
        _keys(entry, allowed, where)
        if needs_source:
            _source(entry, where)
        dates.append(_date(entry["date"], where))
    return _unique(dates, what)


def _check_overlaps(holidays, specials: dict[date, str], mocks, unconfirmed) -> None:
    for d, kind in specials.items():
        if d in holidays and kind != "muhurat":
            raise CalendarError(f"overlap: special session {d} ({kind}) on a holiday")
        if d not in holidays and d.weekday() < 5:
            raise CalendarError(f"special session {d} falls on an ordinary trading day")
    groups = {"holidays": holidays, "special_sessions": frozenset(specials), "mock_sessions": mocks,
              "unconfirmed": unconfirmed}
    names = list(groups)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if (a, b) == ("holidays", "special_sessions"):
                continue
            both = groups[a] & groups[b]
            if both:
                raise CalendarError(f"overlap: {sorted(both)} in both {a} and {b}")


def _check_coverage(coverage: tuple[int, int], *groups: frozenset[date]) -> None:
    first, last = coverage
    for group in groups:
        for d in group:
            if not first <= d.year <= last:
                raise CalendarError(f"{d} is outside the coverage {first}-{last}")


def _list(doc: dict, key: str) -> list:
    value = doc[key]
    if not isinstance(value, list):
        raise CalendarError(f"{key}: expected a list")
    return value


def load_calendar(path: Path | str = DEFAULT_PATH) -> NseCalendar:
    """Load and validate the NSE calendar file.

    Args:
        path: The YAML file. Defaults to the repository calendar.

    Returns:
        The validated, immutable calendar.

    Raises:
        CalendarError: The file is missing or does not match schema 1.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CalendarError(f"cannot read calendar {path}: {exc}") from exc
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise CalendarError(f"calendar {path} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise CalendarError(f"calendar {path}: expected a mapping at the top level")
    _keys(doc, (_TOP_KEYS, frozenset()), "calendar")
    if isinstance(doc["schema"], bool) or doc["schema"] != SCHEMA:
        raise CalendarError(f"calendar schema {doc['schema']!r} is not {SCHEMA}")
    if (doc["exchange"], doc["segment"]) != ("NSE", "CM"):
        raise CalendarError("calendar must be exchange NSE, segment CM")
    session = _keys(doc["regular_session"], _SESSION_KEYS, "regular_session")
    regular = (_hhmm(session["open"], "regular_session"), _hhmm(session["close"], "regular_session"))
    if regular[0] >= regular[1]:
        raise CalendarError(f"regular_session: window {regular[0]}-{regular[1]} is empty")

    coverage, counts = _parse_years(doc["years"])
    holidays = _parse_holidays(_list(doc, "holidays"))
    windows, kinds = _parse_specials(_list(doc, "special_sessions"))
    mocks = _parse_dated(_list(doc, "mock_sessions"), _MOCK_KEYS, "mock_sessions", True)
    unconfirmed = _parse_dated(_list(doc, "unconfirmed"), _UNCONFIRMED_KEYS, "unconfirmed", False)
    _check_overlaps(holidays, kinds, mocks, unconfirmed)
    _check_coverage(coverage, holidays, frozenset(windows), mocks, unconfirmed)
    _check_holiday_counts(holidays, counts)

    cal = NseCalendar(holidays=holidays, specials=MappingProxyType(dict(windows)), mocks=mocks,
                      unconfirmed=unconfirmed, coverage=coverage, regular_session=regular,
                      version=hashlib.sha256(raw).hexdigest(), source=str(path))
    logger.debug(f"loaded NSE calendar {path} version {cal.version[:12]} coverage {coverage}")
    return cal


