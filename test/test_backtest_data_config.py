"""Tests for shared data download configuration."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from backtest.config import load_data_config, ConfigError


class TestLoadDataConfig:
    def test_loads_valid_config(self, tmp_path):
        config_file = tmp_path / "data.yaml"
        config_file.write_text("""
start_date: "2021-01-01"
end_date: "2026-03-16"
interval: 1m

symbols:
  orb:
    exchange: NSE
    list:
      - SBIN
      - PNB
      - CANBK
""")
        config = load_data_config(str(config_file))
        assert config["start_date"] == "2021-01-01"
        assert config["end_date"] == "2026-03-16"
        assert config["interval"] == "1m"
        assert len(config["symbols"]) == 3
        assert {"symbol": "SBIN", "exchange": "NSE"} in config["symbols"]

    def test_deduplicates_across_pools(self, tmp_path):
        config_file = tmp_path / "data.yaml"
        config_file.write_text("""
start_date: "2021-01-01"
end_date: "2026-03-16"
interval: 1m

symbols:
  orb:
    exchange: NSE
    list:
      - SBIN
      - PNB
  momentum:
    exchange: NSE
    list:
      - SBIN
      - RELIANCE
""")
        config = load_data_config(str(config_file))
        # SBIN appears in both pools but should be deduplicated
        assert len(config["symbols"]) == 3
        sbin_count = sum(1 for s in config["symbols"] if s["symbol"] == "SBIN")
        assert sbin_count == 1

    def test_preserves_pool_info(self, tmp_path):
        config_file = tmp_path / "data.yaml"
        config_file.write_text("""
start_date: "2021-01-01"
end_date: "2026-03-16"
interval: 1m

symbols:
  orb:
    exchange: NSE
    list:
      - SBIN
  momentum:
    exchange: NSE
    list:
      - RELIANCE
""")
        config = load_data_config(str(config_file))
        assert "pools" in config
        assert "orb" in config["pools"]
        assert "momentum" in config["pools"]
        assert {"symbol": "SBIN", "exchange": "NSE"} in config["pools"]["orb"]

    def test_filters_by_pool(self, tmp_path):
        config_file = tmp_path / "data.yaml"
        config_file.write_text("""
start_date: "2021-01-01"
end_date: "2026-03-16"
interval: 1m

symbols:
  orb:
    exchange: NSE
    list:
      - SBIN
      - PNB
  momentum:
    exchange: NSE
    list:
      - RELIANCE
""")
        config = load_data_config(str(config_file), pool="orb")
        assert len(config["symbols"]) == 2
        symbols = [s["symbol"] for s in config["symbols"]]
        assert "SBIN" in symbols
        assert "PNB" in symbols
        assert "RELIANCE" not in symbols

    def test_invalid_pool_raises(self, tmp_path):
        config_file = tmp_path / "data.yaml"
        config_file.write_text("""
start_date: "2021-01-01"
end_date: "2026-03-16"
interval: 1m

symbols:
  orb:
    exchange: NSE
    list:
      - SBIN
""")
        with pytest.raises(ConfigError, match="Unknown pool"):
            load_data_config(str(config_file), pool="nonexistent")

    def test_missing_file_raises(self):
        with pytest.raises(ConfigError):
            load_data_config("/tmp/nonexistent_data.yaml")

    def test_missing_required_keys_raises(self, tmp_path):
        config_file = tmp_path / "data.yaml"
        config_file.write_text("""
interval: 1m
symbols:
  orb:
    exchange: NSE
    list:
      - SBIN
""")
        with pytest.raises(ConfigError, match="start_date"):
            load_data_config(str(config_file))

    def test_mixed_exchanges(self, tmp_path):
        config_file = tmp_path / "data.yaml"
        config_file.write_text("""
start_date: "2021-01-01"
end_date: "2026-03-16"
interval: 1m

symbols:
  nse_stocks:
    exchange: NSE
    list:
      - SBIN
  bse_stocks:
    exchange: BSE
    list:
      - SBIN
""")
        config = load_data_config(str(config_file))
        # Same symbol on different exchanges = NOT deduplicated
        assert len(config["symbols"]) == 2

    def test_indices_section(self, tmp_path):
        config_file = tmp_path / "data.yaml"
        config_file.write_text("""
start_date: "2021-01-01"
end_date: "2026-03-16"
interval: 1m

symbols:
  orb:
    exchange: NSE
    list:
      - SBIN

indices:
  - symbol: NIFTY 50
    exchange: NSE
""")
        config = load_data_config(str(config_file))
        assert len(config["indices"]) == 1
        assert config["indices"][0]["symbol"] == "NIFTY 50"


class TestEstimateDownloadTime:
    def test_returns_positive(self):
        from backtest.download_data import estimate_download_time
        minutes = estimate_download_time(
            num_symbols=25, start_date="2021-01-01", end_date="2026-03-16",
        )
        assert minutes > 0

    def test_more_symbols_takes_longer(self):
        from backtest.download_data import estimate_download_time
        t1 = estimate_download_time(10, "2021-01-01", "2026-01-01")
        t2 = estimate_download_time(25, "2021-01-01", "2026-01-01")
        assert t2 > t1

    def test_longer_period_takes_longer(self):
        from backtest.download_data import estimate_download_time
        t1 = estimate_download_time(25, "2025-01-01", "2026-01-01")
        t2 = estimate_download_time(25, "2021-01-01", "2026-01-01")
        assert t2 > t1
