#!/usr/bin/env python
"""
Repair discretionary ledger sync-run account labels (dry run by default).

Older ledger_sync_runs rows may carry the OpenAlgo user name as their account label instead
of the broker account id. This relabels them to the one account the broker reported on its
fills (see services.discretionary_ledger_service.normalize_run_accounts) and stores the
login's canonical account in ledger_account_identity. ledger_fills is never touched.

Eligibility is per broker and per login: a broker with two or more logins (two Auth names)
is skipped and reported as ambiguous_login; so is a broker with two or more fill accounts.
Idempotent. Each broker item is written in its own transaction.

Guards: writing needs BOTH --apply and --yes (--apply alone prints what would change and
exits 1). Option abbreviations are rejected. Before any write a backup is made with the
sqlite3 backup API to '<db>.bak-<YYYYmmdd-HHMMSS>'; no backup (or a locked DB) -> no write.
The exact restore command is printed.

Exit codes: 0 ok, 1 --apply without --yes, 2 no such database, 3 unusable database
(missing/unmigrated tables, not a database, locked), 4 backup failed or database locked,
5 write failed (restore from the backup).

Usage:
    cd upgrade
    uv run repair_discretionary_accounts.py --db /path/to/openalgo.db                  # dry run
    uv run repair_discretionary_accounts.py --db /path/to/openalgo.db --apply --yes    # write

Stop the app before writing. Output masks account ids and user names.
"""

import argparse
import os
import shlex
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REQUIRED_TABLES = ("auth", "ledger_sync_runs", "ledger_fills")


def _mask(value) -> str:
    v = str(value or "")
    return v if len(v) <= 4 else f"{v[:2]}***{v[-2:]}"


def _uri(db_path: str, mode: str) -> str:
    return "file:" + os.path.abspath(db_path).replace("?", "%3f") + f"?mode={mode}"


def _read_state(db_path: str) -> dict[str, str]:
    """{Auth name: broker}; raises sqlite3.Error / LookupError with a clean reason."""
    con = sqlite3.connect(_uri(db_path, "ro"), uri=True, timeout=10)
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in REQUIRED_TABLES if t not in have]
        if missing:
            raise LookupError("missing table(s): " + ", ".join(missing))
        return {r[0]: (r[1] or "") for r in con.execute("SELECT name, broker FROM auth") if r[0]}
    finally:
        con.close()


def _backup(db_path: str) -> str:
    """Copy the DB with the sqlite3 backup API; returns the backup path. Raises on failure."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = f"{os.path.abspath(db_path)}.bak-{stamp}"
    if os.path.exists(dest):
        raise FileExistsError("backup path exists")
    src = sqlite3.connect(_uri(db_path, "ro"), uri=True, timeout=10)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        dst.commit()
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("backup integrity check failed")
    except Exception:
        dst.close()
        src.close()
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    dst.close()
    src.close()
    return dest


def _writable_now(db_path: str) -> None:
    """Raise sqlite3.OperationalError when another process holds a write lock."""
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
    finally:
        con.close()


def _restore_command(db_path: str, backup: str) -> str:
    db = os.path.abspath(db_path)
    return (
        f"rm -f {shlex.quote(db + '-wal')} {shlex.quote(db + '-shm')} && "
        f"cp -p {shlex.quote(backup)} {shlex.quote(db)}"
    )


def _print_items(mode: str, out: dict) -> None:
    for item in out["items"]:
        print(
            f"{mode}: broker={item['broker']} from={_mask(item['from'])} "
            f"to={_mask(item['to']) if item['to'] else '-'} runs={item['runs']} "
            f"status={item['status']}"
        )
    print(
        f"{mode}: runs_reported={out['reported']} runs_written={out['written']} "
        f"ambiguous_login={out.get('ambiguous_login', 0)}"
    )
    for item in out["items"]:
        if item["status"] == "ambiguous_login":
            print(
                f"{mode}: SKIPPED broker={item['broker']} from={_mask(item['from'])} "
                f"reason=ambiguous_login (broker has more than one login, or the run label is "
                f"not its login); not repaired"
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Relabel user-name accounts in ledger_sync_runs", allow_abbrev=False
    )
    ap.add_argument("--db", required=True, help="path of the SQLite DB holding ledger and auth")
    ap.add_argument("--apply", action="store_true", help="write (needs --yes; default: dry run)")
    ap.add_argument("--yes", action="store_true", help="confirm the write requested by --apply")
    args = ap.parse_args(argv)
    db_path = os.path.abspath(args.db)
    if not os.path.isfile(db_path):
        print(f"no such database: {db_path}")
        return 2
    try:
        logins = _read_state(db_path)
    except LookupError as e:
        print(f"database not usable ({e}); run the migrations first. Nothing was changed.")
        return 3
    except sqlite3.Error as e:
        print(f"database not readable ({type(e).__name__}): {e}. Nothing was changed.")
        return 3

    url = f"sqlite:///{db_path}"
    os.environ["DATABASE_URL"] = url

    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from database import discretionary_ledger_db as ledger
    from services import discretionary_ledger_service as svc

    # Bind explicitly: never rely on whatever DATABASE_URL an imported module resolved.
    eng = create_engine(url, poolclass=NullPool, connect_args={"check_same_thread": False})
    ledger.db_session.remove()
    ledger.db_session.configure(bind=eng)
    ledger.engine = eng

    write = args.apply and args.yes
    backup = None
    if write:
        try:
            _writable_now(db_path)
            backup = _backup(db_path)
        except (sqlite3.Error, OSError) as e:
            print(
                f"refusing to write: backup failed or database locked ({type(e).__name__}); "
                "stop the app and retry. Nothing was changed."
            )
            return 4
        print(f"backup: {backup}")
        print(f"restore (stop the app first): {_restore_command(db_path, backup)}")
        try:
            # the identity table may predate the migration; created only now, after the backup
            ledger.LedgerAccountIdentity.__table__.create(bind=eng, checkfirst=True)
        except Exception as e:
            print(f"write failed ({type(e).__name__}); restore from the backup. See above.")
            return 5

    try:
        out = svc.normalize_run_accounts(dry_run=not write, auth_names=logins)
    except Exception as e:
        print(
            f"{'write' if write else 'read'} failed ({type(e).__name__}): "
            f"{'some broker items may be written; restore from the backup' if write else 'nothing changed'}"
        )
        return 5 if write else 3
    mode = "apply" if write else "dry-run"
    _print_items(mode, out)
    if write:
        for item in out["items"]:
            if item["status"] == "relabel":
                print(
                    f"changed: broker={item['broker']} {_mask(item['from'])} -> "
                    f"{_mask(item['to'])} runs={item['runs']}"
                )
        print(f"summary: runs_written={out['written']} backup={backup}")
        print(f"restore (stop the app first): {_restore_command(db_path, backup)}")
        return 0
    if args.apply:
        print(
            f"not written: --apply needs --yes as well. {out['reported']} run(s) would be "
            "relabelled. Re-run with --apply --yes to write."
        )
        return 1
    if args.yes:
        print("note: --yes has no effect without --apply; this was a dry run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
