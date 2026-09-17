"""Cross-sectional portfolio backtester, for ranking strategies.

`harness.Backtest` runs one symbol at a time, which cannot express "buy the strongest
30 names in the universe this month" - that decision needs every symbol visible on the
same date. This engine holds a price panel instead of a per-symbol loop.

THE THREE THINGS THAT DECIDE WHETHER A FACTOR RESULT IS REAL

  1. BEAT THE UNIVERSE, NOT ZERO.  A long-only book in NSE large/mid caps over
     2016-2026 makes money because the market went up. Every result here is reported
     against an EQUAL-WEIGHT basket of the SAME universe over the SAME dates. That
     difference is the only part attributable to the ranking. `alpha_pa` is that number;
     `cagr` on its own means almost nothing.

  2. NO LOOKAHEAD AT THE REBALANCE.  The factor is computed from closes up to and
     including the rebalance date; the trade fills at the NEXT session's open. A factor
     computed on date D and filled at D's close is reading its own signal bar.

  3. SURVIVORSHIP IS PRESENT AND IS NOT FIXABLE HERE.  The universe is the F&O list as
     it stands TODAY, walked backwards ten years. Names that were liquid in 2017 and
     later delisted or were dropped from F&O are absent, and they are disproportionately
     the losers. This inflates BOTH the strategy and the equal-weight benchmark, which
     is exactly why the benchmark-relative number is the one reported: the bias largely
     cancels in the difference, while it does not cancel in `cagr`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PortfolioConfig:
    #: Sessions between rebalances. 21 ~ monthly, 63 ~ quarterly.
    rebalance_days: int = 21
    #: Names held, equal weight.
    top_n: int = 30
    #: Round-trip cost in bps of the notional TRADED (charged on turnover only, so a
    #: name that stays in the book across a rebalance is not charged again).
    #: NSE delivery reference: STT 0.1% each way dominates -> ~22 bps round trip.
    cost_bps: float = 22.0
    #: Drop names priced below this at the rebalance.
    min_price: float = 20.0
    #: Fraction of the date range used for parameter choice.
    is_fraction: float = 0.60
    #: Dual-momentum absolute filter (Antonacci). A pick must ALSO have a positive
    #: factor score, not just top-N relative rank - a name that is merely the best
    #: of a falling universe is excluded. Its slot then sits in CASH (0% that period)
    #: instead of being backfilled by the next-best negative-momentum name, which is
    #: what actually cuts drawdown in a broad market decline. False reproduces the
    #: plain relative-momentum book exactly (momentum-rank's validated behaviour).
    require_positive: bool = False


def _range_position(px: pd.DataFrame, hi: pd.DataFrame, lo: pd.DataFrame,
                    n: int) -> pd.DataFrame:
    """Where each name sits inside its own n-day range, 0..100.

    The factor the forward-return test flagged: high in the range predicts higher
    forward returns on NSE F&O names, low in the range predicts lower.
    """
    ll = lo.rolling(n).min()
    hh = hi.rolling(n).max()
    return (px - ll) / (hh - ll).replace(0, np.nan) * 100.0


def _total_return(px: pd.DataFrame, n: int) -> pd.DataFrame:
    return px / px.shift(n) - 1.0


def build_factor(panel: dict, kind: str, lookback: int, skip: int = 0) -> pd.DataFrame:
    """Factor score per (date, symbol). Higher = more attractive.

    kind:
      "range_pos"  position inside the trailing `lookback`-day high/low range
      "total_ret"  plain trailing return
      "mom_skip"   trailing return EXCLUDING the last `skip` days - the classic 12-1
                   construction, which skips the most recent month because
                   short-horizon reversal runs against momentum there
      "range_vol"  range position divided by trailing volatility, a crude risk-adjust
    """
    px, hi, lo = panel["Close"], panel["High"], panel["Low"]
    if kind == "range_pos":
        return _range_position(px, hi, lo, lookback)
    if kind == "total_ret":
        return _total_return(px, lookback)
    if kind == "mom_skip":
        return px.shift(skip) / px.shift(lookback) - 1.0
    if kind == "range_vol":
        vol = px.pct_change().rolling(lookback).std()
        return _range_position(px, hi, lo, lookback) / vol.replace(0, np.nan)
    raise ValueError(f"unknown factor {kind!r}")


class PortfolioBacktest:
    """Monthly-rebalanced, equal-weight, long-only ranking book."""

    def __init__(self, frames: dict[str, pd.DataFrame], cfg: PortfolioConfig | None = None):
        self.cfg = cfg or PortfolioConfig()
        cols = ["Open", "High", "Low", "Close"]
        self.panel = {c: pd.DataFrame({s: df[c] for s, df in frames.items()}).sort_index()
                      for c in cols}
        self.dates = self.panel["Close"].index
        self.cut = self.dates[int(len(self.dates) * self.cfg.is_fraction)]

    def describe(self) -> str:
        n_is = int((self.dates < self.cut).sum())
        return (f"{self.panel['Close'].shape[1]} symbols | {self.dates[0].date()} .. "
                f"{self.dates[-1].date()} | IS {n_is} sessions, OOS "
                f"{len(self.dates) - n_is} (cut {self.cut.date()}) | "
                f"cost {self.cfg.cost_bps:.0f} bps on turnover")

    # ---- core ----------------------------------------------------------

    def run(self, factor: pd.DataFrame, cfg: PortfolioConfig | None = None) -> pd.DataFrame:
        """Period-by-period returns for the book and for the equal-weight universe.

        Fills at the open of the session AFTER the rebalance date, and exits at the open
        of the session after the next rebalance date - so both the book and the benchmark
        are measured open-to-open over identical windows.
        """
        cfg = cfg or self.cfg
        op, px = self.panel["Open"], self.panel["Close"]
        idx = self.dates
        rebals = list(range(0, len(idx) - 1, cfg.rebalance_days))

        rows, held_prev = [], set()
        for a, b in zip(rebals, rebals[1:], strict=False):
            sig_date = idx[a]
            f = factor.loc[sig_date]
            price_ok = px.loc[sig_date] >= cfg.min_price
            # investable only with a factor AND a fill price at both ends
            valid = f.notna() & price_ok & op.iloc[a + 1].notna() & op.iloc[b + 1].notna()
            f = f[valid]
            if len(f) < cfg.top_n:
                continue
            picks = list(f.nlargest(cfg.top_n).index)

            # dual momentum: a pick also needs positive ABSOLUTE momentum (the same
            # factor value > 0), not just top-N relative rank. A shortfall sits in
            # cash rather than being backfilled by the next-best negative name.
            cash_frac = 0.0
            if cfg.require_positive:
                picks = [s for s in picks if f[s] > 0]
                cash_frac = (cfg.top_n - len(picks)) / cfg.top_n

            ret = op.iloc[b + 1] / op.iloc[a + 1] - 1.0
            book = float(ret[picks].mean()) * (1.0 - cash_frac) if picks else 0.0
            bench = float(ret[valid[valid].index].mean())

            # cost is charged on turnover: names entering and names leaving
            turn = len(set(picks) ^ held_prev) / (2.0 * cfg.top_n)
            cost = turn * cfg.cost_bps / 10_000.0
            held_prev = set(picks)

            rows.append({"date": idx[a + 1], "n": len(picks), "book_gross": book,
                         "book": book - cost, "bench": bench, "turnover": turn,
                         "excess": book - cost - bench, "cash_frac": cash_frac})
        return pd.DataFrame(rows).set_index("date")

    # ---- reporting -----------------------------------------------------

    @staticmethod
    def stats(r: pd.DataFrame, label: str, periods_per_year: float) -> dict:
        if r.empty:
            return {"label": label, "periods": 0}
        b, m, x = r["book"].to_numpy(), r["bench"].to_numpy(), r["excess"].to_numpy()
        eq = np.cumprod(1 + b)
        dd = float(np.max(1 - eq / np.maximum.accumulate(eq))) * 100
        yrs = len(b) / periods_per_year
        t = x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 2 and x.std() > 0 else np.nan
        cagr = float(eq[-1]) ** (1 / yrs) - 1
        bcagr = float(np.prod(1 + m)) ** (1 / yrs) - 1
        return {
            "label": label, "periods": len(b),
            "cagr": round(cagr * 100, 2),
            "bench_cagr": round(bcagr * 100, 2),
            "alpha_pa": round((cagr - bcagr) * 100, 2),
            "vol_pa": round(float(b.std(ddof=1)) * np.sqrt(periods_per_year) * 100, 1),
            "sharpe": round(float(b.mean() / b.std(ddof=1)) * np.sqrt(periods_per_year), 2),
            "max_dd": round(dd, 1),
            "win_periods": round(100 * float((x > 0).mean()), 1),
            "t_excess": round(float(t), 2),
            "turnover": round(float(r["turnover"].mean()) * 100, 1),
        }

    def report(self, factor: pd.DataFrame, label: str,
               cfg: PortfolioConfig | None = None) -> pd.DataFrame:
        cfg = cfg or self.cfg
        ppy = 252.0 / cfg.rebalance_days
        r = self.run(factor, cfg)
        out = []
        for w, sel in (("IS", r.index < self.cut), ("OOS", r.index >= self.cut),
                       ("ALL", np.ones(len(r), dtype=bool))):
            out.append(self.stats(r[sel], f"{label} [{w}]", ppy))
        return pd.DataFrame(out)

    def cost_curve(self, factor: pd.DataFrame, bps=(0, 10, 22, 40),
                   cfg: PortfolioConfig | None = None) -> pd.DataFrame:
        cfg = cfg or self.cfg
        ppy = 252.0 / cfg.rebalance_days
        return pd.DataFrame([
            self.stats(self.run(factor, replace(cfg, cost_bps=b)), f"{b:.0f} bps", ppy)
            for b in bps])
