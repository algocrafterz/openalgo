"""
Regression test: pruning with nothing to prune must not hold a write lock.

`_prune_old_tags()` runs a bulk DELETE inside `init_strategy_book_db()`. When
nothing matches (the common case - the 30-day retention window rarely has
anything to prune), the delete still opens a write transaction on this
module's scoped session. Skipping the commit on that 0-row path left the
transaction open indefinitely, holding SQLite's write lock and blocking any
other engine pointed at the same file - including, in production, the very
next `record_order_tag`/`apply_fill` call on a slow trading day, and in the
test suite, any other test module's DB engine that happened to run next.

Run with: uv run pytest test/test_strategy_book_prune_lock.py -v
"""

from sqlalchemy import create_engine, text

from database.strategy_book_db import _prune_old_tags, engine, init_strategy_book_db


def test_prune_with_nothing_to_remove_does_not_hold_the_write_lock():
    init_strategy_book_db()
    _prune_old_tags()  # Typically a no-op: nothing is past the retention window.

    # A second, independent engine on the same file must be able to write
    # immediately - proves _prune_old_tags() released its transaction rather
    # than leaving it open on the shared scoped session. Built from the
    # module's own bound URL, not os.environ["DATABASE_URL"]: some unrelated
    # test modules reassign that env var at collection time with no teardown,
    # so by the time this test runs it may no longer match what this engine
    # is actually pointed at.
    other_engine = create_engine(
        engine.url, connect_args={"check_same_thread": False, "timeout": 1}
    )
    try:
        with other_engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS _lock_probe (id INTEGER)"))
            conn.execute(text("INSERT INTO _lock_probe (id) VALUES (1)"))
    finally:
        with other_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS _lock_probe"))
        other_engine.dispose()
