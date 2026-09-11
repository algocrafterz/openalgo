"""Staged evaluation, with the anti-overfitting discipline built in.

Intraday history is short - yfinance gives ~59 sessions of 5-minute bars - and a
cartesian sweep over a dozen toggles will always find a configuration that looks
excellent on it. Three rules make the difference between a finding and a fit:

  1. SPLIT THE DATA. The first `is_fraction` of sessions choose settings; the rest are
     touched once, to check them. `confirm()` reports both.
  2. A FILTER MUST HELP IN BOTH WINDOWS. `ablation()` marks each variant BOTH / IS only
     / OOS only / neither. "IS only" is the signature of fitting noise, and is the
     single most common way a strategy dies in production.
  3. CHECK DISPERSION. `by_symbol()` shows whether the result is broad or carried by
     one or two names.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from signal_engine.backtest import metrics
from signal_engine.backtest.data import sessions
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.types import Ctx, RunConfig


class Backtest:
    """Binds one strategy to one set of symbol frames."""

    def __init__(self, strategy, frames: dict[str, pd.DataFrame],
                 run: RunConfig | None = None, is_fraction: float = 0.60,
                 engine=simulate):
        # `engine` swaps the simulator without touching the reporting. The intraday
        # `engine.simulate` forces an EOD exit, so a multi-day strategy passes
        # `swing_engine.simulate_swing` here and reuses every report below unchanged.
        self.strategy = strategy
        self.frames = frames
        self.engine = engine
        self.run = run or RunConfig()
        self.days = sessions(frames)
        self.cut = self.days[int(len(self.days) * is_fraction)]
        self._prep: dict = {}

    # ---- windows --------------------------------------------------------

    def _filter(self, window: str):
        if window == "is":
            return lambda d: d < self.cut
        if window == "oos":
            return lambda d: d >= self.cut
        return lambda d: True

    def describe(self) -> str:
        n_is = sum(1 for d in self.days if d < self.cut)
        return (f"{len(self.frames)} symbols | {self.days[0]} .. {self.days[-1]} | "
                f"IS {n_is} sessions, OOS {len(self.days) - n_is} (cut {self.cut}) | "
                f"cost {self.run.cost_bps:.0f} bps")

    # ---- execution ------------------------------------------------------

    def trades(self, p, window: str = "all", run: RunConfig | None = None):
        run = run or self.run
        keep = self._filter(window)
        out = []
        for sym, df in self.frames.items():
            key = (sym, self.strategy.prepare_key(p))
            if key not in self._prep:
                self._prep[key] = Ctx(self.strategy.prepare(df, p), symbol=sym)
            out += [t for t in self.engine(self._prep[key], self.strategy, p, run) if keep(t.day)]
        return out

    # ---- reports --------------------------------------------------------

    def confirm(self, p, label: str = "config") -> pd.DataFrame:
        """The honest test: in-sample, out-of-sample, and the whole period."""
        return pd.DataFrame([metrics.summary(self.trades(p, w), f"{label} [{w.upper()}]")
                             for w in ("is", "oos", "all")])

    def ablation(self, base_p, variants: dict[str, dict]) -> pd.DataFrame:
        """Marginal effect of each variant against the base, IN BOTH WINDOWS.

        `variants` maps a label to the params fields it overrides. The `helps` column is
        the one to read: only BOTH is evidence.
        """
        rows = []
        b_is = metrics.summary(self.trades(base_p, "is"))
        b_oos = metrics.summary(self.trades(base_p, "oos"))
        rows.append({"variant": "base", "n_IS": b_is["n"], "bps_IS": b_is["gross_bps"],
                         "n_OOS": b_oos["n"], "bps_OOS": b_oos["gross_bps"], "helps": "-"})
        for label, kw in variants.items():
            p = replace(base_p, **kw)
            i = metrics.summary(self.trades(p, "is"))
            o = metrics.summary(self.trades(p, "oos"))
            up_i = i["gross_bps"] > b_is["gross_bps"]
            up_o = o["gross_bps"] > b_oos["gross_bps"]
            rows.append({"variant": label, "n_IS": i["n"], "bps_IS": i["gross_bps"],
                             "n_OOS": o["n"], "bps_OOS": o["gross_bps"],
                             "helps": "BOTH" if up_i and up_o else
                                   "IS only" if up_i else "OOS only" if up_o else "neither"})
        return pd.DataFrame(rows)

    def sweep(self, base_p, field: str, values, window: str = "is") -> pd.DataFrame:
        """One parameter at a time. Keep the grid coarse - fine grids fit noise."""
        return pd.DataFrame([
            metrics.summary(self.trades(replace(base_p, **{field: v}), window), f"{field}={v}")
            for v in values])

    def cost_sensitivity(self, p, bps=(6, 8, 10, 12), window: str = "all") -> pd.DataFrame:
        """Where the sign flips. For a marginal strategy this IS the result."""
        return pd.DataFrame([
            metrics.summary(self.trades(p, window, run=self.run.with_(cost_bps=b)), f"{b:.0f} bps")
            for b in bps])

    def by_symbol(self, p, window: str = "all") -> pd.DataFrame:
        return metrics.by_symbol(self.trades(p, window))

    def by_reason(self, p, window: str = "all") -> pd.DataFrame:
        return metrics.by_reason(self.trades(p, window))
