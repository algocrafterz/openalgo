"""Facts a run records about its own inputs: bar quality, strategy counters, spec provenance.

All three are read-only observations. None of them changes what a run trades.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from utils.logging import get_logger

logger = get_logger(__name__)

#: A 5m bar is built from one to five source minutes; the keys are always present.
N_MINUTES_BASE = (1, 2, 3, 4, 5)


def _counts(values: pd.Series) -> dict[str, int]:
    seen = values.value_counts()
    keys = sorted({*N_MINUTES_BASE, *(int(k) for k in seen.index)})
    return {str(k): int(seen.get(k, 0)) for k in keys}


def n_minutes_distribution(frames: Mapping[str, pd.DataFrame]) -> dict[str, Any] | None:
    """Counts of bars by source-minute count, overall and by year (IST), over every bar the run
    read. None when no frame carries an `n_minutes` column."""
    parts = [f[["n_minutes"]] for f in frames.values() if "n_minutes" in f.columns]
    if not parts:
        return None
    bars = pd.concat(parts)
    years = bars.index.year
    return {
        "bars": int(len(bars)),
        "overall": _counts(bars.n_minutes),
        "by_year": {str(y): _counts(g.n_minutes) for y, g in bars.groupby(years)},
    }


def strategy_diagnostics(strategy: Any) -> dict[str, int] | None:
    """The strategy's own counters (`diagnostics()` returning name -> int), or None when it has
    none. Raises TypeError for any other shape: a malformed record is worse than none."""
    method = getattr(strategy, "diagnostics", None)
    if not callable(method):
        return None
    out = method()
    ok = isinstance(out, Mapping) and all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        for k, v in out.items()
    )
    if not ok:
        raise TypeError(f"strategy diagnostics() must return dict[str, int], got {out!r}")
    return dict(out)


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        logger.exception("git unavailable while reading spec provenance")
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def spec_provenance(folder: Path) -> dict[str, Any] | None:
    """sha256 of the idea's spec.yaml bytes and the commit that last changed it.

    `commit` is None when the file was never committed, `modified_since_commit` is True when the
    working copy differs from that commit (or the file is untracked). None without a spec."""
    path = Path(folder) / "spec.yaml"
    if not path.is_file():
        return None
    commit = _git(["log", "-1", "--format=%H", "--", path.name], path.parent) or None
    changed = _git(["status", "--porcelain", "--", path.name], path.parent)
    return {
        "path": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "commit": commit,
        "modified_since_commit": bool(changed) if commit else True,
    }
