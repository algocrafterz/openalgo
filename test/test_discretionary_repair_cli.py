"""Repair CLI guards and eligibility (upgrade/repair_discretionary_accounts.py).

Temp SQLite files only; no production database.
Run with: uv run pytest test/test_discretionary_repair_cli.py -q
"""

import hashlib
import importlib.util
import pathlib
import sqlite3
import stat

import pytest
from sqlalchemy import create_engine

from database import discretionary_ledger_db as ledger
from services import discretionary_ledger_service as svc

UPGRADE = pathlib.Path(svc.__file__).resolve().parent.parent / "upgrade"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "repair_cli_guard", UPGRADE / "repair_discretionary_accounts.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'unused.db'}")  # restored after
    orig = ledger.db_session.session_factory.kw.get("bind"), ledger.engine
    yield mod
    ledger.db_session.remove()
    ledger.db_session.configure(bind=orig[0])
    ledger.engine = orig[1]


def make_db(path, logins=(("alice", "flattrade"),), fill_accounts=("FZ0074",), runs=3):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE auth (id INTEGER PRIMARY KEY, name TEXT, broker TEXT)")
    for name, broker in logins:
        con.execute("INSERT INTO auth (name, broker) VALUES (?, ?)", (name, broker))
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{path}")
    ledger.Base.metadata.tables["ledger_sync_runs"].create(eng)
    ledger.Base.metadata.tables["ledger_fills"].create(eng)
    eng.dispose()
    con = sqlite3.connect(path)
    for name, _b in logins:
        for i in range(runs):
            con.execute(
                "INSERT INTO ledger_sync_runs (broker, account, started_at, status, fetched,"
                " inserted) VALUES ('flattrade', ?, ?, 'ok', 0, 0)",
                (name, f"2026-11-18 08:3{i}:00"),
            )
    for n, acct in enumerate(fill_accounts):
        con.execute(
            "INSERT INTO ledger_fills (broker, account, order_id, symbol, exchange, product,"
            " side, qty, price, fill_time, occurrence_idx, dedup_key, attribution_state,"
            " synced_at) VALUES ('flattrade', ?, 'O', 'TCS', 'NSE', 'MIS', 'BUY', 1, 1,"
            " '2026-11-18 09:46:18', 0, ?, 'unresolved', '2026-11-18 10:00:00')",
            (acct, f"k{n}"),
        )
    con.commit()
    con.close()


def labels(path):
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT account FROM ledger_sync_runs ORDER BY id")]
    finally:
        con.close()


def digest(path, table):
    con = sqlite3.connect(path)
    try:
        rows = con.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    finally:
        con.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def backups(path):
    return sorted(path.parent.glob(path.name + ".bak-*"))


def test_apply_without_yes_writes_nothing_and_exits_nonzero(tmp_path, cli, capsys):
    db = tmp_path / "a.db"
    make_db(db)
    assert cli.main(["--db", str(db), "--apply"]) == 1
    out = capsys.readouterr().out
    assert "not written" in out and "--apply --yes" in out and "runs_reported=3" in out
    assert labels(db) == ["alice"] * 3
    assert backups(db) == []
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert "ledger_account_identity" not in tables


def test_option_abbreviation_is_rejected(tmp_path, cli):
    db = tmp_path / "a.db"
    make_db(db)
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", str(db), "--app", "--yes"])
    assert e.value.code == 2
    assert labels(db) == ["alice"] * 3 and backups(db) == []


def test_apply_yes_makes_a_restorable_backup_and_prints_the_restore_command(tmp_path, cli, capsys):
    db = tmp_path / "a.db"
    make_db(db)
    fills_before = digest(db, "ledger_fills")
    original = db.read_bytes()
    assert cli.main(["--db", str(db), "--apply", "--yes"]) == 0
    out = capsys.readouterr().out
    bk = backups(db)
    assert len(bk) == 1 and f"backup: {bk[0]}" in out
    assert "restore (stop the app first): rm -f" in out and "cp -p" in out
    assert "changed: broker=flattrade al***ce -> FZ***74 runs=3" in out
    assert "alice" not in out and "FZ0074" not in out  # masked
    assert labels(db) == ["FZ0074"] * 3
    assert digest(db, "ledger_fills") == fills_before
    # the backup holds the pre-repair data and restores it
    assert labels(bk[0]) == ["alice"] * 3
    db.write_bytes(bk[0].read_bytes())
    assert labels(db) == ["alice"] * 3
    assert original  # the original file existed before the repair
    # idempotent
    assert cli.main(["--db", str(db)]) == 0
    assert "runs_reported=3" in capsys.readouterr().out  # restored state is repairable again


def test_second_apply_after_repair_finds_nothing(tmp_path, cli, capsys):
    db = tmp_path / "a.db"
    make_db(db)
    assert cli.main(["--db", str(db), "--apply", "--yes"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", str(db)]) == 0
    assert "runs_reported=0 runs_written=0" in capsys.readouterr().out


def test_two_logins_on_one_broker_are_skipped_as_ambiguous_login(tmp_path, cli, capsys):
    db = tmp_path / "a.db"
    make_db(db, logins=(("alice", "flattrade"), ("bob", "flattrade")))
    before = labels(db)
    assert cli.main(["--db", str(db), "--apply", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "status=ambiguous_login" in out and "ambiguous_login=2" in out
    assert "reason=ambiguous_login" in out and "runs_written=0" in out
    assert labels(db) == before
    assert "from=al***ce" in out and "from=alice" not in out  # long names are masked


def test_service_two_logins_one_with_fills_is_not_relabelled(tmp_path):
    db = tmp_path / "a.db"
    make_db(db, logins=(("alice", "flattrade"), ("bob", "flattrade")))
    eng = create_engine(f"sqlite:///{db}")
    ledger.Base.metadata.tables["ledger_account_identity"].create(eng)
    ledger.db_session.remove()
    ledger.db_session.configure(bind=eng)
    try:
        out = svc.normalize_run_accounts(
            dry_run=False, auth_names={"alice": "flattrade", "bob": "flattrade"}
        )
    finally:
        ledger.db_session.remove()
    assert {i["status"] for i in out["items"]} == {"ambiguous_login"}
    assert (out["written"], out["reported"], out["ambiguous_login"]) == (0, 0, 2)
    assert labels(db) == ["alice"] * 3 + ["bob"] * 3
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM ledger_account_identity").fetchone()[0] == 0
    con.close()


def test_other_broker_login_does_not_make_a_broker_ambiguous(tmp_path, cli, capsys):
    db = tmp_path / "a.db"
    make_db(db, logins=(("alice", "flattrade"), ("zed", "zerodha")), runs=1)
    assert cli.main(["--db", str(db), "--apply", "--yes"]) == 0
    assert "status=relabel" in capsys.readouterr().out
    assert labels(db)[0] == "FZ0074"


def test_unmigrated_database_gives_a_clean_error(tmp_path, cli, capsys):
    db = tmp_path / "a.db"
    sqlite3.connect(db).close()
    assert cli.main(["--db", str(db), "--apply", "--yes"]) == 3
    out = capsys.readouterr().out
    assert "database not usable" in out and "missing table(s)" in out and "Traceback" not in out
    assert backups(db) == []


def test_not_a_database_gives_a_clean_error(tmp_path, cli, capsys):
    db = tmp_path / "a.db"
    db.write_bytes(b"this is not sqlite" * 50)
    assert cli.main(["--db", str(db)]) == 3
    assert "Nothing was changed" in capsys.readouterr().out


def test_missing_file_exits_2(tmp_path, cli, capsys):
    assert cli.main(["--db", str(tmp_path / "nope.db")]) == 2
    assert "no such database" in capsys.readouterr().out


def test_locked_database_refuses_to_write_cleanly(tmp_path, cli, capsys, monkeypatch):
    db = tmp_path / "a.db"
    make_db(db)
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3, "connect", lambda p, timeout=5, **kw: real_connect(p, timeout=0.05, **kw)
    )
    holder = real_connect(db)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert cli.main(["--db", str(db), "--apply", "--yes"]) == 4
    finally:
        holder.rollback()
        holder.close()
    out = capsys.readouterr().out
    assert "refusing to write" in out and "Nothing was changed" in out
    assert "Traceback" not in out
    assert labels(db) == ["alice"] * 3 and backups(db) == []


def test_backup_failure_refuses_to_write(tmp_path, cli, capsys, monkeypatch):
    db = tmp_path / "a.db"
    make_db(db)

    def boom(path):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "_backup", boom)
    assert cli.main(["--db", str(db), "--apply", "--yes"]) == 4
    assert "refusing to write" in capsys.readouterr().out
    assert labels(db) == ["alice"] * 3


def test_dry_run_is_the_default_and_never_touches_the_file(tmp_path, cli, capsys):
    db = tmp_path / "a.db"
    make_db(db)
    before = db.read_bytes()
    assert cli.main(["--db", str(db)]) == 0
    assert db.read_bytes() == before and backups(db) == []
    assert "dry-run: broker=flattrade" in capsys.readouterr().out
    assert stat.S_ISREG(db.stat().st_mode)
