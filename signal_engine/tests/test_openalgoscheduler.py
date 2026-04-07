"""Tests for openalgoscheduler — broker-neutral startup/shutdown automation."""

import importlib
from unittest.mock import MagicMock, patch

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
