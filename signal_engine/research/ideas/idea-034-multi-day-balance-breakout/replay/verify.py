"""Check that the replay IS the recorder: for each given day, record_day with the replay's
injected year-sliced Historify loader must return exactly the rows of record_day with its own
default per-day loaders (both dry runs; nothing is written).

    PYTHONPATH=. uv run python <this dir>/verify.py 2026-10-16 2026-10-01 2025-03-12
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay as R  # noqa: E402


def _same(a: dict, b: dict) -> bool:
    if a.keys() != b.keys():
        return False
    for k in a:
        x, y = a[k], b[k]
        if isinstance(x, float) and isinstance(y, float):
            if not (np.isclose(x, y, rtol=0, atol=1e-9) or (np.isnan(x) and np.isnan(y))):
                return False
        elif x != y:
            return False
    return True


def main(days: list[dt.date]) -> int:
    from signal_engine.analysis import idea034_paper as m

    universe = R._universe()
    bad = 0
    for day in days:
        ref = m.record_day(day, dry_run=True)
        frames = m._historify5(universe, day - dt.timedelta(days=R.LOOKBACK_DAYS), day)
        got = m.record_day(day, dry_run=True, symbols=universe, load5=R._slicer(frames))
        drop = ("data_hash",)  # hashes the loaded day only; equal frames give equal hashes
        a = [{k: v for k, v in r.items() if k not in drop} for r in ref.rows]
        b = [{k: v for k, v in r.items() if k not in drop} for r in got.rows]
        same = ref.status == got.status and len(a) == len(b) and all(map(_same, a, b))
        hashes = {r["data_hash"] for r in ref.rows} == {r["data_hash"] for r in got.rows}
        print(
            f"{day}: status {ref.status}/{got.status} rows {len(a)}/{len(b)} "
            f"identical {same} data_hash_equal {hashes}"
        )
        bad += not (same and hashes)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main([dt.date.fromisoformat(d) for d in sys.argv[1:]]))
