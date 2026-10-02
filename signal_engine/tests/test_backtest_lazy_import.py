"""The backtest package must not import the harness (and with it duckdb) at import time."""

import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_compare_import_stays_light():
    result = _run(
        """
        import sys
        import signal_engine.backtest.compare  # noqa: F401
        heavy = [
            m for m in ("signal_engine.backtest.harness", "duckdb", "signal_engine.backtest.data")
            if m in sys.modules
        ]
        print("HEAVY=" + ",".join(heavy))
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("HEAVY="), result.stdout


def test_package_import_alone_is_light():
    result = _run(
        """
        import sys
        import signal_engine.backtest  # noqa: F401
        print("HARNESS=" + str("signal_engine.backtest.harness" in sys.modules))
        """
    )
    assert result.returncode == 0, result.stderr
    assert "HARNESS=False" in result.stdout


def test_old_import_forms_still_work():
    result = _run(
        """
        import signal_engine.backtest as bt
        from signal_engine.backtest import Backtest, EntrySignal, RunConfig, Trade
        from signal_engine.backtest.harness import Backtest as Direct
        assert bt.Backtest is Backtest is Direct
        assert bt.RunConfig is RunConfig and bt.EntrySignal is EntrySignal and bt.Trade is Trade
        assert bt.__all__ == ["Backtest", "RunConfig", "EntrySignal", "Trade"]
        print("OK")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


def test_unknown_attribute_raises_attribute_error():
    result = _run(
        """
        import signal_engine.backtest as bt
        try:
            bt.NoSuchThing
        except AttributeError:
            print("OK")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")
