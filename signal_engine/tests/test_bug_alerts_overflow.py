"""O1/O2: alert-cap overflow, never-sent expiry and lock-busy skips are visible, never silent.

Every path is a tmp path; the NSE calendar is replaced and delivery is stubbed.
"""

import json
import subprocess
import sys
import textwrap
import time

import pytest

from signal_engine.bugs import store
from signal_engine.ops import alerts, automation, bugalerts

pytestmark = pytest.mark.filterwarnings("ignore")
WED, THU, FRI, MON, TUE = "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17"


def _bug(path, title, seen):
    bug = store.add(
        title=title, kind="BUG", severity="HIGH", area="x", seen=seen, impact="i", path=path
    )
    data = store.load(path)
    for b in data["bugs"]:
        if b["id"] == bug["id"]:
            b["created_at"] = f"{seen}T09:00:00+05:30"
    store._save(data, path)
    return bug


@pytest.fixture(autouse=True)
def _weekdays_only(monkeypatch):
    monkeypatch.setattr(automation, "DB", "/nonexistent/none.db")
    monkeypatch.setattr(bugalerts, "_is_business_day", lambda d: d.weekday() < 5)


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        alerts, "deliver", lambda text, **kw: calls.append((kw["condition"], text)) or True
    )
    return calls


def _state(folder):
    return set(json.loads((folder / "bug-alert-state.json").read_text())["sent"])


def test_overflow_is_visible_unsent_and_retried(tmp_path, sent, capsys):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    for n in range(8):
        _bug(path, f"Bug {n}", MON)
    lines = bugalerts.run(MON, dry_run=False, path=path, folder=folder)
    err = capsys.readouterr().err
    assert len(sent) == 5
    assert "and 3 more" in sent[-1][1]  # Telegram text unchanged
    assert "and 3 more" in lines
    over = next(ln for ln in lines if ln.startswith("SUPPRESSED"))
    assert "(3," in over and all(f"bug-new|BUG-00{n}" in over for n in (6, 7, 8))
    assert "WARNING" in err and "3 event(s)" in err and "bug-new|BUG-008" in err
    # suppressed events are NOT marked sent
    assert _state(tmp_path) == {f"bug-new|BUG-00{n}" for n in range(1, 6)}
    sent.clear()
    again = bugalerts.run(TUE, dry_run=False, path=path, folder=folder)
    assert [c for c, _ in sent] == ["BUG-006", "BUG-007", "BUG-008"]
    assert not any(ln.startswith("SUPPRESSED") for ln in again)
    assert len(_state(tmp_path)) == 8


def test_event_about_to_age_out_unsent_is_flagged(tmp_path, monkeypatch, capsys):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Seen Wednesday", WED)  # last alertable weekday window day is Friday
    monkeypatch.setattr(alerts, "deliver", lambda *a, **k: False)
    lines = bugalerts.run(FRI, dry_run=False, path=path, folder=folder)
    err = capsys.readouterr().err
    assert any(ln.startswith("EXPIRING UNSENT") and "bug-new|BUG-001" in ln for ln in lines)
    assert "WARNING" in err and "EXPIRING UNSENT" in err and "bug-new|BUG-001" in err


def test_an_event_still_inside_next_days_window_is_not_flagged(tmp_path, monkeypatch, capsys):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Seen Thursday", THU)  # still alertable on Monday (Mon, Fri, Thu)
    monkeypatch.setattr(alerts, "deliver", lambda *a, **k: False)
    lines = bugalerts.run(FRI, dry_run=False, path=path, folder=folder)
    assert not any("EXPIRING" in ln for ln in lines)
    assert "EXPIRING" not in capsys.readouterr().err


def test_a_sent_event_is_never_flagged_expiring(tmp_path, sent, capsys):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Seen Wednesday", WED)
    lines = bugalerts.run(FRI, dry_run=False, path=path, folder=folder)
    assert len(sent) == 1 and not any("EXPIRING" in ln for ln in lines)
    assert "EXPIRING" not in capsys.readouterr().err


def test_lock_busy_warns_exits_nonfailing_and_a_later_run_delivers(
    tmp_path, sent, monkeypatch, capsys
):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck", MON)
    state = str(tmp_path / "bug-alert-state.json")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import time
                from signal_engine.bugs import store
                with store._locked({state!r}):
                    print("held", flush=True)
                    time.sleep(30)
                """
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.2)
        out = bugalerts.run(MON, dry_run=False, path=path, folder=folder)  # returns, never raises
    finally:
        holder.kill()
        holder.wait()
        time.sleep(0.1)
    err = capsys.readouterr().err
    assert out and out[0].startswith("skipped")  # stdout line kept
    assert "WARNING" in err and "SKIPPED" in err and MON in err
    assert sent == []
    later = bugalerts.run(TUE, dry_run=False, path=path, folder=folder)
    assert [c for c, _ in sent] == ["BUG-001"] and later == ["sent bug-new BUG-001"]
