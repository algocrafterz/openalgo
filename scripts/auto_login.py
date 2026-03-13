"""Automated broker login with TOTP generation.

Reads BROKER_PASSWORD and BROKER_TOTP_SECRET from environment,
generates a TOTP code, authenticates with the broker, and stores
the auth token in the database — bypassing the web UI.

Usage:
    uv run python -m scripts.auto_login
"""

import os
import sys

import pyotp

BROKER_NAME = "mstock"


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
    logger.info("Auto-login starting for user: %s", username)

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
        username, auth_token, BROKER_NAME, feed_token=feed_token
    )
    if not inserted_id:
        return False, "Failed to store auth token in database"

    logger.info("Auth token stored for user: %s", username)

    # 6. Init broker status and trigger master contract download
    # (same logic as handle_auth_success in utils/auth_utils.py)
    _init_broker_status(BROKER_NAME)

    should_download, reason = _should_download_master_contract(BROKER_NAME)
    logger.info("Smart download check: should_download=%s, reason=%s",
                should_download, reason)

    if should_download:
        thread = Thread(
            target=_async_master_contract_download,
            args=(BROKER_NAME,), daemon=True
        )
        thread.start()
        logger.info("Master contract download started in background")
    else:
        thread = Thread(
            target=_load_existing_master_contract,
            args=(BROKER_NAME,), daemon=True
        )
        thread.start()
        logger.info("Loading cached master contract: %s", reason)

    return True, f"Auto-login successful for {username}"


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    success, message = auto_login()
    if success:
        from utils.logging import get_logger
        get_logger(__name__).info(message)
    else:
        from utils.logging import get_logger
        get_logger(__name__).error("Auto-login failed: %s", message)
        sys.exit(1)
