"""Two first requests on pooled threads must create the discretionary book tables once.

_ensure_tables() checked a module flag and then ran create_all, with nothing between the
check and the write. Under gthread two threads both pass the check and both run
create_all; on a fresh SQLite file the second CREATE TABLE fails as "already exists".
"""

import threading
import time

from services import discretionary_pnl_service as pnl


def test_concurrent_first_calls_initialise_the_book_tables_once(monkeypatch):
    calls = []

    def slow_init():
        calls.append(threading.get_ident())
        time.sleep(0.2)  # the window a real CREATE TABLE gives the other thread

    monkeypatch.setattr(pnl.book, "init_discretionary_book_db", slow_init)
    monkeypatch.setattr(pnl, "_tables_ready", False)

    n = 8
    barrier = threading.Barrier(n)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=10)
            pnl._ensure_tables()
        except Exception as exc:  # noqa: BLE001 - reported through the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert len(calls) == 1
    assert pnl._tables_ready is True
