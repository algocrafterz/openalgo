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

import numpy as np
import pandas as pd

from signal_engine.backtest import metrics, session_coverage, tickgrid
from signal_engine.backtest.causal import GuardedCtx
from signal_engine.backtest.data import sessions
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.types import HOLDOUT_OOS_FRACTION, Ctx, RunConfig


class Backtest:
    """Binds one strategy to one set of symbol frames."""

    def __init__(
        self,
        strategy,
        frames: dict[str, pd.DataFrame],
        run: RunConfig | None = None,
        is_fraction: float = 1.0 - HOLDOUT_OOS_FRACTION,
        engine=simulate,
        release_raw: bool = False,
        single_pass: bool = False,
    ):
        # `engine` swaps the simulator without touching the reporting. The intraday
        # `engine.simulate` forces an EOD exit, so a multi-day strategy passes
        # `swing_engine.simulate_swing` here and reuses every report below unchanged.
        #
        # `release_raw`: once a symbol's Ctx is built, its raw OHLCV frame is dropped
        # from `self.frames` rather than kept alongside the prepared copy for the rest
        # of the run - halves steady-state memory on a full-universe pull. Only safe
        # when every call uses the SAME strategy params (so prepare_key never changes,
        # e.g. the CLI's single-config report). Leave False for sweep()/ablation(),
        # which re-prepare each symbol under multiple param variants and need the raw
        # frame still there on a cache miss.
        #
        # `single_pass`: trades() may run once. Each symbol's Ctx is built, simulated and
        # dropped together with its raw frame, so peak memory is one Ctx rather than the
        # whole universe. Requires release_raw=True.
        if single_pass and not release_raw:
            raise ValueError("single_pass=True requires release_raw=True")
        self.strategy = strategy
        self.frames = frames
        self.engine = engine
        self.run = run or RunConfig()
        self.days = sessions(frames)  # computed before any release, below
        self.cut = self.days[int(len(self.days) * is_fraction)]
        self.release_raw = release_raw
        self.single_pass = single_pass
        self._spent = False
        self._prep: dict = {}
        # Look-ahead guard bookkeeping: symbol -> sessions simulated under a GuardedCtx.
        self._guarded: dict[str, int] = {}
        self._unguarded: set[str] = set()

    # ---- windows --------------------------------------------------------

    def _filter(self, window: str):
        if window == "is":
            return lambda d: d < self.cut
        if window == "oos":
            return lambda d: d >= self.cut
        return lambda d: True

    def describe(self) -> str:
        n_is = sum(1 for d in self.days if d < self.cut)
        return (
            f"{len(self.frames)} symbols | {self.days[0]} .. {self.days[-1]} | "
            f"IS {n_is} sessions, OOS {len(self.days) - n_is} (cut {self.cut}) | "
            f"cost {self.run.cost_bps:.0f} bps"
        )

    # ---- execution ------------------------------------------------------

    def _ctx(self, sym, df, p) -> Ctx:
        """The prepared symbol, with the frame's `price_factor` carried through `prepare`."""
        factor = df["price_factor"] if "price_factor" in df.columns else None
        # `session_after_gap` (session_coverage) marks the first session after a missing official
        # session; a strategy's `prepare` may rebuild the frame, so it is put back by bar time.
        gap = df[session_coverage.AFTER_GAP_COLUMN] if session_coverage.AFTER_GAP_COLUMN in df.columns else None
        prepared = self.strategy.prepare_for(sym, df, p)
        prepared = session_coverage.carry_column(prepared, gap)
        return Ctx(tickgrid.carry_price_factor(prepared, factor), symbol=sym)

    def _engine_ctx(self, ctx: Ctx):
        """The Ctx the engine is given: always a GuardedCtx for the two engines that unwrap it.

        This is the funnel every backtest, sweep, ablation and recorded run passes through, so a
        strategy hook cannot read a bar after the one it may know (`backtest.causal`) without
        raising LookAheadError. There is no switch to turn it off. An engine that does not declare
        `guard_aware` (a test double) runs on the plain Ctx and is listed as unguarded.
        """
        if not getattr(self.engine, "guard_aware", False):
            self._unguarded.add(ctx.symbol)
            return ctx
        if "new_session" in ctx.a:
            sessions_n = int(np.count_nonzero(ctx.a["new_session"]))
        else:
            sessions_n = int(ctx.n)  # a daily bar is its own session
        self._guarded[ctx.symbol] = sessions_n
        return GuardedCtx(ctx)

    def lookahead_guard(self) -> dict:
        """What the guarded run covered: for the run record's `lookahead_guard` block.

        `sessions_checked` counts (symbol, session) pairs. The guard runs inside the one
        simulation, over every bar of every symbol; a violation aborts the run with
        LookAheadError, so a record that exists has `violations` 0.
        """
        if self._unguarded and not self._guarded:
            mode = "not_run"
        elif self._unguarded:
            mode = "partial"
        elif self._guarded:
            mode = "full"
        else:
            mode = "not_run"
        return {
            "mode": mode,
            "scope": "every bar of every symbol, in the headline simulation",
            "symbols_checked": len(self._guarded),
            "sessions_checked": int(sum(self._guarded.values())),
            "symbols_unguarded": sorted(self._unguarded),
            "violations": 0,
        }

    def trades(self, p, window: str = "all", run: RunConfig | None = None, recorder=None):
        """Simulate every symbol. `recorder` (a `ledgers.LedgerRecorder`) is handed to the engine
        only when given, so engines that take no `rec` keep working. Only the single recorded run
        passes one; sweep, ablation and cost-sensitivity never do."""
        run = run or self.run
        keep = self._filter(window)
        out = []
        extra = {} if recorder is None else {"rec": recorder}
        if self.single_pass:
            if self._spent:
                raise RuntimeError(
                    "trades() already ran under single_pass=True and its frames were "
                    "released; construct a new Backtest to run again"
                )
            self._spent = True
            for sym in list(self.frames):
                df = self.frames[sym]
                ctx = self._engine_ctx(self._ctx(sym, df, p))
                self.frames[sym] = None
                del df
                out += [t for t in self.engine(ctx, self.strategy, p, run, **extra) if keep(t.day)]
                del ctx
            return out
        for sym, df in self.frames.items():
            key = (sym, self.strategy.prepare_key(p))
            if key not in self._prep:
                if df is None:
                    # raw was released under release_raw=True and this is a NEW
                    # prepare_key - the one case that combination cannot support.
                    raise RuntimeError(
                        f"{sym}: raw frame was released (release_raw=True) and no "
                        f"cached prepare() exists for this parameter set; construct "
                        f"Backtest with release_raw=False to vary strategy params"
                    )
                self._prep[key] = self._engine_ctx(self._ctx(sym, df, p))
                if self.release_raw:
                    self.frames[sym] = None
            out += [
                t
                for t in self.engine(self._prep[key], self.strategy, p, run, **extra)
                if keep(t.day)
            ]
        return out

    # ---- reports --------------------------------------------------------

    def confirm(self, p, label: str = "config") -> pd.DataFrame:
        """The honest test: in-sample, out-of-sample, and the whole period."""
        return pd.DataFrame(
            [
                metrics.summary(self.trades(p, w), f"{label} [{w.upper()}]")
                for w in ("is", "oos", "all")
            ]
        )

    def ablation(self, base_p, variants: dict[str, dict]) -> pd.DataFrame:
        """Marginal effect of each variant against the base, IN BOTH WINDOWS.

        `variants` maps a label to the params fields it overrides. The `helps` column is
        the one to read: only BOTH is evidence.
        """
        rows = []
        b_is = metrics.summary(self.trades(base_p, "is"))
        b_oos = metrics.summary(self.trades(base_p, "oos"))
        rows.append(
            {
                "variant": "base",
                "n_IS": b_is["n"],
                "bps_IS": b_is["gross_bps"],
                "n_OOS": b_oos["n"],
                "bps_OOS": b_oos["gross_bps"],
                "helps": "-",
            }
        )
        for label, kw in variants.items():
            p = replace(base_p, **kw)
            i = metrics.summary(self.trades(p, "is"))
            o = metrics.summary(self.trades(p, "oos"))
            up_i = i["gross_bps"] > b_is["gross_bps"]
            up_o = o["gross_bps"] > b_oos["gross_bps"]
            rows.append(
                {
                    "variant": label,
                    "n_IS": i["n"],
                    "bps_IS": i["gross_bps"],
                    "n_OOS": o["n"],
                    "bps_OOS": o["gross_bps"],
                    "helps": "BOTH"
                    if up_i and up_o
                    else "IS only"
                    if up_i
                    else "OOS only"
                    if up_o
                    else "neither",
                }
            )
        return pd.DataFrame(rows)

    def sweep(self, base_p, field: str, values, window: str = "is") -> pd.DataFrame:
        """One parameter at a time. Keep the grid coarse - fine grids fit noise."""
        return pd.DataFrame(
            [
                metrics.summary(self.trades(replace(base_p, **{field: v}), window), f"{field}={v}")
                for v in values
            ]
        )

    def cost_sensitivity(self, p, bps=(6, 8, 10, 12), window: str = "all") -> pd.DataFrame:
        """Where the sign flips. For a marginal strategy this IS the result.

        The book is simulated once, at `self.run.cost_bps`; each level restates net from the
        trades' own gross (`metrics.with_flat_cost`), so the cost of the report no longer
        grows with the number of levels.
        """
        trades = self.trades(p, window)
        return pd.DataFrame(
            [metrics.summary(metrics.with_flat_cost(trades, b), f"{b:.0f} bps") for b in bps]
        )

    def by_symbol(self, p, window: str = "all") -> pd.DataFrame:
        return metrics.by_symbol(self.trades(p, window))

    def by_reason(self, p, window: str = "all") -> pd.DataFrame:
        return metrics.by_reason(self.trades(p, window))
