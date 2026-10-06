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

from signal_engine.backtest.ledgers import _COUNT_BITS, CODE_INDEX
from signal_engine.backtest.ledgers import N_CODES as _N_CODES
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, Position, RunConfig, Trade
from signal_engine.strategy_core.exits import Ladder

# Reason codes as small ints for the hot loop; a skip is packed as (bar index << 6) | code.
_K_MAX_TRADES = CODE_INDEX["MAX_TRADES_DAY"]
_K_OPEN_WINDOW = CODE_INDEX["OPEN_WINDOW"]
_K_CUTOFF = CODE_INDEX["ENTRY_CUTOFF"]
_K_DIR_OFF = CODE_INDEX["DIRECTION_DISABLED"]
_K_DIR_TAKEN = CODE_INDEX["DIRECTION_ALREADY_TAKEN"]
_K_RISK = CODE_INDEX["RISK_INVALID"]
_K_WRONG_SIDE = CODE_INDEX["STOP_WRONG_SIDE"]
_K_TIGHT = CODE_INDEX["STOP_TOO_TIGHT"]
_K_POS_OPEN = CODE_INDEX["POSITION_OPEN"]
_K_NO_NEXT = CODE_INDEX["NO_NEXT_BAR"]
_K_DROP_SESSION = CODE_INDEX["ORDER_DROPPED_SESSION_END"]
_K_DROP_POS = CODE_INDEX["ORDER_DROPPED_POSITION_OPEN"]
_K_WARMUP = CODE_INDEX["WARMUP"]
_K_SKIP_FILL = CODE_INDEX["ENTRY_SKIPPED_BEYOND_LEVEL"]


def simulate(c: Ctx, strategy: Strategy, p, run: RunConfig, rec=None) -> list[Trade]:
    """Run one symbol. `rec` is an optional `ledgers.LedgerRecorder`; with None the run is
    exactly the unrecorded simulation and no recording code executes."""
    o, h, low, close = c["Open"], c["High"], c["Low"], c["Close"]
    mins, from_open = c["mins"], c["from_open"]
    days, new_sess = c["day"], c["new_session"]
    times, n = c.index, c.n

    trades: list[Trade] = []
    pos: Position | None = None
    pending = None  # (EntrySignal, signal_price) armed for i+1
    pend_i = -1  # signal bar of `pending` (recorder only)
    cur_sig = -1  # signal bar of the open position (recorder only)
    legs_logged = 0  # ladder legs already written to the ledger
    b = rec.begin(c) if rec is not None else None
    skip = b.skip if rec is not None else None
    cnt = b._cnt if rec is not None else None
    di = b._di if rec is not None else None
    b_sig = b.signals.append if rec is not None else None
    b_ord = b.orders.append if rec is not None else None
    b_fill = b.fills.append if rec is not None else None
    b_stamp = b._pending.append if rec is not None else None
    trades_today = 0
    long_taken = short_taken = False

    strategy.reset_symbol(p)
    cost_frac = run.cost_bps / 100.0 / 100.0  # bps -> fraction of notional
    warmup = run.warmup_bars
    # Both opt-in; with the defaults neither changes a single comparison below.
    dyn_target = bool(getattr(strategy, "dynamic_target", False))
    skip_fill = run.skip_fill_beyond_levels

    # Broker MIS square-off guard: the exit bar must CLOSE margin minutes before the broker's
    # auto square-off, so the latest allowed exit-bar stamp is square-off - margin - bar length.
    steps = np.diff(times.values).astype("timedelta64[m]").astype(int) if n > 1 else []
    steps = [s for s in steps if 0 < s <= 60]
    bar_min = int(np.median(steps)) if steps else 5
    time_exit_min = min(
        run.time_exit_min, run.mis_squareoff_min - run.squareoff_margin_min - bar_min
    )

    def log_legs(i: int) -> None:
        # ladder legs not yet in the ledger, stamped with the bar they filled on
        nonlocal legs_logged
        fills = pos.ladder.fills
        for k in range(legs_logged, len(fills)):
            b_fill((cur_sig, pos.direction, 1, i, fills[k].price, fills[k].reason, k + 1))
        legs_logged = len(fills)

    def close_out(reason: str, price: float, i: int) -> None:
        nonlocal pos
        legs = 1
        exit_px, exit_reason = price, reason
        if pos.ladder is not None:
            # Scaled-out position: P&L is the size-weighted sum of its fills. Costs below are
            # still charged once on the entry notional - partial legs sell the same total
            # notional, so the proportional cost model is unchanged.
            pos.ladder.close_all(price, reason)
            fills = pos.ladder.fills
            weight = sum(f.frac for f in fills)
            move = sum(
                f.frac * ((f.price - pos.entry) if pos.direction == 1 else (pos.entry - f.price))
                for f in fills
            )
            price = sum(f.frac * f.price for f in fills) / weight
            reason = "TP" if fills[-1].reason.startswith("TP") else fills[-1].reason
            legs = len(fills)
            if rec is not None:
                log_legs(i)
        else:
            move = (price - pos.entry) if pos.direction == 1 else (pos.entry - price)
        r_gross = move / pos.risk
        trade = Trade(
            symbol=c.symbol,
            day=days[i],
            direction=pos.direction,
            tag=pos.tag,
            entry_time=times[pos.entry_bar],
            entry=pos.entry,
            signal_price=pos.signal_price,
            sl=pos.sl,
            tp=pos.tp,
            risk=pos.risk,
            exit_time=times[i],
            exit=float(price),
            reason=reason,
            r_gross=r_gross,
            r_net=r_gross - cost_frac * pos.entry / pos.risk,
            legs=legs,
        )
        if rec is not None:
            if pos.ladder is None:
                b_fill((cur_sig, pos.direction, 1, i, exit_px, exit_reason, 1))
            b_stamp((trade, cur_sig, pos.direction, legs))
        trades.append(trade)
        pos = None
        strategy.on_exit(c, i, p, trade)

    for i in range(n):
        if new_sess[i]:
            trades_today, long_taken, short_taken = 0, False, False
            if rec is not None and pending is not None:
                b_ord((pend_i, pending[0].direction, -1, _K_DROP_SESSION))
            pending = None
            strategy.reset_session(p)

        # 1. fill what the previous close armed
        if pending is not None:
            sig, sig_px = pending
            pending = None
            fill = float(o[i])
            if (
                pos is None
                and skip_fill
                and (
                    (fill >= sig.tp or fill <= sig.sl)
                    if sig.direction == 1
                    else (fill <= sig.tp or fill >= sig.sl)
                )
            ):
                # Skip-at-fill: the open is already at/beyond the target or the stop, so the
                # order is not taken. Not a trade, so it uses no daily slot or direction.
                if rec is not None:
                    b_ord((pend_i, sig.direction, -1, _K_SKIP_FILL))
                strategy.on_fill_skipped(c, i, p, sig)
            elif pos is None:
                pos = Position(
                    direction=sig.direction,
                    entry=fill,
                    signal_price=float(sig_px),
                    sl=float(sig.sl),
                    sl_eff=float(sig.sl),
                    tp=float(sig.tp),
                    # Risk is measured from the SIGNAL price, not the fill,
                    # because that is what the live engine sizes on - the
                    # alert carries the signal bar's close. The fill's drift
                    # away from it is a real cost and stays in the result.
                    risk=abs(float(sig_px) - float(sig.sl)),
                    tag=sig.tag,
                    entry_bar=i,
                    targets=tuple(sig.meta.get("targets", ())),
                )
                if run.exit_plan is not None and pos.targets:
                    # Anchored to the ACTUAL fill: breakeven and buffer distances are
                    # measured from what was paid, not from the alert price.
                    pos.ladder = Ladder(
                        sig.direction, fill, float(sig.sl), pos.targets, run.exit_plan
                    )
                trades_today += 1
                strategy.on_fill(c, i, p, pos)
                if rec is not None:
                    cur_sig, legs_logged = pend_i, 0
                    b_ord((pend_i, sig.direction, i, -1))
                    # Next-bar open as-is. Slippage is a cost component (plan decision 1),
                    # never added to a recorded fill price.
                    b_fill((pend_i, sig.direction, 0, i, fill, "", 1))
                # The gap between the signal close and this open can carry price clean
                # through the stop before the order is even placed. The live system
                # STILL TAKES IT: `validator._check_price_ordering` compares the stop
                # against the alert price, not against the fill, so the order goes out
                # as a market buy and the stop it then places sits on the wrong side of
                # the market. Modelling that as a skipped trade would flatter the
                # backtest, so the fill happens and is closed flat below - but it is
                # tagged so the count is visible rather than buried inside SL_GAP.
                entered_through_stop = (fill <= sig.sl) if sig.direction == 1 else (fill >= sig.sl)
                if sig.direction == 1:
                    long_taken = True
                else:
                    short_taken = True
                if entered_through_stop:
                    close_out("SL_GAP_ENTRY", fill, i)
            elif rec is not None:
                b_ord((pend_i, sig.direction, -1, _K_DROP_POS))

        # 2. manage an open position on this bar
        if pos is not None and pos.ladder is not None:
            # Shared exit model (strategy_core.exits): partial targets and stop moves.
            pos.ladder.step(o[i], h[i], low[i])
            if rec is not None:
                log_legs(i)
            if pos.ladder.done:
                last = pos.ladder.fills[-1]
                close_out(last.reason, last.price, i)
            else:
                reason = ""
                if mins[i] >= time_exit_min:
                    reason = "TIME_EXIT"
                elif i + 1 >= n or days[i + 1] != days[i]:
                    reason = "EOD"
                else:
                    reason = strategy.custom_exit(c, i, p, pos) or ""
                if reason:
                    close_out(reason, close[i], i)
        elif pos is not None:
            d = pos.direction
            new_stop = strategy.trail(c, i, p, pos)
            if new_stop is not None and not np.isnan(new_stop):
                # ratchet only - a trail must never loosen
                pos.sl_eff = max(pos.sl_eff, new_stop) if d == 1 else min(pos.sl_eff, new_stop)

            gapped = (o[i] <= pos.sl_eff) if d == 1 else (o[i] >= pos.sl_eff)
            hit_sl = (low[i] <= pos.sl_eff) if d == 1 else (h[i] >= pos.sl_eff)
            tp = strategy.target(c, i, p, pos) if dyn_target else pos.tp
            hit_tp = (h[i] >= tp) if d == 1 else (low[i] <= tp)

            if gapped:
                close_out("SL_GAP", o[i], i)
            elif hit_sl:
                close_out("SL", pos.sl_eff, i)
            elif hit_tp:
                if dyn_target:
                    # a resting limit: a bar that opens through it fills at the open
                    tp = max(float(o[i]), tp) if d == 1 else min(float(o[i]), tp)
                close_out("TP", tp, i)
            else:
                reason = ""
                if mins[i] >= time_exit_min:
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
        if i < warmup:
            # warm-up bars never produce a signal; counted per day like the other bar skips
            if rec is not None:
                skip(i, _K_WARMUP)
            continue
        if pos is not None or pending is not None:
            if rec is not None:
                # inlined skip(): the two hottest sites, same counter layout as ledgers
                k = di[i] * _N_CODES + _K_POS_OPEN
                v = cnt[k]
                cnt[k] = v + 1 if v else (i << _COUNT_BITS) | 1
            continue
        if i + 1 >= n or days[i + 1] != days[i]:
            if rec is not None:
                skip(i, _K_NO_NEXT)
            continue  # nothing left to fill into
        if trades_today >= run.max_trades_per_day:
            if rec is not None:
                skip(i, _K_MAX_TRADES)
            continue
        if from_open[i] < run.skip_open_minutes:
            if rec is not None:
                skip(i, _K_OPEN_WINDOW)
            continue
        if mins[i] >= run.entry_cutoff_min or mins[i] >= time_exit_min:
            if rec is not None:
                # inlined skip(): the two hottest sites, same counter layout as ledgers
                k = di[i] * _N_CODES + _K_CUTOFF
                v = cnt[k]
                cnt[k] = v + 1 if v else (i << _COUNT_BITS) | 1
            continue

        for d in (1, -1):
            if d == 1 and (not run.allow_longs or (run.one_trade_per_direction and long_taken)):
                if rec is not None:
                    skip(i, (_K_DIR_OFF if not run.allow_longs else _K_DIR_TAKEN))
                continue
            if d == -1 and (not run.allow_shorts or (run.one_trade_per_direction and short_taken)):
                if rec is not None:
                    skip(i, (_K_DIR_OFF if not run.allow_shorts else _K_DIR_TAKEN))
                continue
            sig = strategy.entry(c, i, p, d)
            if sig is None:
                continue
            risk = abs(close[i] - sig.sl)
            if risk <= 0 or np.isnan(risk):
                if rec is not None:
                    b_sig((i, d, sig.tag, close[i], sig.sl, sig.tp, risk, _K_RISK))
                continue
            # stop must be on the correct side, and no tighter than the live
            # validator's min_sl_pct - otherwise the engine would reject the signal.
            if (sig.sl >= close[i]) if d == 1 else (sig.sl <= close[i]):
                if rec is not None:
                    b_sig((i, d, sig.tag, close[i], sig.sl, sig.tp, risk, _K_WRONG_SIDE))
                continue
            if risk / close[i] < run.min_sl_pct:
                if rec is not None:
                    b_sig((i, d, sig.tag, close[i], sig.sl, sig.tp, risk, _K_TIGHT))
                continue
            if rec is not None:
                b_sig((i, d, sig.tag, close[i], sig.sl, sig.tp, risk, -1))
                pend_i = i
            pending = (sig, close[i])
            strategy.on_entry(c, i, p, sig)
            break

    if rec is not None:
        b.finish()
    return trades
