"""The EOD tradebook snapshot is the only record of broker fill prices; it must never lose fills.

Before 2026-10-23 `snapshot()` overwrote `tradebook_<date.today()>.json` with whatever the API
returned: a re-run that got fewer fills replaced a good file, and `eod.sh 2026-10-21` run by hand on
10-22 wrote over TODAY's file, since the broker tradebook only ever holds today's fills.
Now a snapshot is taken only for today's IST date and merged into the existing file by trade id.
"""

import datetime as dt
import json
import os
import re

import pytest

from signal_engine.analysis import __main__ as analysis

TODAY = dt.date(2026, 10, 22)


def _fill(tradeid, orderid="O1", symbol="SBIN", ts="10:05:01", qty=10, price=800.0):
    return {
        "action": "BUY", "average_price": price, "exchange": "NSE", "orderid": orderid,
        "price": price, "product": "MIS", "quantity": qty, "strategy": "BREAKOUT-PY",
        "symbol": symbol, "timestamp": f"2026-10-22 {ts}", "trade_value": qty * price,
        "tradeid": tradeid,
    }


@pytest.fixture
def snap(tmp_path, monkeypatch):
    monkeypatch.setattr(analysis, "SNAP_DIR", str(tmp_path))
    monkeypatch.setattr(analysis, "ist_today", lambda: TODAY)
    fetched = {"rows": []}

    async def _fake_fetch():
        return list(fetched["rows"])

    monkeypatch.setattr(analysis, "_fetch_tradebook", _fake_fetch)

    def run(rows, day=None):
        fetched["rows"] = rows
        return analysis.snapshot(day)

    def stored(day=TODAY):
        with open(tmp_path / f"tradebook_{day.isoformat()}.json", encoding="utf-8") as fh:
            return json.load(fh)

    return run, stored, tmp_path


def test_the_first_snapshot_writes_todays_fills(snap):
    run, stored, _ = snap
    run([_fill("T1"), _fill("T2", orderid="O2")])
    assert [f["tradeid"] for f in stored()] == ["T1", "T2"]


def test_a_later_snapshot_adds_new_fills_without_duplicates(snap):
    run, stored, _ = snap
    run([_fill("T1")])
    run([_fill("T1"), _fill("T2", orderid="O2")])
    assert [f["tradeid"] for f in stored()] == ["T1", "T2"]


def test_a_later_snapshot_with_fewer_fills_never_shrinks_the_file(snap):
    run, stored, _ = snap
    run([_fill("T1"), _fill("T2", orderid="O2")])
    run([])  # e.g. the tradebook came back empty after a broker-side reset
    assert [f["tradeid"] for f in stored()] == ["T1", "T2"]


def test_the_first_seen_copy_of_a_fill_is_kept(snap):
    run, stored, _ = snap
    run([_fill("T1", price=800.0)])
    run([_fill("T1", price=801.0)])
    assert stored()[0]["average_price"] == 800.0


def test_a_fill_repeated_within_one_response_is_kept_once(snap):
    run, stored, _ = snap
    run([_fill("T1"), _fill("T1"), _fill("T2", orderid="O2")])
    assert [f["tradeid"] for f in stored()] == ["T1", "T2"]


def test_fills_without_a_trade_id_are_merged_on_their_other_fields(snap):
    run, stored, _ = snap
    run([_fill("", orderid="O1"), _fill("", orderid="O2")])
    run([_fill("", orderid="O1"), _fill("", orderid="O2"), _fill("", orderid="O3")])
    assert [f["orderid"] for f in stored()] == ["O1", "O2", "O3"]


def test_partial_fills_of_one_order_are_all_kept(snap):
    """Legitimate fills: one order filled in three parts has three trade ids."""
    run, stored, _ = snap
    run([_fill("T1", orderid="O1", qty=4), _fill("T2", orderid="O1", qty=3), _fill("T3", orderid="O1", qty=3)])
    run([_fill("T1", orderid="O1", qty=4), _fill("T2", orderid="O1", qty=3), _fill("T3", orderid="O1", qty=3)])
    assert [(f["tradeid"], f["quantity"]) for f in stored()] == [("T1", 4), ("T2", 3), ("T3", 3)]


def test_the_fallback_key_keeps_fills_that_differ_in_any_field(snap):
    """Without a trade id, two fills of one order a second apart are two fills, not one."""
    run, stored, _ = snap
    run([_fill("", orderid="O1", ts="10:05:01"), _fill("", orderid="O1", ts="10:05:02"),
         _fill("", orderid="O1", ts="10:05:02", qty=5)])
    assert len(stored()) == 3


def test_a_past_day_is_refused_and_no_file_is_touched(snap, capsys):
    run, stored, tmp = snap
    run([_fill("T1")])
    before = sorted(os.listdir(tmp))
    assert run([_fill("T9")], day=dt.date(2026, 10, 21)) is None
    assert sorted(os.listdir(tmp)) == before
    assert [f["tradeid"] for f in stored()] == ["T1"]
    assert "only today's fills" in capsys.readouterr().out


def test_the_file_is_named_for_the_ist_date(snap):
    run, _stored, tmp = snap
    path = run([_fill("T1")])
    assert os.path.basename(path) == "tradebook_2026-10-22.json"


def test_an_unreadable_existing_file_is_kept_aside_not_overwritten(snap):
    run, stored, tmp = snap
    (tmp / "tradebook_2026-10-22.json").write_text("{not json", encoding="utf-8")
    run([_fill("T1")])
    assert [f["tradeid"] for f in stored()] == ["T1"]
    kept = [n for n in os.listdir(tmp) if n.startswith("tradebook_2026-10-22.json.unreadable")]
    assert len(kept) == 1 and (tmp / kept[0]).read_text(encoding="utf-8") == "{not json"


def test_a_failed_tradebook_call_writes_nothing(snap, monkeypatch):
    run, stored, tmp = snap
    run([_fill("T1")])

    async def _broken():
        raise RuntimeError("tradebook call failed: HTTP 500")

    monkeypatch.setattr(analysis, "_fetch_tradebook", _broken)
    with pytest.raises(RuntimeError):
        analysis.snapshot()
    assert [f["tradeid"] for f in stored()] == ["T1"]


def test_eod_sh_snapshots_the_day_it_reviews():
    script = os.path.join(os.path.dirname(analysis.__file__), "eod.sh")
    with open(script, encoding="utf-8") as fh:
        text = fh.read()
    assert re.search(r'-m signal_engine\.analysis --snapshot --day "\$DAY"', text)


def test_a_past_day_with_a_saved_snapshot_returns_it_without_fetching(snap, monkeypatch):
    """A re-run for a reviewed day reuses that day's file; the broker is not asked."""
    run, stored, tmp = snap
    past = dt.date(2026, 10, 21)
    (tmp / "tradebook_2026-10-21.json").write_text(json.dumps([_fill("P1")]), encoding="utf-8")

    async def _must_not_fetch():
        raise AssertionError("the broker tradebook was fetched for a past day")

    monkeypatch.setattr(analysis, "_fetch_tradebook", _must_not_fetch)
    path = analysis.snapshot(past)
    assert os.path.basename(path) == "tradebook_2026-10-21.json"
    assert [f["tradeid"] for f in stored(past)] == ["P1"]


def _cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["signal_engine.analysis", *argv])
    analysis.main()


def test_the_cli_fails_for_a_past_day_with_no_saved_snapshot(snap, monkeypatch):
    """eod.sh reads a non-zero exit as a missing snapshot and says so in the report."""
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, "--snapshot", "--day", "2026-10-21")
    assert exc.value.code != 0


def test_the_cli_succeeds_for_a_past_day_with_a_saved_snapshot(snap, monkeypatch):
    _run, _stored, tmp = snap
    (tmp / "tradebook_2026-10-21.json").write_text(json.dumps([_fill("P1")]), encoding="utf-8")
    _cli(monkeypatch, "--snapshot", "--day", "2026-10-21")  # returns, no SystemExit


def test_fills_dated_another_ist_day_are_not_saved_as_todays(snap, capsys):
    run, stored, _ = snap
    yesterday = dict(_fill("Y1"), timestamp="2026-10-21 15:10:00")
    run([yesterday, _fill("T1")])
    assert [f["tradeid"] for f in stored()] == ["T1"]
    assert "1 fill(s) dated another day" in capsys.readouterr().out


def test_the_flattrade_tradebook_time_format_is_read(snap):
    """Noren's norentm is 'HH:MM:SS DD-MM-YYYY', IST wall clock."""
    run, stored, _ = snap
    today = dict(_fill("F1"), timestamp="10:05:01 22-10-2026")
    yesterday = dict(_fill("F0", orderid="O0"), timestamp="15:10:00 21-10-2026")
    run([today, yesterday])
    assert [f["tradeid"] for f in stored()] == ["F1"]


def _set_aside(tmp, day=TODAY):
    path = tmp / f"unverified_tradebook_{day.isoformat()}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


@pytest.mark.parametrize("stamp", ["", "22/10/2026 10:05", None])
def test_a_fill_without_a_readable_time_is_set_aside_not_saved_as_todays(snap, stamp, capsys):
    """A fill that cannot be dated may be another day's: it never enters today's snapshot."""
    run, stored, tmp = snap
    odd = dict(_fill("U1"), timestamp=stamp)
    if stamp is None:
        del odd["timestamp"]
    with pytest.raises(analysis.UnverifiedFills) as exc:
        run([odd, _fill("T1", orderid="O2")])
    assert [f["tradeid"] for f in stored()] == ["T1"]          # today's file is still written
    assert [f["tradeid"] for f in _set_aside(tmp)] == ["U1"]
    assert exc.value.count == 1
    assert "1 fill(s) with no readable time" in capsys.readouterr().out


def test_set_aside_fills_are_never_read_back_as_a_days_fills(snap):
    run, _stored, _tmp = snap
    with pytest.raises(analysis.UnverifiedFills):
        run([dict(_fill("U1"), timestamp=""), _fill("T1", orderid="O2")])
    assert [f["tradeid"] for f in analysis.load_snapshots()] == ["T1"]


def test_set_aside_fills_are_merged_without_duplicates(snap):
    run, _stored, tmp = snap
    for _ in range(2):
        with pytest.raises(analysis.UnverifiedFills):
            run([dict(_fill("U1"), timestamp="")])
    assert [f["tradeid"] for f in _set_aside(tmp)] == ["U1"]


def test_fills_dated_another_day_are_neither_saved_nor_set_aside(snap):
    run, stored, tmp = snap
    run([dict(_fill("Y1"), timestamp="2026-10-21 15:10:00"), _fill("T1")])
    assert [f["tradeid"] for f in stored()] == ["T1"]
    assert _set_aside(tmp) == []


def test_the_cli_exits_3_when_fills_were_set_aside(snap, monkeypatch):
    run, stored, _tmp = snap

    async def _fetch():
        return [dict(_fill("U1"), timestamp=""), _fill("T1", orderid="O2")]

    monkeypatch.setattr(analysis, "_fetch_tradebook", _fetch)
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, "--snapshot")
    assert exc.value.code == 3
    assert [f["tradeid"] for f in stored()] == ["T1"]


def test_eod_sh_reports_set_aside_fills_and_fails_the_run():
    script = os.path.join(os.path.dirname(analysis.__file__), "eod.sh")
    with open(script, encoding="utf-8") as fh:
        text = fh.read()
    assert re.search(r'snapshot_rc" -eq 3', text)
    assert "unverified_tradebook_" in text
    assert re.search(r'\[ "\$snapshot_rc" -eq 0 \] \|\| exit 2', text)
