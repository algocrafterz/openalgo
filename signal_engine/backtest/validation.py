"""Calibration: does this engine reproduce results whose answers are already known?

A backtest that only ever tested unproven strategies can never tell you whether a
negative result means "no edge" or "broken harness". Every number below has an
answer established outside this repository, so a disagreement is a bug HERE.

The suite is deliberately built as a falsification exercise rather than a
confirmation one - three of the four checks can only be passed by an engine that is
right, and would be FAILED by the specific bugs that flatter a backtest.

  A. OVERNIGHT vs INTRADAY          Known: in essentially every equity market studied,
                                    the whole equity premium accrues close-to-open,
                                    while open-to-close is flat to negative (Cliff,
                                    Cooper & Gulen 2008; Lachance 2021; Bogousslavsky
                                    2021). Reproducing it on NSE validates the price
                                    data AND explains, without any appeal to strategy
                                    quality, why a long-biased intraday book struggles.

  B. ENGINE DIFFERENTIAL            The same economic quantity - buy the open, sell
                                    later the same day - computed two independent ways:
                                    once straight off the price panel, once pushed
                                    through the real `engine.simulate`. They must
                                    agree. A fill-timing error, an off-by-one bar, or
                                    a cost applied to the wrong notional shows up here
                                    and NOWHERE else, because both a lookahead bug and
                                    an over-conservative rule change only one side.

  C. CROSS-SECTIONAL MOMENTUM 12-1  Known POSITIVE. The most replicated anomaly in
                                    finance (Jegadeesh & Titman 1993), documented on
                                    Indian equities repeatedly since.

  D. SHORT-TERM REVERSAL 1-month    Known NEGATIVE at this horizon - last month's
                                    losers beat last month's winners (Jegadeesh 1990).

C and D are run as a PAIR and that is the point. The classic backtest bug - filling on
the signal bar instead of the next one - makes a ranking strategy read its own outcome,
and would print BOTH as strongly positive. Only an engine with clean timing produces
the sign flip between them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_engine.backtest import data, metrics
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.portfolio import PortfolioBacktest, PortfolioConfig, build_factor
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig

TRADING_DAYS = 252


def _fmt(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


# ---------------------------------------------------------------------------
# A. overnight vs intraday
# ---------------------------------------------------------------------------

def overnight_vs_intraday(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Split each symbol's total return into its close-to-open and open-to-close parts.

    Uses daily bars built from whatever frames are handed in, so it can be run against
    the same intraday store the strategies use - which is the point: it audits THOSE
    prices, not a separate daily download.
    """
    rows = []
    for sym, df in frames.items():
        o = df["Open"].resample("1D").first().dropna()
        c = df["Close"].resample("1D").last().dropna()
        j = pd.concat([o.rename("o"), c.rename("c")], axis=1).dropna()
        if len(j) < 100:
            continue
        intraday = (j.c / j.o - 1.0)
        overnight = (j.o / j.c.shift(1) - 1.0).dropna()
        full = (j.c / j.c.shift(1) - 1.0).dropna()
        yrs = len(j) / TRADING_DAYS

        def cagr(series, yrs=yrs):
            return (float(np.prod(1 + series)) ** (1 / yrs) - 1) * 100

        rows.append({"symbol": sym.replace(".NS", ""), "sessions": len(j),
                     "overnight_pa": round(cagr(overnight), 1),
                     "intraday_pa": round(cagr(intraday), 1),
                     "total_pa": round(cagr(full), 1)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# B. engine differential
# ---------------------------------------------------------------------------

class _BuyTheOpen(Strategy):
    """Enter long on the first eligible bar of every session. No stop that can bind.

    A deliberately edge-free probe. What it measures is not a strategy but the
    ENGINE's arithmetic: if the simulator fills, accounts and exits correctly, the
    aggregate result must equal the open-to-close return computed directly from the
    same bars.
    """

    name = "buy-the-open"

    def prepare(self, df, p):
        return df

    def prepare_key(self, p):
        return ("buy_the_open",)

    def entry(self, c, i, p, direction):
        if direction != 1:
            return None
        # A stop far enough away that price cannot reach it, so every trade ends at
        # the timed exit and the comparison is not contaminated by stop handling.
        px = float(c["Close"][i])
        return EntrySignal(direction=1, sl=px * 0.5, tp=px * 10.0, tag="probe")


def engine_differential(frames: dict[str, pd.DataFrame], exit_min: int = 15 * 60) -> dict:
    """Same trade, two code paths. Panel arithmetic vs the real simulator."""
    run = RunConfig(skip_open_minutes=0, entry_cutoff_min=exit_min - 5,
                    time_exit_min=exit_min, max_trades_per_day=1,
                    one_trade_per_direction=True, allow_shorts=False,
                    cost_bps=0.0, min_sl_pct=0.0)
    strat = _BuyTheOpen()

    direct, engine_r = [], []
    for sym, df in frames.items():
        d = df.copy()
        day = pd.Series(d.index.date, index=d.index)
        d["day"] = day
        d["mins"] = d.index.hour * 60 + d.index.minute
        d["new_session"] = (day != day.shift(1)).to_numpy()
        d["from_open"] = d["mins"] - day.map(d.groupby(day)["mins"].min())
        trades = simulate(Ctx(d, symbol=sym), strat, None, run)
        for t in trades:
            engine_r.append((t.exit - t.entry) / t.entry)

        # The same quantity read straight off the panel. Both legs must match the
        # engine's own rules EXACTLY or the test measures the reference's bugs:
        #   entry - bar 0 closes and arms the signal, bar 1's OPEN is the fill
        #   exit  - the close of the FIRST bar at or after time_exit_min, which is one
        #           bar later than "the last bar before the cutoff". That single bar of
        #           difference showed up as a 0.24 bps disagreement, which is exactly
        #           the size of error this test exists to catch.
        for _, g in d.groupby(d["day"]):
            if len(g) < 3:
                continue
            entry_px = float(g["Open"].iloc[1])
            at_or_after = g[g["mins"] >= exit_min]
            exit_px = float(at_or_after["Close"].iloc[0]) if len(at_or_after) \
                else float(g["Close"].iloc[-1])          # engine's EOD fallback
            direct.append(exit_px / entry_px - 1.0)

    a, b = np.array(direct), np.array(engine_r)
    n = min(len(a), len(b))
    return {"panel_trades": len(a), "engine_trades": len(b),
            "panel_mean_bps": round(float(a.mean()) * 10_000, 3) if len(a) else np.nan,
            "engine_mean_bps": round(float(b.mean()) * 10_000, 3) if len(b) else np.nan,
            "diff_bps": round(abs(float(a.mean() - b.mean())) * 10_000, 4) if n else np.nan,
            "count_match": len(a) == len(b)}


# ---------------------------------------------------------------------------
# C + D. the momentum / reversal sign-flip pair
# ---------------------------------------------------------------------------

def momentum_reversal_pair(daily: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """12-1 momentum (expect POSITIVE alpha) and 1-month reversal (expect NEGATIVE).

    Both long the top of the ranking, so the reversal book deliberately buys last
    month's WINNERS - the side the literature says underperforms - which makes the
    expected sign negative and the test a real one rather than a restatement.
    """
    pb = PortfolioBacktest(daily, PortfolioConfig(rebalance_days=21, top_n=30))
    out = []
    for label, kind, lookback, skip, expect in [
        ("C momentum 12-1", "mom_skip", 252, 21, "positive"),
        ("D reversal 1-month", "total_ret", 21, 0, "negative"),
    ]:
        f = build_factor(pb.panel, kind, lookback, skip)
        s = pb.stats(pb.run(f), label, 252.0 / 21)
        s["expected"] = expect
        s["result"] = _fmt((s["alpha_pa"] > 0) if expect == "positive" else (s["alpha_pa"] < 0))
        out.append(s)
    return pd.DataFrame(out)[
        ["label", "periods", "cagr", "bench_cagr", "alpha_pa", "sharpe", "max_dd",
         "t_excess", "turnover", "expected", "result"]]


# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="signal_engine.backtest.validation")
    ap.add_argument("--source", default="historify", choices=["historify", "yahoo"])
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--symbols", default=None)
    args = ap.parse_args()

    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 30)
    syms = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    if args.source == "historify":
        intraday = data.from_historify(symbols=syms, interval=args.interval)
    else:
        intraday = data.load(symbols=[f"{s}.NS" for s in syms] if syms else None,
                             period="60d", interval=args.interval)

    print("=" * 100)
    print("A. OVERNIGHT vs INTRADAY   known: overnight carries the premium, intraday is flat/negative")
    print("=" * 100)
    ov = overnight_vs_intraday(intraday)
    print(ov.to_string(index=False))
    share = float((ov.overnight_pa > ov.intraday_pa).mean())
    print(f"\nmedian overnight {ov.overnight_pa.median():+.1f}%/yr vs "
          f"intraday {ov.intraday_pa.median():+.1f}%/yr | "
          f"overnight wins in {100 * share:.0f}% of symbols -> "
          f"{_fmt(ov.overnight_pa.median() > ov.intraday_pa.median() and share > 0.7)}")

    print("\n" + "=" * 100)
    print("B. ENGINE DIFFERENTIAL     panel arithmetic must equal the simulator, to the basis point")
    print("=" * 100)
    d = engine_differential(intraday)
    for k, v in d.items():
        print(f"  {k:18s} {v}")
    print(f"  -> {_fmt(bool(d['count_match']) and d['diff_bps'] < 0.01)}")

    print("\n" + "=" * 100)
    print("C+D. MOMENTUM / REVERSAL   the signs must DIFFER; a lookahead bug makes both positive")
    print("=" * 100)
    daily = data.load(period="10y", interval="1d", min_bars=750, auto_adjust=True)
    pair = momentum_reversal_pair(daily)
    print(pair.to_string(index=False))
    flip = pair.alpha_pa.iloc[0] > 0 > pair.alpha_pa.iloc[1]
    print(f"\n  sign flip present -> {_fmt(bool(flip))}")


if __name__ == "__main__":
    main()
