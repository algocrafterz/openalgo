"""Safe environment defaults for tests collected outside an installation.

Database URLs are assigned, not defaulted. setdefault() would let an exported
DATABASE_URL or SANDBOX_DATABASE_URL win, so a developer or CI box with the
production values in its environment would run this suite against the real
databases - resetting funds and creating orders in live sandbox state. The
credentials below are still setdefault(), since those are only placeholders.
"""

import os

import pytest

os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "test-only-app-key")

# Keep LiteLLM's model catalogue the same on every machine.
#
# LiteLLM fetches its newest model list from GitHub when it is imported, unless
# this is set. A model added upstream with no capability flags then failed
# agent tests that read the catalogue (test_agent_chatgpt_wiring and
# test_agent_chatgpt_oauth), on some days and on some machines only. The
# bundled list is what the tests were written against. Production is not
# affected: this is confined to the test harness.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# Neutralise dotenv before anything can call it.
#
# utils/config.py runs load_dotenv(override=True) at import, which re-reads the
# operator's .env and overwrites the assignments below. Whether that happens
# depends purely on which module a given test imports first, so the suite wrote
# to the isolated databases on some runs and to the real ones on others -- the
# Flow QA tests putting seven workflows into the operator's Flow Editor, needing
# manual deletion. Disabling the loader here is confined to the test harness and
# makes the isolation below hold whatever the import order turns out to be.
import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False
dotenv.main.load_dotenv = dotenv.load_dotenv

# Bind ``openalgo`` to the installed SDK before pytest sets up the repository
# root. The root carries an ``__init__.py``, so pytest imports it as a package
# named ``openalgo`` during setup and puts its parent directory first on
# sys.path. A module that first imported ``openalgo.ta`` after that point got
# the repository instead of the SDK, and every agent toolkit built on it was
# silently skipped, so whether a test saw the market tools depended on which
# test had happened to import the SDK first. Production never has the parent
# directory on sys.path; this is confined to the test harness.
import openalgo  # noqa: E402,F401

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

# These are manual diagnostics, not pytest modules. test_bot_web.py starts the
# Telegram bot from its module body. The WebSocket scripts require a live proxy,
# operator API key and timed terminal interaction; their ``test_*`` helpers take
# ordinary arguments rather than fixtures.
#
# Keep the scripts runnable directly while preventing collection from starting
# external services or misclassifying their function parameters as fixtures.
collect_ignore = [
    "test_bot_web.py",
    "test_websocket.py",
    "test_websocket_service.py",
]


# The OpenScript state files, kept out of the operator's own.
#
# Three files under strategies/ say what each deployment runs on, which
# deployments a trader means to have running, and what has been asked of a run
# that is still going. They are the same class of thing as the databases and the
# log above: a test that writes one is writing a trader's own state.
#
# This is not hypothetical. The runner blueprint restores schedules and runs at
# import, which reads the running file, and a run recorded there whose process
# has gone is started again or dropped. A suite run on a machine with live
# strategies cleared the record of every one of them, so the next restart of the
# real application brought none of them back.
#
# Reassigned rather than configured, because these are not settings: nothing an
# operator installs should have a knob for where a trader's strategies live.
def _isolate_openscript_state() -> None:
    from pathlib import Path

    here = Path("strategies") / "test"
    here.mkdir(parents=True, exist_ok=True)

    from services import openscript_commands, openscript_run_config, openscript_running

    openscript_run_config.CONFIG_FILE = here / "openscript_run_configs.json"
    openscript_running.STATE_FILE = here / "openscript_running.json"
    openscript_commands.COMMAND_FILE = here / "openscript_commands.json"

    import blueprints.openscript_runner as runner_blueprint

    runner_blueprint.SCHEDULES_FILE = here / "openscript_runner_schedules.json"


_isolate_openscript_state()


# A fake sleep for one module, never for the whole interpreter.
#
# ``monkeypatch.setattr(module.time, "sleep", fake)`` looks local but is not:
# ``module.time`` is the one shared ``time`` module, so every thread in the
# process gets the fake. The suite shares one interpreter with threads earlier
# tests left running, and chartink's order loop idles on ``time.sleep(0.1)``.
# Given a recorder, that loop spun at full speed appending 0.1 to the test's
# list, the test's ``assert slept == []`` failed on millions of entries, and
# formatting that failure while the loop held the GIL ran past the 120s timeout
# (gthread-migration on main, run 38561590951).
#
# This swaps the module's own ``time`` name for a proxy instead: the fake
# answers ``sleep`` and everything else is the real module.
class _ModuleTime:
    def __init__(self, sleep):
        self.sleep = sleep

    def __getattr__(self, name):
        import time

        return getattr(time, name)


@pytest.fixture
def module_sleep(monkeypatch):
    """Return ``patch(module, fake)``, which fakes ``time.sleep`` for that module only."""
    import time

    def patch(module, fake):
        current = getattr(module, "time", None)
        assert current is time or isinstance(current, _ModuleTime), (
            f"{module.__name__} does not reach sleep through 'import time'"
        )
        monkeypatch.setattr(module, "time", _ModuleTime(fake))
        return fake

    return patch
