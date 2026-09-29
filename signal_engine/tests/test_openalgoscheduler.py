"""Tests for openalgoscheduler — broker-neutral startup/shutdown automation."""

import asyncio
import importlib
from unittest.mock import MagicMock

import pytest


class TestGetBrokerName:
    def test_raises_when_neither_var_set(self, monkeypatch):
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.delenv("REDIRECT_URL", raising=False)
        from signal_engine.scripts.openalgoscheduler import get_broker_name
        with pytest.raises(EnvironmentError):
            get_broker_name()

    def test_reads_from_broker_name_env_var(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "zerodha")
        monkeypatch.delenv("REDIRECT_URL", raising=False)
        from signal_engine.scripts.openalgoscheduler import get_broker_name
        assert get_broker_name() == "zerodha"

    def test_broker_name_takes_priority_over_redirect_url(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "mstock")
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/flattrade/callback")
        from signal_engine.scripts.openalgoscheduler import get_broker_name
        assert get_broker_name() == "mstock"

    def test_parses_broker_from_redirect_url(self, monkeypatch):
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/flattrade/callback")
        from signal_engine.scripts.openalgoscheduler import get_broker_name
        assert get_broker_name() == "flattrade"

    def test_parses_broker_from_redirect_url_any_broker(self, monkeypatch):
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.setenv("REDIRECT_URL", "https://myserver.com/zerodha/callback")
        from signal_engine.scripts.openalgoscheduler import get_broker_name
        assert get_broker_name() == "zerodha"

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "  angel  ")
        from signal_engine.scripts.openalgoscheduler import get_broker_name
        assert get_broker_name() == "angel"

    def test_lowercases(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "DHAN")
        from signal_engine.scripts.openalgoscheduler import get_broker_name
        assert get_broker_name() == "dhan"


class TestVerifyBrokerAuth:
    """verify_broker_auth must be broker-neutral — no hardcoded broker names."""

    def test_uses_configured_broker_not_flattrade(self, monkeypatch):
        """Should call configured broker's get_margin_data, not hardcoded flattrade."""
        monkeypatch.setenv("BROKER_NAME", "zerodha")

        mock_margin_data = {"availablecash": "50000.00", "utiliseddebits": "5000.00",
                            "m2mrealized": "0.00", "m2munrealized": "0.00", "collateral": "0.00"}
        mock_get_margin_data = MagicMock(return_value=mock_margin_data)

        from signal_engine.scripts.openalgoscheduler import verify_broker_auth
        result = verify_broker_auth("test_token", _get_margin_data=mock_get_margin_data)

        mock_get_margin_data.assert_called_once_with("test_token")
        assert result == mock_margin_data

    def test_returns_none_when_no_auth_token(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "flattrade")
        from signal_engine.scripts.openalgoscheduler import verify_broker_auth
        result = verify_broker_auth(None)
        assert result is None

    def test_returns_none_when_broker_module_import_fails(self, monkeypatch):
        """If the configured broker's funds module doesn't exist, return None (not crash)."""
        monkeypatch.setenv("BROKER_NAME", "nonexistent_broker_xyz")
        from signal_engine.scripts.openalgoscheduler import verify_broker_auth
        result = verify_broker_auth("some_token")
        assert result is None

    def test_returns_none_when_margin_data_empty(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "flattrade")
        mock_get_margin_data = MagicMock(return_value={})
        from signal_engine.scripts.openalgoscheduler import verify_broker_auth
        result = verify_broker_auth("test_token", _get_margin_data=mock_get_margin_data)
        assert result is None

    def test_returns_none_on_exception(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "flattrade")
        mock_get_margin_data = MagicMock(side_effect=RuntimeError("API down"))
        from signal_engine.scripts.openalgoscheduler import verify_broker_auth
        result = verify_broker_auth("test_token", _get_margin_data=mock_get_margin_data)
        assert result is None

    def test_returns_fund_data_on_success(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "flattrade")
        expected = {"availablecash": "25000.00", "utiliseddebits": "0.00",
                    "m2mrealized": "500.00", "m2munrealized": "-100.00", "collateral": "0.00"}
        mock_get_margin_data = MagicMock(return_value=expected)
        from signal_engine.scripts.openalgoscheduler import verify_broker_auth
        result = verify_broker_auth("live_token", _get_margin_data=mock_get_margin_data)
        assert result == expected


class TestAutoLoginBrokerNeutral:
    """auto_login must not hardcode flattrade — uses configured broker."""

    def _make_mock_auth_fn(self, auth_token="test_token_123", feed_token=None, error=None):
        return MagicMock(return_value=(auth_token, feed_token, error))

    def _make_stubs(self, auth_fn=None):
        if auth_fn is None:
            auth_fn = self._make_mock_auth_fn()
        admin_user = MagicMock()
        admin_user.username = "admin"
        upsert_auth = MagicMock(return_value=1)
        find_user = MagicMock(return_value=admin_user)
        init_broker_status = MagicMock()
        should_download = MagicMock(return_value=(False, "cached"))
        async_download = MagicMock()
        load_existing = MagicMock()
        return (auth_fn, upsert_auth, find_user, init_broker_status,
                should_download, async_download, load_existing)

    def test_succeeds_with_configured_broker(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "flattrade")
        monkeypatch.setenv("BROKER_PASSWORD", "pass123")
        monkeypatch.setenv("BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        stubs = self._make_stubs()
        from signal_engine.scripts.openalgoscheduler import auto_login
        result = auto_login(*stubs)
        success, msg, token = result
        assert success is True
        assert "admin" in msg
        assert token == "test_token_123"

    def test_fails_when_no_admin_user(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "flattrade")
        monkeypatch.setenv("BROKER_PASSWORD", "pass123")
        monkeypatch.setenv("BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        auth_fn = self._make_mock_auth_fn()
        stubs = list(self._make_stubs(auth_fn))
        stubs[2] = MagicMock(return_value=None)  # find_user returns None
        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, _ = auto_login(*stubs)
        assert success is False
        assert "admin" in msg.lower() or "user" in msg.lower()

    def test_fails_when_broker_auth_returns_error(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "flattrade")
        monkeypatch.setenv("BROKER_PASSWORD", "pass123")
        monkeypatch.setenv("BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        stubs = list(self._make_stubs(self._make_mock_auth_fn(auth_token=None, error="Invalid credentials")))
        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, _ = auto_login(*stubs)
        assert success is False
        assert "Invalid credentials" in msg

    def test_fails_when_missing_broker_password(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "flattrade")
        monkeypatch.delenv("BROKER_PASSWORD", raising=False)
        monkeypatch.setenv("BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        stubs = self._make_stubs()
        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, _ = auto_login(*stubs)
        assert success is False
        assert "BROKER_PASSWORD" in msg

    def test_fails_when_missing_totp_secret(self, monkeypatch):
        monkeypatch.setenv("BROKER_NAME", "flattrade")
        monkeypatch.setenv("BROKER_PASSWORD", "pass123")
        monkeypatch.delenv("BROKER_TOTP_SECRET", raising=False)

        stubs = self._make_stubs()
        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, _ = auto_login(*stubs)
        assert success is False
        assert "BROKER_TOTP_SECRET" in msg

    def test_does_not_hardcode_flattrade_module(self, monkeypatch):
        """Ensure auto_login does not import broker.flattrade directly when broker is different."""
        monkeypatch.setenv("BROKER_NAME", "zerodha")
        monkeypatch.setenv("BROKER_PASSWORD", "pass123")
        monkeypatch.setenv("BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        stubs = self._make_stubs()
        # If flattrade module were hardcoded, we could catch the import.
        # With broker-neutral code, auto_login should try broker.zerodha.api.auth_api
        # and use the injected auth_fn (stubs[0]) since _authenticate_with_totp is provided.
        from signal_engine.scripts.openalgoscheduler import auto_login
        result = auto_login(*stubs)
        success, msg, token = result
        # Should succeed via injected stub — never falls back to hardcoded flattrade
        assert success is True


class TestAutoLoginOAuthBroker:
    """OAuth-only brokers (e.g. zerodha) must use existing DB token — no programmatic login.

    Flattrade now supports authenticate_with_totp (direct TOTP login).
    Zerodha is used here as an example of a pure OAuth broker.
    """

    def _make_oauth_stubs(self, existing_token="existing_zerodha_token", stored_broker="zerodha"):
        """Build stubs for an OAuth broker scenario (no _authenticate_with_totp injected)."""
        admin_user = MagicMock()
        admin_user.username = "admin"
        find_user = MagicMock(return_value=admin_user)
        init_broker_status = MagicMock()
        should_download = MagicMock(return_value=(False, "cached"))
        async_download = MagicMock()
        load_existing = MagicMock()

        auth_obj = MagicMock()
        auth_obj.broker = stored_broker
        get_existing_auth = MagicMock(return_value=(existing_token, auth_obj))

        return (find_user, init_broker_status, should_download, async_download,
                load_existing, get_existing_auth)

    def test_oauth_broker_uses_existing_db_token(self, monkeypatch):
        """OAuth broker (no authenticate_with_totp) retrieves and returns existing DB token."""
        monkeypatch.setenv("BROKER_NAME", "zerodha")  # zerodha has no authenticate_with_totp

        find_user, init_broker_status, should_download, async_download, load_existing, get_existing_auth = (
            self._make_oauth_stubs()
        )

        from signal_engine.scripts.openalgoscheduler import auto_login
        # No _authenticate_with_totp injected — simulates production OAuth path for zerodha
        success, msg, token = auto_login(
            _authenticate_with_totp=None,
            _upsert_auth=None,
            _find_user_by_username=find_user,
            _init_broker_status=init_broker_status,
            _should_download_master_contract=should_download,
            _async_master_contract_download=async_download,
            _load_existing_master_contract=load_existing,
            _get_existing_auth=get_existing_auth,
        )
        assert success is True
        assert token == "existing_zerodha_token"
        assert "successful" in msg.lower() or "admin" in msg

    def test_oauth_broker_fails_when_no_existing_token(self, monkeypatch):
        """OAuth broker with no DB token returns clear error asking user to login via browser."""
        monkeypatch.setenv("BROKER_NAME", "zerodha")

        find_user, init_broker_status, should_download, async_download, load_existing, _ = (
            self._make_oauth_stubs()
        )
        get_existing_auth = MagicMock(return_value=(None, None))  # No token in DB

        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, token = auto_login(
            _authenticate_with_totp=None,
            _upsert_auth=None,
            _find_user_by_username=find_user,
            _init_broker_status=init_broker_status,
            _should_download_master_contract=should_download,
            _async_master_contract_download=async_download,
            _load_existing_master_contract=load_existing,
            _get_existing_auth=get_existing_auth,
        )
        assert success is False
        assert token is None
        assert "oauth" in msg.lower() or "web" in msg.lower() or "browser" in msg.lower()

    def test_oauth_broker_fails_on_broker_mismatch(self, monkeypatch):
        """Stored token is from a different broker → clear error to re-login."""
        monkeypatch.setenv("BROKER_NAME", "zerodha")

        find_user, init_broker_status, should_download, async_download, load_existing, _ = (
            self._make_oauth_stubs()
        )
        # Token exists but was stored for mstock, not zerodha
        auth_obj_mismatch = MagicMock()
        auth_obj_mismatch.broker = "mstock"
        get_existing_auth = MagicMock(return_value=("old_mstock_token", auth_obj_mismatch))

        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, token = auto_login(
            _authenticate_with_totp=None,
            _upsert_auth=None,
            _find_user_by_username=find_user,
            _init_broker_status=init_broker_status,
            _should_download_master_contract=should_download,
            _async_master_contract_download=async_download,
            _load_existing_master_contract=load_existing,
            _get_existing_auth=get_existing_auth,
        )
        assert success is False
        assert token is None
        assert "mstock" in msg or "zerodha" in msg or "broker" in msg.lower()


class TestBrokerModuleContracts:
    """Both mstock and flattrade must satisfy the auto_login contract."""

    def test_mstock_has_authenticate_with_totp(self):
        """mstock supports programmatic TOTP login."""
        m = importlib.import_module("broker.mstock.api.auth_api")
        assert hasattr(m, "authenticate_with_totp"), (
            "broker.mstock.api.auth_api must expose authenticate_with_totp"
        )

    def test_flattrade_has_authenticate_with_totp(self):
        """flattrade supports programmatic TOTP login (NorenAPI PiConnect)."""
        m = importlib.import_module("broker.flattrade.api.auth_api")
        assert hasattr(m, "authenticate_with_totp"), (
            "broker.flattrade.api.auth_api must expose authenticate_with_totp"
        )

    def test_mstock_authenticate_with_totp_returns_3tuple(self, monkeypatch):
        """mstock authenticate_with_totp always returns (token, feed_token, error)."""
        monkeypatch.setenv("BROKER_API_KEY", "MA123456")
        # Missing BROKER_API_KEY → should return a 3-tuple with error, not raise
        m = importlib.import_module("broker.mstock.api.auth_api")
        result = m.authenticate_with_totp("", "")
        assert isinstance(result, tuple) and len(result) == 3

    def test_flattrade_authenticate_with_totp_returns_3tuple(self, monkeypatch):
        """flattrade authenticate_with_totp always returns (token, feed_token, error)."""
        # Missing credentials → should return error 3-tuple, not raise
        monkeypatch.delenv("BROKER_API_KEY", raising=False)
        m = importlib.import_module("broker.flattrade.api.auth_api")
        result = m.authenticate_with_totp("somepass", "123456")
        assert isinstance(result, tuple) and len(result) == 3
        assert result[0] is None   # no token
        assert result[2] is not None  # has error message


class TestBrokerSwitch:
    """Switching between mstock and flattrade by only changing .env variables."""

    def _make_stubs(self, auth_fn=None, token="tok_123"):
        """Build full auto_login stubs for a programmatic-login broker."""
        if auth_fn is None:
            auth_fn = MagicMock(return_value=(token, None, None))
        admin_user = MagicMock()
        admin_user.username = "admin"
        return (
            auth_fn,
            MagicMock(return_value=1),          # upsert_auth
            MagicMock(return_value=admin_user),  # find_user
            MagicMock(),                         # init_broker_status
            MagicMock(return_value=(False, "cached")),  # should_download
            MagicMock(),                         # async_download
            MagicMock(),                         # load_existing
        )

    # ------------------------------------------------------------------
    # Scenario A: user switches from mstock to flattrade via REDIRECT_URL
    # ------------------------------------------------------------------

    def test_mstock_redirect_url_resolves_to_mstock(self, monkeypatch):
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/mstock/callback")
        from signal_engine.scripts.openalgoscheduler import get_broker_name
        assert get_broker_name() == "mstock"

    def test_flattrade_redirect_url_resolves_to_flattrade(self, monkeypatch):
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/flattrade/callback")
        from signal_engine.scripts.openalgoscheduler import get_broker_name
        assert get_broker_name() == "flattrade"

    def test_mstock_auto_login_via_redirect_url(self, monkeypatch):
        """Full auto_login flow when REDIRECT_URL points to mstock."""
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/mstock/callback")
        monkeypatch.setenv("BROKER_PASSWORD", "pass123")
        monkeypatch.setenv("BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        stubs = self._make_stubs(token="mstock_token_abc")
        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, token = auto_login(*stubs)

        assert success is True
        assert token == "mstock_token_abc"

    def test_flattrade_auto_login_via_redirect_url(self, monkeypatch):
        """Full auto_login flow when REDIRECT_URL points to flattrade."""
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/flattrade/callback")
        monkeypatch.setenv("BROKER_PASSWORD", "pass123")
        monkeypatch.setenv("BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        stubs = self._make_stubs(token="flattrade_token_xyz")
        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, token = auto_login(*stubs)

        assert success is True
        assert token == "flattrade_token_xyz"

    def test_switch_mstock_to_flattrade_generates_new_token(self, monkeypatch):
        """Switching REDIRECT_URL from mstock to flattrade produces a new token.

        Both brokers use programmatic TOTP login, so switching is seamless —
        auto_login calls authenticate_with_totp for the new broker and stores
        a fresh token. No stale-token mismatch issue.
        """
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/flattrade/callback")
        monkeypatch.setenv("BROKER_PASSWORD", "pass123")
        monkeypatch.setenv("BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        new_flattrade_token = "flattrade_fresh_token"
        stubs = self._make_stubs(token=new_flattrade_token)
        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, token = auto_login(*stubs)

        assert success is True
        assert token == new_flattrade_token
        # upsert_auth called → new token stored for flattrade
        upsert_mock = stubs[1]
        upsert_mock.assert_called_once()
        call_args = upsert_mock.call_args
        assert call_args[0][2] == "flattrade"  # broker arg = flattrade

    def test_switch_flattrade_to_mstock_generates_new_token(self, monkeypatch):
        """Switching from flattrade to mstock works identically — new token for mstock."""
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/mstock/callback")
        monkeypatch.setenv("BROKER_PASSWORD", "pass123")
        monkeypatch.setenv("BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        new_mstock_token = "mstock_fresh_token"
        stubs = self._make_stubs(token=new_mstock_token)
        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, token = auto_login(*stubs)

        assert success is True
        assert token == new_mstock_token
        upsert_mock = stubs[1]
        upsert_mock.assert_called_once()
        assert upsert_mock.call_args[0][2] == "mstock"

    # ------------------------------------------------------------------
    # Scenario B: switching to an OAuth-only broker (e.g. zerodha)
    # ------------------------------------------------------------------

    def test_switch_to_oauth_broker_fails_with_stale_programmatic_token(self, monkeypatch):
        """Switching to zerodha (OAuth-only) when DB still holds an mstock token → clear error."""
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/zerodha/callback")

        admin_user = MagicMock()
        admin_user.username = "admin"
        auth_obj = MagicMock()
        auth_obj.broker = "mstock"   # old token for mstock in DB
        get_existing_auth = MagicMock(return_value=("old_mstock_token", auth_obj))

        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, token = auto_login(
            _authenticate_with_totp=None,
            _upsert_auth=None,
            _find_user_by_username=MagicMock(return_value=admin_user),
            _init_broker_status=MagicMock(),
            _should_download_master_contract=MagicMock(return_value=(False, "cached")),
            _async_master_contract_download=MagicMock(),
            _load_existing_master_contract=MagicMock(),
            _get_existing_auth=get_existing_auth,
        )
        assert success is False
        assert token is None
        # Error must name both brokers so user knows exactly what happened
        assert "mstock" in msg
        assert "zerodha" in msg

    def test_switch_to_oauth_broker_succeeds_with_correct_existing_token(self, monkeypatch):
        """Switching to zerodha (OAuth) with a valid zerodha token already in DB → succeeds."""
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/zerodha/callback")

        admin_user = MagicMock()
        admin_user.username = "admin"
        auth_obj = MagicMock()
        auth_obj.broker = "zerodha"  # DB token is already for zerodha
        get_existing_auth = MagicMock(return_value=("zerodha_live_token", auth_obj))

        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, token = auto_login(
            _authenticate_with_totp=None,
            _upsert_auth=None,
            _find_user_by_username=MagicMock(return_value=admin_user),
            _init_broker_status=MagicMock(),
            _should_download_master_contract=MagicMock(return_value=(False, "cached")),
            _async_master_contract_download=MagicMock(),
            _load_existing_master_contract=MagicMock(),
            _get_existing_auth=get_existing_auth,
        )
        assert success is True
        assert token == "zerodha_live_token"

    # ------------------------------------------------------------------
    # Scenario C: misconfigured .env (missing credentials)
    # ------------------------------------------------------------------

    def test_missing_broker_password_fails_fast(self, monkeypatch):
        """If BROKER_PASSWORD is not set, fail immediately — don't silently proceed."""
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/flattrade/callback")
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.delenv("BROKER_PASSWORD", raising=False)
        monkeypatch.setenv("BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        stubs = self._make_stubs()
        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, token = auto_login(*stubs)
        assert success is False
        assert token is None
        assert "BROKER_PASSWORD" in msg

    def test_missing_totp_secret_fails_fast(self, monkeypatch):
        """If BROKER_TOTP_SECRET is not set, fail immediately."""
        monkeypatch.setenv("REDIRECT_URL", "http://127.0.0.1:5000/mstock/callback")
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.setenv("BROKER_PASSWORD", "pass123")
        monkeypatch.delenv("BROKER_TOTP_SECRET", raising=False)

        stubs = self._make_stubs()
        from signal_engine.scripts.openalgoscheduler import auto_login
        success, msg, token = auto_login(*stubs)
        assert success is False
        assert token is None
        assert "BROKER_TOTP_SECRET" in msg

    def test_no_redirect_url_and_no_broker_name_fails_fast(self, monkeypatch):
        """No REDIRECT_URL and no BROKER_NAME → EnvironmentError immediately."""
        monkeypatch.delenv("BROKER_NAME", raising=False)
        monkeypatch.delenv("REDIRECT_URL", raising=False)
        from signal_engine.scripts.openalgoscheduler import get_broker_name
        with pytest.raises(EnvironmentError):
            get_broker_name()


class TestGenerateTotp:
    def test_generates_6_digit_code(self):
        from signal_engine.scripts.openalgoscheduler import generate_totp
        code = generate_totp("JBSWY3DPEHPK3PXP")
        assert len(code) == 6
        assert code.isdigit()

    def test_raises_on_empty_secret(self):
        from signal_engine.scripts.openalgoscheduler import generate_totp
        with pytest.raises(ValueError):
            generate_totp("")

    def test_raises_on_none_secret(self):
        from signal_engine.scripts.openalgoscheduler import generate_totp
        with pytest.raises(ValueError):
            generate_totp(None)


class TestNotifyFailure:
    """A failed startup must be announced, not swallowed.

    Regression guard for 2026-08-24: one auth failure at 09:03 wrote a 24h
    cooldown that silently blocked 80 further start attempts across the whole
    trading day. Nothing reached Telegram because _run_startup exited before
    the notification step.
    """

    def test_message_contains_stage_and_detail(self):
        from signal_engine.scripts.openalgoscheduler import notify_failure
        sent = {}

        def _send(msg):
            sent["msg"] = msg
            return True

        assert notify_failure("broker-auth", "Invalid credentials", _send=_send) is True
        assert "broker-auth" in sent["msg"]
        assert "Invalid credentials" in sent["msg"]

    def test_message_is_clearly_a_failure(self):
        from signal_engine.scripts.openalgoscheduler import notify_failure
        sent = {}
        notify_failure("login", "boom", _send=lambda m: sent.setdefault("msg", m) or True)
        assert "FAILED" in sent["msg"].upper()

    def test_message_is_ascii_only(self):
        """Logs and alerts stay ASCII - no unicode."""
        from signal_engine.scripts.openalgoscheduler import notify_failure
        sent = {}
        notify_failure("login", "boom", _send=lambda m: sent.setdefault("msg", m) or True)
        sent["msg"].encode("ascii")

    def test_returns_false_when_send_fails(self):
        from signal_engine.scripts.openalgoscheduler import notify_failure
        assert notify_failure("login", "x", _send=lambda m: False) is False

    def test_never_raises_when_sender_explodes(self):
        """An alert failure must not mask the original startup failure."""
        from signal_engine.scripts.openalgoscheduler import notify_failure

        def _boom(msg):
            raise RuntimeError("telegram down")

        assert notify_failure("login", "x", _send=_boom) is False


class TestStartupAlertsOnFailure:
    """_run_startup must alert before exiting on every failure path."""

    def _patch(self, monkeypatch, **attrs):
        import signal_engine.scripts.openalgoscheduler as sched
        for k, v in attrs.items():
            monkeypatch.setattr(sched, k, v)
        return sched

    def test_alerts_when_auto_login_fails(self, monkeypatch):
        calls = []
        sched = self._patch(
            monkeypatch,
            auto_login=lambda: (False, "Invalid credentials", None),
            notify_failure=lambda stage, detail: calls.append((stage, detail)),
        )
        with pytest.raises(SystemExit):
            sched._run_startup()
        assert calls, "no alert was sent on auto-login failure"
        assert "Invalid credentials" in calls[0][1]

    def test_alerts_when_token_verification_fails(self, monkeypatch):
        calls = []
        sched = self._patch(
            monkeypatch,
            auto_login=lambda: (True, "ok", "tok"),
            verify_broker_auth=lambda tok: None,
            notify_failure=lambda stage, detail: calls.append((stage, detail)),
        )
        with pytest.raises(SystemExit):
            sched._run_startup()
        assert calls, "no alert was sent on token verification failure"

    def test_alerts_on_configuration_error(self, monkeypatch):
        calls = []

        def _boom():
            raise OSError("BROKER_NAME missing")

        sched = self._patch(
            monkeypatch,
            auto_login=_boom,
            notify_failure=lambda stage, detail: calls.append((stage, detail)),
        )
        with pytest.raises(SystemExit):
            sched._run_startup()
        assert calls, "no alert was sent on configuration error"
        assert "BROKER_NAME missing" in calls[0][1]


class TestHealthcheck:
    """_run_healthcheck must self-heal a dead broker session, quietly when
    nothing changed, loudly when it does.

    Regression guard for 2026-09-09: openalgoctl.sh's run() supervisor only
    logs in once, at process start. The Flattrade session expired before
    market open and stayed dead for ~6 hours until a manual restart, because
    nothing was periodically re-checking it. _run_healthcheck() is what
    openalgoctl.sh now calls on a timer to catch that automatically.
    """

    def _patch(self, monkeypatch, **attrs):
        import signal_engine.scripts.openalgoscheduler as sched
        for k, v in attrs.items():
            monkeypatch.setattr(sched, k, v)
        return sched

    def test_skips_quietly_when_auto_login_not_configured(self, monkeypatch):
        """OAuth-only brokers (no TOTP secret) have nothing for this check to
        do - it must not raise or exit."""
        def _boom():
            raise OSError("BROKER_TOTP_SECRET is not set")

        sched = self._patch(
            monkeypatch,
            validate_auto_login_env=_boom,
            _ensure_openalgo_api_usable=lambda context: True,
        )
        sched._run_healthcheck()  # must not raise

    def test_session_still_valid_sends_no_alert(self, monkeypatch):
        """auto_login() reusing an existing valid session must not trigger a
        Telegram alert - that would fire every cycle, all day."""
        calls = []
        sched = self._patch(
            monkeypatch,
            _ensure_openalgo_api_usable=lambda context: True,
            validate_auto_login_env=lambda: {"broker_password": "x", "totp_secret": "y"},
            auto_login=lambda: (True, "Session reused (no TOTP needed) for user: anand123hai", "tok"),
            notify_failure=lambda stage, detail: calls.append(("fail", stage, detail)),
        )
        sched._run_healthcheck()
        assert not calls, "no alert should fire when the session was already valid"

    def test_recovered_session_alerts_once(self, monkeypatch):
        """A fresh login performed by this check (session was dead) must send
        exactly one recovery notification."""
        sent = []

        async def _send(msg):
            sent.append(msg)
            return True

        sched = self._patch(
            monkeypatch,
            _ensure_openalgo_api_usable=lambda context: True,
            validate_auto_login_env=lambda: {"broker_password": "x", "totp_secret": "y"},
            auto_login=lambda: (True, "Auto-login successful for anand123hai", "tok"),
            send_telegram_notification=_send,
        )
        sched._run_healthcheck()
        assert len(sent) == 1
        assert "expired" in sent[0].lower()

    def test_relogin_failure_alerts_and_exits_nonzero(self, monkeypatch):
        """A dead session that auto re-login can't fix must alert and signal
        failure via exit code, so openalgoctl.sh's cooldown kicks in."""
        calls = []
        sched = self._patch(
            monkeypatch,
            _ensure_openalgo_api_usable=lambda context: True,
            validate_auto_login_env=lambda: {"broker_password": "x", "totp_secret": "y"},
            auto_login=lambda: (False, "Invalid credentials", None),
            notify_failure=lambda stage, detail: calls.append((stage, detail)),
        )
        with pytest.raises(SystemExit):
            sched._run_healthcheck()
        assert calls, "no alert was sent when re-login failed"
        assert "Invalid credentials" in calls[0][1]

    def test_configuration_error_during_relogin_alerts_and_exits(self, monkeypatch):
        def _boom():
            raise OSError("BROKER_PASSWORD missing")

        calls = []
        sched = self._patch(
            monkeypatch,
            _ensure_openalgo_api_usable=lambda context: True,
            validate_auto_login_env=lambda: {"broker_password": "x", "totp_secret": "y"},
            auto_login=_boom,
            notify_failure=lambda stage, detail: calls.append((stage, detail)),
        )
        with pytest.raises(SystemExit):
            sched._run_healthcheck()
        assert calls, "no alert was sent on configuration error during re-login"
        assert "BROKER_PASSWORD missing" in calls[0][1]

    def test_api_smoke_test_failure_alerts_and_exits(self, monkeypatch):
        """If OpenAlgo's own API is unusable even after self-heal, healthcheck
        must alert and exit non-zero - the exact gap that let 2026-09-28's
        ~90 minute outage run silently until a human noticed the missing trades."""
        calls = []

        def _no_totp():
            raise OSError("BROKER_TOTP_SECRET is not set")

        sched = self._patch(
            monkeypatch,
            validate_auto_login_env=_no_totp,
            _ensure_openalgo_api_usable=lambda context: (
                calls.append(context) or False
            ),
        )
        with pytest.raises(SystemExit):
            sched._run_healthcheck()
        assert calls == ["healthcheck"]

    def test_confirmed_session_but_api_rejected_exits_with_stale_app_code(self, monkeypatch):
        """Broker session fine, OpenAlgo API still 403: the app holds a stale auth cache.
        The supervisor must be told (exit 3) so it restarts app.py, not cooldown a login."""
        sched = self._patch(
            monkeypatch,
            validate_auto_login_env=lambda: {"broker_password": "x", "totp_secret": "y"},
            auto_login=lambda: (True, "fresh login", "tok"),
            _ensure_openalgo_api_usable=lambda context: False,
            send_telegram_notification=lambda msg: asyncio.sleep(0),
        )
        with pytest.raises(SystemExit) as exc:
            sched._run_healthcheck()
        assert exc.value.code == sched.EXIT_APP_STATE_STALE == 3

    def test_unconfirmed_session_and_api_rejected_exits_1(self, monkeypatch):
        """If the session could not be checked, do not claim the app is the culprit."""
        def _no_totp():
            raise OSError("BROKER_TOTP_SECRET is not set")

        sched = self._patch(
            monkeypatch,
            validate_auto_login_env=_no_totp,
            _ensure_openalgo_api_usable=lambda context: False,
        )
        with pytest.raises(SystemExit) as exc:
            sched._run_healthcheck()
        assert exc.value.code == 1

    def test_relogin_runs_before_api_smoke_test(self, monkeypatch):
        """A revoked token 403s every OpenAlgo API call, so the smoke test can only
        pass after the re-login. Regression guard for 2026-09-29 10:00, where the
        smoke test ran first, failed, and exited without ever trying the login."""
        order = []
        sched = self._patch(
            monkeypatch,
            validate_auto_login_env=lambda: {"broker_password": "x", "totp_secret": "y"},
            auto_login=lambda: order.append("login") or (True, "fresh login", "tok"),
            _ensure_openalgo_api_usable=lambda context: order.append("smoke") or True,
            send_telegram_notification=lambda msg: asyncio.sleep(0),
        )
        sched._run_healthcheck()
        assert order == ["login", "smoke"]


class TestMasterContractCallOk:
    """_master_contract_call_ok must read both load functions' actual success contracts."""

    def test_true_bool_is_ok(self):
        from signal_engine.scripts.openalgoscheduler import _master_contract_call_ok
        assert _master_contract_call_ok(True) is True

    def test_false_bool_is_not_ok(self):
        from signal_engine.scripts.openalgoscheduler import _master_contract_call_ok
        assert _master_contract_call_ok(False) is False

    def test_error_dict_is_not_ok(self):
        from signal_engine.scripts.openalgoscheduler import _master_contract_call_ok
        assert _master_contract_call_ok({"status": "error", "message": "boom"}) is False

    def test_opaque_success_value_is_ok(self):
        """async_master_contract_download() returns a broker-specific opaque value on
        success (whatever master_contract_download() returned) - anything that isn't
        an explicit error dict or False must be treated as success."""
        from signal_engine.scripts.openalgoscheduler import _master_contract_call_ok
        assert _master_contract_call_ok({"status": "ok"}) is True
        assert _master_contract_call_ok("some-broker-specific-value") is True
        assert _master_contract_call_ok(None) is True


def _ready_status(total_symbols="152238"):
    """A master_contract_status row shaped like a genuinely completed download."""
    return {"status": "success", "is_ready": True, "total_symbols": total_symbols}


def _must_not_be_called(*args, **kwargs):
    raise AssertionError("get_status must not be called when the load call already failed")


class TestMasterContractLoadConfirmed:
    """_master_contract_load_confirmed is the authoritative check: it must trust the
    database row master_contract_status, not the load call's own return value - a call
    can report success while a killed thread left the row stuck at "downloading" (the
    exact 2026-09-28 shape), or while a partial download silently skipped an exchange.
    """

    def test_failed_call_short_circuits_without_checking_status(self):
        from signal_engine.scripts.openalgoscheduler import _master_contract_load_confirmed

        confirmed, detail = _master_contract_load_confirmed(
            "flattrade", _must_not_be_called, {"status": "error"}, should_download=True
        )
        assert confirmed is False

    def test_cached_load_success_trusts_the_calls_own_bool(self):
        """load_existing_master_contract() already checked is_ready itself - no need
        to re-query the database for the cache-load path."""
        from signal_engine.scripts.openalgoscheduler import _master_contract_load_confirmed

        confirmed, detail = _master_contract_load_confirmed(
            "flattrade", _must_not_be_called, True, should_download=False
        )
        assert confirmed is True
        assert "cache" in detail.lower()

    def test_download_confirmed_when_status_row_is_ready_with_symbols(self):
        from signal_engine.scripts.openalgoscheduler import _master_contract_load_confirmed

        get_status = MagicMock(return_value=_ready_status())
        confirmed, detail = _master_contract_load_confirmed(
            "flattrade", get_status, {"status": "ok"}, should_download=True
        )
        assert confirmed is True
        get_status.assert_called_once_with("flattrade")

    def test_stuck_downloading_status_is_not_confirmed(self):
        """Regression guard for the exact 2026-09-28 shape: the call returns without
        raising, but the killed background thread never got to update_status(...,
        "success", ...), so the row is still "downloading"."""
        from signal_engine.scripts.openalgoscheduler import _master_contract_load_confirmed

        get_status = MagicMock(
            return_value={"status": "downloading", "is_ready": False, "total_symbols": "0"}
        )
        confirmed, detail = _master_contract_load_confirmed(
            "flattrade", get_status, {"status": "ok"}, should_download=True
        )
        assert confirmed is False
        assert "downloading" in detail

    def test_success_status_but_zero_symbols_is_not_confirmed(self):
        """A partial download that silently skipped every exchange (download_csv_data()
        swallows per-exchange failures) could still land status="success" - the symbol
        count is the last line of defense."""
        from signal_engine.scripts.openalgoscheduler import _master_contract_load_confirmed

        get_status = MagicMock(return_value=_ready_status(total_symbols="0"))
        confirmed, detail = _master_contract_load_confirmed(
            "flattrade", get_status, {"status": "ok"}, should_download=True
        )
        assert confirmed is False

    def test_missing_status_row_is_not_confirmed(self):
        from signal_engine.scripts.openalgoscheduler import _master_contract_load_confirmed

        get_status = MagicMock(return_value=None)
        confirmed, detail = _master_contract_load_confirmed(
            "flattrade", get_status, {"status": "ok"}, should_download=True
        )
        assert confirmed is False

    def test_non_numeric_symbol_count_is_not_confirmed(self):
        from signal_engine.scripts.openalgoscheduler import _master_contract_load_confirmed

        get_status = MagicMock(return_value=_ready_status(total_symbols="not-a-number"))
        confirmed, detail = _master_contract_load_confirmed(
            "flattrade", get_status, {"status": "ok"}, should_download=True
        )
        assert confirmed is False


class TestStartMasterContractLoadSynchronous:
    """Regression guard for 2026-09-28: this must run synchronously, confirm the
    result against the database row, and report its real outcome - not fire a daemon
    Thread and return immediately. The old behavior let the download die silently when
    the short-lived CLI process exited right after starting it, leaving
    master_contract_status stuck at "downloading" for ~90 minutes while every
    /api/v1/funds and /api/v1/analyzer call 403'd.
    """

    def test_download_success_runs_inline_and_returns_true(self):
        from signal_engine.scripts.openalgoscheduler import _start_master_contract_load

        calls = []

        def download(broker):
            calls.append(broker)
            return {"status": "ok"}

        result = _start_master_contract_load(
            "flattrade", MagicMock(), MagicMock(return_value=(True, "stale")),
            download, MagicMock(),
            _get_master_contract_status=lambda broker: _ready_status(),
        )
        assert result is True
        assert calls == ["flattrade"], "download must run synchronously, exactly once"

    def test_download_failure_retries_once_then_succeeds(self):
        from signal_engine.scripts.openalgoscheduler import _start_master_contract_load

        attempts = []

        def download(broker):
            attempts.append(broker)
            if len(attempts) == 1:
                return {"status": "error", "message": "network blip"}
            return {"status": "ok"}

        result = _start_master_contract_load(
            "flattrade", MagicMock(), MagicMock(return_value=(True, "stale")),
            download, MagicMock(),
            _get_master_contract_status=lambda broker: _ready_status(),
        )
        assert result is True
        assert len(attempts) == 2, "a failed download must get exactly one retry"

    def test_download_failure_twice_returns_false(self):
        from signal_engine.scripts.openalgoscheduler import _start_master_contract_load

        attempts = []

        def download(broker):
            attempts.append(broker)
            return {"status": "error"}

        result = _start_master_contract_load(
            "flattrade", MagicMock(), MagicMock(return_value=(True, "stale")),
            download, MagicMock(),
            _get_master_contract_status=_must_not_be_called,
        )
        assert result is False
        assert len(attempts) == 2, "must give up after one retry, not loop forever"

    def test_reported_success_but_status_stuck_downloading_is_retried_then_fails(self):
        """Regression guard for the exact 2026-09-28 incident: the call itself never
        raises and never returns an explicit error, but the database row it was
        supposed to finish writing is stuck at "downloading" both times - this must
        not be reported as success."""
        from signal_engine.scripts.openalgoscheduler import _start_master_contract_load

        attempts = []

        def download(broker):
            attempts.append(broker)
            return {"status": "ok"}  # call "succeeds" - the DB row is what's actually stuck

        result = _start_master_contract_load(
            "flattrade", MagicMock(), MagicMock(return_value=(True, "stale")),
            download, MagicMock(),
            _get_master_contract_status=lambda broker: {
                "status": "downloading", "is_ready": False, "total_symbols": "0",
            },
        )
        assert result is False
        assert len(attempts) == 2, "a call that 'succeeds' but leaves the row unconfirmed must retry"

    def test_reported_success_but_zero_symbols_is_not_confirmed(self):
        from signal_engine.scripts.openalgoscheduler import _start_master_contract_load

        result = _start_master_contract_load(
            "flattrade", MagicMock(), MagicMock(return_value=(True, "stale")),
            lambda broker: {"status": "ok"}, MagicMock(),
            _get_master_contract_status=lambda broker: _ready_status(total_symbols="0"),
        )
        assert result is False

    def test_cached_load_failure_does_not_retry(self):
        """Retrying a cache-load failure can't succeed differently - no cached data
        means no cached data on the second try either."""
        from signal_engine.scripts.openalgoscheduler import _start_master_contract_load

        attempts = []

        def load_existing(broker):
            attempts.append(broker)
            return False

        result = _start_master_contract_load(
            "flattrade", MagicMock(), MagicMock(return_value=(False, "cached")),
            MagicMock(), load_existing,
            _get_master_contract_status=_must_not_be_called,
        )
        assert result is False
        assert len(attempts) == 1

    def test_force_download_skips_smart_check(self):
        """The self-heal path must always redownload, never fall back to the cached
        path a smart-download check might otherwise pick."""
        from signal_engine.scripts.openalgoscheduler import _start_master_contract_load

        should_download_fn = MagicMock(side_effect=AssertionError("must not be called"))
        download = MagicMock(return_value={"status": "ok"})
        load_existing = MagicMock(side_effect=AssertionError("must not be called"))

        result = _start_master_contract_load(
            "flattrade", MagicMock(), should_download_fn,
            download, load_existing, force_download=True,
            _get_master_contract_status=lambda broker: _ready_status(),
        )
        assert result is True
        download.assert_called_once_with("flattrade")

    def test_exception_from_target_is_caught_and_counted_as_failure(self):
        from signal_engine.scripts.openalgoscheduler import _start_master_contract_load

        def download(broker):
            raise RuntimeError("boom")

        result = _start_master_contract_load(
            "flattrade", MagicMock(), MagicMock(return_value=(True, "stale")),
            download, MagicMock(),
            _get_master_contract_status=_must_not_be_called,
        )
        assert result is False


class TestVerifyOpenAlgoApi:
    """_verify_openalgo_api retries with backoff before giving up."""

    def test_succeeds_first_try_no_delay_needed(self, monkeypatch):
        import signal_engine.scripts.openalgoscheduler as sched

        calls = []

        async def _smoke():
            calls.append(1)
            return True, "OK"

        monkeypatch.setattr(sched, "_openalgo_api_smoke_test", _smoke)
        ok, detail = sched._verify_openalgo_api(max_attempts=3, retry_delay=0)
        assert ok is True
        assert detail == "OK"
        assert len(calls) == 1

    def test_recovers_on_a_later_attempt(self, monkeypatch):
        import signal_engine.scripts.openalgoscheduler as sched

        calls = []

        async def _smoke():
            calls.append(1)
            if len(calls) < 3:
                return False, "403"
            return True, "recovered"

        monkeypatch.setattr(sched, "_openalgo_api_smoke_test", _smoke)
        ok, detail = sched._verify_openalgo_api(max_attempts=3, retry_delay=0)
        assert ok is True
        assert detail == "recovered"
        assert len(calls) == 3

    def test_gives_up_after_max_attempts(self, monkeypatch):
        import signal_engine.scripts.openalgoscheduler as sched

        calls = []

        async def _smoke():
            calls.append(1)
            return False, "still 403"

        monkeypatch.setattr(sched, "_openalgo_api_smoke_test", _smoke)
        ok, detail = sched._verify_openalgo_api(max_attempts=3, retry_delay=0)
        assert ok is False
        assert detail == "still 403"
        assert len(calls) == 3


class TestEnsureOpenAlgoApiUsable:
    """_ensure_openalgo_api_usable: self-heal once, alert on the outcome either way."""

    def test_passes_immediately_without_self_heal(self, monkeypatch):
        import signal_engine.scripts.openalgoscheduler as sched

        heal_calls = []
        monkeypatch.setattr(sched, "_verify_openalgo_api", lambda **kw: (True, "OK"))
        monkeypatch.setattr(sched, "_self_heal_master_contract", lambda: heal_calls.append(1))

        assert sched._ensure_openalgo_api_usable("startup") is True
        assert heal_calls == [], "must not self-heal when the first check already passed"

    def test_self_heals_and_recovers(self, monkeypatch):
        import signal_engine.scripts.openalgoscheduler as sched

        calls = {"verify": 0}
        recovered_msgs = []

        def _verify(**kw):
            calls["verify"] += 1
            if calls["verify"] == 1:
                return False, "403 forbidden"
            return True, "recovered"

        async def _send(msg):
            recovered_msgs.append(msg)
            return True

        monkeypatch.setattr(sched, "_verify_openalgo_api", _verify)
        monkeypatch.setattr(sched, "_self_heal_master_contract", lambda: True)
        monkeypatch.setattr(sched, "send_telegram_notification", _send)

        assert sched._ensure_openalgo_api_usable("healthcheck") is True
        assert calls["verify"] == 2
        assert len(recovered_msgs) == 1
        assert "recovered" in recovered_msgs[0].lower()

    def test_alerts_when_self_heal_does_not_recover(self, monkeypatch):
        import signal_engine.scripts.openalgoscheduler as sched

        fail_calls = []
        monkeypatch.setattr(sched, "_verify_openalgo_api", lambda **kw: (False, "still broken"))
        monkeypatch.setattr(sched, "_self_heal_master_contract", lambda: False)
        monkeypatch.setattr(
            sched, "notify_failure",
            lambda stage, detail: fail_calls.append((stage, detail)),
        )

        assert sched._ensure_openalgo_api_usable("startup") is False
        assert fail_calls, "must alert when self-heal does not recover the API"
        assert fail_calls[0][0] == "startup-api-smoke-test"
        assert "still broken" in fail_calls[0][1]


@pytest.fixture(autouse=True)
def _no_real_session_registration(monkeypatch):
    """auto_login tests must never write an active_sessions row to the real database."""
    import signal_engine.scripts.openalgoscheduler as sched

    monkeypatch.setattr(sched, "_register_trading_session", MagicMock(return_value=True))


class TestRegisterTradingSession:
    """Guard against 2026-09-29: a CLI login left no post-03:00 active_sessions row, so a
    stale browser cookie revoked the fresh broker token."""

    def _real(self):
        import importlib

        import signal_engine.scripts.openalgoscheduler as sched

        # the autouse fixture replaced the attribute; reload gives the real function
        return importlib.reload(sched)._register_trading_session

    def test_fresh_login_always_registers(self):
        fn = self._real()
        reg = MagicMock()
        assert fn("u", "flattrade", True, _register=reg) is True
        args, kwargs = reg.call_args
        assert args[0] == "u" and kwargs["broker"] == "flattrade"
        assert kwargs["ip_address"] == "signal_engine-auto-login"

    def test_reused_session_registers_only_when_no_fresh_row(self):
        fn = self._real()
        reg = MagicMock()
        assert fn("u", "b", False, _register=reg, _has_fresh=lambda u: True) is False
        reg.assert_not_called()
        assert fn("u", "b", False, _register=reg, _has_fresh=lambda u: False) is True
        reg.assert_called_once()

    def test_failure_is_swallowed(self):
        fn = self._real()
        assert fn("u", "b", True, _register=MagicMock(side_effect=RuntimeError("db"))) is False


class TestDetachServerlessSocketio:
    """Broker master-contract modules crash on socketio.emit when the SocketIO object
    was never bound to an app (the scheduler CLI process). Detach only in that case."""

    def _fake_module(self, monkeypatch, sio):
        import sys
        import types

        mod = types.ModuleType("broker.fakebroker.database.master_contract_db")
        mod.socketio = sio
        monkeypatch.setitem(sys.modules, "broker.fakebroker.database.master_contract_db", mod)
        return mod

    def test_serverless_socketio_is_detached(self, monkeypatch):
        from signal_engine.scripts.openalgoscheduler import _detach_serverless_socketio

        mod = self._fake_module(monkeypatch, MagicMock(server=None))
        assert _detach_serverless_socketio("fakebroker") is True
        assert mod.socketio is None

    def test_live_socketio_is_left_alone(self, monkeypatch):
        from signal_engine.scripts.openalgoscheduler import _detach_serverless_socketio

        sio = MagicMock(server=object())
        mod = self._fake_module(monkeypatch, sio)
        assert _detach_serverless_socketio("fakebroker") is False
        assert mod.socketio is sio

    def test_missing_socketio_or_module_is_safe(self, monkeypatch):
        from signal_engine.scripts.openalgoscheduler import _detach_serverless_socketio

        self._fake_module(monkeypatch, None)
        assert _detach_serverless_socketio("fakebroker") is False
        assert _detach_serverless_socketio("no_such_broker") is False


class TestRevokeBrokerSessionAtStop:
    """The 4 PM scheduled stop must leave no token or session to carry into tomorrow."""

    def test_scheduled_stop_revokes_token_and_clears_sessions(self):
        from signal_engine.scripts.openalgoscheduler import _revoke_broker_session_at_stop

        upsert, clear = MagicMock(), MagicMock()
        user = MagicMock()
        user.username = "anand"
        assert _revoke_broker_session_at_stop(
            "scheduled", _upsert_auth=upsert,
            _find_user_by_username=lambda: user, _clear_sessions=clear,
        ) is True
        upsert.assert_called_once_with("anand", "", "", revoke=True)
        clear.assert_called_once_with("anand")

    def test_non_scheduled_stop_leaves_session_alone(self):
        from signal_engine.scripts.openalgoscheduler import _revoke_broker_session_at_stop

        upsert = MagicMock()
        assert _revoke_broker_session_at_stop(
            "manual", _upsert_auth=upsert,
            _find_user_by_username=lambda: MagicMock(), _clear_sessions=MagicMock(),
        ) is False
        upsert.assert_not_called()

    def test_no_user_or_db_error_is_swallowed(self):
        from signal_engine.scripts.openalgoscheduler import _revoke_broker_session_at_stop

        assert _revoke_broker_session_at_stop(
            "scheduled", _upsert_auth=MagicMock(),
            _find_user_by_username=lambda: None, _clear_sessions=MagicMock(),
        ) is False
        assert _revoke_broker_session_at_stop(
            "scheduled", _upsert_auth=MagicMock(side_effect=RuntimeError("db")),
            _find_user_by_username=lambda: MagicMock(username="u"), _clear_sessions=MagicMock(),
        ) is False
