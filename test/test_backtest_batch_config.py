"""Tests for batch backtest configuration loader."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import yaml

from backtest.config import ConfigError, load_batch_config


@pytest.fixture
def valid_batch_config_file():
    """Create a temporary valid batch config file."""
    config = {
        "symbols": [
            {"symbol": "SBIN", "exchange": "NSE"},
            {"symbol": "PNB", "exchange": "NSE"},
            {"symbol": "CANBK", "exchange": "NSE"},
        ],
        "interval": "5m",
        "start_date": "2025-01-01",
        "end_date": "2026-03-01",
        "initial_capital": 100000,
        "position_size_pct": 0.10,
        "product": "MIS",
        "strategy": "orb",
        "orb": {
            "orb_minutes": 15,
            "tp_multiplier": 1.0,
            "stop_mode": "ATR",
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f)
        return f.name


@pytest.fixture
def shorthand_symbols_config_file():
    """Config with shorthand symbol list (strings, default exchange NSE)."""
    config = {
        "symbols": ["SBIN", "PNB", "CANBK"],
        "interval": "5m",
        "start_date": "2025-01-01",
        "end_date": "2026-03-01",
        "initial_capital": 100000,
        "strategy": "orb",
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f)
        return f.name


@pytest.fixture
def missing_symbols_config_file():
    """Config file missing symbols list."""
    config = {
        "interval": "5m",
        "start_date": "2025-01-01",
        "end_date": "2026-03-01",
        "initial_capital": 100000,
        "strategy": "orb",
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f)
        return f.name


@pytest.fixture
def empty_symbols_config_file():
    """Config file with empty symbols list."""
    config = {
        "symbols": [],
        "interval": "5m",
        "start_date": "2025-01-01",
        "end_date": "2026-03-01",
        "initial_capital": 100000,
        "strategy": "orb",
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f)
        return f.name


class TestLoadBatchConfig:
    def test_loads_valid_batch_config(self, valid_batch_config_file):
        config = load_batch_config(valid_batch_config_file)
        assert len(config["symbols"]) == 3
        assert config["symbols"][0] == {"symbol": "SBIN", "exchange": "NSE"}
        assert config["symbols"][1] == {"symbol": "PNB", "exchange": "NSE"}
        assert config["interval"] == "5m"
        assert config["initial_capital"] == 100000
        assert config["strategy"] == "orb"
        assert config["orb_config"].orb_minutes == 15
        assert config["orb_config"].tp_multiplier == 1.0

    def test_shorthand_symbols_expanded(self, shorthand_symbols_config_file):
        """Plain string symbols should be expanded to {symbol, exchange: NSE}."""
        config = load_batch_config(shorthand_symbols_config_file)
        assert len(config["symbols"]) == 3
        assert config["symbols"][0] == {"symbol": "SBIN", "exchange": "NSE"}
        assert config["symbols"][2] == {"symbol": "CANBK", "exchange": "NSE"}

    def test_missing_symbols_raises(self, missing_symbols_config_file):
        with pytest.raises(ConfigError, match="symbols"):
            load_batch_config(missing_symbols_config_file)

    def test_empty_symbols_raises(self, empty_symbols_config_file):
        with pytest.raises(ConfigError, match="empty"):
            load_batch_config(empty_symbols_config_file)

    def test_missing_file_raises(self):
        with pytest.raises(ConfigError, match="not found"):
            load_batch_config("/nonexistent/batch.yaml")

    def test_missing_strategy_defaults_to_orb(self):
        """If strategy key is missing, default to 'orb'."""
        config_data = {
            "symbols": ["SBIN"],
            "interval": "5m",
            "start_date": "2025-01-01",
            "end_date": "2026-03-01",
            "initial_capital": 100000,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            config = load_batch_config(f.name)
        assert config["strategy"] == "orb"

    def test_costs_loaded(self, valid_batch_config_file):
        config = load_batch_config(valid_batch_config_file)
        assert config["costs"] is not None

    def test_default_position_size(self):
        """Position size defaults to 0.10 if not specified."""
        config_data = {
            "symbols": ["SBIN"],
            "interval": "5m",
            "start_date": "2025-01-01",
            "end_date": "2026-03-01",
            "initial_capital": 100000,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            config = load_batch_config(f.name)
        assert config["position_size_pct"] == 0.10

    def test_mixed_symbol_formats(self):
        """Mix of string and dict symbols should work."""
        config_data = {
            "symbols": [
                "SBIN",
                {"symbol": "PNB", "exchange": "BSE"},
            ],
            "interval": "5m",
            "start_date": "2025-01-01",
            "end_date": "2026-03-01",
            "initial_capital": 100000,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            config = load_batch_config(f.name)
        assert config["symbols"][0] == {"symbol": "SBIN", "exchange": "NSE"}
        assert config["symbols"][1] == {"symbol": "PNB", "exchange": "BSE"}
