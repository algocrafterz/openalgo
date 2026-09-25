"""Process startup, CLI flags, and engine lifecycle.

Separated from main.py so the trading pipeline is not interleaved with process
wiring. Collaborators (risk engine, tracker, message handler) are passed in
rather than imported, so this module holds no global state of its own.
"""

import asyncio
import signal as signal_module
import sys

from loguru import logger

from signal_engine import mode_guard, notifier
from signal_engine.api_client import fetch_available_capital, fetch_trading_mode
from signal_engine.config import settings
from signal_engine.db import fetch_last_entry_trade, set_trade_mode
from signal_engine.listener import start_listener
from signal_engine.logger_setup import set_mode as set_log_mode
from signal_engine.models import Direction
from signal_engine.runtime import apply_trade_mode
from signal_engine.tracker import TimeExitScheduler, TrackedPosition

# ---------------------------------------------------------------------------
# CLI flag handling
# ---------------------------------------------------------------------------


def run_health_check_cli(argv) -> bool:
    """Handle --smoke-test / --dry-run. Returns True if the flag was handled.

    Exits the process with 0 (all checks passed) or 1 (any failure).
    """
    if not ("--smoke-test" in argv or "--dry-run" in argv):
        return False

    from signal_engine.smoke_test import run_dry_run, run_smoke_test

    is_dry = "--dry-run" in argv

    async def _run_checks():
        report = await run_dry_run() if is_dry else await run_smoke_test()
        report.print()
        sys.exit(0 if report.all_passed else 1)

    try:
        asyncio.run(_run_checks())
    except KeyboardInterrupt:
        sys.exit(1)
    return True


def read_test_signal_text(argv) -> str:
    """Resolve the --test signal text from an inline arg, --test-file, or stdin."""
    idx = argv.index("--test")
    if idx + 1 < len(argv) and not argv[idx + 1].startswith("--"):
        return argv[idx + 1]
    if "--test-file" in argv:
        fidx = argv.index("--test-file")
        if fidx + 1 < len(argv):
            with open(argv[fidx + 1]) as f:
                return f.read().strip()
        logger.error("--test-file requires a path argument")
        sys.exit(1)
    logger.info("Reading signal from stdin (paste signal, then Ctrl+D):")
    return sys.stdin.read().strip()


def run_test_signal_cli(argv, handle_message) -> bool:
    """Handle --test mode: process one signal, then exit. True if handled."""
    if "--test" not in argv:
        return False

    signal_text = read_test_signal_text(argv)
    if not signal_text:
        logger.error("No signal text provided")
        sys.exit(1)

    asyncio.run(process_test_signal(signal_text, handle_message))
    sys.exit(0)


async def process_test_signal(text: str, handle_message) -> None:
    """Process a single test signal through the full pipeline, then exit.

    Used with --test CLI flag for manual live market testing without Telegram.
    """
    logger.info("Signal Engine TEST MODE — processing single signal")

    if settings.test_qty_cap > 0:
        logger.info(f"Test qty cap active: max {settings.test_qty_cap} shares per order")
    else:
        logger.warning("test_qty_cap is 0 (disabled) — full position sizing will be used")

    # Detect trading mode for logging
    mode_str, is_analyze = await fetch_trading_mode()
    mode_label = "ANALYZE" if is_analyze else "LIVE"
    logger.info(f"Trading mode: {mode_label}")

    capital = await fetch_available_capital()
    if capital > 0:
        logger.info(f"Available capital: {capital:,.2f} INR")

    logger.info(f"Input signal:\n{text}")
    await handle_message(text)
    logger.info("Test signal processing complete")


def log_config_problems() -> list:
    """Report config.yaml problems that would otherwise fail silently.

    Warnings, not a refusal: a config with a missing -live twin still trades correctly
    today, and refusing to start over it would be worse than the gap. What matters is that
    the gap is SAID OUT LOUD every session instead of being discovered on promotion day.

    Also names every registered strategy that has no engine channel, so "measured, not
    traded" reads as a decision in the log rather than an absence from it.
    """
    from signal_engine import strategies
    from signal_engine.config import validate_channels

    problems = validate_channels(settings.telegram_channels)
    for problem in problems:
        logger.warning(f"Config: {problem}")

    configured = {ch.name.lower() for ch in settings.telegram_channels}
    for tag, meta in sorted(strategies.REGISTRY.items()):
        if meta.channel_base is None:
            logger.info(
                f"Strategy {tag}: no engine channel by design"
                + (f" - {meta.note}" if meta.note else "")
            )
            continue
        missing = [
            f"{meta.channel_base}-{phase}"
            for phase in ("analyze", "live")
            if f"{meta.channel_base}-{phase}" not in configured
        ]
        if missing:
            problems.append(f"Strategy {tag} has no {', '.join(missing)} channel")
            logger.warning(
                f"Config: strategy {tag} is registered with channel base "
                f"'{meta.channel_base}' but {', '.join(missing)} is not in "
                "telegram.channels"
            )
    return problems


def log_startup_banner() -> None:
    """Log the effective configuration the engine is about to run with."""
    logger.info("Signal Engine starting")
    log_config_problems()
    logger.info(f"Sizing mode: {settings.sizing_mode}")
    if settings.use_day_start_capital:
        logger.info("Day-start capital: enabled (equal risk per trade)")
    logger.info(f"Min R:R: {settings.min_rr}")
    logger.info(f"Position poll interval: {settings.poll_interval}s")
    for ch in settings.telegram_channels:
        logger.info(f"Telegram channel: {ch.name} ({ch.id})")


# ---------------------------------------------------------------------------
# Startup health checks
# ---------------------------------------------------------------------------


async def run_startup_health_checks():
    """Run pre-flight checks before accepting any signals.

    A critical failure aborts startup and sends the failure alert here, immediately.
    A passing (or warning-only) result sends nothing itself — the caller still needs
    trading mode and capital to build the one consolidated startup message
    (notifier.notify_startup_summary), so it folds this report into that instead of
    this function sending its own separate message.

    Returns the SmokeTestReport if the engine may start, or None if startup must
    abort (the failure notification has already been sent).
    """
    from signal_engine.smoke_test import _CRITICAL_CHECKS, _WARNING_CHECKS, run_startup_checks

    startup_report = await run_startup_checks()
    startup_report.print()

    failed_critical = [
        c for c in startup_report.checks if not c.passed and c.name in _CRITICAL_CHECKS
    ]
    failed_warnings = [
        c for c in startup_report.checks if not c.passed and c.name in _WARNING_CHECKS
    ]

    if failed_warnings:
        warn_lines = "\n".join(f"  WARN {c.name}: {c.message}" for c in failed_warnings)
        logger.warning(f"Startup warnings (non-fatal):\n{warn_lines}")

    if failed_critical:
        fail_lines = "\n".join(f"  FAIL {c.name}: {c.message}" for c in failed_critical)
        logger.critical(f"Startup checks FAILED — aborting:\n{fail_lines}")
        summary = "\n".join(f"FAIL: {c.name}\n  {c.message}" for c in failed_critical)
        await notifier.notify_startup_result(all_passed=False, summary=summary)
        return None

    return startup_report


# ---------------------------------------------------------------------------
# Position reconciliation
# ---------------------------------------------------------------------------

#: OpenAlgo reports the product either in full or as the broker's single-letter code,
#: depending on the broker. Both spellings have to be recognised as "still open".
_BROKER_PRODUCT_CODES = {"MIS": "I", "CNC": "C", "NRML": "M"}


def _configured_products() -> set:
    """Every product this engine may hold a position under, in both spellings.

    This used to be settings.product alone. A strategy with a
    strategy_profiles.<TAG>.product override (CNC, say) therefore had its still-open
    position filtered OUT of open_broker_positions — and the loop below, finding the symbol
    absent from open_broker_symbols but present in by_symbol, booked it as a reconciled
    EXIT. That closes a live position in the ledger while it is still open at the broker.
    Latent while every profile is MIS; a correctness bug the day one is not.
    """
    products = {str(settings.product or "").upper()}
    for profile in (settings.strategy_profiles or {}).values():
        product = (profile or {}).get("product")
        if product:
            products.add(str(product).upper())
    products.discard("")
    return products | {_BROKER_PRODUCT_CODES[p] for p in products if p in _BROKER_PRODUCT_CODES}


async def reconcile_open_positions(risk_engine, tracker) -> None:
    """Reconcile stored open_positions against the broker and restore tracker state.

    Without restoration the in-memory tracker is empty after a restart, so any
    subsequent TP HIT alert hits the fallback path with EXIT-signal zeroes
    (entry=sl=tp=0) and the partial-exit SL re-placement is skipped, leaving the
    runner qty un-protected. RBLBANK incident, 2026-05-04.

    Also detects the OPPOSITE gap: a position trades.db still shows as open, but the broker
    already closed it (e.g. a stop-loss filled while the engine was crash-looping). The
    original version of this function only ever walked the broker's currently-NONZERO
    positions, so a closed position simply never appeared anywhere in it - trades.db and the
    risk engine's realised-loss counters stayed wrong indefinitely, silently, with nothing to
    point at. HINDALCO incident, 2026-09-08: a stopped-out BREAKOUT position was correctly flat
    at the broker (realized loss -363.80) while trades.db and the daily-loss counter both still
    thought it was open.
    """
    from signal_engine import db

    locally_open = db.fetch_all_open_positions()
    if risk_engine.total_open_positions() <= 0 and not locally_open:
        return

    from signal_engine.api_client import fetch_positionbook

    positions = await fetch_positionbook()
    if positions is None:
        logger.warning("Position reconciliation: could not fetch positionbook, skipping")
        return

    configured_product = settings.product  # MIS or CNC — the default for restored positions
    products = _configured_products()
    open_broker_positions = [
        p
        for p in positions
        if int(p.get("quantity", 0)) != 0 and p.get("product", "").upper() in products
    ]
    open_broker_symbols = {p.get("symbol", "") for p in open_broker_positions}
    by_symbol = {p.get("symbol", ""): p for p in positions}

    for pos in locally_open:
        symbol = pos["symbol"]
        if symbol in open_broker_symbols:
            continue  # still genuinely open - handled by the restore path below
        broker_row = by_symbol.get(symbol)
        if broker_row is None:
            logger.warning(
                f"Reconciliation: {symbol} ({pos['strategy']}) is open in trades.db but the "
                "broker has no record of it at all - cannot recover a P&L, leaving as-is"
            )
            continue
        pnl = float(broker_row.get("today_realized_pnl", broker_row.get("pnl", 0)) or 0)
        fill_price = float(broker_row.get("ltp", 0) or 0)
        note = (
            f"Reconciled at startup: broker shows {symbol} flat with realized P&L {pnl:+.2f}, "
            "but trades.db had no EXIT recorded - likely closed while the engine was down."
        )
        logger.warning(note)
        db.save_reconciled_exit(
            pos["strategy"],
            symbol,
            pos["entry"],
            pos["sl"],
            pos["tp"],
            pos["quantity"],
            fill_price,
            pnl,
            note,
            sig_id=pos.get("sig_id"),
        )
        risk_engine.record_close(pnl, strategy=pos["strategy"], symbol=symbol)
        try:
            await notifier.notify_position_closed(
                symbol,
                pnl,
                strategy=pos["strategy"],
                exit_price=fill_price,
                entry_price=pos["entry"],
                day_context="(reconciled after restart - exact fill time unknown)",
            )
        except Exception:
            logger.warning(f"Could not send reconciliation notification for {symbol}")

    actual_open = len(open_broker_positions)

    if risk_engine.isolates_per_strategy:
        # ANALYZE: each strategy's own open_positions counter is corrected independently.
        # Broker positions don't carry a strategy tag — attribute each one to the strategy
        # of the matching still-open LOCAL position (locally_open rows do carry it).
        open_by_strategy: dict = {}
        unattributed = 0
        locally_open_by_symbol = {pos["symbol"]: pos for pos in locally_open}
        for p in open_broker_positions:
            local = locally_open_by_symbol.get(p.get("symbol", ""))
            if local is None:
                unattributed += 1
                continue
            open_by_strategy[local["strategy"]] = open_by_strategy.get(local["strategy"], 0) + 1
        if unattributed:
            logger.warning(
                f"Position reconciliation: {unattributed} broker position(s) have no local "
                "record at all — cannot attribute to a strategy, excluded from per-strategy correction"
            )

        # Every bucket the risk engine has loaded is included, not just the ones with a
        # position right now — otherwise a strategy carrying a stale non-zero counter and NO
        # local open rows is never visited and keeps that phantom slot all day. At
        # max_open_positions=2 one phantom slot is half the account's capacity.
        known_strategies = (
            set(open_by_strategy)
            | {pos["strategy"] for pos in locally_open}
            | risk_engine.known_strategies()
        )
        for strategy in known_strategies:
            actual = open_by_strategy.get(strategy, 0)
            stored = risk_engine.open_positions_for(strategy)
            if actual != stored:
                logger.warning(
                    f"[{strategy}] Position mismatch: stored open_positions={stored}, "
                    f"broker reports {actual} open — correcting and persisting"
                )
                risk_engine.set_open_positions(strategy, actual)
            else:
                logger.info(
                    f"[{strategy}] Position reconciliation: stored={stored} matches broker={actual}"
                )
    else:
        # LIVE: one shared counter across every strategy — every strategy name resolves
        # to the same pooled bucket (RiskEngine._key), so correct it ONCE against the
        # broker's TOTAL, never per-strategy (that would overwrite the shared total with
        # each strategy's own partial count in turn, last one winning).
        representative_strategy = locally_open[0]["strategy"] if locally_open else "live"
        stored = risk_engine.open_positions_for(representative_strategy)
        if actual_open != stored:
            logger.warning(
                f"Position mismatch (pooled live counter): stored open_positions={stored}, "
                f"broker reports {actual_open} open — correcting and persisting"
            )
            risk_engine.set_open_positions(representative_strategy, actual_open)
        else:
            logger.info(
                f"Position reconciliation (pooled live counter): stored={stored} matches "
                f"broker={actual_open}"
            )

    sl_orders = await _recover_sl_order_ids(open_broker_positions)
    restored = _restore_tracker_positions(
        tracker, open_broker_positions, configured_product, sl_orders
    )
    if restored > 0:
        logger.info(f"Tracker restoration complete: {restored}/{actual_open} position(s) restored")


#: Order types that ARE the bracket stop. A working LIMIT order is something else entirely
#: and must never be cancelled as if it were the stop.
_SL_ORDER_TYPES = ("SL-M", "SL", "SLM", "SL-LIMIT")

#: Order statuses that mean the order is still working at the broker. Anything else
#: (complete, cancelled, rejected) is not a stop that needs cancelling before an exit.
_WORKING_STATUSES = ("open", "trigger pending", "trigger_pending", "pending", "placed")


async def _recover_sl_order_ids(open_broker_positions) -> dict:
    """Live stop-order ids for the symbols still open, from the broker orderbook.

    Best-effort: an unreachable orderbook leaves every restored position without a stop id,
    which is exactly the pre-2026-09-11 behaviour — worse than knowing, but not worse than
    failing startup over it. It is logged loudly because the consequence (a rejected exit)
    shows up much later and looks like something else.
    """
    if not open_broker_positions:
        return {}
    from signal_engine.api_client import fetch_orderbook

    orderbook = await fetch_orderbook()
    if orderbook is None:
        logger.warning(
            "SL recovery: could not fetch the orderbook — restored positions will have no "
            "stop-order id, so an exit signal may be rejected as a new short "
            "(FUND LIMIT INSUFFICIENT). Verify open stops manually."
        )
        return {}
    matched = _match_open_sl_orders(orderbook)
    open_symbols = {str(p.get("symbol", "")).upper() for p in open_broker_positions}
    recovered = {sym: oid for sym, oid in matched.items() if sym in open_symbols}
    missing = open_symbols - set(recovered)
    if recovered:
        logger.info(f"SL recovery: matched working stop orders for {sorted(recovered)}")
    if missing:
        logger.warning(
            f"SL recovery: no working stop order found for {sorted(missing)} — these "
            "positions are restored WITHOUT broker-side stop protection recorded."
        )
    return recovered


def _match_open_sl_orders(orderbook) -> dict:
    """symbol -> order id of the WORKING stop order for that symbol.

    A restored position with no sl_order_id makes main._cancel_sl_before_exit() a no-op, so
    the next TP/EXIT places a SELL while the broker's SL-M is still live — which an Indian
    broker reads as a new SHORT and rejects with FUND LIMIT INSUFFICIENT. Recovering the id
    from the broker's own orderbook is what makes a restart safe.

    Later orders win: after a partial exit the stop is cancelled and re-placed, so the last
    working one for a symbol is the live one.
    """
    matched: dict = {}
    for order in orderbook or []:
        if not isinstance(order, dict):
            continue
        order_type = str(order.get("pricetype") or order.get("price_type") or "").upper()
        if order_type not in _SL_ORDER_TYPES:
            continue
        status = str(order.get("order_status") or order.get("status") or "").lower()
        if status not in _WORKING_STATUSES:
            continue
        symbol = str(order.get("symbol") or "").upper()
        order_id = str(order.get("orderid") or order.get("order_id") or "")
        if symbol and order_id:
            matched[symbol] = order_id
    return matched


def _restore_tracker_positions(
    tracker, open_broker_positions, configured_product, sl_orders: dict = None
) -> int:
    """Rebuild tracker entries from trades.db so exit paths see real entry/sl/tp values.

    `sl_orders` is _match_open_sl_orders()'s symbol -> live stop id map; a symbol missing
    from it keeps the old blank, which is honest — the engine simply does not know of a stop
    for it.

    2026-09-24: when multiple strategies traded the same symbol today (see
    _lookup_entry_legs), this restores ONE TrackedPosition per entry leg instead of
    collapsing them into one under the broker's combined quantity. Only the first leg can
    carry the recovered sl_order_id — _match_open_sl_orders keeps just one working-stop id
    per symbol — but PositionTracker._retry_missing_sl (added the same day) re-protects any
    other leg from the next poll cycle on, so a blank id here is temporary, not permanent.

    Returns the number of positions restored.
    """
    sl_orders = sl_orders or {}
    restored = 0
    for bp in open_broker_positions:
        bsymbol = bp.get("symbol", "")
        if not bsymbol:
            continue
        bqty = abs(int(bp.get("quantity", 0)))
        bdir = Direction.LONG if int(bp.get("quantity", 0)) > 0 else Direction.SHORT
        bexch = bp.get("exchange", settings.exchange)
        bprod = configured_product

        legs = _lookup_entry_legs(bsymbol, broker_qty=bqty, is_long=(bdir == Direction.LONG))
        if not legs:
            logger.warning(
                f"Tracker restore [{bsymbol}]: no entry trade in trades.db today "
                "— skipping (TP/SL alerts will use fallback path)"
            )
            continue

        recovered_sl = sl_orders.get(bsymbol.upper(), "")
        for i, (strategy_for_pos, found) in enumerate(legs):
            if tracker.find_position(bsymbol, strategy_for_pos) is not None:
                continue

            # Single-entry case (the common one): the broker's whole netted quantity is
            # this leg's. Split case: each leg gets its OWN recorded quantity, not the
            # combined total.
            leg_qty = found["quantity"] if len(legs) > 1 else bqty

            tracker.register(
                TrackedPosition(
                    symbol=bsymbol,
                    strategy=strategy_for_pos,
                    exchange=bexch,
                    product=bprod,
                    entry_price=found["entry"],
                    quantity=leg_qty,
                    sl=found["sl"],
                    tp=found["tp"],
                    direction=bdir,
                    entry_order_id=found["order_id"],
                    sl_order_id=recovered_sl if i == 0 else "",
                    fill_price=float(bp.get("average_price", 0) or 0),
                    ever_seen_nonzero_qty=True,
                    sig_id=found.get("sig_id", ""),
                )
            )
            restored += 1
            if i == 0:
                sl_note = (
                    f" sl_order={recovered_sl}"
                    if recovered_sl
                    else " sl_order=UNKNOWN (no working stop found in the orderbook)"
                )
            else:
                sl_note = " sl_order=UNKNOWN (multi-leg restore — poll loop will re-protect)"
            logger.info(
                f"Tracker restored [{bsymbol}:{strategy_for_pos}]: qty={leg_qty} "
                f"entry={found['entry']} sl={found['sl']} tp={found['tp']}{sl_note}"
            )
    return restored


def _gather_candidate_entries(bsymbol: str) -> list:
    """Today's most recent SUCCESS entry row for every known strategy that traded `bsymbol`.

    Returns a list of (strategy, row) tuples, one per strategy with a matching row — shared
    by _lookup_entry_trade (single best match) and _lookup_entry_legs (multi-leg restore).
    """
    candidates = []
    seen = set()
    for strategy in ["ORB", *settings.strategy_profiles.keys()]:
        key = strategy.upper()
        if key in seen:
            continue
        seen.add(key)
        row = fetch_last_entry_trade(bsymbol, strategy)
        if row is not None:
            candidates.append((strategy, row))
    return candidates


def _lookup_entry_trade(bsymbol: str, broker_qty: int = 0, is_long: bool = True):
    """Find today's SINGLE best-matching entry trade for a symbol.

    Returns (trade_row_or_None, strategy_name). This used to try "ORB" first and then
    settings.strategy_profiles in DICT ORDER, taking the first hit — so a symbol traded by
    two strategies today was attributed to whichever happened to be checked first, and the
    restored position carried the wrong strategy's entry/SL/TP into every later exit
    decision. Now every candidate row is collected and scored against what the BROKER
    actually reports:

      1. quantity and direction both match  - unambiguous, take it
      2. direction matches                  - the side is right, the size may be a partial
      3. otherwise                          - most recent executed_at wins

    Ties inside a tier are broken by executed_at, latest first.

    Callers restoring a position for real should use _lookup_entry_legs instead — this
    single-result form is what it falls back to when a clean multi-leg split isn't possible,
    and is kept directly callable for its own coverage.
    """
    candidates = _gather_candidate_entries(bsymbol)
    if not candidates:
        return None, "ORB"

    wanted_direction = "LONG" if is_long else "SHORT"

    def _sort_key(item):
        _strategy, row = item
        direction_ok = str(row.get("direction", "")).upper() == wanted_direction
        qty_ok = broker_qty > 0 and int(row.get("quantity", 0) or 0) == int(broker_qty)
        tier = 0 if (direction_ok and qty_ok) else (1 if direction_ok else 2)
        return (tier, _invert_timestamp(row.get("executed_at")))

    strategy, row = min(candidates, key=_sort_key)
    if len(candidates) > 1:
        logger.info(
            f"Tracker restore [{bsymbol}]: {len(candidates)} strategies traded this symbol "
            f"today ({', '.join(s for s, _ in candidates)}) - attributed to {strategy} "
            f"(broker qty={broker_qty}, side={wanted_direction})"
        )
    return row, strategy


def _lookup_entry_legs(bsymbol: str, broker_qty: int = 0, is_long: bool = True) -> list:
    """Find today's entry trade(s) for a symbol, split per strategy leg when the broker
    position is really the sum of several.

    Returns a list of (strategy, row) tuples — one element in the common case, several when
    multiple strategies each entered this symbol today, in the SAME direction as the broker
    currently holds, with their own quantities summing EXACTLY to the broker's current
    netted quantity. Anything less clean (a partial fill, a manually adjusted broker
    quantity, three-or-more-way ambiguity) falls back to _lookup_entry_trade's single
    best-match behaviour, unchanged — a clean split should never be guessed at.

    2026-09-24: _restore_tracker_positions used to call _lookup_entry_trade directly and
    collapse a same-symbol multi-strategy day into ONE TrackedPosition holding the broker's
    combined quantity under a single arbitrarily-chosen entry. When that merged position
    later closed (a mass restart-triggered exit batch, in the incident that found this),
    exactly one trades.db exit row was written — for the chosen entry's full combined
    quantity — permanently orphaning every other entry's own exit. Confirmed for 7 symbols
    that day (APLAPOLLO, ASIANPAINT, BAJFINANCE, HINDZINC, ICICIPRULI, JIOFIN, PRESTIGE),
    each with exactly two entries (BREAKINGTRADE + BREAKINGTRADE-WATCHLIST) whose quantities
    summed exactly to the broker's reported total.
    """
    candidates = _gather_candidate_entries(bsymbol)
    if not candidates:
        return []

    wanted_direction = "LONG" if is_long else "SHORT"
    same_direction = [
        (s, r) for s, r in candidates if str(r.get("direction", "")).upper() == wanted_direction
    ]
    if (
        len(same_direction) > 1
        and broker_qty > 0
        and sum(int(r.get("quantity", 0) or 0) for _, r in same_direction) == broker_qty
    ):
        logger.info(
            f"Tracker restore [{bsymbol}]: {len(same_direction)} strategies traded this "
            f"symbol today ({', '.join(s for s, _ in same_direction)}), quantities sum "
            f"exactly to the broker's {broker_qty} - restoring one position per entry "
            "instead of merging them"
        )
        # Oldest first: an arbitrary but stable and deterministic choice for which leg is
        # "first" (and so gets the one recoverable sl_order_id, if any — see caller).
        return sorted(same_direction, key=lambda item: item[1].get("executed_at") or "")

    row, strategy = _lookup_entry_trade(bsymbol, broker_qty=broker_qty, is_long=is_long)
    return [(strategy, row)] if row is not None else []


def _invert_timestamp(executed_at) -> tuple:
    """Sort key that puts the LATEST executed_at first under an ascending sort.

    A missing timestamp sorts last rather than first — an undated row is the weakest
    evidence available, not the strongest.
    """
    if not executed_at:
        return (1, "")
    return (
        0,
        "".join(chr(0x10FFFF - ord(c)) if ord(c) < 0x10FFFF else c for c in str(executed_at)),
    )


# ---------------------------------------------------------------------------
# Engine lifecycle
# ---------------------------------------------------------------------------


def _resolve_broker_name() -> str:
    """Best-effort broker name for the startup message — never blocks startup."""
    try:
        from signal_engine.scripts.openalgoscheduler import get_broker_name

        return get_broker_name()
    except Exception:
        return "unknown"


def start_engine(risk_engine, tracker, handle_message) -> None:
    """Run the engine event loop until shutdown."""
    try:
        asyncio.run(_run_engine(risk_engine, tracker, handle_message))
    except KeyboardInterrupt:
        logger.info("Signal Engine interrupted")
        sys.exit(0)


async def _run_engine(risk_engine, tracker, handle_message) -> None:
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received, stopping gracefully...")
        shutdown_event.set()

    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    startup_report = await run_startup_health_checks()
    if startup_report is None:
        shutdown_event.set()
        return

    mode_str, is_analyze = await fetch_trading_mode()
    if is_analyze:
        logger.info("Trading mode: ANALYZE (sandbox capital)")
    else:
        logger.info("Trading mode: LIVE (broker capital)")

    # risk_engine was built at import, before the mode could be known, so it defaults to
    # "live". Correct it here -- BEFORE reconcile and before any signal -- so the mode's own
    # limits apply and its losses land in the mode's own risk_store row.
    trade_mode = "analyze" if is_analyze else "live"
    apply_trade_mode(risk_engine, trade_mode)
    # Arm the guard against this phase. The tracker's poll loop re-checks OpenAlgo from here
    # on and halts new entries if the mode changes underneath a session configured for the
    # old one — see mode_guard's module docstring for what diverges when it does.
    mode_guard.set_startup_phase(trade_mode)
    # Stamp every trades.db row with the mode that produced it, so a paper week and a live
    # week never have to be told apart by date.
    set_trade_mode(trade_mode)
    # Same distinction, for the log files - see logger_setup.set_mode()'s docstring.
    set_log_mode(trade_mode)

    await reconcile_open_positions(risk_engine, tracker)

    # Log risk state summary (restored counters + config)
    startup_capital = await fetch_available_capital()
    if startup_capital > 0:
        risk_engine.log_startup_summary(startup_capital)

    mode_label = "ANALYZE" if is_analyze else "LIVE"
    await notifier.notify_startup_summary(
        startup_report, mode_label, startup_capital, _resolve_broker_name()
    )

    tracker_task = asyncio.create_task(tracker.start())
    time_exit_scheduler, time_exit_task = _start_time_exit_scheduler(tracker)

    try:
        await _serve_until_shutdown(handle_message, shutdown_event)
    finally:
        tracker.stop()
        tracker_task.cancel()
        if time_exit_task is not None:
            time_exit_scheduler.stop()
            time_exit_task.cancel()
        await _close_resources(risk_engine)
        logger.info("Signal Engine stopped")


async def _close_resources(risk_engine) -> None:
    """Release the process's long-lived handles on the way out.

    All three are shared singletons rather than per-call objects (that is the point - see
    api_client._get_client and db._get_connection), so nothing reclaims them until the
    process exits. Closing them explicitly keeps a deliberate shutdown clean and makes the
    ownership obvious; each is best-effort because a failure here must not mask whatever
    actually stopped the engine.
    """
    from signal_engine import api_client, db

    try:
        await api_client.close_client()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Shutdown: could not close the HTTP client: {e}")

    try:
        db.reset_connection()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Shutdown: could not close trades.db: {e}")

    store = getattr(risk_engine, "_store", None)
    if store is not None and hasattr(store, "close"):
        try:
            store.close()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Shutdown: could not close risk.db: {e}")


def _start_time_exit_scheduler(tracker):
    """Start the square-off scheduler if enabled. Returns (scheduler, task) or (None, None)."""
    if not settings.time_exit_enabled:
        return None, None
    scheduler = TimeExitScheduler(tracker, settings.time_exit_hour, settings.time_exit_minute)
    task = asyncio.create_task(scheduler.start())
    logger.info(
        f"Time exit enabled: {settings.time_exit_hour:02d}:{settings.time_exit_minute:02d} IST"
    )
    return scheduler, task


async def _serve_until_shutdown(handle_message, shutdown_event) -> None:
    """Run until a shutdown signal arrives — NOT until the Telegram listener stops.

    A listener that exhausts its reconnect budget used to end the session: this function
    returned, and _run_engine()'s finally block stopped the tracker and the time-exit
    scheduler with it. On 2026-09-08 that happened sixteen times between 11:15 and 11:39
    IST, each time leaving open positions with no close detection, no no-progress gate and
    no 14:45 square-off (see logs/errors_2026-09-08.jsonl and listener.py's flood-wait
    handling).

    Losing the signal feed is bad; abandoning open risk because the signal feed died is
    worse. A dead listener now degrades the session to "manage what is already open, accept
    no new signals" and says so loudly, and only an actual shutdown signal ends it.
    """
    listener_task = asyncio.create_task(start_listener(handle_message))
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, _ = await asyncio.wait(
        [listener_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED
    )

    if listener_task in done and not shutdown_event.is_set():
        await _enter_degraded_mode()
        await shutdown_task

    # Send stopped notification while the Telethon client is still alive
    # (listener_task not yet cancelled — must happen before task.cancel())
    try:
        await asyncio.wait_for(notifier.notify_engine_stopped(), timeout=5.0)
    except Exception:
        logger.debug("Could not send engine stopped notification")
    for task in (listener_task, shutdown_task):
        if not task.done():
            task.cancel()


async def _enter_degraded_mode() -> None:
    """Announce that no further signals will arrive, and block new entries.

    Open positions keep their tracker, their stop-losses and their time exit. What stops is
    NEW risk: with no signal feed the engine cannot see a TP or EXIT alert either, so taking
    a fresh position would mean opening something it has no way to be told to close.
    """
    reason = (
        "Telegram listener exhausted its reconnect budget - NO NEW SIGNALS will be received. "
        "Open positions keep their stop-loss, no-progress gates and time exit; new entries "
        "are blocked. Restart the stack to restore the feed."
    )
    logger.critical(f"DEGRADED MODE: {reason}")
    mode_guard.halt(reason)
    try:
        await asyncio.wait_for(
            notifier.notify_event("listener_degraded", f"DEGRADED MODE\n{reason}"), timeout=5.0
        )
    except Exception:
        logger.warning("Could not send degraded-mode alert")
