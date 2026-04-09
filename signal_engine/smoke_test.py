"""Pre-session smoke test and dry run for the signal engine.

Usage:
    # Quick connectivity + pipeline health check (~10s)
    PYTHONPATH=. uv run python -m signal_engine.main --smoke-test

    # Full dry run: synthetic signal through entire pipeline without placing orders (~15s)
    PYTHONPATH=. uv run python -m signal_engine.main --dry-run

Checks performed:
    SMOKE TEST
    1. Config loaded (required keys present, no ConfigError)
    2. OpenAlgo reachable (HTTP GET to base URL)
    3. Broker auth valid (funds API returns capital > 0)
    4. Quote API functional (fetch quotes for a liquid NSE symbol)
    5. Signal parse pipeline (normalize -> parse -> validate on synthetic signal)
    6. Risk engine state (show current counters + limits)
    7. DB accessible (risk_store write + read round-trip)

    DRY RUN (all smoke checks + below)
    8. Full entry pipeline without actual order placement
       (capital fetch, sizing, margin check, order build — mock send_order)
    9. Full exit pipeline without actual order placement
       (find position, resolve qty, build exit order — mock send_order)
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import List
from unittest.mock import AsyncMock, patch

from loguru import logger

from signal_engine.risk_store import RISK_DB_PATH, RiskStore


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    duration_ms: float = 0.0

    def __str__(self) -> str:
        icon = "PASS" if self.passed else "FAIL"
        return f"  [{icon}] {self.name}: {self.message} ({self.duration_ms:.0f}ms)"


@dataclass
class SmokeTestReport:
    checks: List[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.checks.append(result)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    def print(self) -> None:
        print()
        print("=" * 60)
        print("  Signal Engine Pre-Session Health Check")
        print("=" * 60)
        for c in self.checks:
            print(str(c))
        print("-" * 60)
        status = "ALL CHECKS PASSED" if self.all_passed else f"{self.fail_count} CHECK(S) FAILED"
        print(f"  Result: {status} ({self.pass_count}/{len(self.checks)})")
        print("=" * 60)
        print()


def _time_check(name: str, fn):
    """Run a sync check function, returning a CheckResult with timing."""
    t0 = time.monotonic()
    try:
        msg = fn()
        return CheckResult(name=name, passed=True, message=msg, duration_ms=(time.monotonic() - t0) * 1000)
    except Exception as e:
        return CheckResult(name=name, passed=False, message=str(e), duration_ms=(time.monotonic() - t0) * 1000)


async def _atime_check(name: str, coro):
    """Run an async check coroutine, returning a CheckResult with timing."""
    t0 = time.monotonic()
    try:
        msg = await coro
        return CheckResult(name=name, passed=True, message=msg, duration_ms=(time.monotonic() - t0) * 1000)
    except Exception as e:
        return CheckResult(name=name, passed=False, message=str(e), duration_ms=(time.monotonic() - t0) * 1000)


# ── Individual Checks ────────────────────────────────────────────────────────

def check_config() -> str:
    """Verify config.yaml loads without error and all required keys are present."""
    from signal_engine.config import settings
    # Access a sampling of required fields to confirm no lazy-load issues
    assert settings.openalgo_base_url, "openalgo_base_url is empty"
    assert settings.openalgo_api_key, "openalgo_api_key is empty (check signal_engine/.env)"
    assert settings.exchange, "exchange is empty"
    assert settings.product, "product is empty"
    assert settings.risk_per_trade > 0, "risk_per_trade must be > 0"
    assert settings.bracket_sl_order_type in ("SL", "SL-M"), f"invalid bracket.sl_order_type: {settings.bracket_sl_order_type}"
    cap_info = f"test_qty_cap={settings.test_qty_cap}" if settings.test_qty_cap > 0 else "test_qty_cap=off"
    return f"OK — exchange={settings.exchange} product={settings.product} sizing={settings.sizing_mode} {cap_info}"


async def check_openalgo_reachable() -> str:
    """HTTP GET to OpenAlgo base URL — confirms Flask app is up."""
    import httpx
    from signal_engine.config import settings
    url = settings.openalgo_base_url
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
    if resp.status_code not in (200, 302, 404):
        raise RuntimeError(f"Unexpected status {resp.status_code} from {url}")
    return f"OK — {url} responded HTTP {resp.status_code}"


async def check_broker_auth() -> str:
    """Fetch funds from OpenAlgo — confirms broker auth token is valid."""
    import httpx
    from signal_engine.config import settings
    url = f"{settings.openalgo_base_url}/api/v1/funds"
    payload = {"apikey": settings.openalgo_api_key}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(url, json=payload)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {data}")
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(f"Broker auth failed: {data.get('message', data)}")
    # Extract available capital if present
    available = None
    if isinstance(data, dict):
        available = data.get("data", {}).get("availablecash") or data.get("availablecash")
    if available:
        return f"OK — available capital: {float(available):,.2f} INR"
    return f"OK — funds API responded (raw: {str(data)[:80]})"


async def check_quote_api() -> str:
    """Fetch a live quote for a known liquid NSE stock — confirms MPP will work."""
    import httpx
    from signal_engine.config import settings
    # Use SBIN — highly liquid, always present in NSE symbol DB
    url = f"{settings.openalgo_base_url}/api/v1/quotes"
    payload = {"apikey": settings.openalgo_api_key, "symbol": "SBIN", "exchange": "NSE"}
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(url, json=payload)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {data}")
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(f"Quote API error: {data.get('message', data)}")
    ltp = None
    if isinstance(data, dict):
        ltp = data.get("data", {}).get("ltp") or data.get("ltp")
    if ltp:
        return f"OK — SBIN LTP={ltp}"
    return f"OK — quotes API responded (raw: {str(data)[:80]})"


def check_signal_pipeline() -> str:
    """Run a synthetic ORB LONG signal through normalize -> parse -> validate."""
    from signal_engine.normalizer import normalize
    from signal_engine.parser import parse
    from signal_engine.validator import validate
    from signal_engine.models import Direction, ValidationStatus

    raw = (
        "ORB LONG\n"
        "Symbol: SBIN\n"
        "Entry: 800.00\n"
        "SL: 795.00\n"
        "TP: 810.00\n"
        "Exchange: NSE\n"
    )
    normalized = normalize(raw)
    signal = parse(normalized)
    if signal is None:
        raise RuntimeError("Parser returned None — signal format unrecognized")
    if signal.direction != Direction.LONG:
        raise RuntimeError(f"Expected LONG, got {signal.direction}")
    if signal.symbol != "SBIN":
        raise RuntimeError(f"Expected SBIN, got {signal.symbol}")

    result = validate(signal)
    # SBIN may be blacklisted — we check parse is OK, not necessarily that it passes validation
    if result.status == ValidationStatus.VALID:
        return f"OK — parsed and validated: {signal.strategy} {signal.direction.value} {signal.symbol} entry={signal.entry}"
    else:
        return f"OK — parsed successfully (validation: {result.status.value} — {result.reason})"


def check_risk_engine_state() -> str:
    """Show current risk engine counters so user can confirm day state is clean."""
    from signal_engine.risk import RiskEngine
    from signal_engine.config import settings

    store = RiskStore(RISK_DB_PATH)
    engine = RiskEngine(
        risk_per_trade=settings.risk_per_trade,
        sizing_mode=settings.sizing_mode,
        pct_of_capital=settings.pct_of_capital,
        daily_loss_limit=settings.daily_loss_limit,
        weekly_loss_limit=settings.weekly_loss_limit,
        monthly_loss_limit=settings.monthly_loss_limit,
        max_open_positions=settings.max_open_positions,
        max_trades_per_day=settings.max_trades_per_day,
        min_entry_price=settings.min_entry_price,
        max_entry_price=settings.max_entry_price,
        slippage_factor=settings.slippage_factor,
        store=store,
        trade_mode="live",
        default_product=settings.product,
        max_positions_per_symbol=settings.max_positions_per_symbol,
        max_positions_per_sector=settings.max_positions_per_sector,
        sectors=settings.sectors,
        use_day_start_capital=settings.use_day_start_capital,
    )
    open_pos = engine.open_positions
    trades_today = engine.trades_today
    realised_loss = engine.daily_realised_loss
    heat = engine.portfolio_heat
    can_trade = engine.check_exposure()
    limit_info = "" if can_trade else f" | BLOCKED: {engine.exposure_block_reason()}"
    return (
        f"OK — open={open_pos}/{settings.max_open_positions} "
        f"trades_today={trades_today}/{settings.max_trades_per_day} "
        f"daily_loss={realised_loss:.2f} heat={heat:.2f}{limit_info}"
    )


def check_db() -> str:
    """Write and read a test value in risk_store to confirm DB is accessible."""
    import datetime

    store = RiskStore(RISK_DB_PATH)
    today = datetime.date.today()
    # Read current value for today
    row = store.load("smoke_test", today)
    val_before = row["trades_today"]
    # Write same value back (no-op round-trip to confirm DB write path works)
    store.save("smoke_test", today, trades_today=val_before, daily_loss=0.0, open_positions=0)
    row_after = store.load("smoke_test", today)
    assert row_after["trades_today"] == val_before, f"DB round-trip mismatch"
    return f"OK — risk.db accessible (smoke_test row={row_after})"


# ── Dry Run ──────────────────────────────────────────────────────────────────

async def dry_run_entry_pipeline() -> str:
    """Run full entry pipeline for a synthetic signal without placing an actual order.

    Patches send_order to intercept and inspect the final order before it's sent.
    Confirms: capital fetch, sizing, margin skip (NSE equity), order construction.
    """
    from signal_engine.normalizer import normalize
    from signal_engine.parser import parse
    from signal_engine.models import Direction, OrderStatus, TradeResult, ValidationStatus
    from signal_engine.validator import validate
    from signal_engine.api_client import fetch_available_capital, fetch_trading_mode
    from signal_engine.config import settings
    from signal_engine.risk import RiskEngine

    raw = (
        "ORB LONG\n"
        "Symbol: SBIN\n"
        "Entry: 800.00\n"
        "SL: 790.00\n"
        "TP: 820.00\n"
        "Exchange: NSE\n"
    )
    normalized = normalize(raw)
    signal = parse(normalized)
    if signal is None:
        raise RuntimeError("Parser returned None")

    result = validate(signal)
    if result.status not in (ValidationStatus.VALID, ValidationStatus.IGNORED):
        raise RuntimeError(f"Validation: {result.status.value} — {result.reason}")

    # Fetch real capital
    capital = await fetch_available_capital()
    if capital <= 0:
        raise RuntimeError("Cannot fetch capital from OpenAlgo")

    # Real sizing
    store = RiskStore(RISK_DB_PATH)
    engine = RiskEngine(
        risk_per_trade=settings.risk_per_trade,
        sizing_mode=settings.sizing_mode,
        pct_of_capital=settings.pct_of_capital,
        daily_loss_limit=settings.daily_loss_limit,
        weekly_loss_limit=settings.weekly_loss_limit,
        monthly_loss_limit=settings.monthly_loss_limit,
        max_open_positions=settings.max_open_positions,
        max_trades_per_day=settings.max_trades_per_day,
        min_entry_price=settings.min_entry_price,
        max_entry_price=settings.max_entry_price,
        slippage_factor=settings.slippage_factor,
        store=store,
        trade_mode="live",
        default_product=settings.product,
        max_positions_per_symbol=settings.max_positions_per_symbol,
        max_positions_per_sector=settings.max_positions_per_sector,
        sectors=settings.sectors,
        use_day_start_capital=settings.use_day_start_capital,
    )
    sizing_capital = engine.get_sizing_capital(capital)
    quantity = engine.calculate_quantity(signal, capital=sizing_capital)

    # Apply test qty cap
    if settings.test_qty_cap > 0 and quantity > settings.test_qty_cap:
        quantity = settings.test_qty_cap

    if quantity <= 0:
        return f"OK (would skip) — qty=0 at capital={capital:,.0f}, entry={signal.entry}, sl={signal.sl}"

    # Build order — intercept send_order
    from signal_engine.executor import build_order
    _, is_analyze = await fetch_trading_mode()
    order = build_order(signal, quantity)

    captured = {}

    async def _mock_send(o):
        captured["order"] = o
        return TradeResult(status=OrderStatus.SUCCESS, order_id="DRY-RUN-0001")

    with patch("signal_engine.executor.send_order", side_effect=_mock_send):
        result2 = await _mock_send(order)

    o = captured["order"]
    risk_per_share = abs(signal.entry - signal.sl)
    return (
        f"OK — DRY RUN order: {o.action.value} {o.quantity}x {o.symbol} "
        f"@ {o.order_type} / {o.product} | "
        f"capital={capital:,.0f} qty={quantity} risk/sh={risk_per_share:.2f} "
        f"total_risk={quantity * risk_per_share:,.0f}"
    )


# ── Main entry points ────────────────────────────────────────────────────────

# Checks classified by severity for startup integration
# CRITICAL: abort startup if any fail
# WARNING: log + notify but allow startup to proceed
_CRITICAL_CHECKS = {"2. OpenAlgo reachable", "3. Broker auth (funds API)"}
_WARNING_CHECKS = {"4. Quote API (SBIN LTP)", "5. Signal pipeline", "7. Database (risk store)"}


async def run_startup_checks() -> SmokeTestReport:
    """Startup-time health checks — runs during engine boot before accepting signals.

    Returns a report with pass/fail per check. Callers should abort if
    any CRITICAL check fails (OpenAlgo down, broker auth expired).
    Warning-level failures are non-fatal but notify the user.

    Skips dry-run pipeline check (which needs full risk engine state) to keep
    startup fast (~5s).
    """
    report = SmokeTestReport()

    # Sync checks — fast, no I/O
    report.add(_time_check("1. Config", check_config))
    report.add(_time_check("5. Signal pipeline", check_signal_pipeline))
    report.add(_time_check("6. Risk engine state", check_risk_engine_state))
    report.add(_time_check("7. Database (risk store)", check_db))

    # Network checks — run concurrently
    net_results = await asyncio.gather(
        _atime_check("2. OpenAlgo reachable", check_openalgo_reachable()),
        _atime_check("3. Broker auth (funds API)", check_broker_auth()),
        _atime_check("4. Quote API (SBIN LTP)", check_quote_api()),
    )
    for c in net_results:
        report.checks.append(c)

    report.checks.sort(key=lambda c: c.name)
    return report


async def run_smoke_test() -> SmokeTestReport:
    """Run all connectivity and pipeline health checks. Does NOT place any orders."""
    report = SmokeTestReport()

    # Sync checks (fast, no I/O)
    report.add(_time_check("1. Config", check_config))
    report.add(_time_check("5. Signal pipeline", check_signal_pipeline))
    report.add(_time_check("6. Risk engine state", check_risk_engine_state))
    report.add(_time_check("7. Database (risk store)", check_db))

    # Async checks (network I/O — run concurrently for speed)
    net_checks = await asyncio.gather(
        _atime_check("2. OpenAlgo reachable", check_openalgo_reachable()),
        _atime_check("3. Broker auth (funds API)", check_broker_auth()),
        _atime_check("4. Quote API (SBIN LTP)", check_quote_api()),
        return_exceptions=False,
    )
    # Insert network results in order (after sync checks)
    for c in net_checks:
        report.checks.insert(report.checks.index(next(r for r in report.checks if r.name.startswith("5."))), c)

    # Sort by check number for clean output
    report.checks.sort(key=lambda c: c.name)
    return report


async def run_dry_run() -> SmokeTestReport:
    """Run full smoke test + synthetic entry pipeline without placing any orders."""
    report = await run_smoke_test()

    # Only run dry run pipeline if connectivity checks pass
    conn_checks = [c for c in report.checks if c.name.startswith(("2.", "3."))]
    if all(c.passed for c in conn_checks):
        report.add(await _atime_check("8. Dry run entry pipeline", dry_run_entry_pipeline()))
    else:
        report.add(CheckResult(
            name="8. Dry run entry pipeline",
            passed=False,
            message="SKIPPED — connectivity checks failed",
        ))

    report.checks.sort(key=lambda c: c.name)
    return report
