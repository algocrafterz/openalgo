"""extend_existing must retry symbols that lag the rest of the store, not only stale ones."""
import datetime as dt

import pandas as pd

from signal_engine.backtest import backfill as bf


def _cov(rows):
    today = pd.Timestamp.now(tz="Asia/Kolkata").normalize().date()
    return pd.DataFrame(
        [{"symbol": s, "bars": 1, "first": today, "last": today - dt.timedelta(days=d),
          "stale_days": d} for s, d in rows])


def _run(monkeypatch, rows, **kw):
    calls = []
    monkeypatch.setattr(bf, "coverage", lambda *a, **k: _cov(rows))
    monkeypatch.setattr(bf, "backfill", lambda syms, start, **k: calls.append(syms[0]) or [])
    bf.extend_existing("D", **kw)
    return calls


def test_lagging_symbol_retried_even_when_only_one_day_stale(monkeypatch):
    # A missed write leaves BAD one day behind the store; min_stale=2 alone would skip it.
    assert _run(monkeypatch, [("OK", 0), ("BAD", 1)]) == ["BAD"]


def test_all_current_or_all_one_day_behind_is_untouched(monkeypatch):
    assert _run(monkeypatch, [("A", 1), ("B", 1)]) == []
    assert _run(monkeypatch, [("A", 0), ("B", 0)]) == []


def test_stale_symbol_still_extended(monkeypatch):
    assert _run(monkeypatch, [("OK", 0), ("OLD", 5)]) == ["OLD"]
