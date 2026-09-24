"""Safe environment defaults for tests collected outside an installation.

Database URLs are assigned, not defaulted. setdefault() would let an exported
DATABASE_URL or SANDBOX_DATABASE_URL win, so a developer or CI box with the
production values in its environment would run this suite against the real
databases - resetting funds and creating orders in live sandbox state. The
credentials below are still setdefault(), since those are only placeholders.
"""

import os

os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "test-only-app-key")

# Assigned unconditionally: test isolation must not be overridable from the
# environment.
os.environ["DATABASE_URL"] = "sqlite:///db/openalgo-test.db"
os.environ["SANDBOX_DATABASE_URL"] = "sqlite:///db/sandbox-test.db"
os.environ["LOGS_DATABASE_URL"] = "sqlite:///db/logs-test.db"
os.environ["LATENCY_DATABASE_URL"] = "sqlite:///db/latency-test.db"

# utils.logging calls setup_logging() at import time and always attaches a JSON
# handler on $LOG_DIR/errors.jsonl, so every error a test deliberately provokes
# was appended to the operator's production log -- the file CLAUDE.md names as
# the first place to look when debugging. Worse, setup_logging truncates that
# file to its last 1000 lines on startup, so a test run could evict real errors.
os.environ["LOG_DIR"] = "log/test"

# database/strategy_book_db.py (and similar modules) bind a module-level engine
# to DATABASE_URL the first time they are imported. A handful of test modules
# assign os.environ["DATABASE_URL"] = "sqlite:///:memory:" at bare module scope
# with no teardown (e.g. test_orphaned_apikey.py, test_python_strategy_edge_cases.py) -
# if pytest collects one of those before anything first imports strategy_book_db,
# its engine binds to that throwaway in-memory database instead of the real test
# database, and every write silently fails against a connection with no schema.
# Importing it here, immediately after the env vars above are set, guarantees it
# binds correctly before any other test module's collection-time side effects run.
import database.strategy_book_db  # noqa: E402,F401
