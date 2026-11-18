"""Stage 2 migration: creates the derived tables, idempotent, matches the ORM models.

Uses a temp SQLite file only (never the production DB).
Run with: uv run pytest test/test_migrate_discretionary_book.py -q
"""

import importlib.util
import os
import sqlite3

from database import discretionary_book_db as book

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "upgrade")


def _load():
    spec = importlib.util.spec_from_file_location(
        "migrate_discretionary_book", os.path.join(_PATH, "migrate_discretionary_book.py")
    )
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.path.insert(0, _PATH)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(_PATH)
    return mod


def _schema(path):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        con.close()


def test_migration_creates_tables_and_second_run_changes_nothing(tmp_path):
    mod = _load()
    db = str(tmp_path / "m.db")
    assert mod.run(db) == 0
    first = _schema(db)
    assert {n for t, n, _ in first if t == "table"} == set(mod.TABLES)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO disc_build_meta VALUES ('b1', '2026-11-17 10:00:00', 'f', 3, '1')")
    con.commit()
    con.close()
    assert mod.run(db) == 0
    assert _schema(db) == first
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM disc_build_meta").fetchone()[0] == 1
    con.close()


def test_migration_columns_match_the_orm_models(tmp_path):
    mod = _load()
    db = str(tmp_path / "m.db")
    mod.run(db)
    con = sqlite3.connect(db)
    try:
        for table in book.Base.metadata.sorted_tables:
            migrated = {
                r[1]: (r[2], bool(r[3])) for r in con.execute(f"PRAGMA table_info({table.name})")
            }
            model = {c.name: (str(c.type), not c.nullable) for c in table.columns}
            assert set(migrated) == set(model), table.name
            for name, (_ctype, notnull) in model.items():
                if not table.columns[name].primary_key:
                    assert migrated[name][1] == notnull, (table.name, name)
    finally:
        con.close()


def test_status_only_changes_nothing(tmp_path):
    mod = _load()
    db = str(tmp_path / "m.db")
    sqlite3.connect(db).close()
    assert mod.run(db, status_only=True) == 0
    assert _schema(db) == []


def test_registered_in_migrate_all():
    text = open(os.path.join(_PATH, "migrate_all.py"), encoding="utf-8").read()
    assert text.count("migrate_discretionary_book.py") == 1
    assert text.index("migrate_discretionary_ledger.py") < text.index(
        "migrate_discretionary_book.py"
    )
