"""The open-day check of the price history the engine warms up from (readiness._price_history).

Every test builds its own small DuckDB store under tmp_path. The real store is never opened, and
nothing here writes outside tmp_path.
"""

import dataclasses
import hashlib
import inspect
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from signal_engine import config
from signal_engine.ops import automation, model, readiness, verdict
from signal_engine.ops.model import FAIL, IST, OK, WARN, Check, read_phase
from signal_engine.tests.test_ops_recovery import _calendar

duckdb = pytest.importorskip("duckdb")
pytestmark = pytest.mark.filterwarnings("ignore")

REAL_HISTORY_SYMBOLS = readiness._history_symbols
TODAY = "2026-11-13"  # Friday
PREV = "2026-11-12"  # Thursday
STOCKS = ["AAA", "BBB", "CCC", "DDD"]
FULL, CUT_AT_1454, EARLY = (
    375,
    340,
    100,
)  # 1-minute bars from 09:15: a full day, one that stops at 14:54, one that stops at 10:54


def epoch(day, clock="09:15:00"):
    return int(datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=IST).timestamp())


def make_store(path, sessions):
    """sessions: {symbol: [(day, number of 1-minute bars from 09:15)]}. Never the real store."""
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE market_data (symbol VARCHAR NOT NULL, exchange VARCHAR NOT NULL, "
        "interval VARCHAR NOT NULL, timestamp BIGINT NOT NULL, open DOUBLE, high DOUBLE, "
        "low DOUBLE, close DOUBLE, volume BIGINT, PRIMARY KEY (symbol, exchange, interval, timestamp))"
    )
    for symbol, days in sessions.items():
        for day, bars in days:
            start = epoch(day)
            con.execute(
                "INSERT INTO market_data SELECT ?, 'NSE', '1m', start + 60 * i, 1, 1, 1, 1, 1 "
                "FROM (SELECT ?::BIGINT AS start, unnest(range(?)) AS i)",
                [symbol, start, bars],
            )
    con.close()
    return str(path)


def fresh_sessions(symbols=STOCKS, last=PREV, days=("2026-11-10", "2026-11-11", "2026-11-12")):
    shown = [d for d in days if d <= last]
    return {s: [(d, FULL) for d in shown] for s in symbols}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A calendar with no holiday, the stock list STOCKS and a store path in tmp_path."""
    monkeypatch.setattr(automation, "DB", _calendar(tmp_path, []))
    monkeypatch.setattr(readiness, "_history_symbols", lambda live: (list(STOCKS), False, ""))
    monkeypatch.setattr(readiness, "HISTORY_LOCK_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(readiness, "HISTORY_LOCK_POLL_SECONDS", 0.2)
    return tmp_path


def check_of(env, sessions, day=TODAY):
    path = make_store(env / "historify.duckdb", sessions)
    return readiness._price_history(day, None, path=path)


# ---- the verdicts ---------------------------------------------------------------------------------


def test_every_stock_fresh_is_ok(env):
    check = check_of(env, fresh_sessions())
    assert check.status == OK and check.name == "price history for today"
    assert "all 4 stocks" in check.evidence and PREV in check.evidence


def test_one_stale_stock_is_a_fail_zero_tolerance_with_cause_and_next_action(env):
    sessions = fresh_sessions()
    sessions["CCC"] = [("2026-11-10", FULL), ("2026-11-11", FULL)]  # a day behind
    check = check_of(env, sessions)
    assert check.status == FAIL
    assert "1 of 4 stocks" in check.evidence and "CCC (last bar 2026-11-11)" in check.evidence
    assert "older than 2026-11-12" in check.evidence
    assert "./signal_engine/backtest/backfill_cron.sh by hand" in check.evidence
    assert "restart the engine" in check.evidence


def test_all_stale_is_a_fail(env):
    check = check_of(env, fresh_sessions(last="2026-11-10"))
    assert check.status == FAIL and "4 of 4 stocks" in check.evidence


def test_a_stock_with_no_bars_at_all_is_a_fail_there_is_no_halted_exception(env):
    sessions = fresh_sessions(symbols=["AAA", "BBB", "CCC"])  # DDD is not in the store
    check = check_of(env, sessions)
    assert check.status == FAIL and "DDD (no bars at all)" in check.evidence


def test_a_stock_whose_last_bar_is_older_than_the_look_back_is_named_with_its_date(env):
    sessions = fresh_sessions()
    sessions["DDD"] = [("2026-09-01", FULL)]  # far behind: outside the 10-day window
    check = check_of(env, sessions)
    assert check.status == FAIL and "DDD (last bar 2026-09-01)" in check.evidence


@pytest.mark.parametrize("bars", [EARLY, CUT_AT_1454])
def test_a_previous_session_cut_off_early_is_a_fail_like_the_engines_own_test(env, bars):
    sessions = fresh_sessions()
    sessions["BBB"][-1] = (PREV, bars)
    check = check_of(env, sessions)
    assert check.status == FAIL
    assert "1 of 4 stocks have an incomplete session" in check.evidence
    assert f"BBB ({bars} bars on {PREV})" in check.evidence


def test_a_missing_previous_session_with_older_bars_is_stale_not_ok(env):
    sessions = fresh_sessions()
    sessions["AAA"] = [("2026-11-10", FULL), ("2026-11-11", FULL)]
    assert check_of(env, sessions).status == FAIL


def test_many_names_are_shortened(env, monkeypatch):
    names = [f"S{i:02d}" for i in range(20)]
    monkeypatch.setattr(readiness, "_history_symbols", lambda live: (names, False, ""))
    check = check_of(env, fresh_sessions(symbols=names, last="2026-11-10"))
    assert check.status == FAIL and "and 14 more" in check.evidence


def test_the_store_missing_is_a_fail(env):
    check = readiness._price_history(TODAY, None, path=str(env / "absent.duckdb"))
    assert check.status == FAIL
    assert check.evidence.startswith("The price history could not be read")
    assert "does not exist" in check.evidence and "backfill_cron.sh by hand" in check.evidence


def test_an_unreadable_store_is_a_fail_that_names_no_exception(env):
    path = env / "historify.duckdb"
    path.write_bytes(b"this is not a database" * 100)
    check = readiness._price_history(TODAY, None, path=str(path))
    assert check.status == FAIL and check.evidence.startswith("The price history could not be read")
    assert "damaged" in check.evidence and "false alarm" not in check.evidence


def test_a_store_without_the_table_is_unreadable_not_ok(env):
    path = env / "historify.duckdb"
    duckdb.connect(str(path)).close()
    assert readiness._price_history(TODAY, None, path=str(path)).status == FAIL


def holder(path, seconds):
    """Another process that holds the store read-write, as OpenAlgo does while it works."""
    code = (
        "import duckdb, sys, time\n"
        "con = duckdb.connect(sys.argv[1])\n"
        "print('held', flush=True)\n"
        "time.sleep(float(sys.argv[2]))\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(path), str(seconds)], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_a_lock_that_ends_inside_the_wait_is_ok(env, monkeypatch):
    path = make_store(env / "historify.duckdb", fresh_sessions())
    monkeypatch.setattr(readiness, "HISTORY_LOCK_WAIT_SECONDS", 20.0)
    proc = holder(path, 1.5)
    try:
        started = time.monotonic()
        check = readiness._price_history(TODAY, None, path=path)
        waited = time.monotonic() - started
    finally:
        proc.kill()
        proc.wait()
    assert check.status == OK and 1.0 < waited < 15.0  # it did wait for the lock to end


def test_a_lock_that_never_ends_is_a_fail_that_says_it_can_be_a_false_alarm(env):
    path = make_store(env / "historify.duckdb", fresh_sessions())
    proc = holder(path, 60)
    try:
        started = time.monotonic()
        check = readiness._price_history(TODAY, None, path=path)
        waited = time.monotonic() - started
    finally:
        proc.kill()
        proc.wait()
    assert check.status == FAIL and 0.9 < waited < 10.0  # bounded by HISTORY_LOCK_WAIT_SECONDS
    assert check.evidence.startswith("The price history could not be read")
    assert "false alarm" in check.evidence and "Next:" in check.evidence


def test_a_read_that_is_cut_off_is_tried_once_more(env, monkeypatch):
    path = make_store(env / "historify.duckdb", fresh_sessions())
    real, calls = readiness._history_read, []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("cut off")
        return real(*args)

    monkeypatch.setattr(readiness, "_history_read", flaky)
    assert readiness._price_history(TODAY, None, path=path).status == OK and len(calls) == 2


def test_a_read_cut_off_twice_is_a_fail(env, monkeypatch):
    path = make_store(env / "historify.duckdb", fresh_sessions())
    calls = []

    def stuck(*args):
        calls.append(1)
        raise TimeoutError("cut off")

    monkeypatch.setattr(readiness, "_history_read", stuck)
    check = readiness._price_history(TODAY, None, path=path)
    assert check.status == FAIL and len(calls) == 2
    assert check.evidence.startswith("The price history could not be read")


def test_an_unreadable_holiday_table_is_a_fail_because_the_previous_day_is_unknown(
    env, monkeypatch
):
    monkeypatch.setattr(automation, "DB", str(env / "no-such-calendar.db"))
    path = make_store(env / "historify.duckdb", fresh_sessions())
    check = readiness._price_history(TODAY, None, path=path)
    assert check.status == FAIL and "holiday table" in check.evidence and "Next:" in check.evidence


# ---- the previous trading day ---------------------------------------------------------------------


def test_on_a_monday_the_previous_trading_day_is_the_friday(env):
    stale = check_of(
        env, fresh_sessions(last="2026-11-12", days=("2026-11-12", "2026-11-13")), "2026-11-16"
    )
    assert (
        stale.status == FAIL and "older than 2026-11-13" in stale.evidence
    )  # Thursday is not enough
    (env / "historify.duckdb").unlink()
    fresh = check_of(
        env, fresh_sessions(last="2026-11-13", days=("2026-11-12", "2026-11-13")), "2026-11-16"
    )
    assert fresh.status == OK and "2026-11-13" in fresh.evidence


def test_a_holiday_is_skipped_when_the_previous_day_is_chosen(env, monkeypatch):
    monkeypatch.setattr(automation, "DB", _calendar(env, ["2026-11-10"], "holiday.db"))
    days = ("2026-11-06", "2026-11-09")
    fresh = check_of(env, fresh_sessions(last="2026-11-09", days=days), "2026-11-11")
    assert fresh.status == OK and "2026-11-09" in fresh.evidence  # Tuesday 11-10 was a holiday
    (env / "historify.duckdb").unlink()
    stale = check_of(env, fresh_sessions(last="2026-11-06", days=days), "2026-11-11")
    assert stale.status == FAIL and "older than 2026-11-09" in stale.evidence


def test_a_run_on_a_weekend_looks_for_the_friday(env):
    check = check_of(env, fresh_sessions(last="2026-11-13", days=("2026-11-13",)), "2026-11-14")
    assert check.status == OK and "2026-11-13" in check.evidence


# ---- the required set -----------------------------------------------------------------------------


def real_runner():
    return config._runner_fields(config._load_yaml())["runner"]


def test_the_required_set_is_what_the_engine_warms_up_the_runner_list_and_each_strategy_list():
    runner = real_runner()
    symbols, everything, why = readiness._history_symbols(False)
    assert not why and not everything
    wanted = set(runner.universe)
    for strategy in runner.strategies.values():
        wanted |= set(strategy.universe or ())
    assert set(symbols) == wanted and len(symbols) == len(wanted)  # no duplicates
    own = [s for s in runner.strategies.values() if s.universe]
    assert own and set(own[0].universe) <= set(symbols)  # a strategy list is part of the set


def test_an_unknown_mode_keeps_every_strategy_and_live_drops_the_paper_only_ones():
    assert readiness._history_symbols(None) == readiness._history_symbols(False)
    runner = real_runner()
    live = readiness._history_symbols(True)
    if all(s.paper_only for s in runner.strategies.values()):
        assert live == (
            [],
            False,
            "no Python strategy runs in live mode, so no price history is needed",
        )


def test_a_switched_off_runner_needs_no_price_history(monkeypatch):
    off = dataclasses.replace(real_runner(), enabled=False)
    monkeypatch.setattr(config, "_runner_fields", lambda yml: {"runner": off})
    assert readiness._history_symbols(None)[2].startswith("the Python runner is switched off")


def test_an_unreadable_config_means_the_required_set_cannot_be_determined_and_is_a_fail(
    env, monkeypatch
):
    monkeypatch.setattr(readiness, "_history_symbols", REAL_HISTORY_SYMBOLS)  # not the fixture's

    def broken():
        raise config.ConfigError("config.yaml is empty or invalid")

    monkeypatch.setattr(config, "_load_yaml", broken)
    path = make_store(env / "historify.duckdb", fresh_sessions())
    check = readiness._price_history(TODAY, None, path=path)
    assert (
        check.status == FAIL
        and "could not be read from signal_engine/config.yaml" in check.evidence
    )
    assert "ConfigError" not in check.evidence and "Next:" in check.evidence


def test_a_runner_that_does_not_need_the_store_is_ok_even_when_the_store_is_missing(
    env, monkeypatch
):
    monkeypatch.setattr(
        readiness, "_history_symbols", lambda live: ([], False, "no price history is needed")
    )
    check = readiness._price_history(TODAY, None, path=str(env / "absent.duckdb"))
    assert check.status == OK and "no price history is needed" in check.evidence


def test_the_all_stocks_universe_reads_every_stock_in_the_store(env, monkeypatch):
    monkeypatch.setattr(readiness, "_history_symbols", lambda live: (["ZZZ"], True, ""))
    sessions = fresh_sessions(symbols=["AAA", "BBB"])
    sessions["OLD"] = [("2026-11-10", FULL)]
    check = check_of(env, sessions)
    assert check.status == FAIL
    assert (
        "OLD (last bar 2026-11-10)" in check.evidence and "ZZZ (no bars at all)" in check.evidence
    )
    assert "2 of 4 stocks" in check.evidence


def test_the_all_stocks_universe_with_an_empty_store_is_a_fail(env, monkeypatch):
    monkeypatch.setattr(readiness, "_history_symbols", lambda live: ([], True, ""))
    check = check_of(env, {})
    assert check.status == FAIL and "no 1-minute bars" in check.evidence


def test_live_mode_is_read_from_the_trading_mode_check():
    assert readiness._is_live([Check("trading mode", WARN, "LIVE: real orders today")]) is True
    assert readiness._is_live([Check("trading mode", OK, "analyze (sandbox)")]) is False
    assert readiness._is_live([Check("broker session (funds API)", FAIL, "x")]) is None


# ---- read-only, no write, no other check changes --------------------------------------------------


def test_the_check_never_writes_the_store_and_opens_it_only_through_the_read_only_helper(env):
    path = make_store(env / "historify.duckdb", fresh_sessions())
    before = (hashlib.sha256(Path(path).read_bytes()).hexdigest(), os.stat(path).st_mtime_ns)
    readiness._price_history(TODAY, None, path=path)
    after = (hashlib.sha256(Path(path).read_bytes()).hexdigest(), os.stat(path).st_mtime_ns)
    assert before == after
    source = inspect.getsource(readiness._history_read)
    assert "backfill._connect_read_only" in source and "duckdb.connect" not in source
    for word in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "COPY"):
        assert word not in source


def test_the_check_is_independent_of_the_previous_day_check(env, monkeypatch):
    """No read of a close-day status file, no call of _yesterday_close, no use of REPORTS."""
    source = inspect.getsource(readiness._price_history).split('"""')[2]  # the code, not the doc
    for word in ("_yesterday_close", "status.tsv", "REPORTS", "recovery.outstanding"):
        assert word not in source
    path = make_store(env / "historify.duckdb", fresh_sessions(last="2026-11-10"))
    for rows in (None, "backfill\t15:36:00\t15:36:09\t0\n", "backfill\t15:36:00\t15:36:09\t1\n"):
        folder = env / "reports"
        folder.mkdir(exist_ok=True)
        status = folder / f"close-day-{PREV}-status.tsv"
        status.unlink(missing_ok=True)
        if rows:
            status.write_text("step\tstarted\tended\texit\n" + rows)
        monkeypatch.setattr(readiness, "REPORTS", str(folder))
        assert readiness._price_history(TODAY, None, path=path).status == FAIL  # same each time


def run_open_day(env, monkeypatch, backfill_exit, sessions):
    """open_day with every other check healthy; yesterday's close and the price history are real."""
    folder = env / "reports"
    folder.mkdir(exist_ok=True)
    monkeypatch.setattr(readiness, "REPORTS", str(folder))
    rows = "".join(
        f"{n}\t15:36:00\t15:36:09\t{c}\n" for n, c in (("eod", 0), ("backfill", backfill_exit))
    )
    (folder / f"close-day-{PREV}-status.tsv").write_text("step\tstarted\tended\texit\n" + rows)
    path = make_store(env / "historify.duckdb", sessions)
    monkeypatch.setattr(readiness, "_historify_path", lambda: path)
    healthy = {
        "_services": Check("services (openalgoctl status)", OK, "running"),
        "_health": Check("OpenAlgo health", OK, "pass"),
        "_engine_startup": Check("engine startup", OK, "ok"),
        "_poller_started": Check("BreakingTrade poller", OK, "ok"),
        "_clock": Check("clock (NTP)", OK, "yes"),
        "_overnight_errors": Check("errors since the last close", OK, "none"),
        "_calendar_coverage": Check("NSE calendar coverage", OK, "x"),
    }
    for name, check in healthy.items():
        monkeypatch.setattr(readiness, name, lambda *a, _c=check, **k: _c)
    lists = {
        "_broker_and_mode": [
            Check("broker session (funds API)", OK, "c"),
            Check("trading mode", OK, "analyze"),
        ],
        "_ports": [Check("websocket proxy :8765", OK, "up")],
        "_disk_and_databases": [Check("disk", OK, "100 GB")],
        "_morning_tasks": [Check("task openAlgoAutoStart", OK, "ran")],
    }
    for name, value in lists.items():
        monkeypatch.setattr(readiness, name, lambda *a, _v=value, **k: list(_v))
    captured = {}
    monkeypatch.setattr(
        readiness, "write_phase", lambda phase, day, checks, summary: captured.update(c=checks)
    )
    readiness.open_day(TODAY)
    checks = captured["c"]
    return {c.name: c.status for c in checks}, model.verdict(checks), checks


def test_a_failed_backfill_step_with_a_fresh_store_is_amber_not_red(env, monkeypatch):
    by_name, result, _ = run_open_day(env, monkeypatch, 1, fresh_sessions())
    assert by_name[f"close-day {PREV}"] == WARN  # the existing WARN, unchanged
    assert by_name["price history for today"] == OK
    assert result == "AMBER"


def test_a_successful_backfill_step_with_a_stale_store_is_red(env, monkeypatch):
    by_name, result, _ = run_open_day(env, monkeypatch, 0, fresh_sessions(last="2026-11-11"))
    assert by_name[f"close-day {PREV}"] == OK  # the backfill step says success
    assert by_name["price history for today"] == FAIL
    assert result == "RED"


def test_a_failed_backfill_step_and_a_stale_store_are_red_with_both_findings(env, monkeypatch):
    by_name, result, _ = run_open_day(env, monkeypatch, 1, fresh_sessions(last="2026-11-11"))
    assert by_name[f"close-day {PREV}"] == WARN and by_name["price history for today"] == FAIL
    assert result == "RED"


def test_the_check_is_in_the_open_day_phase_once_and_changes_no_other_check(env, monkeypatch):
    by_name, _, checks = run_open_day(env, monkeypatch, 0, fresh_sessions())
    assert [c.name for c in checks].count("price history for today") == 1
    assert [c.name for c in checks] == [
        "services (openalgoctl status)",
        "OpenAlgo health",
        "broker session (funds API)",
        "trading mode",
        "engine startup",
        "price history for today",
        "BreakingTrade poller",
        "websocket proxy :8765",
        "clock (NTP)",
        "disk",
        "errors since the last close",
        "task openAlgoAutoStart",
        f"close-day {PREV}",
        "NSE calendar coverage",
    ]


def test_a_warn_elsewhere_never_softens_the_price_history_fail(env, monkeypatch):
    monkeypatch.setattr(readiness, "_overnight_errors", lambda day: Check("errors", WARN, "many"))
    by_name, result, _ = run_open_day(env, monkeypatch, 1, fresh_sessions(last="2026-11-11"))
    assert by_name["price history for today"] == FAIL and result == "RED"


def test_the_red_message_and_the_report_name_the_check_in_plain_words(env, monkeypatch):
    _, _, checks = run_open_day(env, monkeypatch, 0, fresh_sessions(last="2026-11-11"))
    folder = str(env / "phase")
    model.write_phase("open-day", TODAY, checks, "Is everything ready?", folder=folder)
    text = verdict.message("open-day", TODAY, folder)
    assert (
        text == f"open-day {TODAY}: RED. Failed: price history for today. Details: /logs/workflow"
    )
    row = next(
        c for c in read_phase("open-day", TODAY, folder=folder)["checks"] if c["status"] == FAIL
    )
    assert row["plain"].startswith("The price history the engine starts from is old")
    report = Path(folder, f"open-day-{TODAY}.md").read_text()
    assert "The engine leaves these stocks out of today's trading" in report
    assert "backfill_cron.sh by hand" in report


def test_the_trader_text_has_no_status_code_exception_or_protocol_word(env):
    sessions = fresh_sessions()
    sessions["AAA"] = [("2026-11-10", FULL)]
    texts = [check_of(env, sessions).evidence]
    texts.append(readiness._price_history(TODAY, None, path=str(env / "absent.duckdb")).evidence)
    bad = env / "bad.duckdb"
    bad.write_bytes(b"x" * 4096)
    texts.append(readiness._price_history(TODAY, None, path=str(bad)).evidence)
    for text in texts:
        assert "Next:" in text
        for word in ("Exception", "Error", "Traceback", "IOException", "HTTP", "errno", "duckdb."):
            assert word not in text, (word, text)
        assert not any(ch in text for ch in "\u2014\u2013")


def test_the_historify_path_follows_the_engines_environment_variable(monkeypatch):
    monkeypatch.delenv("HISTORIFY_DB", raising=False)
    assert readiness._historify_path() == os.path.join(
        readiness.logs.REPO, "db", "historify.duckdb"
    )
    monkeypatch.setenv("HISTORIFY_DB", "/tmp/x/h.duckdb")
    assert readiness._historify_path() == "/tmp/x/h.duckdb"
    monkeypatch.setenv("HISTORIFY_DB", "db/other.duckdb")
    assert readiness._historify_path() == os.path.join(readiness.logs.REPO, "db", "other.duckdb")
