"""Tests for config loader — fail-fast on missing or invalid values."""

import os
import textwrap

import pytest
import yaml

from signal_engine.config import _build_settings, _load_yaml, ConfigError


class TestConfigFileMissing:
    def test_raises_when_config_yaml_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("signal_engine.config._YAML_PATH", str(tmp_path / "nope.yaml"))
        monkeypatch.setattr("signal_engine.config._ENV_PATH", str(tmp_path / ".env"))
        with pytest.raises(ConfigError, match="config.yaml not found"):
            _build_settings()


class TestRequiredYamlSections:
    """Every top-level section must exist in config.yaml."""

    def _write_yaml(self, tmp_path, monkeypatch, content: dict):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(yaml.dump(content))
        monkeypatch.setattr("signal_engine.config._YAML_PATH", str(yaml_path))
        monkeypatch.setattr("signal_engine.config._ENV_PATH", str(tmp_path / ".env"))

    def test_missing_sizing_section(self, tmp_path, monkeypatch):
        self._write_yaml(tmp_path, monkeypatch, {
            "telegram": {"channels": []},
            "risk": {"daily_loss_limit": 0.03, "weekly_loss_limit": 0.06,
                     "monthly_loss_limit": 0.10, "max_portfolio_heat": 0.06,
                     "max_open_positions": 3,
                     "max_trades_per_day": 5, "min_rr": 1.0,
                     "duplicate_window_seconds": 60, "stale_signal_seconds": 60,
                     "min_sl_pct": 0.003},
            "tracking": {"poll_interval": 30},
            "broker": {"exchange": "NSE", "product": "MIS", "order_type": "MARKET"},
            "listener": {"max_retries": 5, "base_backoff": 2},
            "api": {"timeout": 5.0},
        })
        with pytest.raises(ConfigError, match="sizing"):
            _build_settings()

    def test_missing_risk_section(self, tmp_path, monkeypatch):
        self._write_yaml(tmp_path, monkeypatch, {
            "telegram": {"channels": []},
            "sizing": {"mode": "fixed_fractional", "risk_per_trade": 0.01,
                       "pct_of_capital": 0.05, "max_position_size": 0,
                       "min_entry_price": 0, "max_entry_price": 0,
                       "slippage_factor": 0.0, "sandbox_capital": 0},
            "tracking": {"poll_interval": 30},
            "broker": {"exchange": "NSE", "product": "MIS", "order_type": "MARKET"},
            "listener": {"max_retries": 5, "base_backoff": 2},
            "api": {"timeout": 5.0},
        })
        with pytest.raises(ConfigError, match="risk"):
            _build_settings()

    def test_missing_broker_section(self, tmp_path, monkeypatch):
        self._write_yaml(tmp_path, monkeypatch, {
            "telegram": {"channels": []},
            "sizing": {"mode": "fixed_fractional", "risk_per_trade": 0.01,
                       "pct_of_capital": 0.05, "max_position_size": 0,
                       "min_entry_price": 0, "max_entry_price": 0,
                       "slippage_factor": 0.0, "sandbox_capital": 0},
            "risk": {"daily_loss_limit": 0.03, "weekly_loss_limit": 0.06,
                     "monthly_loss_limit": 0.10, "max_portfolio_heat": 0.06,
                     "max_open_positions": 3,
                     "max_trades_per_day": 5, "min_rr": 1.0,
                     "duplicate_window_seconds": 60, "stale_signal_seconds": 60,
                     "min_sl_pct": 0.003},
            "tracking": {"poll_interval": 30},
            "listener": {"max_retries": 5, "base_backoff": 2},
            "api": {"timeout": 5.0},
        })
        with pytest.raises(ConfigError, match="broker"):
            _build_settings()


class TestRequiredYamlKeys:
    """Individual keys within sections must be present."""

    def _full_config(self) -> dict:
        return {
            "telegram": {"channels": []},
            "sizing": {
                "mode": "fixed_fractional",
                "risk_per_trade": 0.01,
                "pct_of_capital": 0.05,
                "max_position_size": 0,
                "min_entry_price": 0,
                "max_entry_price": 0,
                "slippage_factor": 0.0,
                "sandbox_capital": 0,
            },
            "risk": {
                "daily_loss_limit": 0.03,
                "weekly_loss_limit": 0.06,
                "monthly_loss_limit": 0.10,
                "max_portfolio_heat": 0.06,
                "max_open_positions": 3,
                "max_trades_per_day": 5,
                "min_rr": 1.0,
                "duplicate_window_seconds": 60,
                "stale_signal_seconds": 60,
                "min_sl_pct": 0.003,
            },
            "tracking": {"poll_interval": 30},
            "broker": {"exchange": "NSE", "product": "MIS", "order_type": "MARKET"},
            "listener": {"max_retries": 5, "base_backoff": 2},
            "api": {"timeout": 5.0},
        }

    def _write_yaml(self, tmp_path, monkeypatch, content: dict):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(yaml.dump(content))
        monkeypatch.setattr("signal_engine.config._YAML_PATH", str(yaml_path))
        monkeypatch.setattr("signal_engine.config._ENV_PATH", str(tmp_path / ".env"))

    def test_missing_sizing_mode(self, tmp_path, monkeypatch):
        cfg = self._full_config()
        del cfg["sizing"]["mode"]
        self._write_yaml(tmp_path, monkeypatch, cfg)
        with pytest.raises(ConfigError, match="sizing.mode"):
            _build_settings()

    def test_missing_risk_per_trade(self, tmp_path, monkeypatch):
        cfg = self._full_config()
        del cfg["sizing"]["risk_per_trade"]
        self._write_yaml(tmp_path, monkeypatch, cfg)
        with pytest.raises(ConfigError, match="sizing.risk_per_trade"):
            _build_settings()

    def test_missing_daily_loss_limit(self, tmp_path, monkeypatch):
        cfg = self._full_config()
        del cfg["risk"]["daily_loss_limit"]
        self._write_yaml(tmp_path, monkeypatch, cfg)
        with pytest.raises(ConfigError, match="risk.daily_loss_limit"):
            _build_settings()

    def test_missing_exchange(self, tmp_path, monkeypatch):
        cfg = self._full_config()
        del cfg["broker"]["exchange"]
        self._write_yaml(tmp_path, monkeypatch, cfg)
        with pytest.raises(ConfigError, match="broker.exchange"):
            _build_settings()


class TestInvalidSizingMode:
    """sizing_mode must be fixed_fractional or pct_of_capital."""

    def _full_config(self) -> dict:
        return {
            "telegram": {"channels": []},
            "sizing": {
                "mode": "fixed_fractional",
                "risk_per_trade": 0.01,
                "pct_of_capital": 0.05,
                "max_position_size": 0,
                "min_entry_price": 0,
                "max_entry_price": 0,
                "slippage_factor": 0.0,
                "sandbox_capital": 0,
            },
            "risk": {
                "daily_loss_limit": 0.03,
                "weekly_loss_limit": 0.06,
                "monthly_loss_limit": 0.10,
                "max_portfolio_heat": 0.06,
                "max_open_positions": 3,
                "max_trades_per_day": 5,
                "min_rr": 1.0,
                "duplicate_window_seconds": 60,
                "stale_signal_seconds": 60,
                "min_sl_pct": 0.003,
            },
            "tracking": {"poll_interval": 30},
            "broker": {"exchange": "NSE", "product": "MIS", "order_type": "MARKET"},
            "listener": {"max_retries": 5, "base_backoff": 2},
            "api": {"timeout": 5.0},
        }

    def _write_yaml(self, tmp_path, monkeypatch, content: dict):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(yaml.dump(content))
        monkeypatch.setattr("signal_engine.config._YAML_PATH", str(yaml_path))
        monkeypatch.setattr("signal_engine.config._ENV_PATH", str(tmp_path / ".env"))

    def test_invalid_sizing_mode_rejected(self, tmp_path, monkeypatch):
        cfg = self._full_config()
        cfg["sizing"]["mode"] = "yolo"
        self._write_yaml(tmp_path, monkeypatch, cfg)
        with pytest.raises(ConfigError, match="sizing.mode"):
            _build_settings()

    def test_valid_modes_accepted(self, tmp_path, monkeypatch):
        for mode in ("fixed_fractional", "pct_of_capital"):
            cfg = self._full_config()
            cfg["sizing"]["mode"] = mode
            self._write_yaml(tmp_path, monkeypatch, cfg)
            settings = _build_settings()
            assert settings.sizing_mode == mode


class TestRiskEngineRejectsUnknownMode:
    """RiskEngine must fail fast on unknown sizing mode, not silently default to qty=1."""

    def test_unknown_mode_raises(self):
        from signal_engine.risk import RiskEngine
        from signal_engine.tests.conftest import make_signal

        engine = RiskEngine(
            risk_per_trade=0.01,
            sizing_mode="invalid_mode",
            pct_of_capital=0.05,
            max_position_size=0,
            daily_loss_limit=0.03,
            weekly_loss_limit=0.06,
            monthly_loss_limit=0.10,
            max_open_positions=3,
            max_trades_per_day=5,
            min_entry_price=0,
            max_entry_price=0,
            max_portfolio_heat=0.06,
        )
        with pytest.raises(ValueError, match="Unknown sizing mode"):
            engine.calculate_quantity(make_signal(), capital=100_000)


class TestValidConfigLoadsSuccessfully:
    """A complete, valid config.yaml must load without errors."""

    def _full_config(self) -> dict:
        return {
            "telegram": {"channels": [{"name": "test", "id": -123}]},
            "sizing": {
                "mode": "fixed_fractional",
                "risk_per_trade": 0.01,
                "pct_of_capital": 0.05,
                "max_position_size": 0,
                "min_entry_price": 50,
                "max_entry_price": 1500,
                "slippage_factor": 0.0,
                "sandbox_capital": 10000,
            },
            "risk": {
                "daily_loss_limit": 0.03,
                "weekly_loss_limit": 0.06,
                "monthly_loss_limit": 0.10,
                "max_portfolio_heat": 0.06,
                "max_open_positions": 3,
                "max_trades_per_day": 5,
                "min_rr": 1.0,
                "duplicate_window_seconds": 60,
                "stale_signal_seconds": 60,
                "min_sl_pct": 0.003,
            },
            "tracking": {"poll_interval": 30},
            "broker": {"exchange": "NSE", "product": "MIS", "order_type": "MARKET"},
            "listener": {"max_retries": 5, "base_backoff": 2},
            "api": {"timeout": 5.0},
        }

    def test_all_values_loaded_from_yaml(self, tmp_path, monkeypatch):
        cfg = self._full_config()
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(yaml.dump(cfg))
        monkeypatch.setattr("signal_engine.config._YAML_PATH", str(yaml_path))
        monkeypatch.setattr("signal_engine.config._ENV_PATH", str(tmp_path / ".env"))

        s = _build_settings()
        assert s.sizing_mode == "fixed_fractional"
        assert s.risk_per_trade == 0.01
        assert s.pct_of_capital == 0.05
        assert s.max_position_size == 0
        assert s.min_entry_price == 50
        assert s.max_entry_price == 1500
        assert s.daily_loss_limit == 0.03
        assert s.max_portfolio_heat == 0.06
        assert s.max_open_positions == 3
        assert s.exchange == "NSE"
        assert s.product == "MIS"
        assert s.poll_interval == 30
        assert s.sandbox_capital == 10000
