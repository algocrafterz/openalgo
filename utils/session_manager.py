"""Generic broker session refresh utility.

When a broker's API returns a session-expired error, this module re-authenticates
using headless TOTP login (for brokers that support it) and stores the new token.

OAuth-only brokers (Zerodha, etc.) cannot be refreshed automatically — they require
a manual browser login.
"""

import importlib
import os

import pyotp

from database.auth_db import upsert_auth
from utils.logging import get_logger

logger = get_logger(__name__)

# Guard against concurrent refresh storms — one refresh per (username, broker) at a time.
# Uses a simple in-process set; sufficient because Flask runs as a single worker.
_refresh_in_progress: set[tuple[str, str]] = set()


def refresh_broker_session(username: str, broker: str) -> tuple[bool, str | None]:
    """Re-authenticate with broker using headless TOTP.

    Supports any broker that implements ``authenticate_with_totp`` in its
    ``broker.{broker}.api.auth_api`` module.  OAuth-only brokers (no TOTP
    function) return ``(False, None)`` immediately.

    Returns:
        (True, new_auth_token) on success, (False, None) on failure.
    """
    key = (username, broker)
    if key in _refresh_in_progress:
        logger.debug(f"Session refresh already in progress for {broker}/{username}, skipping")
        return False, None

    _refresh_in_progress.add(key)
    try:
        return _do_refresh(username, broker)
    finally:
        _refresh_in_progress.discard(key)


def _do_refresh(username: str, broker: str) -> tuple[bool, str | None]:
    # Import broker auth module
    try:
        auth_module = importlib.import_module(f"broker.{broker}.api.auth_api")
    except ImportError:
        logger.warning(f"Session refresh: no auth module found for broker '{broker}'")
        return False, None

    if not hasattr(auth_module, "authenticate_with_totp"):
        logger.info(
            f"Session refresh: broker '{broker}' uses OAuth-only login — "
            "cannot refresh automatically, manual re-login required"
        )
        return False, None

    # Load credentials from environment
    password = os.environ.get("BROKER_PASSWORD", "").strip()
    totp_secret = os.environ.get("BROKER_TOTP_SECRET", "").strip()

    if not password:
        logger.error("Session refresh: BROKER_PASSWORD not set in environment")
        return False, None
    if not totp_secret:
        logger.error("Session refresh: BROKER_TOTP_SECRET not set in environment")
        return False, None

    try:
        totp_code = pyotp.TOTP(totp_secret).now()
    except Exception as e:
        logger.error(f"Session refresh: TOTP generation failed: {e}")
        return False, None

    logger.info(f"Refreshing session for user='{username}' broker='{broker}'")

    try:
        result = auth_module.authenticate_with_totp(password, totp_code)
    except Exception as e:
        logger.error(f"Session refresh: authenticate_with_totp raised: {e}")
        return False, None

    # Normalize tuple variants: (token, feed, err) | (token, err) | token
    auth_token, feed_token, error = None, None, None
    if isinstance(result, tuple):
        if len(result) == 3:
            auth_token, feed_token, error = result
        elif len(result) == 2:
            auth_token, error = result
        elif len(result) == 1:
            auth_token = result[0]
    else:
        auth_token = result

    if error or not auth_token:
        logger.error(f"Session refresh failed for broker '{broker}': {error}")
        return False, None

    upsert_auth(username, auth_token, broker, feed_token=feed_token)
    logger.info(f"Session refreshed — new token stored for user='{username}' broker='{broker}'")
    return True, auth_token
