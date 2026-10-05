"""Tests for the authoritative NSE CM trading calendar (plan Phase 2, owner decision D10).

The calendar comes from NSE circulars, never from observed market data. These tests check the
loader's fail-fast schema validation on small synthetic files, then a handful of known dates in the
real `signal_engine/research/reference/nse_calendar.yaml` against the circulars cited in it.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest
import yaml

from signal_engine.backtest.data_readiness.calendar import (
    DEFAULT_PATH,
    CalendarError,
    load_calendar,
)

SRC = "https://nsearchives.nseindia.com/content/circulars/CMTR00000.pdf"


def _doc(**over):
    doc = {
        "schema": 1,
        "exchange": "NSE",
        "segment": "CM",
        "regular_session": {"open": "09:15", "close": "15:30"},
        "years": {2024: {"source": SRC, "verified": True, "holiday_count": 2}},
        "holidays": [{"date": "2024-01-26", "name": "Republic Day"}],
        "special_sessions": [
            {"date": "2024-11-01", "kind": "muhurat", "name": "Diwali Muhurat",
             "windows": [["18:00", "19:00"]], "source": SRC, "verified": True},
            {"date": "2024-03-02", "kind": "live_saturday", "name": "DR drill live session",
             "windows": [["09:15", "10:00"], ["11:30", "12:30"]], "source": SRC,
             "verified": True},
        ],
        "mock_sessions": [{"date": "2024-03-09", "source": SRC}],
        "unconfirmed": [{"date": "2024-09-15", "note": "data seen, no NSE source"}],
    }
    doc["holidays"].append({"date": "2024-11-01", "name": "Diwali Laxmi Pujan"})
    doc.update(over)
    return doc


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "cal.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False))
    return p


def _load(tmp_path, **over):
    return load_calendar(_write(tmp_path, _doc(**over)))


# ---- loader and version -------------------------------------------------------------------


def test_valid_file_loads_and_version_is_file_sha256(tmp_path):
    p = _write(tmp_path, _doc())
    cal = load_calendar(p)
    assert cal.version == hashlib.sha256(p.read_bytes()).hexdigest()
    assert cal.source == str(p)
    assert cal.coverage == (2024, 2024)


def test_missing_file_raises(tmp_path):
    with pytest.raises(CalendarError):
        load_calendar(tmp_path / "nope.yaml")


@pytest.mark.parametrize("bad", ["not: [valid", "- a list\n- not a map\n"])
def test_malformed_yaml_raises(tmp_path, bad):
    p = tmp_path / "cal.yaml"
    p.write_text(bad)
    with pytest.raises(CalendarError):
        load_calendar(p)


def test_unknown_top_level_key_raises(tmp_path):
    with pytest.raises(CalendarError, match="unknown"):
        _load(tmp_path, extra=1)


def test_unknown_entry_key_raises(tmp_path):
    with pytest.raises(CalendarError, match="unknown"):
        _load(tmp_path, holidays=[{"date": "2024-01-26", "name": "x", "typo": 1}])


def test_missing_top_level_key_raises(tmp_path):
    doc = _doc()
    del doc["mock_sessions"]
    with pytest.raises(CalendarError, match="missing"):
        load_calendar(_write(tmp_path, doc))


def test_wrong_schema_raises(tmp_path):
    with pytest.raises(CalendarError, match="schema"):
        _load(tmp_path, schema=2)


@pytest.mark.parametrize("bad", ["2024-13-01", "26-01-2024", "", 20240126])
def test_bad_date_raises(tmp_path, bad):
    with pytest.raises(CalendarError):
        _load(tmp_path, holidays=[{"date": bad, "name": "x"}])


def test_duplicate_holiday_raises(tmp_path):
    h = {"date": "2024-01-26", "name": "Republic Day"}
    with pytest.raises(CalendarError, match="duplicate"):
        _load(tmp_path, holidays=[h, dict(h)])


def test_holiday_on_weekend_raises(tmp_path):
    with pytest.raises(CalendarError, match="weekend"):
        _load(tmp_path, holidays=[{"date": "2024-04-14", "name": "Ambedkar Jayanti (Sunday)"}])


def test_holiday_outside_covered_years_raises(tmp_path):
    with pytest.raises(CalendarError, match="coverage"):
        _load(tmp_path, holidays=[{"date": "2023-01-26", "name": "Republic Day"},
                                  {"date": "2024-11-01", "name": "Diwali Laxmi Pujan"}])


def test_year_without_source_raises(tmp_path):
    with pytest.raises(CalendarError, match="source"):
        _load(tmp_path, years={2024: {"source": "", "verified": True, "holiday_count": 2}})


def test_special_session_without_source_raises(tmp_path):
    doc = _doc()
    doc["special_sessions"][1]["source"] = ""
    with pytest.raises(CalendarError, match="source"):
        load_calendar(_write(tmp_path, doc))


def test_mock_session_without_source_raises(tmp_path):
    with pytest.raises(CalendarError, match="source"):
        _load(tmp_path, mock_sessions=[{"date": "2024-03-09"}])


def test_mock_overlapping_special_raises(tmp_path):
    with pytest.raises(CalendarError, match="overlap"):
        _load(tmp_path, mock_sessions=[{"date": "2024-03-02", "source": SRC}])


def test_unconfirmed_overlapping_holiday_raises(tmp_path):
    with pytest.raises(CalendarError, match="overlap"):
        _load(tmp_path, unconfirmed=[{"date": "2024-01-26", "note": "x"}])


def test_non_muhurat_special_on_holiday_raises(tmp_path):
    doc = _doc()
    doc["special_sessions"][0]["kind"] = "live_saturday"
    with pytest.raises(CalendarError, match="overlap"):
        load_calendar(_write(tmp_path, doc))


def test_special_on_ordinary_trading_day_raises(tmp_path):
    doc = _doc()
    doc["special_sessions"][1]["date"] = "2024-03-04"      # a Monday, not a holiday
    with pytest.raises(CalendarError, match="ordinary trading day"):
        load_calendar(_write(tmp_path, doc))


def test_unknown_special_kind_raises(tmp_path):
    doc = _doc()
    doc["special_sessions"][1]["kind"] = "party"
    with pytest.raises(CalendarError, match="kind"):
        load_calendar(_write(tmp_path, doc))


@pytest.mark.parametrize("windows", [[["10:00", "09:15"]], [["9:15", "10:00"]], [["09:15"]], []])
def test_bad_window_raises(tmp_path, windows):
    doc = _doc()
    doc["special_sessions"][1]["windows"] = windows
    with pytest.raises(CalendarError, match="window"):
        load_calendar(_write(tmp_path, doc))


def test_null_window_allowed_but_must_be_unverified(tmp_path):
    doc = _doc()
    doc["special_sessions"][0]["windows"] = None
    with pytest.raises(CalendarError, match="window"):
        load_calendar(_write(tmp_path, doc))
    doc["special_sessions"][0]["verified"] = False
    cal = load_calendar(_write(tmp_path, doc))
    assert cal.special_windows(date(2024, 11, 1)) is None


# ---- classification and expected sessions -------------------------------------------------


def test_classify(tmp_path):
    cal = _load(tmp_path)
    assert cal.classify(date(2024, 1, 25)) == "trading"
    assert cal.classify(date(2024, 1, 26)) == "holiday"
    assert cal.classify(date(2024, 1, 27)) == "weekend"
    assert cal.classify(date(2024, 3, 2)) == "special"
    assert cal.classify(date(2024, 11, 1)) == "special"         # muhurat on a weekday holiday
    assert cal.classify(date(2024, 3, 9)) == "mock"
    assert cal.classify(date(2024, 9, 15)) == "unknown"


def test_classify_outside_coverage_raises(tmp_path):
    cal = _load(tmp_path)
    with pytest.raises(CalendarError, match="coverage"):
        cal.classify(date(2025, 1, 2))


def test_expected_sessions(tmp_path):
    cal = _load(tmp_path)
    got = cal.expected_sessions(date(2024, 2, 26), date(2024, 3, 10))
    # Mon 26 Feb .. Fri 1 Mar, the Saturday 2 Mar live session, Mon 4 .. Fri 8 Mar.
    # The 9 Mar mock is not a session.
    assert date(2024, 3, 2) in got
    assert date(2024, 3, 9) not in got
    assert len(got) == 11
    assert list(got) == sorted(got)
    jan = cal.expected_sessions(date(2024, 1, 22), date(2024, 1, 28))
    assert date(2024, 1, 26) not in jan and len(jan) == 4


def test_expected_sessions_include_muhurat_on_holiday(tmp_path):
    cal = _load(tmp_path)
    assert cal.expected_sessions(date(2024, 11, 1), date(2024, 11, 1)) == (date(2024, 11, 1),)


def test_expected_sessions_outside_coverage_raises(tmp_path):
    cal = _load(tmp_path)
    with pytest.raises(CalendarError, match="coverage"):
        cal.expected_sessions(date(2023, 12, 25), date(2024, 1, 5))


def test_expected_sessions_reversed_range_raises(tmp_path):
    cal = _load(tmp_path)
    with pytest.raises(CalendarError):
        cal.expected_sessions(date(2024, 3, 10), date(2024, 3, 1))


def test_calendar_is_immutable(tmp_path):
    cal = _load(tmp_path)
    with pytest.raises(AttributeError):
        cal.version = "x"


# ---- the real calendar file ----------------------------------------------------------------


@pytest.fixture(scope="module")
def real():
    return load_calendar(DEFAULT_PATH)


def test_real_calendar_covers_store_history(real):
    first, last = real.coverage
    assert first <= 2016 and last >= 2026


@pytest.mark.parametrize("d", ["2017-01-26", "2018-01-26", "2021-01-26", "2022-01-26",
                               "2023-01-26", "2024-01-26", "2026-01-26"])
def test_real_republic_day_on_weekdays(real, d):
    assert real.classify(date.fromisoformat(d)) == "holiday"


def test_real_republic_day_on_weekend_is_weekend(real):
    assert real.classify(date(2025, 1, 26)) == "weekend"       # a Sunday in 2025


@pytest.mark.parametrize("d", [
    "2016-10-30", "2017-10-19", "2018-11-07", "2019-10-27", "2020-11-14", "2021-11-04",
    "2022-10-24", "2023-11-12", "2024-11-01", "2025-10-21", "2026-11-08",
])
def test_real_muhurat_sessions(real, d):
    assert real.classify(date.fromisoformat(d)) == "special"


def test_real_muhurat_windows_from_circulars(real):
    assert real.special_windows(date(2025, 10, 21)) == (("13:45", "14:45"),)     # CMTR70319
    assert real.special_windows(date(2023, 11, 12)) == (("18:15", "19:15"),)     # CMTR59124
    assert real.special_windows(date(2021, 11, 4)) == (("18:15", "19:15"),)      # CMTR50050


@pytest.mark.parametrize("d", ["2020-02-01", "2024-01-20", "2024-03-02", "2024-05-18",
                               "2025-02-01", "2026-02-01"])
def test_real_weekend_live_sessions(real, d):
    assert real.classify(date.fromisoformat(d)) == "special"


@pytest.mark.parametrize("d", ["2019-04-29", "2019-10-21", "2023-06-29", "2024-01-22",
                               "2024-05-20", "2024-11-20", "2026-01-15"])
def test_real_mid_year_amendments(real, d):
    assert real.classify(date.fromisoformat(d)) == "holiday"


def test_real_bakri_id_2023_moved_off_june_28(real):
    assert real.classify(date(2023, 6, 28)) == "trading"


def test_real_2026_09_13_stays_unconfirmed(real):
    # D10: no NSE source confirms this Sunday, so data stamped on it is an error.
    assert real.classify(date(2026, 9, 13)) == "unknown"
    assert date(2026, 9, 13) not in real.expected_sessions(date(2026, 9, 7), date(2026, 9, 18))


def test_real_holiday_counts(real):
    counts = dict.fromkeys(range(2016, 2027), 0)
    for d in real.holidays:
        counts[d.year] += 1
    assert counts == {2016: 15, 2017: 13, 2018: 16, 2019: 17, 2020: 12, 2021: 14, 2022: 13,
                      2023: 15, 2024: 17, 2025: 14, 2026: 16}


def test_real_2025_session_count(real):
    # 261 weekdays - 14 weekday holidays + 1 Saturday budget session; the 21 Oct muhurat falls
    # on a weekday holiday and counts as a session.
    got = real.expected_sessions(date(2025, 1, 1), date(2025, 12, 31))
    assert len(got) == 261 - 14 + 1 + 1


def test_regular_session_window_must_be_ordered(tmp_path):
    with pytest.raises(CalendarError, match="window"):
        _load(tmp_path, regular_session={"open": "15:30", "close": "09:15"})


# ---- review findings (2026-10-04) ----------------------------------------------------------

def test_holiday_count_must_match_listed_holidays(tmp_path):
    doc = _doc()
    doc["years"][2024]["holiday_count"] = 3        # a holiday lost from the list
    with pytest.raises(CalendarError, match="holiday_count"):
        load_calendar(_write(tmp_path, doc))


def test_holiday_count_is_required(tmp_path):
    doc = _doc()
    del doc["years"][2024]["holiday_count"]
    with pytest.raises(CalendarError, match="holiday_count"):
        load_calendar(_write(tmp_path, doc))


def test_boolean_schema_is_rejected(tmp_path):
    with pytest.raises(CalendarError, match="schema"):
        _load(tmp_path, schema=True)
