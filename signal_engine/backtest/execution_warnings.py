"""Execution warnings of a run (engine 1.6): how much of the result rests on a guess about the bars.

Three things the engine can see but a 5-minute bar cannot settle, counted so they are visible:

  ambiguous exits   A bar reached both the stop and the target and did not open through the stop.
                    The engine keeps the conservative resolution (the stop). The block states what
                    the same trades would average if every one of them had instead exited at its
                    target. That figure is an UPPER BOUND on what the ambiguity could be hiding,
                    not a result and not an estimate: the stop was taken in the numbers above it.
  data gaps         A bar was missing inside the session while the position was open. Exits are
                    judged on the next available bar, but the prices in the hole were never seen.
  skipped entries   A pending order whose due bar was missing was dropped, never filled across the
                    hole (ledger reason code ENTRY_SKIPPED_MISSING_BAR).

Pure: no I/O. Reads the trades the headline metrics read.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

BOUND_LABEL = (
    "UPPER BOUND, NOT A RESULT: the mean R these trades would have if every same-bar stop/target "
    "collision had been filled at the target. The run itself takes the stop."
)
TARGET_BASIS = "Trade.tp, the target the signal carried"


def _round(value: float) -> float | None:
    return round(float(value), 6) if np.isfinite(value) else None


def _share(part: int, whole: int) -> float:
    return round(part / whole, 6) if whole else 0.0


def _target_r(trade: Any) -> float:
    """Gross R of the trade had it exited at its target (NaN when there is no finite target)."""
    tp, entry, risk = float(trade.tp), float(trade.entry), float(trade.risk)
    if not (np.isfinite(tp) and np.isfinite(risk) and risk > 0):
        return float("nan")
    return trade.direction * (tp - entry) / risk


def execution_warnings(
    trades: Sequence[Any],
    entries_skipped_missing_bar: int = 0,
    *,
    dynamic_target: bool = False,
) -> dict[str, Any]:
    """The `execution_warnings` run-record block.

    `dynamic_target` marks a strategy whose target moves bar by bar: the bound then uses the
    signal's own target, which is not the level that was resting on the colliding bar, so it is
    flagged as not exact.
    """
    rows = list(trades)
    n = len(rows)
    ambiguous = [t for t in rows if getattr(t, "exit_ambiguous", False)]
    gapped = [t for t in rows if getattr(t, "data_gap", False)]
    block: dict[str, Any] = {
        "trades": n,
        "ambiguous_exit_trades": len(ambiguous),
        "ambiguous_exit_share": _share(len(ambiguous), n),
        "data_gap_trades": len(gapped),
        "data_gap_share": _share(len(gapped), n),
        "entries_skipped_missing_bar": int(entries_skipped_missing_bar),
    }
    scored = [t for t in ambiguous if t.r_defined and np.isfinite(_target_r(t))]
    sens: dict[str, Any] = {
        "label": BOUND_LABEL,
        "trades": len(scored),
        "actual_mean_net_r": None,
        "upper_bound_mean_net_r": None,
        "actual_mean_gross_r": None,
        "upper_bound_mean_gross_r": None,
        "run_mean_net_r_actual": None,
        "run_mean_net_r_upper_bound": None,
        "target_basis": TARGET_BASIS,
        "exact": not dynamic_target and all(getattr(t, "legs", 1) == 1 for t in scored),
    }
    block["ambiguous_sensitivity"] = sens
    if not scored:
        return block
    gross = np.array([t.r_gross for t in scored], dtype=float)
    net = np.array([t.r_net for t in scored], dtype=float)
    alt_gross = np.array([_target_r(t) for t in scored], dtype=float)
    alt_net = net - gross + alt_gross  # same cost in R, only the exit price changes
    run_net = np.array([t.r_net for t in rows if t.r_defined], dtype=float)
    shift = float((alt_net - net).sum())
    sens.update(
        {
            "actual_mean_net_r": _round(net.mean()),
            "upper_bound_mean_net_r": _round(alt_net.mean()),
            "actual_mean_gross_r": _round(gross.mean()),
            "upper_bound_mean_gross_r": _round(alt_gross.mean()),
            "run_mean_net_r_actual": _round(run_net.mean()),
            "run_mean_net_r_upper_bound": _round((run_net.sum() + shift) / len(run_net)),
        }
    )
    return block
