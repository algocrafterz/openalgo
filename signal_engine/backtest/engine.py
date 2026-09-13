"""Strategy-agnostic simulator with PineScript execution semantics.

The three places a backtest usually cheats, and what this does instead:

  NEXT-BAR FILL   A signal is evaluated on a CLOSED bar and filled at the next bar's
                  open. That is what `process_orders_on_close=false` does in Pine and
                  what the live engine does with a market order on the alert. Stop and
                  target are the absolute levels computed at signal time, so the gap
                  between the signal close and the actual fill is a real cost the
                  results carry.

  STOP FIRST      When one bar's range spans both the stop and the target, the STOP is
                  taken. At 5-minute resolution the true sequence is unknowable, and
                  the optimistic read is the single most common way a backtest
                  flatters itself.

  GAP THROUGH     If the bar opens already beyond the stop, the fill is the OPEN, not
                  the stop. Tight stops on 5-minute bars gap through more often than
                  people expect and it belongs in the result.
"""

from __future__ import annotations

import numpy as np

from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, Position, RunConfig, Trade


def simulate(c: Ctx, strategy: Strategy, p, run: RunConfig) -> list[Trade]:
    o, h, low, close = c["Open"], c["High"], c["Low"], c["Close"]
    mins, from_open = c["mins"], c["from_open"]
    days, new_sess = c["day"], c["new_session"]
    times, n = c.index, c.n

    trades: list[Trade] = []
    pos: Position | None = None
    pending = None                      # (EntrySignal, signal_price) armed for i+1
    trades_today = 0
    long_taken = short_taken = False

    strategy.reset_symbol(p)
    cost_frac = run.cost_bps / 100.0 / 100.0    # bps -> fraction of notional

    def close_out(reason: str, price: float, i: int) -> None:
        nonlocal pos
        move = (price - pos.entry) if pos.direction == 1 else (pos.entry - price)
        r_gross = move / pos.risk
        trades.append(Trade(
            symbol=c.symbol, day=days[i], direction=pos.direction, tag=pos.tag,
            entry_time=times[pos.entry_bar], entry=pos.entry,
            signal_price=pos.signal_price, sl=pos.sl, tp=pos.tp, risk=pos.risk,
            exit_time=times[i], exit=float(price), reason=reason,
            r_gross=r_gross, r_net=r_gross - cost_frac * pos.entry / pos.risk))
        pos = None

    for i in range(n):
        if new_sess[i]:
            trades_today, long_taken, short_taken = 0, False, False
            pending = None
            strategy.reset_session(p)

        # 1. fill what the previous close armed
        if pending is not None:
            sig, sig_px = pending
            pending = None
            fill = float(o[i])
            if pos is None:
                pos = Position(direction=sig.direction, entry=fill,
                               signal_price=float(sig_px), sl=float(sig.sl),
                               sl_eff=float(sig.sl), tp=float(sig.tp),
                               # Risk is measured from the SIGNAL price, not the fill,
                               # because that is what the live engine sizes on - the
                               # alert carries the signal bar's close. The fill's drift
                               # away from it is a real cost and stays in the result.
                               risk=abs(float(sig_px) - float(sig.sl)),
                               tag=sig.tag, entry_bar=i)
                trades_today += 1
                # The gap between the signal close and this open can carry price clean
                # through the stop before the order is even placed. The live system
                # STILL TAKES IT: `validator._check_price_ordering` compares the stop
                # against the alert price, not against the fill, so the order goes out
                # as a market buy and the stop it then places sits on the wrong side of
                # the market. Modelling that as a skipped trade would flatter the
                # backtest, so the fill happens and is closed flat below - but it is
                # tagged so the count is visible rather than buried inside SL_GAP.
                entered_through_stop = ((fill <= sig.sl) if sig.direction == 1
                                        else (fill >= sig.sl))
                if sig.direction == 1:
                    long_taken = True
                else:
                    short_taken = True
                if entered_through_stop:
                    close_out("SL_GAP_ENTRY", fill, i)

        # 2. manage an open position on this bar
        if pos is not None:
            d = pos.direction
            new_stop = strategy.trail(c, i, p, pos)
            if new_stop is not None and not np.isnan(new_stop):
                # ratchet only - a trail must never loosen
                pos.sl_eff = max(pos.sl_eff, new_stop) if d == 1 else min(pos.sl_eff, new_stop)

            gapped = (o[i] <= pos.sl_eff) if d == 1 else (o[i] >= pos.sl_eff)
            hit_sl = (low[i] <= pos.sl_eff) if d == 1 else (h[i] >= pos.sl_eff)
            hit_tp = (h[i] >= pos.tp) if d == 1 else (low[i] <= pos.tp)

            if gapped:
                close_out("SL_GAP", o[i], i)
            elif hit_sl:
                close_out("SL", pos.sl_eff, i)
            elif hit_tp:
                close_out("TP", pos.tp, i)
            else:
                reason = ""
                if mins[i] >= run.time_exit_min:
                    reason = "TIME_EXIT"
                elif i + 1 >= n or days[i + 1] != days[i]:
                    reason = "EOD"
                else:
                    reason = strategy.custom_exit(c, i, p, pos) or ""
                if reason:
                    close_out(reason, close[i], i)

        # 3. strategy state machine
        strategy.on_bar(c, i, p)

        # 4. evaluate a signal on this closed bar
        if pos is not None or pending is not None:
            continue
        if i + 1 >= n or days[i + 1] != days[i]:
            continue                                   # nothing left to fill into
        if trades_today >= run.max_trades_per_day:
            continue
        if from_open[i] < run.skip_open_minutes:
            continue
        if mins[i] >= run.entry_cutoff_min or mins[i] >= run.time_exit_min:
            continue

        for d in (1, -1):
            if d == 1 and (not run.allow_longs or (run.one_trade_per_direction and long_taken)):
                continue
            if d == -1 and (not run.allow_shorts or (run.one_trade_per_direction and short_taken)):
                continue
            sig = strategy.entry(c, i, p, d)
            if sig is None:
                continue
            risk = abs(close[i] - sig.sl)
            if risk <= 0 or np.isnan(risk):
                continue
            # stop must be on the correct side, and no tighter than the live
            # validator's min_sl_pct - otherwise the engine would reject the signal.
            if (sig.sl >= close[i]) if d == 1 else (sig.sl <= close[i]):
                continue
            if risk / close[i] < run.min_sl_pct:
                continue
            pending = (sig, close[i])
            strategy.on_entry(c, i, p, sig)
            break

    return trades
