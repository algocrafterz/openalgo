"""Tests for signal_engine.research.readiness."""

from pathlib import Path

import pytest

from signal_engine.research import readiness
from signal_engine.research.readiness import ReadinessError, Row, missing, missing_p1, parse_text

HEAD = "| Capability | Status | Code | Tests | Verified |\n| --- | --- | --- | --- | --- |\n"


def _doc(p1="", p2="", p3="", extra=""):
    parts = [
        "# Title\n\nprose | with pipe\n- bullet\n",
        extra,
        "## P1\n\n" + (HEAD + p1 if p1 is not None else ""),
        "## P2\n\n" + (HEAD + p2 if p2 is not None else ""),
        "## P3\n\n" + (HEAD + p3 if p3 is not None else ""),
    ]
    return "\n".join(parts)


def _row(cap, status="not started", code="", tests="", ver=""):
    return f"| {cap} | {status} | {code} | {tests} | {ver} |\n"


def test_parse_tiers_and_order():
    text = _doc(
        _row("A") + _row("B", "built") + _row("C"),
        _row("D"),
        _row("E", "verified"),
    )
    rows = parse_text(text)
    assert [(r.tier, r.capability) for r in rows] == [
        ("P1", "A"),
        ("P1", "B"),
        ("P1", "C"),
        ("P2", "D"),
        ("P3", "E"),
    ]
    assert isinstance(rows[0], Row)


def test_missing_and_verified_removed():
    text = _doc(_row("A") + _row("B", "verified 2026-10-03"))
    assert missing_p1_text(text) == ["A"]
    assert missing("P1", path=None) is not None


def missing_p1_text(text):
    return [r.capability for r in parse_text(text) if r.tier == "P1" and r.status != "verified"]


def test_missing_from_file_and_verify(tmp_path):
    p = tmp_path / "r.md"
    p.write_text(_doc(_row("A") + _row("B")))
    assert missing_p1(p) == ["A", "B"]
    p.write_text(_doc(_row("A", "verified") + _row("B")))
    assert missing_p1(p) == ["B"]
    assert missing("P2", p) == []


def test_missing_other_tier(tmp_path):
    p = tmp_path / "r.md"
    p.write_text(_doc(_row("A"), _row("X") + _row("Y", "verified")))
    assert missing("P2", p) == ["X"]


def test_progress_table_ignored():
    extra = (
        "## Build order progress\n\n| Order | Work | Status | Commit |\n| --- | --- | --- | --- |\n"
        "| 1 | stuff | done | `abc` |\n"
    )
    rows = parse_text(_doc(_row("A"), extra=extra))
    assert [r.capability for r in rows if r.tier == "P1"] == ["A"]
    assert all(r.capability != "stuff" for r in rows)


def test_status_normalisation():
    text = _doc(
        _row("A", "Built 2026-10-02 (note: a | b uses `x::y` {z})".replace("| ", "/ "))
        + _row("B", "IN PROGRESS")
        + _row("C", "verified")
        + _row("D", "not started")
    )
    rows = parse_text(text)
    a, b, c, d = rows[:4]
    assert a.status == "built"
    assert a.detail.startswith("2026-10-02 (note:")
    assert "`x::y`" in a.detail
    assert b.status == "in progress" and b.detail == ""
    assert c.status == "verified"
    assert d.status == "not started"


def test_cells_with_code_chars():
    rows = parse_text(_doc(_row("Cap `a::b` {x}: y", "built", "`research/x.py`", "`t::u`", "")))
    assert rows[0].capability == "Cap `a::b` {x}: y"
    assert rows[0].code == "`research/x.py`"
    assert rows[0].tests == "`t::u`"
    assert rows[0].verified == ""


@pytest.mark.parametrize(
    "text, needle",
    [
        (_doc("| A | built | x |\n"), "P1"),
        (_doc(_row("", "built")), "capability"),
        (_doc(_row("A", "done")), "status"),
        (_doc(_row("A", "")), "status"),
        (_doc(_row("A") + _row("A", "built")), "duplicate"),
        (_doc(_row("A"), p2=None), "P2"),
        (_doc(_row("A"), p3=None), "P3"),
        (_doc(p1=None), "P1"),
        ("# t\n\n## P2\n\n" + HEAD + _row("A"), "P1"),
    ],
)
def test_malformed_raises(text, needle):
    with pytest.raises(ReadinessError) as ei:
        parse_text(text, source="doc.md")
    assert needle.lower() in str(ei.value).lower()


def test_error_names_line():
    text = _doc(_row("A", "done"))
    with pytest.raises(ReadinessError) as ei:
        parse_text(text, source="doc.md")
    msg = str(ei.value)
    assert "doc.md" in msg and "line" in msg and "P1" in msg


def test_same_capability_across_tiers_allowed():
    rows = parse_text(_doc(_row("A"), _row("A")))
    assert [r.tier for r in rows] == ["P1", "P2"]


def test_real_doc_works_from_other_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rows = readiness.parse()
    p1 = [r for r in rows if r.tier == "P1"]
    assert len(p1) >= 13
    assert all(r.status in {"not started", "in progress", "built", "verified"} for r in rows)
    miss = missing_p1()
    # Point-in-time universe membership stays unverified until a historical F&O membership source
    # exists (owner decision 2026-10-10), so the real document always has at least this row missing.
    assert "Point-in-time universe membership and per-date eligibility" in miss
    verified = {r.capability for r in p1 if r.status == "verified"}
    assert not verified & set(miss)
    assert isinstance(readiness.DEFAULT_PATH, Path)
