"""The two evaluation views of a run (BREAKOUT spec item 20; `docs/ACCOUNTING-INVARIANTS.md` I3).

Economic view: every realised trade and event. A close the clock or the execution guard caused
(TIME_EXIT, BAD_FILL_CLOSE) is a trade, its cost is real, and it stays in net expectancy and
drawdown. This view decides economic viability (G1).

Strategy view: decided exits only (stop, gap stop, target). It diagnoses whether the logic has an
edge, so time exits and bad-fill closes are listed beside it with their counts and R, never mixed in.
It never passes G1.

DATA_GAP closes (a session with no bar in the time-exit window) are an exception, not a normal time
exit: they are taken out of both views, reported on their own with each date and symbol, and never
substituted into the normal evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from signal_engine.strategy_core.trade_policy import (
    DATA_GAP,
    STRATEGY_EXIT,
    classify_exit,
)


def _num(value: float) -> float | None:
    return None if value is None or not np.isfinite(value) else round(float(value), 6)


def _r(trades: Sequence, field: str) -> np.ndarray:
    return np.array([getattr(t, field) for t in trades], dtype=float)


def _economic(trades: Sequence) -> dict[str, Any]:
    if not trades:
        return {"trades": 0, "net_r_total": 0.0, "net_r_mean": None, "gross_r_mean": None}
    net, gross = _r(trades, "r_net"), _r(trades, "r_gross")
    return {
        "trades": len(trades),
        "net_r_total": _num(net.sum()),
        "net_r_mean": _num(net.mean()),
        "gross_r_mean": _num(gross.mean()),
    }


def _strategy(trades: Sequence) -> dict[str, Any]:
    if not trades:
        return {"decided_trades": 0, "win_rate_net": None, "avg_r_net": None, "avg_r_gross": None}
    net, gross = _r(trades, "r_net"), _r(trades, "r_gross")
    return {
        "decided_trades": len(trades),
        "win_rate_net": _num((net > 0).mean()),
        "avg_r_net": _num(net.mean()),
        "avg_r_gross": _num(gross.mean()),
    }


def split_data_gap(trades: Sequence) -> tuple[list, list]:
    """(trades without DATA_GAP closes, the DATA_GAP closes)."""
    kept, gap = [], []
    for t in trades:
        (gap if classify_exit(t.reason) == DATA_GAP else kept).append(t)
    return kept, gap


def exit_views(trades: Sequence, data_gap: Sequence = ()) -> dict[str, Any]:
    """The economic and strategy views of `trades` (DATA_GAP closes already removed), plus what was removed."""
    protection: dict[str, list] = {}
    decided = []
    for t in trades:
        cls = classify_exit(t.reason)
        if cls == STRATEGY_EXIT:
            decided.append(t)
        else:
            protection.setdefault(cls, []).append(t)
    return {
        "economic": _economic(list(trades)),
        "strategy": _strategy(decided),
        "protection": {
            cls: {"trades": len(rows), "net_r_total": _num(_r(rows, "r_net").sum()),
                  "net_r_mean": _num(_r(rows, "r_net").mean())}
            for cls, rows in sorted(protection.items())
        },
        "data_gap": {
            "trades": len(data_gap),
            "net_r_total": _num(_r(data_gap, "r_net").sum()) if data_gap else 0.0,
            "closes": [
                {"symbol": t.symbol, "day": str(t.day)} for t in data_gap
            ],
        },
    }
