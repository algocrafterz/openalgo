"""Backfill must extend per exchange, find interior gaps, repair only missing sessions, and
report a store that is not current.

The scheduled run only extended each symbol forward from its last bar, and keyed that on the
symbol alone. NIFTY (stored under NSE_INDEX) was therefore requested under NSE every day and
stayed stale for months, and a session missing in the middle of a symbol's history was never
looked at again.
"""
import datetime as dt

import duckdb
import pandas as pd
import pytest

from signal_engine.backtest import backfill as bf

D = dt.date


@pytest.fixture(autouse=True)
def _no_live_broker(monkeypatch):
    """The tests never read the machine's real OpenAlgo session (`repair_gaps` builds its
    token-liveness canary from it); a test that needs another answer patches it again."""
    monkeypatch.setattr(bf, "_broker_session", lambda *a, **k: ("t", None, "flattrade"))


def _weekdays(start, end):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _epoch(day, hh=10, mm=0):
    ist = pd.Timestamp(day.isoformat() + f" {hh:02d}:{mm:02d}", tz="Asia/Kolkata")
    return int(ist.timestamp())


@pytest.fixture
def store(tmp_path):
    path = str(tmp_path / "h.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE market_data (symbol VARCHAR, exchange VARCHAR, interval VARCHAR, "
                "timestamp BIGINT, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
                "volume BIGINT)")
    con.close()

    def put(symbol, exchange, days, interval="1m"):
        c = duckdb.connect(path)
        for day in days:
            c.execute("INSERT INTO market_data VALUES (?,?,?,?,1,1,1,1,1)",
                      [symbol, exchange, interval, _epoch(day)])
        c.close()

    return path, put


def test_coverage_keeps_the_two_exchanges_of_one_symbol_apart(store):
    path, put = store
    put("NIFTY", "NSE_INDEX", [D(2026, 2, 27)])
    put("NIFTY", "NSE", [D(2026, 10, 19)])
    cov = bf.coverage("1m", path).set_index("exchange")
    assert set(cov.index) == {"NSE_INDEX", "NSE"}
    assert cov.loc["NSE_INDEX", "last"] == D(2026, 2, 27)


def test_extend_requests_each_symbol_under_its_own_exchange(monkeypatch):
    today = pd.Timestamp.now(tz="Asia/Kolkata").normalize().date()
    cov = pd.DataFrame([
        {"symbol": "NIFTY", "exchange": "NSE_INDEX", "bars": 1, "first": today,
         "last": today - dt.timedelta(days=9), "stale_days": 9},
        {"symbol": "SBIN", "exchange": "NSE", "bars": 1, "first": today, "last": today,
         "stale_days": 0}])
    calls = []
    monkeypatch.setattr(bf, "coverage", lambda *a, **k: cov)
    monkeypatch.setattr(bf, "backfill",
                        lambda syms, start, **k: calls.append((syms[0], k.get("exchange"))) or [])
    bf.extend_existing("1m")
    assert calls == [("NIFTY", "NSE_INDEX")]


def test_find_gaps_reports_only_sessions_inside_the_symbols_own_span(store):
    path, put = store
    sessions = _weekdays(D(2026, 10, 5), D(2026, 10, 16))
    present = [s for s in sessions if s not in (D(2026, 10, 7), D(2026, 10, 8), D(2026, 10, 13))]
    put("AAA", "NSE", present)
    # a calendar that expects sessions both before AAA's first bar and after its last one
    def expected(a, b):
        return _weekdays(max(a, D(2026, 10, 1)), min(b, D(2026, 10, 23)))

    gaps = bf.find_gaps("1m", expected=expected, historify_db=path, today=D(2026, 10, 19))
    assert gaps == {("AAA", "NSE"): [(D(2026, 10, 7), D(2026, 10, 8)),
                                     (D(2026, 10, 13), D(2026, 10, 13))]}


def test_find_gaps_ignores_sessions_older_than_the_broker_depth(store):
    path, put = store
    # 2026-01-06 is missing but far older than the depth; the last 30 days are complete
    put("AAA", "NSE", [D(2026, 1, 5), D(2026, 1, 7)] + _weekdays(D(2026, 9, 19), D(2026, 10, 19)))
    expected = _weekdays
    gaps = bf.find_gaps("1m", expected=expected, historify_db=path, today=D(2026, 10, 19),
                        depth_days=30)
    assert gaps == {}


def test_a_long_gap_is_split_so_no_request_exceeds_the_chunk(store):
    path, put = store
    first, last = D(2026, 3, 2), D(2026, 6, 30)
    put("AAA", "NSE", [first, last])
    expected = _weekdays
    ranges = bf.find_gaps("1m", expected=expected, historify_db=path,
                          today=D(2026, 7, 1))[("AAA", "NSE")]
    assert all((b - a).days + 1 <= bf.CHUNK_DAYS for a, b in ranges)
    covered = [d for a, b in ranges for d in _weekdays(a, b)]
    assert covered == _weekdays(D(2026, 3, 3), D(2026, 6, 29))


def test_repair_stores_only_the_missing_sessions(monkeypatch):
    # The broker answers a range with more than was asked for (Flattrade daily appends today's
    # bar). The extra day already exists in the store and must not be rewritten.
    wanted, extra = D(2026, 10, 7), D(2026, 10, 19)
    rows = [{"timestamp": _epoch(wanted), "open": 1, "high": 1, "low": 1, "close": 1,
             "volume": 5},
            {"timestamp": _epoch(extra), "open": 9, "high": 9, "low": 9, "close": 9,
             "volume": 9}]
    monkeypatch.setattr("services.history_service.get_history_with_auth",
                        lambda *a, **k: (True, {"data": rows}, 200))
    stored = []
    monkeypatch.setattr("database.historify_db.upsert_market_data",
                        lambda df, *a: stored.append(df) or len(df))
    res = bf.backfill(["AAA"], "2026-10-07", "2026-10-07", interval="D", pause=0,
                      session=("t", None, "flattrade"), keep_dates={wanted})
    assert res[0].stored == 1
    assert list(stored[0]["timestamp"]) == [_epoch(wanted)]


def test_repair_gaps_asks_for_each_gap_range_under_its_exchange(monkeypatch):
    gaps = {("NIFTY", "NSE_INDEX"): [(D(2026, 10, 7), D(2026, 10, 8))]}
    monkeypatch.setattr(bf, "find_gaps", lambda *a, **k: gaps)
    calls = []

    def fake_backfill(syms, start, end, **kw):
        calls.append((syms, start, end, kw["exchange"], kw["keep_dates"]))
        return [bf.SymbolResult(symbol=syms[0], stored=2)]

    monkeypatch.setattr(bf, "backfill", fake_backfill)
    monkeypatch.setattr(bf, "_expected_sessions", lambda: (lambda a, b: _weekdays(a, b)))
    bf.repair_gaps("1m")
    assert calls == [(["NIFTY"], "2026-10-07", "2026-10-08", "NSE_INDEX",
                      {D(2026, 10, 7), D(2026, 10, 8)})]


def test_repair_gaps_stops_when_nothing_comes_back_for_a_long_run(monkeypatch):
    gaps = {(f"S{i}", "NSE"): [(D(2026, 10, 7), D(2026, 10, 7))] for i in range(10)}
    monkeypatch.setattr(bf, "find_gaps", lambda *a, **k: gaps)
    monkeypatch.setattr(bf, "backfill", lambda syms, *a, **k: [bf.SymbolResult(symbol=syms[0])])
    monkeypatch.setattr(bf, "_expected_sessions", lambda: (lambda a, b: _weekdays(a, b)))
    with pytest.raises(RuntimeError, match="dead/expired session token"):
        bf.repair_gaps("1m", max_empty_streak=5, canary=lambda: False)


def test_a_streak_of_empty_answers_is_not_a_dead_token_when_the_canary_answers(monkeypatch):
    # Flattrade does not serve the oldest 1m gaps at all (measured 2026-10-19: the 60 oldest
    # gap requests came back empty with a valid token), so an empty streak alone proves nothing.
    gaps = {(f"S{i}", "NSE"): [(D(2026, 10, 7), D(2026, 10, 7))] for i in range(10)}
    monkeypatch.setattr(bf, "find_gaps", lambda *a, **k: gaps)
    asked = []
    monkeypatch.setattr(bf, "backfill", lambda syms, *a, **k: asked.append(syms[0]) or
                        [bf.SymbolResult(symbol=syms[0])])
    monkeypatch.setattr(bf, "_expected_sessions", lambda: _weekdays)
    out = bf.repair_gaps("1m", max_empty_streak=3, canary=lambda: True)
    assert len(asked) == 10 and len(out) == 10


def test_repair_gaps_does_the_oldest_gaps_first_and_honours_the_request_cap(monkeypatch):
    # The oldest sessions are the ones about to fall out of the broker's depth, and the session
    # token dies at the 16:00 AutoStop, so a capped run must spend its requests there first.
    gaps = {("NEW", "NSE"): [(D(2026, 10, 14), D(2026, 10, 14))],
            ("OLD", "NSE"): [(D(2025, 11, 3), D(2025, 11, 3)), (D(2026, 1, 7), D(2026, 1, 7))]}
    monkeypatch.setattr(bf, "find_gaps", lambda *a, **k: gaps)
    asked = []
    monkeypatch.setattr(bf, "backfill",
                        lambda syms, start, end, **k: asked.append((syms[0], start)) or
                        [bf.SymbolResult(symbol=syms[0], stored=1)])
    monkeypatch.setattr(bf, "_expected_sessions", lambda: _weekdays)
    bf.repair_gaps("1m", max_requests=2)
    assert asked == [("OLD", "2025-11-03"), ("OLD", "2026-01-07")]


def test_verify_lists_symbols_behind_the_last_expected_session(store):
    path, put = store
    put("OK", "NSE", [D(2026, 10, 16), D(2026, 10, 19)])
    put("BEHIND", "NSE", [D(2026, 9, 29)])
    put("NIFTY", "NSE_INDEX", [D(2026, 2, 27)])
    bad = bf.verify_current("1m", expected_last=D(2026, 10, 19), historify_db=path)
    assert sorted(bad.symbol) == ["BEHIND", "NIFTY"]
    tolerated = bf.verify_current("1m", expected_last=D(2026, 10, 19), historify_db=path,
                                  tolerate={"BEHIND"})
    assert list(tolerated.symbol) == ["NIFTY"]


def test_verify_reports_a_symbol_the_interval_has_never_stored(store):
    """verify only looked at symbols already in that interval's store, so one that was never
    backfilled for it (M&M had 1m history and no daily bars at all) was invisible to it."""
    path, put = store
    put("OK", "NSE", [D(2026, 10, 19)], interval="1m")
    put("OK", "NSE", [D(2026, 10, 19)], interval="D")
    put("M&M", "NSE", [D(2026, 10, 19)], interval="1m")
    bad = bf.verify_current("D", expected_last=D(2026, 10, 19), historify_db=path)
    assert list(bad.symbol) == ["M&M"]
    assert bf.verify_current("D", expected_last=D(2026, 10, 19), historify_db=path,
                             tolerate={"M&M"}).empty


def test_expected_last_session_waits_for_the_close_and_skips_holidays():
    def expected(a, b):
        return [s for s in _weekdays(a, b) if s != D(2026, 10, 2)]

    def ist(text):
        return pd.Timestamp(text, tz="Asia/Kolkata")

    # before the cash close the day's session is not complete yet
    assert bf.expected_last_session(expected, ist("2026-10-19 12:00")) == D(2026, 10, 16)
    assert bf.expected_last_session(expected, ist("2026-10-19 15:45")) == D(2026, 10, 19)
    # the Saturday after a Friday session is still that Friday
    assert bf.expected_last_session(expected, ist("2026-10-17 10:00")) == D(2026, 10, 16)
    # a holiday is not expected
    assert bf.expected_last_session(expected, ist("2026-10-02 18:00")) == D(2026, 10, 1)


def test_bars_without_a_positive_price_are_never_stored(monkeypatch):
    # Flattrade's daily path appends a zero-filled "today" row for an instrument it cannot price
    # (COCHINSHIP: 9 all-zero daily rows were stored from 2026-09-13). Such a row is not data.
    good = {"timestamp": _epoch(D(2026, 10, 7)), "open": 5, "high": 6, "low": 4, "close": 5,
            "volume": 10}
    zero = {"timestamp": _epoch(D(2026, 10, 8)), "open": 0, "high": 0, "low": 0, "close": 0,
            "volume": 0}
    monkeypatch.setattr("services.history_service.get_history_with_auth",
                        lambda *a, **k: (True, {"data": [good, zero]}, 200))
    stored = []
    monkeypatch.setattr("database.historify_db.upsert_market_data",
                        lambda df, *a: stored.append(df) or len(df))
    res = bf.backfill(["AAA"], "2026-10-07", "2026-10-08", interval="D", pause=0,
                      session=("t", None, "flattrade"))
    assert list(stored[0]["timestamp"]) == [good["timestamp"]]
    assert (res[0].stored, res[0].rejected) == (1, 1)


def test_a_dry_run_does_not_need_a_broker_session(monkeypatch):
    # After the 16:00 AutoStop the session is revoked; the plan must still be inspectable.
    def no_session(*a, **k):
        raise RuntimeError("no active broker session - log in to OpenAlgo first")

    monkeypatch.setattr(bf, "_broker_session", no_session)
    res = bf.backfill(["AAA"], "2026-10-07", "2026-10-08", interval="1m", dry_run=True)
    assert res[0].chunks_ok == 1 and res[0].stored == 0


# ---------------------------------------------------------------- calendar coverage ends

def _calendar_ended(monkeypatch):
    from signal_engine.backtest.data_readiness.calendar import CalendarError

    def ended():
        def expected(a, b):
            raise CalendarError("2027-01-04 is outside the calendar coverage 2016-2026")
        return expected

    monkeypatch.setattr(bf, "_expected_sessions", ended)


@pytest.mark.parametrize("flag", ["--verify", "--repair-gaps"])
def test_a_calendar_that_does_not_cover_today_stops_verify_and_repair_with_an_explanation(
        monkeypatch, capsys, flag, store, tmp_path):
    # main() reads the relative default db/historify.duckdb; without this the test opened the
    # machine's real store, and failed where none exists (a fresh worktree).
    import shutil

    path, put = store
    put("OK", "NSE", [D(2026, 10, 19)])
    (tmp_path / "db").mkdir()
    shutil.copy(path, tmp_path / "db" / "historify.duckdb")
    monkeypatch.chdir(tmp_path)
    _calendar_ended(monkeypatch)
    requested = []
    monkeypatch.setattr(bf, "backfill", lambda *a, **k: requested.append(a) or [])
    monkeypatch.setattr("sys.argv", ["backfill", "--interval", "1m", flag])
    with pytest.raises(SystemExit) as stop:
        bf.main()
    out = capsys.readouterr().out
    assert stop.value.code == bf.EXIT_CALENDAR_ENDED == 3
    assert "official NSE" in out and "nse_calendar.yaml" in out
    assert "no sessions were guessed" in out and "2027-01-04" in out
    assert requested == []                      # no broker request, no false gap repair


# ---- daily extend (2026-10-23) -----------------------------------------------------------------
# With min_stale=2 the 15:35 run skipped every symbol that was only one day stale, so the bulk fetch
# ran every other trading day and the runner's warm-up lacked the previous session on the mornings in
# between (2026-10-14 PFC traded on 10-12's value area). --daily targets the NSE calendar's session:
# today once the cash market has closed.

def _ist(ts):
    return pd.Timestamp(ts, tz="Asia/Kolkata")


def test_daily_target_is_today_once_the_cash_market_has_closed():
    expected = bf._expected_sessions()
    assert bf.daily_target(expected, _ist("2026-10-22 15:35")) == D(2026, 10, 22)
    assert bf.daily_target(expected, _ist("2026-10-22 15:29")) == D(2026, 10, 21)


def test_daily_target_on_a_holiday_and_a_weekend_is_the_last_session():
    expected = bf._expected_sessions()
    assert bf.daily_target(expected, _ist("2026-10-20 15:35")) == D(2026, 10, 19)  # NSE holiday
    assert bf.daily_target(expected, _ist("2026-10-24 15:35")) == D(2026, 10, 23)  # Saturday


def test_extend_through_a_session_fetches_every_symbol_behind_it(store, monkeypatch):
    path, put = store
    put("BEHIND", "NSE", [D(2026, 10, 21)])
    put("CURRENT", "NSE", [D(2026, 10, 22)])
    calls = []
    monkeypatch.setattr(bf, "backfill", lambda syms, start, **k: calls.append((syms[0], start)) or [])
    bf.extend_existing("1m", through=D(2026, 10, 22), historify_db=path)
    assert calls == [("BEHIND", "2026-10-20")]  # one day before its last bar, through today


def test_extend_through_on_a_holiday_fetches_nothing_new(store, monkeypatch):
    path, put = store
    put("A", "NSE", [D(2026, 10, 19)])
    calls = []
    monkeypatch.setattr(bf, "backfill", lambda syms, start, **k: calls.append(syms[0]) or [])
    bf.extend_existing("1m", through=D(2026, 10, 19), historify_db=path)
    assert calls == []


def _run_main(monkeypatch, argv, now):
    monkeypatch.setattr(bf, "_now", lambda: _ist(now))
    monkeypatch.setattr("sys.argv", ["backfill", *argv])
    bf.main()


def test_the_daily_flag_extends_through_todays_session(monkeypatch):
    seen = {}
    monkeypatch.setattr(bf, "extend_existing", lambda interval, **k: seen.update(k, interval=interval) or [])
    _run_main(monkeypatch, ["--interval", "1m", "--daily"], "2026-10-22 15:35")
    assert seen["interval"] == "1m" and seen["through"] == D(2026, 10, 22)


def test_daily_verify_flags_a_symbol_whose_fetch_failed_today(store, monkeypatch, tmp_path):
    import shutil

    path, put = store
    put("FETCHED", "NSE", [D(2026, 10, 22)])
    put("FAILED", "NSE", [D(2026, 10, 21)])
    (tmp_path / "db").mkdir()
    shutil.copy(path, tmp_path / "db" / "historify.duckdb")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bf, "find_gaps", lambda *a, **k: {})
    with pytest.raises(SystemExit) as stop:
        _run_main(monkeypatch, ["--interval", "1m", "--verify", "--daily"], "2026-10-22 15:36")
    assert stop.value.code == 1


def test_without_a_calendar_daily_extend_falls_back_to_anything_not_updated_today(monkeypatch, capsys):
    _calendar_ended(monkeypatch)
    seen = {}
    monkeypatch.setattr(bf, "extend_existing", lambda interval, **k: seen.update(k) or [])
    _run_main(monkeypatch, ["--interval", "1m", "--daily"], "2027-01-04 15:35")
    assert seen.get("min_stale") == 1 and "through" not in seen
    assert "calendar" in capsys.readouterr().out

