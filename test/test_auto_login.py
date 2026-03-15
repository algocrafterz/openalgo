"""Tests for signal_engine/scripts/auto_login.py — automated broker login with TOTP."""

import os
from unittest.mock import MagicMock, patch

import pyotp
import pytest

from signal_engine.scripts.auto_login import (
    auto_login,
    generate_totp,
    get_broker_name,
    validate_auto_login_env,
)


class TestGenerateTotp:
    """Test TOTP code generation from stored secret."""

    def test_generates_6_digit_code(self):
        secret = pyotp.random_base32()
        code = generate_totp(secret)
        assert len(code) == 6
        assert code.isdigit()

    def test_code_matches_pyotp(self):
        secret = pyotp.random_base32()
        expected = pyotp.TOTP(secret).now()
        actual = generate_totp(secret)
        assert actual == expected

    def test_invalid_secret_raises(self):
        with pytest.raises(ValueError, match="TOTP secret"):
            generate_totp("")

    def test_none_secret_raises(self):
        with pytest.raises(ValueError, match="TOTP secret"):
            generate_totp(None)


class TestGetBrokerName:
    """Test broker name resolution from environment."""

    def test_reads_from_env(self):
        with patch.dict(os.environ, {"BROKER_NAME": "zerodha"}):
            assert get_broker_name() == "zerodha"

    def test_defaults_to_mstock(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BROKER_NAME", None)
            assert get_broker_name() == "mstock"

    def test_strips_whitespace(self):
        with patch.dict(os.environ, {"BROKER_NAME": "  dhan  "}):
            assert get_broker_name() == "dhan"

    def test_lowercases(self):
        with patch.dict(os.environ, {"BROKER_NAME": "Zerodha"}):
            assert get_broker_name() == "zerodha"


class TestValidateAutoLoginEnv:
    """Test environment variable validation for auto-login."""

    def test_all_vars_present(self):
        env = {
            "BROKER_PASSWORD": "mypassword",
            "BROKER_TOTP_SECRET": pyotp.random_base32(),
        }
        with patch.dict(os.environ, env):
            result = validate_auto_login_env()
            assert result["broker_password"] == "mypassword"
            assert result["totp_secret"] == env["BROKER_TOTP_SECRET"]

    def test_missing_password_raises(self):
        with patch.dict(os.environ, {"BROKER_TOTP_SECRET": "secret"}, clear=False):
            os.environ.pop("BROKER_PASSWORD", None)
            with pytest.raises(EnvironmentError, match="BROKER_PASSWORD"):
                validate_auto_login_env()

    def test_missing_totp_secret_raises(self):
        with patch.dict(os.environ, {"BROKER_PASSWORD": "pw"}, clear=False):
            os.environ.pop("BROKER_TOTP_SECRET", None)
            with pytest.raises(EnvironmentError, match="BROKER_TOTP_SECRET"):
                validate_auto_login_env()


class TestAutoLogin:
    """Test the main auto_login function end-to-end (with DI mocks)."""

    def _make_mocks(self, auth_return, user=None, upsert_return=1):
        """Helper to build mock dependencies including master contract mocks."""
        mock_auth = MagicMock(return_value=auth_return)

        mock_find_user = MagicMock()
        if user is None:
            mock_user = MagicMock()
            mock_user.username = "admin"
            mock_find_user.return_value = mock_user
        else:
            mock_find_user.return_value = user

        mock_upsert = MagicMock(return_value=upsert_return)
        mock_init_status = MagicMock()
        mock_should_download = MagicMock(return_value=(True, "No previous download"))
        mock_download = MagicMock()
        mock_load_existing = MagicMock()

        return {
            "_authenticate_with_totp": mock_auth,
            "_upsert_auth": mock_upsert,
            "_find_user_by_username": mock_find_user,
            "_init_broker_status": mock_init_status,
            "_should_download_master_contract": mock_should_download,
            "_async_master_contract_download": mock_download,
            "_load_existing_master_contract": mock_load_existing,
        }

    @patch.dict(os.environ, {
        "BROKER_PASSWORD": "pass123",
        "BROKER_TOTP_SECRET": pyotp.random_base32(),
    })
    def test_successful_login(self):
        mocks = self._make_mocks(
            auth_return=("jwt_token_xyz", "feed_token_abc", None),
        )

        success, message = auto_login(**mocks)

        assert success is True
        assert "success" in message.lower()
        mocks["_authenticate_with_totp"].assert_called_once()
        call_args = mocks["_authenticate_with_totp"].call_args
        assert call_args[0][0] == "pass123"
        mocks["_upsert_auth"].assert_called_once_with(
            "admin", "jwt_token_xyz", "mstock", feed_token="feed_token_abc"
        )

    @patch.dict(os.environ, {
        "BROKER_PASSWORD": "pass123",
        "BROKER_TOTP_SECRET": pyotp.random_base32(),
        "BROKER_NAME": "zerodha",
    })
    def test_uses_broker_name_from_env(self):
        mocks = self._make_mocks(
            auth_return=("jwt_token", "feed_token", None),
        )

        success, _ = auto_login(**mocks)

        assert success is True
        mocks["_upsert_auth"].assert_called_once_with(
            "admin", "jwt_token", "zerodha", feed_token="feed_token"
        )
        mocks["_init_broker_status"].assert_called_once_with("zerodha")

    @patch.dict(os.environ, {
        "BROKER_PASSWORD": "pass123",
        "BROKER_TOTP_SECRET": pyotp.random_base32(),
    })
    def test_triggers_master_contract_download(self):
        mocks = self._make_mocks(
            auth_return=("jwt_token", "feed_token", None),
        )
        # should_download returns True
        mocks["_should_download_master_contract"].return_value = (
            True, "No previous download"
        )

        success, _ = auto_login(**mocks)

        assert success is True
        mocks["_init_broker_status"].assert_called_once_with("mstock")
        mocks["_should_download_master_contract"].assert_called_once_with("mstock")

    @patch.dict(os.environ, {
        "BROKER_PASSWORD": "pass123",
        "BROKER_TOTP_SECRET": pyotp.random_base32(),
    })
    def test_uses_cached_master_contract(self):
        mocks = self._make_mocks(
            auth_return=("jwt_token", "feed_token", None),
        )
        # should_download returns False (cached)
        mocks["_should_download_master_contract"].return_value = (
            False, "Already downloaded today"
        )

        success, _ = auto_login(**mocks)

        assert success is True
        mocks["_init_broker_status"].assert_called_once_with("mstock")

    @patch.dict(os.environ, {
        "BROKER_PASSWORD": "pass",
        "BROKER_TOTP_SECRET": pyotp.random_base32(),
    })
    def test_broker_auth_fails(self):
        mocks = self._make_mocks(
            auth_return=(None, None, "Invalid TOTP code"),
        )

        success, message = auto_login(**mocks)

        assert success is False
        assert "Invalid TOTP" in message
        mocks["_upsert_auth"].assert_not_called()
        mocks["_init_broker_status"].assert_not_called()

    @patch.dict(os.environ, {
        "BROKER_PASSWORD": "pass",
        "BROKER_TOTP_SECRET": pyotp.random_base32(),
    })
    def test_no_admin_user(self):
        mocks = self._make_mocks(
            auth_return=("token", None, None),
        )
        mocks["_find_user_by_username"].return_value = None

        success, message = auto_login(**mocks)

        assert success is False
        assert "admin" in message.lower() or "user" in message.lower()
        mocks["_authenticate_with_totp"].assert_not_called()

    def test_missing_env_vars(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BROKER_PASSWORD", None)
            os.environ.pop("BROKER_TOTP_SECRET", None)

            mocks = self._make_mocks(auth_return=("t", None, None))
            success, message = auto_login(**mocks)

            assert success is False
            assert "BROKER_PASSWORD" in message

    @patch.dict(os.environ, {
        "BROKER_PASSWORD": "pass",
        "BROKER_TOTP_SECRET": pyotp.random_base32(),
    })
    def test_db_upsert_fails(self):
        mocks = self._make_mocks(
            auth_return=("jwt_token", "feed_token", None),
            upsert_return=None,
        )

        success, message = auto_login(**mocks)

        assert success is False
        assert "store" in message.lower() or "database" in message.lower()
        # Master contract should NOT be triggered if upsert failed
        mocks["_init_broker_status"].assert_not_called()

    @patch.dict(os.environ, {
        "BROKER_PASSWORD": "pass",
        "BROKER_TOTP_SECRET": pyotp.random_base32(),
    })
    def test_no_feed_token_still_succeeds(self):
        mocks = self._make_mocks(
            auth_return=("jwt_token", None, None),
        )

        success, message = auto_login(**mocks)

        assert success is True
        mocks["_upsert_auth"].assert_called_once_with(
            "admin", "jwt_token", "mstock", feed_token=None
        )
