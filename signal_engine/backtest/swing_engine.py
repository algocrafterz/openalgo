"""Multi-day simulator for swing strategies on daily bars.

The intraday `engine.simulate` forces an EOD exit on every bar boundary, so it cannot
express a setup that is entered on a gap and held for weeks. This engine keeps the same
three honesty rules and adds the two that matter once a position survives the session:

  OPEN FILL, NOT CLOSE FILL   A gap strategy is armed by YESTERDAY's closed bar and
                              triggered by TODAY's open. The fill is that open. The
                              adapter may read bar i's Open plus anything at i-1 and
                              earlier - never bar i's High/Low/Close.

  SAME-BAR STOP               A position opened at today's open is exposed to today's
                              own range immediately. For a gap strategy whose stop is
                              the previous close, most losers die on the entry bar, so
                              skipping this check would invent the entire edge.

  STOP FIRST                  When a daily bar spans both the stop and a favourable
                              extreme, the STOP is taken. Daily resolution hides the
                              true sequence and the optimistic read flatters the result.

  GAP THROUGH                 A bar that opens already beyond the stop fills at the
                              OPEN. Overnight risk is the whole point of a swing book
                              and it belongs in the numbers.

`Trade.day` is the ENTRY day here, not the exit day, so the harness's in/out-of-sample
split is made on the information the decision was taken with.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from signal_engine.backtest.types import Ctx, Position, Trade


@dataclass(frozen=True)
class SwingRunConfig:
    """Engine-level settings for a multi-day book."""

    allow_longs: bool = True
    allow_shorts: bool = True

    #: Round-trip cost in basis points of notional. NSE reference points:
    #: cash delivery ~22 bps (STT 10 bps each way dominates); stock futures ~8 bps.
    #: A gap-open entry adds slippage on top - see cost_sensitivity().
    cost_bps: float = 20.0

    #: Reject stops tighter than this fraction of price, mirroring the live validator.
    #: A gap strategy stops at the previous close, so a 0.1% gap would otherwise size
    #: into a position no broker fill could honour.
    min_sl_pct: float = 0.005

    #: Hard cap on holding period. Without it a trend exit can hold for years and one
    #: trade dominates the sample.
    max_hold_bars: int = 120

    def with_(self, **kw) -> "SwingRunConfig":
        return replace(self, **kw)


def simulate_swing(c: Ctx, strategy, p, run: SwingRunConfig) -> list[Trade]:
    o, h, low, close = c["Open"], c["High"], c["Low"], c["Close"]
    days, times, n = c["day"], c.index, c.n

    trades: list[Trade] = []
    pos: Position | None = None
    entry_day = None
    cost_frac = run.cost_bps / 100.0 / 100.0

    strategy.reset_symbol(p)

    def close_out(reason: str, price: float, i: int) -> None:
        nonlocal pos, entry_day
        move = (price - pos.entry) if pos.direction == 1 else (pos.entry - price)
        r_gross = move / pos.risk
        trades.append(Trade(
            symbol=c.symbol, day=entry_day, direction=pos.direction, tag=pos.tag,
            entry_time=times[pos.entry_bar], entry=pos.entry,
            signal_price=pos.signal_price, sl=pos.sl, tp=pos.tp, risk=pos.risk,
            exit_time=times[i], exit=float(price), reason=reason,
            r_gross=r_gross, r_net=r_gross - cost_frac * pos.entry / pos.risk))
        pos, entry_day = None, None

    for i in range(n):
        # 1. flat: today's OPEN may trigger a setup armed by yesterday's CLOSE.
        if pos is None:
            for d in (1, -1):
                if d == 1 and not run.allow_longs:
                    continue
                if d == -1 and not run.allow_shorts:
                    continue
                sig = strategy.entry_at_open(c, i, p, d)
                if sig is None:
                    continue
                fill = float(o[i])
                risk = abs(fill - float(sig.sl))
                if risk <= 0 or np.isnan(risk):
                    continue
                # stop on the correct side, and wide enough to be a real order
                if (sig.sl >= fill) if d == 1 else (sig.sl <= fill):
                    continue
                if risk / fill < run.min_sl_pct:
                    continue
                pos = Position(direction=d, entry=fill, signal_price=fill,
                               sl=float(sig.sl), sl_eff=float(sig.sl), tp=float(sig.tp),
                               risk=risk, tag=sig.tag, entry_bar=i)
                entry_day = days[i]
                strategy.on_entry(c, i, p, sig)
                break

        # 2. manage the position against THIS bar - including its own entry bar.
        if pos is not None:
            d = pos.direction
            new_stop = strategy.trail(c, i, p, pos)
            if new_stop is not None and not np.isnan(new_stop):
                pos.sl_eff = max(pos.sl_eff, new_stop) if d == 1 else min(pos.sl_eff, new_stop)

            # a bar that opens beyond the stop fills at the open, except on the entry
            # bar itself, where the open IS the fill and cannot also be the exit
            beyond_open = (o[i] <= pos.sl_eff) if d == 1 else (o[i] >= pos.sl_eff)
            gapped = beyond_open and i != pos.entry_bar
            hit_sl = (low[i] <= pos.sl_eff) if d == 1 else (h[i] >= pos.sl_eff)
            hit_tp = (h[i] >= pos.tp) if d == 1 else (low[i] <= pos.tp)

            if gapped:
                close_out("SL_GAP", o[i], i)
            elif hit_sl:
                close_out("SL", pos.sl_eff, i)
            elif hit_tp:
                close_out("TP", pos.tp, i)
            elif i - pos.entry_bar >= run.max_hold_bars:
                close_out("MAX_HOLD", close[i], i)
            elif i + 1 >= n:
                close_out("EOD_DATA", close[i], i)
            else:
                reason = strategy.custom_exit(c, i, p, pos)
                if reason:
                    close_out(reason, close[i], i)

        strategy.on_bar(c, i, p)

    return trades
