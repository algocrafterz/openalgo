"""Session volume factor and per-slot bar baseline, ported from klVolFactor (pine 1098-1143).

  vf            cumulative session volume / mean cumulative volume at the same slot
                across prior sessions. "Is this stock in play today?"
  bar_baseline  mean volume of the bar AT this slot across prior sessions. RVOL is
                `volume / bar_baseline` -- not time-of-day RVOL against a rolling mean.

Faithful to the Pine, including its quirks:
  * The ring buffer has `days` rows and the CURRENT session occupies one, so `days=14`
    averages 13 prior sessions, not 14.
  * A reading needs at least min(min_hist, days - 1) prior sessions with volume at the
    slot; below that both outputs are NaN (the Pine filters then fail open).
  * Prior sessions with zero cumulative volume at a slot are ignored.
"""

from __future__ import annotations

import pandas as pd

from signal_engine.features.session import add_session


def session_volume_factor(df: pd.DataFrame, days: int = 14, min_hist: int = 3) -> pd.DataFrame:
    """Return DataFrame[vf, bar_baseline] aligned to df.index."""
    sess = add_session(df)
    cum = df["Volume"].fillna(0.0).groupby(sess["day"]).cumsum()

    # sessions (rows, chronological) x slots (columns) of cumulative volume
    mat = cum.groupby([sess["day"], sess["slot"]]).last().unstack()
    has_volume = mat > 0
    valid = mat.where(has_volume)
    prev_slot = mat.shift(1, axis=1).fillna(0.0)  # nz(cum at slot - 1); slot 0 -> 0

    window = max(days - 1, 1)  # prior sessions held besides the current row
    need = min(min_hist, days - 1)

    def prior_sum(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.shift(1).rolling(window, min_periods=1).sum()

    n = has_volume.astype(float).shift(1).rolling(window, min_periods=1).sum()
    s_now = prior_sum(valid)
    s_prev = prior_sum(prev_slot.where(has_volume))

    enough = n >= need
    base_cum = (s_now / n).where(enough)
    base_bar = ((s_now - s_prev) / n).where(enough)
    vf = (mat / base_cum).where(base_cum > 0)
    baseline = base_bar.where(base_bar > 0)

    keys = pd.MultiIndex.from_arrays([sess["day"].values, sess["slot"].values])
    return pd.DataFrame(
        {
            "vf": vf.stack(future_stack=True).reindex(keys).values,
            "bar_baseline": baseline.stack(future_stack=True).reindex(keys).values,
        },
        index=df.index,
    )
