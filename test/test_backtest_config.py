"""Tests for backtest configuration loader."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import yaml

from backtest.config import ConfigError, load_config


@pytest.fixture
def valid_config_file():
    """Create a temporary valid config file."""
    config = {
        "symbol": "SBIN",
        "exchange": "NSE",
        "interval": "5m",
        "start_date": "2025-01-01",
        "end_date": "2025-06-01",
        "initial_capital": 100000,
        "position_size_pct": 0.10,
        "product": "MIS",
        "orb": {
            "orb_minutes": 15,
            "tp_multiplier": 2.0,
            "stop_mode": "ATR",
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f)
        return f.name


@pytest.fixture
def missing_key_config_file():
    """Config file missing required keys."""
    config = {"symbol": "SBIN"}  # missing exchange, interval, dates, etc.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f)
        return f.name


class TestLoadConfig:
    def test_loads_valid_config(self, valid_config_file):
        config = load_config(valid_config_file)
        assert config["symbol"] == "SBIN"
        assert config["exchange"] == "NSE"
        assert config["initial_capital"] == 100000
        assert config["orb_config"].orb_minutes == 15
        assert config["orb_config"].tp_multiplier == 2.0

    def test_missing_file_raises(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config("/nonexistent/config.yaml")

    def test_missing_key_raises(self, missing_key_config_file):
        with pytest.raises(ConfigError, match="Missing required key"):
            load_config(missing_key_config_file)

    def test_empty_file_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            f.flush()
            with pytest.raises(ConfigError, match="empty"):
                load_config(f.name)

    def test_default_values(self, valid_config_file):
        config = load_config(valid_config_file)
        assert config["product"] == "MIS"
        assert config["costs"] is not None

    def test_orb_config_override(self):
        config_data = {
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "interval": "5m",
            "start_date": "2025-01-01",
            "end_date": "2025-06-01",
            "initial_capital": 200000,
            "orb": {
                "orb_minutes": 30,
                "breakout_buffer_pct": 0.3,
                "enable_volume_filter": False,
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            config = load_config(f.name)

        assert config["orb_config"].orb_minutes == 30
        assert config["orb_config"].breakout_buffer_pct == 0.3
        assert config["orb_config"].enable_volume_filter is False
        # Defaults preserved for unset values
        assert config["orb_config"].tp_multiplier == 1.0
