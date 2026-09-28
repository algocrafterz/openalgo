"""OpenAlgo scheduler — startup and shutdown automation.

Startup: auto-login with TOTP, verify broker auth, send Telegram summary.
Shutdown: send Telegram shutdown notification.
Squareoff: cancel all pending orders and close all MIS positions (3:02 PM failsafe).

Usage:
    uv run python -m signal_engine.scripts.openalgoscheduler startup
    uv run python -m signal_engine.scripts.openalgoscheduler shutdown [reason]
    uv run python -m signal_engine.scripts.openalgoscheduler squareoff
"""

import asyncio
import os
import sys
from datetime import datetime

import pyotp


def get_broker_name() -> str:
    """Resolve the active broker name.

    Resolution order:
    1. BROKER_NAME env var (explicit override)
    2. REDIRECT_URL env var — parses broker from path: http://host/BROKER/callback

    Raises:
        EnvironmentError: if neither source yields a non-empty broker name.
    """
    name = os.environ.get("BROKER_NAME", "").strip().lower()
    if name:
        return name

    redirect_url = os.environ.get("REDIRECT_URL", "").strip()
    if redirect_url:
        from urllib.parse import urlparse
        path_parts = [p for p in urlparse(redirect_url).path.split("/") if p]
        if path_parts:
            return path_parts[0].lower()

    raise OSError(
        "Cannot determine broker name. Set BROKER_NAME in .env, "
        "or ensure REDIRECT_URL follows the pattern http://host/BROKER/callback"
    )


def generate_totp(secret: str) -> str:
    """Generate a 6-digit TOTP code from the given secret.

    Args:
        secret: Base32-encoded TOTP secret seed.

    Returns:
        6-digit TOTP code string.

    Raises:
        ValueError: If secret is empty or None.
    """
    if not secret:
        raise ValueError("TOTP secret must not be empty")
    return pyotp.TOTP(secret).now()


def validate_auto_login_env() -> dict:
    """Validate that required environment variables are set.

    Returns:
        Dict with broker_password and totp_secret.

    Raises:
        EnvironmentError: If any required variable is missing.
    """
    broker_password = os.environ.get("BROKER_PASSWORD")
    if not broker_password:
        raise OSError(
            "BROKER_PASSWORD is not set. "
            "Add it to your .env file for auto-login."
        )

    totp_secret = os.environ.get("BROKER_TOTP_SECRET")
    if not totp_secret:
        raise OSError(
            "BROKER_TOTP_SECRET is not set. "
            "Add your TOTP seed (from authenticator setup) to .env."
        )

    return {
        "broker_password": broker_password,
        "totp_secret": totp_secret,
    }


def _log():
    """Module logger.

    `utils.logging` pulls in the OpenAlgo app configuration, so the import stays
    deferred to call time — the scheduler is also run as a standalone CLI.
    """
    from utils.logging import get_logger
    return get_logger(__name__)


def _resolve_totp_authenticator(broker_name: str):
    """Find the broker's programmatic TOTP login, if it has one.

    Returns a callable normalising the broker's return shape to (token, feed, err),
    or None when the broker is OAuth-only (browser redirect, e.g. flattrade/zerodha).
    """
    logger = _log()
    import importlib

    try:
        auth_module = importlib.import_module(f"broker.{broker_name}.api.auth_api")
    except Exception as e:
        logger.error("Failed to import auth module for broker '%s': %s", broker_name, e)
        return None

    if not hasattr(auth_module, "authenticate_with_totp"):
        return None

    base_fn = auth_module.authenticate_with_totp

    def _normalize_and_call(password, totp_code):
        """Call base_fn and normalize return to (token, feed, err)."""
        try:
            result = base_fn(password, totp_code)
        except TypeError:
            try:
                result = base_fn(password)
            except Exception as e:
                return None, None, str(e)
        except Exception as e:
            return None, None, str(e)

        if result is None:
            return None, None, "Authentication returned no result"
        if isinstance(result, tuple):
            if len(result) == 3:
                return result
            if len(result) == 2:
                token, err = result
                return token, None, err
            if len(result) == 1:
                return result[0], None, None
        return result, None, None

    return _normalize_and_call


def _default_existing_auth_lookup():
    """Default (token, auth_row) lookup for a username."""
    from database.auth_db import get_auth_token as _dba_get
    from database.auth_db import get_auth_token_dbquery as _dba_query

    def _lookup(uname):
        return _dba_get(uname), _dba_query(uname)

    return _lookup


def _oauth_session_token(broker_name: str, username: str, get_existing_auth) -> tuple:
    """Retrieve the token a browser OAuth login already stored.

    Returns (success, message, auth_token).
    """
    logger = _log()
    logger.info("Broker '%s' uses OAuth — retrieving existing session token", broker_name)
    auth_token, auth_obj = get_existing_auth(username)

    if not auth_token:
        msg = (
            f"Broker '{broker_name}' requires OAuth login. "
            "Please authenticate via the OpenAlgo web interface first, "
            "then restart the signal engine."
        )
        logger.error(msg)
        return False, msg, None

    # Detect broker mismatch: stored token is for a different broker
    if auth_obj is not None and auth_obj.broker != broker_name:
        msg = (
            f"Broker changed from '{auth_obj.broker}' to '{broker_name}'. "
            "Please log in via the OpenAlgo web interface to authenticate "
            "with the new broker, then restart the signal engine."
        )
        logger.error(msg)
        return False, msg, None

    return True, "", auth_token


def _totp_session_token(
    broker_name: str, username: str, get_existing_auth,
    authenticate_with_totp, upsert_auth,
) -> tuple:
    """Reuse a live session, else authenticate with TOTP and store the new token.

    Flattrade tokens expire at midnight per SEBI rules, so re-auth is only needed
    once per day. On any intra-day restart the stored token is reused.

    Returns (success, message, auth_token). A non-empty message on success means
    the session was reused and the caller should return immediately.
    """
    logger = _log()
    existing_token, _ = get_existing_auth(username)
    if existing_token and verify_broker_auth(existing_token):
        logger.info("Existing session valid — skipping TOTP for user: %s", username)
        return True, f"Session reused (no TOTP needed) for user: {username}", existing_token

    logger.info("No valid session found — authenticating via TOTP")

    try:
        env = validate_auto_login_env()
    except OSError as e:
        return False, str(e), None

    totp_code = generate_totp(env["totp_secret"])
    logger.info("TOTP code generated")

    auth_token, feed_token, error = authenticate_with_totp(env["broker_password"], totp_code)
    if error:
        logger.error("Broker authentication failed: %s", error)
        return False, error, None
    if not auth_token:
        logger.error("Broker authentication returned empty token for user: %s", username)
        return False, "Authentication succeeded but returned empty/null token", None

    if not upsert_auth(username, auth_token, broker_name, feed_token=feed_token):
        return False, "Failed to store auth token in database", None

    logger.info("Auth token stored for user: %s", username)
    return True, "", auth_token


def _master_contract_call_ok(result) -> bool:
    """Did a master-contract load call itself report success?

    load_existing_master_contract() returns a plain bool. async_master_contract_download()
    is messier (broker-specific opaque value on success, an explicit {"status": "error", ...}
    dict on the one failure mode it documents) - check the one contract we can rely on and
    otherwise assume success. This is only the first, cheap filter - it does NOT prove the
    contract is actually usable; see _master_contract_load_confirmed() for that.
    """
    if isinstance(result, bool):
        return result
    if isinstance(result, dict) and result.get("status") == "error":
        return False
    return True


def _master_contract_load_confirmed(broker_name, get_status, call_result, should_download) -> tuple:
    """The authoritative "is the master contract actually usable right now" check.

    A call reporting success is not proof of anything: broker.*.database.master_contract_db
    modules catch and log a per-exchange CSV download failure without re-raising
    (download_csv_data()), so a partial download can still return {"status": "success", ...}
    to us having silently skipped one or more exchanges. And on 2026-09-28 the call never
    even got that far - a killed background thread left status stuck at "downloading"
    forever, which _master_contract_call_ok() alone can't see since it only looks at the
    call's return value, not the database row the call is supposed to have written.

    The one thing that cannot lie is the master_contract_status row
    async_master_contract_download() itself writes via update_status(): status, is_ready,
    and total_symbols. Check that instead of trusting the call's own return value.

    Args:
        get_status: database.master_contract_status_db.get_status, or a test double.
        call_result: whatever target(broker_name) returned.
        should_download: True for the download path (checked against the DB row below),
            False for the cached-load path (load_existing_master_contract()'s own bool
            return is already authoritative there - it already checked is_ready itself).

    Returns:
        (confirmed: bool, detail: str)
    """
    if not _master_contract_call_ok(call_result):
        return False, f"load call reported failure: {call_result!r}"

    if not should_download:
        return True, "loaded from cache"

    status = get_status(broker_name)
    if not status or status.get("status") != "success" or not status.get("is_ready"):
        return False, f"master_contract_status not confirmed ready after download: {status!r}"

    try:
        total_symbols = int(status.get("total_symbols") or 0)
    except (TypeError, ValueError):
        total_symbols = 0
    if total_symbols <= 0:
        return False, f"master_contract_status reports success but 0 symbols loaded: {status!r}"

    return True, f"confirmed ready with {total_symbols} symbols"


def _start_master_contract_load(
    broker_name, init_broker_status, should_download_master_contract,
    async_master_contract_download, load_existing_master_contract,
    force_download: bool = False,
    _get_master_contract_status=None,
) -> bool:
    """Init broker status and load the symbol master - synchronously, with one retry,
    confirmed against the database row before returning success.

    This runs inside a short-lived CLI process: openalgoscheduler.py's `startup` and
    `healthcheck` commands both exit right after auto_login() returns. The previous
    implementation fired this off in a daemon Thread and returned immediately, which
    looked harmless but meant the download raced the process's own exit. On 2026-09-28
    that race lost: the process exited ~2 seconds after starting the download, killing
    the thread after only 6 of 8 exchange files. master_contract_status stayed stuck at
    "downloading" for the next ~90 minutes (nobody polled it to trip the stuck-download
    detector), and every signal_engine call to /api/v1/funds and /api/v1/analyzer 403'd
    for that entire window despite the broker session itself being fine throughout -
    silently dropping every paper trade until a manual browser login (which runs the
    same work inside the long-lived app.py process, where a background thread survives)
    fixed it by accident. Block here instead: the process only exits once the load has
    actually finished AND been confirmed against the database row, one way or the other,
    and a transient or partial failure gets one retry instead of waiting for the next
    15-minute cron cycle - this is critical, one-shot-per-day infrastructure that every
    trade depends on, not best-effort background work.

    Args:
        force_download: skip the smart-download check and always redownload. Used by
            the self-heal path when something downstream has already proven the current
            state unusable - retrying the cached-data path would just reproduce the
            same failure.
        _get_master_contract_status: override for database.master_contract_status_db.get_status
            (DI for testing).

    Returns:
        True if the master contract load is confirmed ready, False otherwise.
    """
    logger = _log()

    if _get_master_contract_status is None:
        from database.master_contract_status_db import get_status as _get_master_contract_status

    init_broker_status(broker_name)

    if force_download:
        should_download, reason = True, "forced by self-heal"
    else:
        should_download, reason = should_download_master_contract(broker_name)
    logger.info("Smart download check: should_download=%s, reason=%s", should_download, reason)

    target = async_master_contract_download if should_download else load_existing_master_contract
    label = "download" if should_download else "cached load"

    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            result = target(broker_name)
        except Exception:
            logger.exception(
                "Master contract %s raised an exception (attempt %d/%d)",
                label, attempt, max_attempts,
            )
            result = {"status": "error", "message": "raised exception"}

        confirmed, detail = _master_contract_load_confirmed(
            broker_name, _get_master_contract_status, result, should_download
        )
        if confirmed:
            logger.info(
                "Master contract %s completed and confirmed (attempt %d/%d): %s",
                label, attempt, max_attempts, detail,
            )
            return True

        logger.error(
            "Master contract %s not confirmed (attempt %d/%d): %s",
            label, attempt, max_attempts, detail,
        )
        # Retrying a cache-load failure (no download exists yet) can't succeed any
        # differently - only retry the actual download path.
        if attempt < max_attempts and should_download:
            continue
        break

    return False


def _auto_login_deps(
    upsert_auth, find_user_by_username, init_broker_status,
    should_download_master_contract, async_master_contract_download,
    load_existing_master_contract, get_existing_auth,
) -> dict:
    """Fill in the real OpenAlgo collaborators for any that were not injected.

    Imports stay deferred: the scheduler also runs as a standalone CLI where the
    app's database modules may not be importable.
    """
    if upsert_auth is None:
        from database.auth_db import upsert_auth
    if find_user_by_username is None:
        from database.user_db import find_user_by_username
    if init_broker_status is None:
        from database.master_contract_status_db import init_broker_status
    if should_download_master_contract is None:
        from utils.auth_utils import should_download_master_contract
    if async_master_contract_download is None:
        from utils.auth_utils import async_master_contract_download
    if load_existing_master_contract is None:
        from utils.auth_utils import load_existing_master_contract
    if get_existing_auth is None:
        get_existing_auth = _default_existing_auth_lookup()
    return {
        "upsert_auth": upsert_auth,
        "find_user_by_username": find_user_by_username,
        "init_broker_status": init_broker_status,
        "should_download_master_contract": should_download_master_contract,
        "async_master_contract_download": async_master_contract_download,
        "load_existing_master_contract": load_existing_master_contract,
        "get_existing_auth": get_existing_auth,
    }


def auto_login(
    _authenticate_with_totp=None,
    _upsert_auth=None,
    _find_user_by_username=None,
    _init_broker_status=None,
    _should_download_master_contract=None,
    _async_master_contract_download=None,
    _load_existing_master_contract=None,
    _get_existing_auth=None,
) -> tuple:
    """Perform automated broker login.

    Supports two auth modes:
    - Programmatic (TOTP): broker has authenticate_with_totp → generates TOTP, logs in, stores token
    - OAuth-only: broker uses browser redirect (e.g. flattrade) → retrieves existing DB token set
      via browser OAuth; fails clearly if no token or if broker has changed

    Dependency injection parameters (for testing only):
        _authenticate_with_totp: Override broker auth function (implies programmatic mode).
        _upsert_auth: Override DB upsert function.
        _find_user_by_username: Override user lookup function.
        _init_broker_status: Override broker status init.
        _should_download_master_contract: Override download check.
        _async_master_contract_download: Override master contract download.
        _load_existing_master_contract: Override cached contract loader.
        _get_existing_auth: Override existing token lookup (OAuth brokers).

    Returns:
        Tuple of (success: bool, message: str, auth_token: str | None).
    """
    broker_name = get_broker_name()

    # Detect auth mode: programmatic (TOTP) vs OAuth-only.
    # When _authenticate_with_totp is injected (tests), always use programmatic path.
    is_oauth_only = False
    if _authenticate_with_totp is None:
        _authenticate_with_totp = _resolve_totp_authenticator(broker_name)
        is_oauth_only = _authenticate_with_totp is None

    deps = _auto_login_deps(
        _upsert_auth, _find_user_by_username, _init_broker_status,
        _should_download_master_contract, _async_master_contract_download,
        _load_existing_master_contract, _get_existing_auth,
    )
    _upsert_auth = deps["upsert_auth"]
    _find_user_by_username = deps["find_user_by_username"]
    _init_broker_status = deps["init_broker_status"]
    _should_download_master_contract = deps["should_download_master_contract"]
    _async_master_contract_download = deps["async_master_contract_download"]
    _load_existing_master_contract = deps["load_existing_master_contract"]
    _get_existing_auth = deps["get_existing_auth"]

    logger = _log()

    # Find admin user (env vars only needed for programmatic login)
    admin_user = _find_user_by_username()
    if not admin_user:
        return False, "No admin user found in database. Run setup first.", None

    username = admin_user.username
    logger.info("Auto-login starting for user: %s (broker: %s)", username, broker_name)

    if is_oauth_only:
        ok, msg, auth_token = _oauth_session_token(broker_name, username, _get_existing_auth)
        if not ok:
            return False, msg, None
    else:
        ok, msg, auth_token = _totp_session_token(
            broker_name, username, _get_existing_auth, _authenticate_with_totp, _upsert_auth
        )
        if not ok:
            return False, msg, None
        if msg:  # session reused — nothing further to set up
            return True, msg, auth_token

    _start_master_contract_load(
        broker_name, _init_broker_status, _should_download_master_contract,
        _async_master_contract_download, _load_existing_master_contract,
    )

    return True, f"Auto-login successful for {username}", auth_token


def verify_broker_auth(auth_token, _get_margin_data=None) -> dict | None:
    """Verify broker auth token is live by calling the funds API.

    Makes a lightweight API call to fetch account funds/margins.
    If the broker returns valid data, the token is confirmed working.

    Args:
        auth_token: The broker auth token to verify.
        _get_margin_data: Override for testing (DI).

    Returns:
        Fund data dict if token is valid, None otherwise.
    """
    from utils.logging import get_logger
    logger = get_logger(__name__)

    if not auth_token:
        logger.error("No auth token to verify")
        return None

    try:
        if _get_margin_data is None:
            import importlib
            broker_name = get_broker_name()
            try:
                broker_funds = importlib.import_module(f"broker.{broker_name}.api.funds")
                _get_margin_data = broker_funds.get_margin_data
            except Exception as import_err:
                logger.error(
                    f"Failed to import funds module for broker '{broker_name}': {import_err}"
                )
                return None

        margin_data = _get_margin_data(auth_token)

        if not margin_data:
            logger.error("Auth verification failed: broker returned empty funds data")
            return None

        available = margin_data.get("availablecash", "0")
        logger.info("Auth verified: available cash = %s", available)
        return margin_data

    except Exception:
        logger.exception("Auth verification failed with exception")
        return None


async def _openalgo_api_smoke_test() -> tuple:
    """Prove signal_engine can actually use OpenAlgo's own API, not just the broker.

    verify_broker_auth() only proves the broker session is alive by calling the broker
    directly - it says nothing about whether OpenAlgo's own /api/v1/funds and
    /api/v1/analyzer will accept signal_engine's API key. On 2026-09-28 those two
    endpoints returned 403 "Invalid openalgo apikey" continuously for ~90 minutes while
    verify_broker_auth's own healthcheck logged a live broker balance every 15 minutes
    the entire time - the broker session was never the problem. This calls the exact
    endpoints and the real configured API key that signal_engine.main depends on for
    every trade, so a repeat is caught here instead of discovered later as a day of
    missing paper trades.

    Returns:
        (ok: bool, detail: str)
    """
    from signal_engine import api_client

    try:
        mode, is_analyze = await api_client.fetch_trading_mode()
    except Exception as e:
        return False, f"/api/v1/analyzer raised: {e}"
    if mode == "unknown":
        return False, "/api/v1/analyzer did not return a usable mode (rejected API key or unreachable)"

    try:
        funds = await api_client._post_json("funds", api_client._auth())
    except Exception as e:
        return False, f"/api/v1/funds rejected the configured API key: {e}"
    if funds.get("status") != "success":
        return False, f"/api/v1/funds returned non-success: {funds}"

    return True, f"OpenAlgo API OK (mode={mode}, analyze={is_analyze})"


def _verify_openalgo_api(max_attempts: int = 3, retry_delay: float = 5.0) -> tuple:
    """Synchronous, retrying wrapper around _openalgo_api_smoke_test().

    Retries with a short delay before giving up - a stack that just (re)started may
    need a few seconds to settle, same reasoning as fetch_available_capital()'s own
    retry loop in signal_engine/api_client.py.

    Returns:
        (ok: bool, detail: str) - detail is the last attempt's message either way.
    """
    import time

    logger = _log()
    detail = "not attempted"

    for attempt in range(1, max_attempts + 1):
        ok, detail = asyncio.run(_openalgo_api_smoke_test())
        if ok:
            if attempt > 1:
                logger.warning(
                    "OpenAlgo API smoke test recovered on attempt %d/%d: %s",
                    attempt, max_attempts, detail,
                )
            else:
                logger.info("OpenAlgo API smoke test passed: %s", detail)
            return True, detail

        logger.error(
            "OpenAlgo API smoke test FAILED (attempt %d/%d): %s", attempt, max_attempts, detail
        )
        if attempt < max_attempts:
            time.sleep(retry_delay)

    return False, detail


def _self_heal_master_contract() -> bool:
    """Force a fresh master-contract download, bypassing the smart-download check.

    Called only after _verify_openalgo_api() has already failed every retry - a stuck
    or partial master-contract load (the confirmed cause on 2026-09-28) is the most
    likely reason the smoke test above still fails despite a live broker session, and
    a forced redownload is idempotent and safe to run again even if that guess is wrong.
    """
    logger = _log()
    try:
        from database.master_contract_status_db import init_broker_status
        from utils.auth_utils import async_master_contract_download, load_existing_master_contract

        broker_name = get_broker_name()
        logger.warning("Self-heal: forcing a fresh master contract download for %s", broker_name)
        return _start_master_contract_load(
            broker_name, init_broker_status, None,
            async_master_contract_download, load_existing_master_contract,
            force_download=True,
        )
    except Exception:
        logger.exception("Self-heal master-contract reload failed")
        return False


def _ensure_openalgo_api_usable(context: str) -> bool:
    """Smoke-test OpenAlgo's own API and self-heal once if it fails. Alerts either way.

    Args:
        context: label for log/alert messages ("startup" or "healthcheck").

    Returns:
        True if the API is confirmed usable (first try or after self-heal),
        False if it is still broken after self-heal - the caller must treat this
        as a hard failure (alert + exit), the same as a broker-auth failure.
    """
    logger = _log()

    ok, detail = _verify_openalgo_api()
    if ok:
        return True

    logger.error(
        "%s: OpenAlgo API unusable after retries (%s) - attempting self-heal via "
        "master-contract reload", context, detail,
    )
    healed = _self_heal_master_contract()

    ok2, detail2 = _verify_openalgo_api(max_attempts=2, retry_delay=5.0)
    if ok2:
        try:
            asyncio.run(send_telegram_notification(
                f"OpenAlgo API smoke test failed after {context} ({detail}) but recovered "
                f"after a forced master-contract reload (heal_ran={healed}). Signals during "
                "the outage window may have been dropped - check today's orderbook."
            ))
        except Exception:
            logger.exception("Recovery notification failed (non-fatal)")
        return True

    notify_failure(
        f"{context}-api-smoke-test",
        f"{detail2} (self-heal attempted, heal_ran={healed}, still failing)",
    )
    return False


def build_startup_summary(
    broker_name: str,
    fund_data: dict,
    sizing_mode: str,
    risk_per_trade: float,
    max_open_positions: int,
    daily_loss_limit: float,
    exchange: str,
    product: str,
    order_type: str,
    channels: list,
) -> str:
    """Build a human-readable startup summary for logs and Telegram.

    Args:
        broker_name: Active broker.
        fund_data: Dict from verify_broker_auth with fund fields.
        sizing_mode: Position sizing mode.
        risk_per_trade: Risk fraction per trade.
        max_open_positions: Max concurrent positions.
        daily_loss_limit: Daily loss limit fraction.
        exchange: Trading exchange.
        product: Order product type.
        order_type: Order type.
        channels: List of Telegram channel names.

    Returns:
        Formatted summary string.
    """
    available = fund_data.get("availablecash", "0.00")
    utilized = fund_data.get("utiliseddebits", "0.00")
    realized = fund_data.get("m2mrealized", "0.00")
    unrealized = fund_data.get("m2munrealized", "0.00")
    collateral = fund_data.get("collateral", "0.00")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ch_list = ", ".join(channels) if channels else "none"

    lines = [
        "OpenAlgo Signal Engine - Ready",
        f"Time: {now}",
        "",
        "-- Account --",
        f"Broker: {broker_name}",
        f"Available Cash: {available}",
        f"Utilized Margin: {utilized}",
        f"Realized P&L: {realized}",
        f"Unrealized P&L: {unrealized}",
        f"Collateral: {collateral}",
        "",
        "-- Trading Config --",
        f"Exchange: {exchange} | Product: {product} | Order: {order_type}",
        f"Sizing: {sizing_mode}",
        f"Risk/Trade: {risk_per_trade * 100:.1f}%",
        f"Max Positions: {max_open_positions}",
        f"Daily Loss Limit: {daily_loss_limit * 100:.1f}%",
        "",
        "-- Channels --",
        ch_list,
    ]

    return "\n".join(lines)


def build_shutdown_summary(broker_name: str, reason: str = "scheduled") -> str:
    """Build a human-readable shutdown summary for logs and Telegram.

    Args:
        broker_name: Active broker.
        reason: Why shutdown is happening (e.g. "scheduled", "manual").

    Returns:
        Formatted summary string.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "OpenAlgo Signal Engine - Stopped",
        f"Time: {now}",
        f"Broker: {broker_name}",
        f"Reason: {reason}",
    ]

    return "\n".join(lines)


async def send_telegram_notification(message: str, _client=None) -> bool:
    """Send a message to all configured Telegram channels.

    Args:
        message: The formatted message string.
        _client: Override TelegramClient for testing (DI).

    Returns:
        True if message sent successfully to at least one channel.
    """
    from utils.logging import get_logger
    logger = get_logger(__name__)

    try:
        from signal_engine.config import settings

        # Use dedicated notify_channel if configured, else fall back to signal channels.
        # notify_channel is now {"analyze": ..., "live": ...} - broker-login status is
        # mode-independent (it happens once a day regardless of analyze/live), so broadcast
        # to every configured phase rather than picking one.
        if settings.notify_channel:
            notify_targets = list(settings.notify_channel.values())
        elif settings.telegram_channels:
            notify_targets = list(settings.telegram_channels)
        else:
            logger.warning("No Telegram channels configured, skipping notification")
            return False

        if _client is None:
            from telethon import TelegramClient

            session_path = "signal_engine/data/telegram"
            client = TelegramClient(
                session_path,
                settings.telegram_api_id,
                settings.telegram_api_hash,
            )
            await client.start(phone=settings.telegram_phone)
            should_disconnect = True
        else:
            client = _client
            should_disconnect = False

        sent = False
        for ch in notify_targets:
            try:
                await client.send_message(ch.id, message)
                logger.info("Notification sent to channel: %s", ch.name)
                sent = True
            except Exception:
                logger.exception(
                    "Failed to send notification to channel: %s", ch.name
                )

        if should_disconnect:
            await client.disconnect()

        return sent

    except Exception:
        logger.exception("Failed to send Telegram notification")
        return False


def notify_failure(stage: str, detail: str, _send=None) -> bool:
    """Announce a startup failure to Telegram.

    Startup used to exit(1) before ever reaching the notification step, so a
    failed auto-login was invisible: on 2026-08-24 a single 09:03 failure wrote
    a 24h cooldown that blocked 80 further start attempts for the whole trading
    day with nothing sent anywhere. Alerting has to happen on the failure path
    itself, and it must never raise -- a dead Telegram must not mask the real
    error underneath it.

    Args:
        stage: Which step failed (e.g. "auto-login", "broker-auth").
        detail: The underlying error message.
        _send: Override sender for testing (DI). Takes the message, returns bool.

    Returns:
        True if the alert was delivered to at least one channel.
    """
    from utils.logging import get_logger

    logger = get_logger(__name__)

    try:
        host = os.uname().nodename
    except Exception:
        host = "unknown"

    message = (
        "OpenAlgo Startup FAILED\n"
        "\n"
        f"Stage : {stage}\n"
        f"Error : {detail}\n"
        f"Host  : {host}\n"
        f"Time  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "\n"
        "The signal engine is NOT running. No trades will be taken."
    )

    try:
        sender = _send
        if sender is None:
            def sender(msg):
                return asyncio.run(send_telegram_notification(msg))
        return bool(sender(message))
    except Exception:
        logger.exception("Failed to send startup-failure alert (non-fatal)")
        return False


def _run_startup():
    """Full startup flow: login, verify, summarise, notify."""
    from utils.logging import get_logger
    logger = get_logger(__name__)

    # 1. Auto-login
    try:
        login_result = auto_login()
    except OSError as e:
        logger.error("Configuration error: %s", e)
        notify_failure("configuration", str(e))
        sys.exit(1)

    if not login_result[0]:
        logger.error("Auto-login failed: %s", login_result[1])
        notify_failure("auto-login", str(login_result[1]))
        sys.exit(1)

    # Unpack: (success, message, auth_token)
    _, message, auth_token = login_result
    logger.info(message)

    # 2. Verify token works by calling broker API
    # Use the token returned directly from auto_login — avoids DB round-trip
    # which can fail if find_user_by_username() returns None in subprocess context.
    fund_data = verify_broker_auth(auth_token)
    if not fund_data:
        logger.error("Broker auth token verification FAILED")
        notify_failure(
            "broker-auth",
            "token returned by login did not work against the broker funds API",
        )
        sys.exit(1)

    logger.info("Broker auth token verified - ready to trade")

    # 3. Prove OpenAlgo's own API (not just the broker) is usable with signal_engine's
    # API key — self-heals via a forced master-contract reload, alerts either way.
    # See _ensure_openalgo_api_usable's docstring for why step 2 above is not enough.
    if not _ensure_openalgo_api_usable("startup"):
        logger.error("OpenAlgo API smoke test failed and self-heal did not recover it")
        sys.exit(1)

    # 4. Build and log startup summary
    from signal_engine.config import settings

    broker_name = get_broker_name()
    channel_names = [ch.name for ch in settings.telegram_channels]

    summary = build_startup_summary(
        broker_name=broker_name,
        fund_data=fund_data,
        sizing_mode=settings.sizing_mode,
        risk_per_trade=settings.risk_per_trade,
        max_open_positions=settings.max_open_positions,
        daily_loss_limit=settings.daily_loss_limit,
        exchange=settings.exchange,
        product=settings.product,
        order_type=settings.order_type,
        channels=channel_names,
    )

    for line in summary.splitlines():
        if line.strip():
            logger.info(line)

    # 5. Send Telegram notification
    try:
        sent = asyncio.run(send_telegram_notification(summary))
        if sent:
            logger.info("Startup notification sent to Telegram")
        else:
            logger.warning("Startup notification not sent (no channels or send failed)")
    except Exception:
        logger.exception("Telegram notification failed (non-fatal)")

    # 6. Weekly watchlist screen, LAST so it can never delay trading readiness or the
    # notification above. Runs on whatever day the system actually next starts up
    # rather than a fixed clock time - see watchlist_screen.py's module docstring for
    # why, and why the Telegram send here does not race the listener's own session.
    try:
        from signal_engine.scripts.watchlist_screen import maybe_run_weekly_screen

        if maybe_run_weekly_screen():
            logger.info("Weekly watchlist screen ran and notified Telegram")
    except Exception:
        logger.exception("Weekly watchlist screen failed (non-fatal)")


def _run_shutdown(reason: str = "scheduled"):
    """Shutdown flow: build summary, notify, exit."""
    from utils.logging import get_logger
    logger = get_logger(__name__)

    broker_name = get_broker_name()
    summary = build_shutdown_summary(broker_name, reason=reason)

    for line in summary.splitlines():
        if line.strip():
            logger.info(line)

    try:
        sent = asyncio.run(send_telegram_notification(summary))
        if sent:
            logger.info("Shutdown notification sent to Telegram")
        else:
            logger.warning("Shutdown notification not sent (no channels or send failed)")
    except Exception:
        logger.exception("Telegram notification failed (non-fatal)")


def _run_healthcheck():
    """Periodic check: is the stored broker session still alive?

    openalgoctl.sh's run() supervisor only calls _run_startup() (a real login)
    once, at process start. Indian broker tokens expire daily at ~03:00 IST
    regardless of when the process started, so a stack started before that
    rollover and kept running (the normal case) drifts onto a dead session for
    the rest of the day with nothing to notice or recover — every quote call
    fails silently until someone restarts the stack. That is exactly what
    happened on 2026-09-09: session died before market open, stayed dead until
    a manual restart at 15:05 IST, ~6 hours in which paper orders were
    rejected or stuck unfilled (see breakout.md's 2026-09-09 postmortem).

    auto_login() is already safe to call repeatedly: _totp_session_token()
    checks the existing token with verify_broker_auth() first and only
    performs a fresh TOTP login when that check fails, so calling it every
    few minutes costs one cheap funds-API call in the common case (session
    still valid) and only pays for a real login when the session is actually
    dead. This wraps it with quiet-by-default logging and alerts only on a
    genuine state change — session found dead, recovered, or re-login
    failing — not a message every cycle.

    Exit code 0 = session confirmed alive or recovered.
    Exit code 1 = session dead AND auto re-login also failed — the caller
    (openalgoctl.sh) is expected to alert/cooldown same as a bootstrap
    failure, so a repeatedly-dead broker doesn't get hammered with logins.
    """
    logger = _log()

    # Independent of broker auth mode (TOTP vs OAuth-only) - this checks OpenAlgo's own
    # API layer, a different failure mode from "is the broker session alive" below. See
    # 2026-09-28: the broker session was fine and verified every 15 minutes by this same
    # healthcheck, while /api/v1/funds and /api/v1/analyzer 403'd for ~90 minutes because
    # a master-contract download died mid-flight after the earlier login. Runs on every
    # cycle, self-heals via a forced master-contract reload, and alerts either way.
    if not _ensure_openalgo_api_usable("healthcheck"):
        logger.error("Healthcheck: OpenAlgo API smoke test failed and self-heal did not recover it")
        sys.exit(1)

    try:
        validate_auto_login_env()
    except OSError:
        # OAuth-only broker (no TOTP secret configured) — auto_login() can't
        # self-heal this path (see architecture note in project memory), so
        # there is nothing for a periodic check to do beyond what the human
        # who did the manual browser login already knows.
        logger.debug("Healthcheck: TOTP auto-login not configured, skipping")
        return

    try:
        success, message, auth_token = auto_login()
    except OSError as e:
        logger.error("Healthcheck: configuration error: %s", e)
        notify_failure("healthcheck", str(e))
        sys.exit(1)

    if not success:
        logger.error("Healthcheck: broker session dead and re-login FAILED: %s", message)
        notify_failure("healthcheck", str(message))
        sys.exit(1)

    if message and "reused" in message.lower():
        logger.debug("Healthcheck: %s", message)
        return

    # Non-reuse success means _totp_session_token() actually performed a
    # fresh TOTP login just now — the stored session was dead until this
    # check caught it.
    logger.warning("Healthcheck: broker session had expired — auto re-login succeeded")
    try:
        asyncio.run(send_telegram_notification(
            "Broker session had expired and was auto re-authenticated by the periodic "
            "health check. Signals during the outage window may have been rejected, "
            "delayed, or filled late at a stale price — check today's orderbook."
        ))
    except Exception:
        logger.exception("Recovery notification failed (non-fatal)")


def _run_squareoff():
    """3:02 PM failsafe: cancel all pending orders and close all MIS positions.

    Called by Windows Task Scheduler at 3:02 PM, independent of the signal engine process.
    Fires 2 minutes after the engine's own 3:00 PM time exit, so it's a no-op when the
    engine already closed positions. Acts as the safety net when the engine is crashed,
    frozen, or the system woke from sleep after 3:00 PM.
    """
    from signal_engine.api_client import cancel_all_orders, close_all_positions
    from signal_engine.config import settings
    from utils.logging import get_logger

    logger = get_logger(__name__)
    logger.info("Squareoff 15:02: failsafe close of all MIS positions")

    mis_strategies = [
        name for name, profile in settings.strategy_profiles.items()
        if profile.get("product", "") == "MIS"
    ]

    if not mis_strategies:
        logger.warning("Squareoff: no MIS strategies found in config")
        return

    logger.info(f"Squareoff: strategies={mis_strategies}")

    async def _do():
        for strategy in mis_strategies:
            logger.info(f"Squareoff: cancelling orders for {strategy}")
            await cancel_all_orders(strategy)
            logger.info(f"Squareoff: closing positions for {strategy}")
            await close_all_positions(strategy)

    asyncio.run(_do())
    logger.info("Squareoff: done")

    try:
        msg = f"Squareoff 15:02 (failsafe): {', '.join(mis_strategies)} processed"
        asyncio.run(send_telegram_notification(msg))
    except Exception:
        pass


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    command = sys.argv[1] if len(sys.argv) > 1 else "startup"
    reason = sys.argv[2] if len(sys.argv) > 2 else "scheduled"

    if command == "startup":
        _run_startup()
    elif command == "shutdown":
        _run_shutdown(reason=reason)
    elif command == "squareoff":
        _run_squareoff()
    elif command == "healthcheck":
        _run_healthcheck()
    elif command == "notify":
        # Lets openalgoctl.sh raise an alert from shell without duplicating
        # Telegram wiring. Arg 2 is the stage, the rest is the detail.
        stage = sys.argv[2] if len(sys.argv) > 2 else "supervisor"
        detail = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else "(no detail)"
        sys.exit(0 if notify_failure(stage, detail) else 1)
    else:
        print("Usage: python -m signal_engine.scripts.openalgoscheduler "
              "[startup|shutdown|squareoff|healthcheck|notify] [reason|stage] [detail...]")
        sys.exit(1)
