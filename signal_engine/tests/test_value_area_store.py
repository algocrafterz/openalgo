"""The stored value areas are the Pine-parity profile, one row per symbol per session, re-runnable."""

import numpy as np
import pandas as pd

from signal_engine.features import value_area_store as store
from signal_engine.features.value_area import prev_session_value_area


def _bars(days=("2026-10-29", "2026-10-30"), seed=1):
    rng = np.random.default_rng(seed)
    frames = []
    for day in days:
        idx = pd.date_range(f"{day} 09:15", f"{day} 15:29", freq="1min", tz="Asia/Kolkata")
        close = 1000 + np.cumsum(rng.normal(0, 0.5, len(idx)))
        frames.append(
            pd.DataFrame(
                {
                    "Open": close,
                    "High": close + 0.4,
                    "Low": close - 0.4,
                    "Close": close,
                    "Volume": rng.integers(100, 1000, len(idx)),
                },
                index=idx,
            )
        )
    return pd.concat(frames)


def test_a_row_per_session_matches_what_a_strategy_reads_next_day():
    bars = _bars()
    rows = store.rows_for("RELIANCE", bars, 0.05)
    assert [r[1] for r in rows] == ["2026-10-29", "2026-10-30"]
    assert all(r[2] == store.METHOD and r[7] == 375 for r in rows)
    # The 10-30 session reads 10-29's profile as its previous value area.
    nxt = prev_session_value_area(bars, bars.index, 0.05, store.ROWS_PER_TICK, store.VA_PCT, True)
    first_30 = nxt.loc[nxt.index.date == pd.Timestamp("2026-10-30").date()].iloc[0]
    vah, poc, val = rows[0][3], rows[0][4], rows[0][5]
    assert (first_30["pvah"], first_30["ppoc"], first_30["pval"]) == (vah, poc, val)
    assert val <= poc <= vah


def test_storing_twice_replaces_rather_than_duplicates(tmp_path):
    db = str(tmp_path / "va.db")
    conn = store.connect(db)
    rows = store.rows_for("RELIANCE", _bars(), 0.05)
    for _ in range(2):
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO value_areas VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
    conn.close()
    frame = store.load(["RELIANCE"], db_path=db)
    assert len(frame) == 2 and list(frame.columns)[:5] == [
        "symbol",
        "session_date",
        "vah",
        "poc",
        "val",
    ]
    assert len(store.load(["RELIANCE"], start="2026-10-30", db_path=db)) == 1


def test_an_empty_window_is_nothing_to_add_not_a_failure():
    def loader(*_a, **_k):
        raise RuntimeError(
            "no symbol in db/historify.duckdb has 0+ sessions at 1m - download history"
        )

    assert store._bars_since(loader, "SBIN", "2026-10-31", None, None) is None


def test_any_other_loader_error_still_fails():
    def loader(*_a, **_k):
        raise RuntimeError("duckdb lock held by another process")

    import pytest

    with pytest.raises(RuntimeError):
        store._bars_since(loader, "SBIN", "2026-10-31", None, None)
