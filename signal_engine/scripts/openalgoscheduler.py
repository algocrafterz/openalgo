"""OpenAlgo scheduler — startup and shutdown automation.

Startup: auto-login with TOTP, verify broker auth, send Telegram summary.
Shutdown: send Telegram shutdown notification.

Usage:
    uv run python -m signal_engine.scripts.openalgoscheduler startup
    uv run python -m signal_engine.scripts.openalgoscheduler shutdown [reason]
"""

import asyncio
import os
import sys
from datetime import datetime

import pyotp


def get_broker_name() -> str:
    """Read broker name from BROKER_NAME env var, default to mstock.

    Returns:
        Lowercase, stripped broker name string.
    """
    return os.environ.get("BROKER_NAME", "mstock").strip().lower()


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
        raise EnvironmentError(
            "BROKER_PASSWORD is not set. "
            "Add it to your .env file for auto-login."
        )

    totp_secret = os.environ.get("BROKER_TOTP_SECRET")
    if not totp_secret:
        raise EnvironmentError(
            "BROKER_TOTP_SECRET is not set. "
            "Add your TOTP seed (from authenticator setup) to .env."
        )

    return {
        "broker_password": broker_password,
        "totp_secret": totp_secret,
    }


def auto_login(
    _authenticate_with_totp=None,
    _upsert_auth=None,
    _find_user_by_username=None,
    _init_broker_status=None,
    _should_download_master_contract=None,
    _async_master_contract_download=None,
    _load_existing_master_contract=None,
) -> tuple:
    """Perform automated broker login.

    Replicates the full handle_auth_success flow without Flask session:
    1. Validate env vars
    2. Find admin user
    3. Generate TOTP and authenticate with broker
    4. Store auth token in DB
    5. Init broker status and trigger master contract download

    Dependency injection parameters (for testing only):
        _authenticate_with_totp: Override broker auth function.
        _upsert_auth: Override DB upsert function.
        _find_user_by_username: Override user lookup function.
        _init_broker_status: Override broker status init.
        _should_download_master_contract: Override download check.
        _async_master_contract_download: Override master contract download.
        _load_existing_master_contract: Override cached contract loader.

    Returns:
        Tuple of (success: bool, message: str).
    """
    broker_name = get_broker_name()

    # Lazy imports — only loaded when actually running (not at import time)
    if _authenticate_with_totp is None:
        from broker.mstock.api.auth_api import authenticate_with_totp
        _authenticate_with_totp = authenticate_with_totp
    if _upsert_auth is None:
        from database.auth_db import upsert_auth
        _upsert_auth = upsert_auth
    if _find_user_by_username is None:
        from database.user_db import find_user_by_username
        _find_user_by_username = find_user_by_username
    if _init_broker_status is None:
        from database.master_contract_status_db import init_broker_status
        _init_broker_status = init_broker_status
    if _should_download_master_contract is None:
        from utils.auth_utils import should_download_master_contract
        _should_download_master_contract = should_download_master_contract
    if _async_master_contract_download is None:
        from utils.auth_utils import async_master_contract_download
        _async_master_contract_download = async_master_contract_download
    if _load_existing_master_contract is None:
        from utils.auth_utils import load_existing_master_contract
        _load_existing_master_contract = load_existing_master_contract

    from threading import Thread

    from utils.logging import get_logger
    logger = get_logger(__name__)

    # 1. Validate env
    try:
        env = validate_auto_login_env()
    except EnvironmentError as e:
        return False, str(e)

    # 2. Find admin user
    admin_user = _find_user_by_username()
    if not admin_user:
        return False, "No admin user found in database. Run setup first."

    username = admin_user.username
    logger.info("Auto-login starting for user: %s (broker: %s)", username, broker_name)

    # 3. Generate TOTP
    totp_code = generate_totp(env["totp_secret"])
    logger.info("TOTP code generated")

    # 4. Authenticate with broker
    auth_token, feed_token, error = _authenticate_with_totp(
        env["broker_password"], totp_code
    )
    if error:
        logger.error("Broker authentication failed: %s", error)
        return False, error

    # 5. Store token in DB
    inserted_id = _upsert_auth(
        username, auth_token, broker_name, feed_token=feed_token
    )
    if not inserted_id:
        return False, "Failed to store auth token in database"

    logger.info("Auth token stored for user: %s", username)

    # 6. Init broker status and trigger master contract download
    _init_broker_status(broker_name)

    should_download, reason = _should_download_master_contract(broker_name)
    logger.info("Smart download check: should_download=%s, reason=%s",
                should_download, reason)

    if should_download:
        thread = Thread(
            target=_async_master_contract_download,
            args=(broker_name,), daemon=True
        )
        thread.start()
        logger.info("Master contract download started in background")
    else:
        thread = Thread(
            target=_load_existing_master_contract,
            args=(broker_name,), daemon=True
        )
        thread.start()
        logger.info("Loading cached master contract: %s", reason)

    return True, f"Auto-login successful for {username}"


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
            from broker.mstock.api.funds import get_margin_data
            _get_margin_data = get_margin_data

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


def build_startup_summary(
    broker_name: str,
    fund_data: dict,
    sizing_mode: str,
    risk_per_trade: float,
    max_open_positions: int,
    daily_loss_limit: float,
    max_portfolio_heat: float,
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
        max_portfolio_heat: Max portfolio heat fraction.
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
        f"Portfolio Heat Cap: {max_portfolio_heat * 100:.1f}%",
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

        # Use dedicated notify_channel if configured, else fall back to signal channels
        if settings.notify_channel:
            notify_targets = [settings.notify_channel]
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


def _run_startup():
    """Full startup flow: login, verify, summarise, notify."""
    from utils.logging import get_logger
    logger = get_logger(__name__)

    # 1. Auto-login
    success, message = auto_login()
    if not success:
        logger.error("Auto-login failed: %s", message)
        sys.exit(1)

    logger.info(message)

    # 2. Verify token works by calling broker API
    from database.auth_db import get_auth_token
    from database.user_db import find_user_by_username
    admin_user = find_user_by_username()
    username = admin_user.username if admin_user else "admin"
    token = get_auth_token(username)
    fund_data = verify_broker_auth(token)
    if not fund_data:
        logger.error("Broker auth token verification FAILED")
        sys.exit(1)

    logger.info("Broker auth token verified - ready to trade")

    # 3. Build and log startup summary
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
        max_portfolio_heat=settings.max_portfolio_heat,
        exchange=settings.exchange,
        product=settings.product,
        order_type=settings.order_type,
        channels=channel_names,
    )

    for line in summary.splitlines():
        if line.strip():
            logger.info(line)

    # 4. Send Telegram notification
    try:
        sent = asyncio.run(send_telegram_notification(summary))
        if sent:
            logger.info("Startup notification sent to Telegram")
        else:
            logger.warning("Startup notification not sent (no channels or send failed)")
    except Exception:
        logger.exception("Telegram notification failed (non-fatal)")


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


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    command = sys.argv[1] if len(sys.argv) > 1 else "startup"
    reason = sys.argv[2] if len(sys.argv) > 2 else "scheduled"

    if command == "startup":
        _run_startup()
    elif command == "shutdown":
        _run_shutdown(reason=reason)
    else:
        print(f"Usage: python -m signal_engine.scripts.openalgoscheduler [startup|shutdown] [reason]")
        sys.exit(1)
