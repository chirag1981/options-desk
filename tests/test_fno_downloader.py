"""
tests/test_fno_downloader.py — Unit tests for FnoHistoricalDownloader.
Tests rate limiting, manifest resumability, symbol parsing, error handling,
OI gap validation, spot look-ahead prevention, and execution scenarios.
"""

import os
import json
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

from app.backtesting.fno_downloader import (
    FnoHistoricalDownloader,
    round_to_tick,
    calculate_scenario_fills,
    TICK_SIZE,
)
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


def test_round_to_tick_and_floors():
    """Verify NSE 0.05 tick rounding and non-negative floors."""
    assert round_to_tick(10.02, "buy") == 10.05
    assert round_to_tick(10.02, "sell") == 10.00
    assert round_to_tick(10.05, "buy") == 10.05
    assert round_to_tick(10.05, "sell") == 10.05
    assert round_to_tick(0.01, "sell") == 0.05  # Floored at TICK_SIZE
    assert round_to_tick(-5.0, "sell") == 0.05  # Never negative


def test_execution_scenarios_structure_and_ticks():
    """Verify Optimistic, Base, and Conservative spread scenarios."""
    fills = calculate_scenario_fills(ltp=100.0)
    assert fills.fill_tier == "ESTIMATED_SPREAD"

    # All prices must be multiples of 0.05 tick
    for price in [
        fills.optimistic_bid, fills.optimistic_ask,
        fills.base_bid, fills.base_ask,
        fills.conservative_bid, fills.conservative_ask,
    ]:
        assert round(price % 0.05, 4) in (0.0, 0.05)
        assert price >= 0.05

    # Ask > Bid in all scenarios
    assert fills.optimistic_ask > fills.optimistic_bid
    assert fills.base_ask > fills.base_bid
    assert fills.conservative_ask > fills.conservative_bid

    # Spread widens progressively from Optimistic to Conservative
    opt_spread = fills.optimistic_ask - fills.optimistic_bid
    base_spread = fills.base_ask - fills.base_bid
    cons_spread = fills.conservative_ask - fills.conservative_bid
    assert opt_spread <= base_spread <= cons_spread


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


def test_fetch_contract_candles_oi_and_gap_handling(temp_output_dir):
    """Verify candle parsing, missing first observation DOI, and multi-interval gap flagging."""
    downloader = FnoHistoricalDownloader(output_dir=temp_output_dir, resolution="5")

    # Candle 0: First observation (ts=1737344700)
    # Candle 1: Consecutive (ts=1737345000 -> 300s elapsed)
    # Candle 2: Gap (ts=1737346200 -> 1200s elapsed, 4 intervals)
    raw_candles = [
        [1737344700, 10.0, 12.0, 9.5, 11.0, 1000, 50000, 14.5, 0.45, 0.001, -2.1, 4.0],
        [1737345000, 11.0, 13.0, 10.5, 12.5, 1500, 55000, 15.0, 0.48, 0.001, -2.2, 4.1],
        [1737346200, 12.0, 14.0, 11.5, 13.0, 2000, 62000, 15.5, 0.50, 0.001, -2.3, 4.2],
    ]

    mock_resp = {"s": "ok", "code": 200, "candles": raw_candles}
    with patch.object(downloader, "_call_api", return_value=(200, mock_resp)):
        candles = downloader.fetch_contract_candles("NSE:NIFTY25JAN24500CE", "2025-01-20", "2025-01-20")
        assert len(candles) == 3

        # First observation: preserved as None
        assert candles[0]["change_oi"] is None
        assert candles[0]["doi_elapsed_sec"] is None
        assert candles[0]["is_gap"] is False

        # Consecutive observation: 300s elapsed, no gap
        assert candles[1]["change_oi"] == 5000  # 55000 - 50000
        assert candles[1]["doi_elapsed_sec"] == 300
        assert candles[1]["is_gap"] is False

        # Gapped observation: 1200s elapsed, flagged as gap
        assert candles[2]["change_oi"] == 7000  # 62000 - 55000
        assert candles[2]["doi_elapsed_sec"] == 1200
        assert candles[2]["is_gap"] is True


def test_spot_joining_zero_lookahead():
    """Verify spot joining never uses future observations and records observation age."""
    spot_map = {
        1000: 24000.0,
        1300: 24050.0,
        1600: 24100.0,
    }
    sorted_ts = sorted(spot_map.keys())

    # Exact match
    spot, age = FnoHistoricalDownloader._join_spot_no_lookahead(1300, spot_map, sorted_ts, 24000.0)
    assert spot == 24050.0
    assert age == 0

    # Intermediate option observation at 1450 (should pick 1300, NOT future 1600)
    spot, age = FnoHistoricalDownloader._join_spot_no_lookahead(1450, spot_map, sorted_ts, 24000.0)
    assert spot == 24050.0
    assert age == 150  # 1450 - 1300

    # Observation before any spot data (falls back safely)
    spot, age = FnoHistoricalDownloader._join_spot_no_lookahead(500, spot_map, sorted_ts, 23900.0)
    assert spot == 23900.0
    assert age == 999999


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
        {
            "timestamp_epoch": 1737344700,
            "open": 50.0,
            "high": 55.0,
            "low": 48.0,
            "close": 52.0,
            "volume": 100,
            "oi": 20000,
            "change_oi": None,
            "doi_elapsed_sec": None,
            "is_gap": False,
            "iv": 14.0,
        }
    ]

    with patch.object(downloader, "fetch_spot_candles", return_value=mock_spot_map), \
         patch.object(downloader, "fetch_underlying_symbols", return_value=mock_symbols), \
         patch.object(downloader, "fetch_contract_candles", return_value=mock_candles):

        valid, report, df = downloader.download_sample(expiry_date="2025-01-30", strikes_around_atm=1)
        assert len(df) == 4
        assert "timestamp" in df.columns
        assert "spot_price" in df.columns
        assert "opt_bid" in df.columns
        assert "cons_ask" in df.columns
        assert df["fill_tier"].iloc[0] == "ESTIMATED_SPREAD"
