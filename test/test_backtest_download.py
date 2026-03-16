"""Tests for batch data download script."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from backtest.download_data import (
    check_data_availability,
    get_missing_symbols,
)


class TestCheckDataAvailability:
    """Test data availability checking (no Flask needed)."""

    @patch("backtest.download_data.get_data_catalog")
    def test_returns_available_symbols(self, mock_catalog):
        mock_catalog.return_value = [
            {"symbol": "SBIN", "exchange": "NSE", "interval": "1m"},
            {"symbol": "PNB", "exchange": "NSE", "interval": "1m"},
        ]
        symbols = [
            {"symbol": "SBIN", "exchange": "NSE"},
            {"symbol": "PNB", "exchange": "NSE"},
            {"symbol": "CANBK", "exchange": "NSE"},
        ]
        available, missing = check_data_availability(symbols, "1m")
        assert len(available) == 2
        assert len(missing) == 1
        assert missing[0]["symbol"] == "CANBK"

    @patch("backtest.download_data.get_data_catalog")
    def test_all_missing(self, mock_catalog):
        mock_catalog.return_value = []
        symbols = [
            {"symbol": "SBIN", "exchange": "NSE"},
            {"symbol": "PNB", "exchange": "NSE"},
        ]
        available, missing = check_data_availability(symbols, "1m")
        assert len(available) == 0
        assert len(missing) == 2

    @patch("backtest.download_data.get_data_catalog")
    def test_all_available(self, mock_catalog):
        mock_catalog.return_value = [
            {"symbol": "SBIN", "exchange": "NSE", "interval": "1m"},
            {"symbol": "PNB", "exchange": "NSE", "interval": "1m"},
        ]
        symbols = [
            {"symbol": "SBIN", "exchange": "NSE"},
            {"symbol": "PNB", "exchange": "NSE"},
        ]
        available, missing = check_data_availability(symbols, "1m")
        assert len(available) == 2
        assert len(missing) == 0

    @patch("backtest.download_data.get_data_catalog")
    def test_interval_mismatch_counts_as_missing(self, mock_catalog):
        """Symbol with daily data but missing 1m is still missing."""
        mock_catalog.return_value = [
            {"symbol": "SBIN", "exchange": "NSE", "interval": "D"},
        ]
        symbols = [{"symbol": "SBIN", "exchange": "NSE"}]
        available, missing = check_data_availability(symbols, "1m")
        assert len(available) == 0
        assert len(missing) == 1


class TestGetMissingSymbols:
    """Test missing symbol detection from batch config."""

    @patch("backtest.download_data.get_data_catalog")
    def test_returns_symbols_needing_download(self, mock_catalog):
        mock_catalog.return_value = [
            {"symbol": "SBIN", "exchange": "NSE", "interval": "1m"},
        ]
        symbols = [
            {"symbol": "SBIN", "exchange": "NSE"},
            {"symbol": "PNB", "exchange": "NSE"},
            {"symbol": "CANBK", "exchange": "NSE"},
        ]
        missing = get_missing_symbols(symbols, "1m")
        assert len(missing) == 2
        assert {"symbol": "PNB", "exchange": "NSE"} in missing
        assert {"symbol": "CANBK", "exchange": "NSE"} in missing

    @patch("backtest.download_data.get_data_catalog")
    def test_empty_when_all_present(self, mock_catalog):
        mock_catalog.return_value = [
            {"symbol": "SBIN", "exchange": "NSE", "interval": "1m"},
        ]
        symbols = [{"symbol": "SBIN", "exchange": "NSE"}]
        missing = get_missing_symbols(symbols, "1m")
        assert len(missing) == 0
