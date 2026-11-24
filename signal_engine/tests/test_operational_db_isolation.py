"""No test may be able to reach an operational file under signal_engine/data/.

signal_engine.main builds RiskStore(RISK_DB_PATH) at import and its risk engine defaults to
mode "live", so a test that imports main and reaches an unpatched record_trade() or
record_close() writes a PORTFOLIO/live row for today into the operational risk.db. This file
pins that the suite cannot reach the real files, whichever test runs first.
"""

import os

from signal_engine import db, main, risk_store
from signal_engine.pipeline.state import RunnerStateStore

DATA_DIR = os.path.realpath(os.path.join(os.path.dirname(risk_store.__file__), "data"))


def _inside_data_dir(path: str) -> bool:
    real = os.path.realpath(path)
    return real == DATA_DIR or real.startswith(DATA_DIR + os.sep)


def _main_file(conn) -> str:
    rows = conn.execute("PRAGMA database_list").fetchall()
    return next(row[2] for row in rows if row[1] == "main")


def test_risk_db_path_is_not_the_operational_file():
    assert not _inside_data_dir(risk_store.RISK_DB_PATH), risk_store.RISK_DB_PATH


def test_main_risk_store_writes_to_a_file_outside_the_data_dir():
    # PRAGMA database_list names the file the connection really opened, whatever the path
    # variable says now.
    real_file = _main_file(main._risk_store._conn)
    assert real_file, "risk store is not file backed"
    assert not _inside_data_dir(real_file), real_file


def test_trades_db_path_is_not_the_operational_file():
    assert not _inside_data_dir(db._DB_PATH), db._DB_PATH


def test_runner_state_default_path_is_not_the_operational_file(tmp_path):
    store = RunnerStateStore()
    try:
        real_file = _main_file(store._conn)
        assert not _inside_data_dir(real_file), real_file
    finally:
        store.close()


def test_small_state_files_are_not_the_operational_files():
    from signal_engine.bugs import store as bug_store
    from signal_engine.shadow import store as shadow_store

    package_dir = os.path.dirname(DATA_DIR)
    real_bugs_file = os.path.realpath(os.path.join(package_dir, "bugs", "bugs.json"))
    assert not _inside_data_dir(str(shadow_store.DATA_DIR))
    assert os.path.realpath(bug_store.TRACKER_PATH) != real_bugs_file
