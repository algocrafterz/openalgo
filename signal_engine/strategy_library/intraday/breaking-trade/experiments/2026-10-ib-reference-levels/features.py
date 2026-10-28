"""Entry-time features for H1-H4 (see README). Shared by sample_a.py and sample_b.py.

Every feature for a trade on day D entered at E uses completed sessions before D and bars of D
stamped before E only. Bars are stamped with their open time, IST.
"""

import datetime as dt

import numpy as np
import pandas as pd

from signal_engine.backtest.metrics import clustered_t
from signal_engine.backtest.strategies.balance_breakout import value_area

CONT_END = dt.time(15, 15)  # same continuous-session cut as the IDEA-034 adapter
IB_END = dt.time(10, 15)
N_NAKED = 10
N_COMPOSITE = 5
ATR_LEN = 14


class SymbolProfile:
    """Per-session value areas and true ranges of one symbol, built once from its bars."""

    def __init__(self, bars: pd.DataFrame, min_session_bars: int):
        self.bars = bars
        cont = bars[bars.index.time < CONT_END]
        self.cont = cont
        self.by_day = dict(cont.groupby(cont.index.date))
        rows = []
        for d, g in self.by_day.items():
            if len(g) < min_session_bars:
                continue
            va = value_area(
                g["High"].to_numpy(float),
                g["Low"].to_numpy(float),
                g["Volume"].to_numpy(float),
                float(g["Close"].mean()),
            )
            if va is None:
                continue
            rows.append((d, *va, g["High"].max(), g["Low"].min(), g["Close"].iloc[-1]))
        tab = pd.DataFrame(rows, columns=["day", "val", "vah", "poc", "hi", "lo", "cl"]).set_index(
            "day"
        )
        prev_cl = tab["cl"].shift(1)
        tr = pd.concat(
            [tab.hi - tab.lo, (tab.hi - prev_cl).abs(), (tab.lo - prev_cl).abs()], axis=1
        ).max(axis=1)
        if len(tr):
            tr.iloc[0] = np.nan
        tab["tr"] = tr
        self.tab = tab

    def features(
        self, day: dt.date, entry_time: pd.Timestamp, d: int, entry: float, sl: float, tp: float
    ) -> dict | None:
        prior = self.tab[self.tab.index < day]
        today = self.by_day.get(day)
        if len(prior) < ATR_LEN + 1 or today is None:
            return None
        today = today[today.index < entry_time]
        if today.empty:
            return None
        atr = prior["tr"].iloc[-ATR_LEN:].mean()
        ib = today[today.index.time < IB_END]
        day_open = float(today["Open"].iloc[0])
        ib_rng = float(ib["High"].max() - ib["Low"].min()) if len(ib) else np.nan
        pv = prior.iloc[-1]

        # H2: open vs prior value area, in the trade direction
        if day_open > pv.vah:
            side = 1
        elif day_open < pv.val:
            side = -1
        else:
            side = 0
        open_loc = "inside" if side == 0 else ("outside_with" if side == d else "outside_against")

        # H3: naked POCs of the last N_NAKED sessions, untouched by any later bar before entry
        last = prior.iloc[-N_NAKED:]
        his, los = last["hi"].to_numpy(), last["lo"].to_numpy()
        t_hi, t_lo = float(today["High"].max()), float(today["Low"].min())
        nk_target = nk_stop = False
        n_naked = 0
        for k in range(len(last)):
            poc = last["poc"].iloc[k]
            hi_after = max(np.max(his[k + 1 :], initial=-np.inf), t_hi)
            lo_after = min(np.min(los[k + 1 :], initial=np.inf), t_lo)
            if lo_after <= poc <= hi_after:
                continue
            n_naked += 1
            if d * (poc - entry) > 0 and d * (tp - poc) >= 0:
                nk_target = True
            if d * (entry - poc) > 0 and d * (poc - sl) >= 0:
                nk_stop = True

        # H4: entry vs prior-day POC and the 5-session composite POC
        comp_days = list(prior.index[-N_COMPOSITE:])
        comp = pd.concat([self.by_day[x] for x in comp_days])
        cva = value_area(
            comp["High"].to_numpy(float),
            comp["Low"].to_numpy(float),
            comp["Volume"].to_numpy(float),
            float(comp["Close"].mean()),
        )
        beyond_prev = d * (entry - pv.poc) > 0
        beyond_comp = cva is not None and d * (entry - cva[2]) > 0
        n_beyond = int(beyond_prev) + int(beyond_comp)
        h4 = {2: "beyond_both", 1: "beyond_one", 0: "value_side"}[n_beyond]

        return {
            "atr": atr,
            "ib_rng": ib_rng,
            "ib_atr": ib_rng / atr if atr > 0 else np.nan,
            "ib_pct": ib_rng / day_open * 100,
            "day_open": day_open,
            "open_loc": open_loc,
            "n_naked": n_naked,
            "nk_target": nk_target,
            "nk_stop": nk_stop,
            "prev_poc": pv.poc,
            "comp_poc": cva[2] if cva else np.nan,
            "h4": h4,
        }


def tercile(x: pd.Series, edges) -> pd.Series:
    return pd.cut(x, [-np.inf, edges[0], edges[1], np.inf], labels=["narrow", "mid", "wide"])


def bucket_table(df: pd.DataFrame, col: str, periods: dict) -> pd.DataFrame:
    """n / gross / net / day-clustered t of net (and of gross) per bucket and period."""
    rows = []
    for b in list(pd.unique(df[col].dropna())):
        rec = {"bucket": b}
        for name, mask in periods.items():
            s = df[mask & (df[col] == b)]
            rec[f"{name}_n"] = len(s)
            rec[f"{name}_gross"] = s.g.mean() if len(s) else np.nan
            rec[f"{name}_net"] = s.n.mean() if len(s) else np.nan
            rec[f"{name}_t"] = (
                clustered_t(s.n.to_numpy(), s.day.to_numpy()) if s.day.nunique() > 2 else np.nan
            )
            rec[f"{name}_tg"] = (
                clustered_t(s.g.to_numpy(), s.day.to_numpy()) if s.day.nunique() > 2 else np.nan
            )
        rows.append(rec)
    return pd.DataFrame(rows).sort_values("bucket").round(2)
