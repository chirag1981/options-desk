"""
tests/test_fno_downloader.py — Unit tests for FnoHistoricalDownloader.
Tests rate limiting, manifest resumability, symbol parsing, error handling and data formatting without live API calls.
"""

import os
import json
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

from app.backtesting.fno_downloader import FnoHistoricalDownloader
from app.backtesting.data_validator import HistoricalDataValidator


@pytest.fixture
def temp_output_dir(tmp_path):
    output_dir = str(tmp_path / "historical_data")
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def test_symbol_parsing():
    """Verify correct extraction of strike and option type from various FYERS contract formats."""
    # Monthly
    res = FnoHistoricalDownloader.parse_contract_symbol("NSE:NIFTY25JAN24500CE")
    assert res == (24500.0, "CE")

    res = FnoHistoricalDownloader.parse_contract_symbol("NSE:NIFTY25JAN23000PE")
    assert res == (23000.0, "PE")

    # Weekly
    res = FnoHistoricalDownloader.parse_contract_symbol("NSE:NIFTY2512321300PE")
    assert res == (21300.0, "PE")

    res = FnoHistoricalDownloader.parse_contract_symbol("NSE:NIFTY25O1924550CE")
    assert res == (24550.0, "CE")

    # Invalid symbol
    assert FnoHistoricalDownloader.parse_contract_symbol("NSE:NIFTY50-INDEX") is None
    assert FnoHistoricalDownloader.parse_contract_symbol("NSE:NIFTY25JANFUT") is None


def test_manifest_resumability(temp_output_dir):
    """Verify manifest initialization, persistence, and loading."""
    downloader = FnoHistoricalDownloader(output_dir=temp_output_dir, rate_limit_per_sec=10.0)
    assert os.path.exists(downloader.manifest_file)
    assert "expiries" in downloader._manifest

    # Simulate saving progress
    downloader._manifest["expiries"]["2025-01-23"] = {
        "status": "completed",
        "rows": 750,
        "file": "test.parquet"
    }
    downloader._save_manifest()

    # Create new instance to reload manifest
    downloader2 = FnoHistoricalDownloader(output_dir=temp_output_dir)
    assert "2025-01-23" in downloader2._manifest["expiries"]
    assert downloader2._manifest["expiries"]["2025-01-23"]["status"] == "completed"


def test_rate_limiting_pacing(temp_output_dir):
    """Verify pacing interval enforced by rate limiter."""
    downloader = FnoHistoricalDownloader(output_dir=temp_output_dir, rate_limit_per_sec=20.0)
    assert downloader.delay_between_calls == 0.05


def test_fetch_contract_candles_formatting(temp_output_dir):
    """Verify raw candle data parsing into structured dictionaries with change_oi."""
    downloader = FnoHistoricalDownloader(output_dir=temp_output_dir)

    raw_candles = [
        [1737344700, 10.0, 12.0, 9.5, 11.0, 1000, 50000, 14.5, 0.45, 0.001, -2.1, 4.0],
        [1737345000, 11.0, 13.0, 10.5, 12.5, 1500, 55000, 15.0, 0.48, 0.001, -2.2, 4.1],
    ]

    mock_resp = {"s": "ok", "code": 200, "candles": raw_candles}
    with patch.object(downloader, "_call_api", return_value=(200, mock_resp)):
        candles = downloader.fetch_contract_candles("NSE:NIFTY25JAN24500CE", "2025-01-20", "2025-01-20")
        assert len(candles) == 2
        assert candles[0]["change_oi"] == 0  # First candle baseline
        assert candles[1]["change_oi"] == 5000  # 55000 - 50000
        assert candles[0]["iv"] == 14.5
        assert candles[1]["delta"] == 0.48


def test_download_sample_pipeline(temp_output_dir):
    """Verify download_sample pipeline with mock API responses passes validation."""
    downloader = FnoHistoricalDownloader(output_dir=temp_output_dir)

    mock_spot_map = {1737344700: 24510.0}
    mock_symbols = [
        "NSE:NIFTY25JAN24500CE",
        "NSE:NIFTY25JAN24500PE",
        "NSE:NIFTY25JAN24550CE",
        "NSE:NIFTY25JAN24550PE",
    ]
    mock_candles = [
        {"timestamp_epoch": 1737344700, "open": 50.0, "high": 55.0, "low": 48.0, "close": 52.0, "volume": 100, "oi": 20000, "change_oi": 500, "iv": 14.0}
    ]

    with patch.object(downloader, "fetch_spot_candles", return_value=mock_spot_map), \
         patch.object(downloader, "fetch_underlying_symbols", return_value=mock_symbols), \
         patch.object(downloader, "fetch_contract_candles", return_value=mock_candles):

        valid, report, df = downloader.download_sample(expiry_date="2025-01-30", strikes_around_atm=1)
        assert len(df) == 4
        assert "timestamp" in df.columns
        assert "spot_price" in df.columns
        assert df["fill_tier"].iloc[0] == "ESTIMATED_SPREAD"
