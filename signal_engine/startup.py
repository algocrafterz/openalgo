"""Process startup, CLI flags, and engine lifecycle.

Separated from main.py so the trading pipeline is not interleaved with process
wiring. Collaborators (risk engine, tracker, message handler) are passed in
rather than imported, so this module holds no global state of its own.
"""

import asyncio
import signal as signal_module
import sys

from loguru import logger

from signal_engine.api_client import fetch_available_capital, fetch_trading_mode
from signal_engine.config import settings
from signal_engine.db import fetch_last_entry_trade
from signal_engine.listener import start_listener
from signal_engine.models import Direction
from signal_engine import notifier
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

    from signal_engine.smoke_test import run_smoke_test, run_dry_run
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


def log_startup_banner() -> None:
    """Log the effective configuration the engine is about to run with."""
    logger.info("Signal Engine starting")
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

async def run_startup_health_checks() -> bool:
    """Run pre-flight checks before accepting any signals.

    Critical failures abort startup and notify via Telegram. Warning failures are
    logged but allow startup. Returns True if the engine may start.
    """
    from signal_engine.smoke_test import run_startup_checks, _CRITICAL_CHECKS, _WARNING_CHECKS

    startup_report = await run_startup_checks()
    startup_report.print()

    failed_critical = [c for c in startup_report.checks if not c.passed and c.name in _CRITICAL_CHECKS]
    failed_warnings = [c for c in startup_report.checks if not c.passed and c.name in _WARNING_CHECKS]

    if failed_warnings:
        warn_lines = "\n".join(f"  WARN {c.name}: {c.message}" for c in failed_warnings)
        logger.warning(f"Startup warnings (non-fatal):\n{warn_lines}")

    if failed_critical:
        fail_lines = "\n".join(f"  FAIL {c.name}: {c.message}" for c in failed_critical)
        logger.critical(f"Startup checks FAILED — aborting:\n{fail_lines}")
        summary = "\n".join(f"FAIL: {c.name}\n  {c.message}" for c in failed_critical)
        await notifier.notify_startup_result(all_passed=False, summary=summary)
        return False

    warn_note = ""
    if failed_warnings:
        warn_note = "\nWarnings:\n" + "\n".join(f"  {c.name}" for c in failed_warnings)
    await notifier.notify_startup_result(all_passed=True, summary=f"All critical checks passed.{warn_note}")
    return True


# ---------------------------------------------------------------------------
# Position reconciliation
# ---------------------------------------------------------------------------

async def reconcile_open_positions(risk_engine, tracker) -> None:
    """Reconcile stored open_positions against the broker and restore tracker state.

    Without restoration the in-memory tracker is empty after a restart, so any
    subsequent TP HIT alert hits the fallback path with EXIT-signal zeroes
    (entry=sl=tp=0) and the partial-exit SL re-placement is skipped, leaving the
    runner qty un-protected. RBLBANK incident, 2026-05-04.
    """
    if risk_engine.open_positions <= 0:
        return

    from signal_engine.api_client import fetch_positionbook
    positions = await fetch_positionbook()
    if positions is None:
        logger.warning("Position reconciliation: could not fetch positionbook, skipping")
        return

    configured_product = settings.product  # MIS or CNC
    product_map = {"MIS": "I", "CNC": "C", "NRML": "M"}
    broker_product = product_map.get(configured_product, configured_product)
    open_broker_positions = [
        p for p in positions
        if int(p.get("quantity", 0)) != 0
        and p.get("product", "").upper() in (configured_product, broker_product)
    ]
    actual_open = len(open_broker_positions)
    if actual_open != risk_engine.open_positions:
        logger.warning(
            f"Position mismatch: stored open_positions={risk_engine.open_positions}, "
            f"broker reports {actual_open} open — correcting and persisting"
        )
        risk_engine.open_positions = actual_open
        risk_engine._persist()
    else:
        logger.info(f"Position reconciliation: stored={risk_engine.open_positions} matches broker={actual_open}")

    restored = _restore_tracker_positions(tracker, open_broker_positions, configured_product)
    if restored > 0:
        logger.info(f"Tracker restoration complete: {restored}/{actual_open} position(s) restored")


def _restore_tracker_positions(tracker, open_broker_positions, configured_product) -> int:
    """Rebuild tracker entries from trades.db so exit paths see real entry/sl/tp values.

    Returns the number of positions restored.
    """
    restored = 0
    for bp in open_broker_positions:
        bsymbol = bp.get("symbol", "")
        if not bsymbol:
            continue
        bqty = abs(int(bp.get("quantity", 0)))
        bdir = Direction.LONG if int(bp.get("quantity", 0)) > 0 else Direction.SHORT
        bexch = bp.get("exchange", settings.exchange)
        bprod = configured_product

        found, strategy_for_pos = _lookup_entry_trade(bsymbol)
        if found is None:
            logger.warning(
                f"Tracker restore [{bsymbol}]: no entry trade in trades.db today "
                "— skipping (TP/SL alerts will use fallback path)"
            )
            continue
        if tracker.find_position(bsymbol, strategy_for_pos) is not None:
            continue

        tracker.register(TrackedPosition(
            symbol=bsymbol,
            strategy=strategy_for_pos,
            exchange=bexch,
            product=bprod,
            entry_price=found["entry"],
            quantity=bqty,
            sl=found["sl"],
            tp=found["tp"],
            direction=bdir,
            entry_order_id=found["order_id"],
            sl_order_id="",  # unknown after restart; new SL placed only on partial-exit
            fill_price=float(bp.get("average_price", 0) or 0),
            ever_seen_nonzero_qty=True,
        ))
        restored += 1
        logger.info(
            f"Tracker restored [{bsymbol}:{strategy_for_pos}]: qty={bqty} "
            f"entry={found['entry']} sl={found['sl']} tp={found['tp']}"
        )
    return restored


def _lookup_entry_trade(bsymbol: str):
    """Find today's most recent entry trade for a symbol across configured strategies.

    Returns (trade_row_or_None, strategy_name).
    """
    found = fetch_last_entry_trade(bsymbol, "ORB")
    if found is not None:
        return found, "ORB"
    for sk in settings.strategy_profiles.keys():
        found = fetch_last_entry_trade(bsymbol, sk)
        if found is not None:
            return found, sk
    return None, "ORB"


# ---------------------------------------------------------------------------
# Engine lifecycle
# ---------------------------------------------------------------------------

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

    if not await run_startup_health_checks():
        shutdown_event.set()
        return

    mode_str, is_analyze = await fetch_trading_mode()
    if is_analyze:
        logger.info("Trading mode: ANALYZE (sandbox capital)")
    else:
        logger.info("Trading mode: LIVE (broker capital)")

    await reconcile_open_positions(risk_engine, tracker)

    # Log risk state summary (restored counters + config)
    startup_capital = await fetch_available_capital()
    if startup_capital > 0:
        risk_engine.log_startup_summary(startup_capital)
        mode_label = "ANALYZE" if is_analyze else "LIVE"
        await notifier.notify_engine_started(startup_capital, mode_label)

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
        logger.info("Signal Engine stopped")


def _start_time_exit_scheduler(tracker):
    """Start the square-off scheduler if enabled. Returns (scheduler, task) or (None, None)."""
    if not settings.time_exit_enabled:
        return None, None
    scheduler = TimeExitScheduler(
        tracker, settings.time_exit_hour, settings.time_exit_minute
    )
    task = asyncio.create_task(scheduler.start())
    logger.info(
        f"Time exit enabled: {settings.time_exit_hour:02d}:{settings.time_exit_minute:02d} IST"
    )
    return scheduler, task


async def _serve_until_shutdown(handle_message, shutdown_event) -> None:
    """Run the Telegram listener until it finishes or a shutdown signal arrives."""
    listener_task = asyncio.create_task(start_listener(handle_message))
    _, pending = await asyncio.wait(
        [listener_task, asyncio.create_task(shutdown_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    # Send stopped notification while the Telethon client is still alive
    # (listener_task not yet cancelled — must happen before task.cancel())
    try:
        await asyncio.wait_for(notifier.notify_engine_stopped(), timeout=5.0)
    except Exception:
        logger.debug("Could not send engine stopped notification")
    for task in pending:
        task.cancel()
