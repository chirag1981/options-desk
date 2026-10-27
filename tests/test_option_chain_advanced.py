"""
tests/test_option_chain_advanced.py — Unit Tests for OI Pulse-Style Option Chain & Support/Resistance
Verifies:
1. Historical date discovery and session indexing.
2. Exact point-in-time time-travel replay (e.g. 12:00:00 snapshot).
3. Institutional S1/S2 and R1/R2 Support & Resistance wall identification.
4. Strike-wise PCR and total option chain PCR calculation.
5. Black-76 Greeks (Delta) and OI writer interpretation (LB, SB, SC, LU).
6. 7-Day multi-day historical OHLC and OI build-up modal query.
7. REST API endpoints /api/option-chain, /api/option-chain/dates, /api/strike-history.
"""

import pytest
from app import create_app
from app.option_chain_service import OptionChainService


@pytest.fixture
def app_client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_available_dates_indexing():
    """Verifies that available historical sessions are discovered from parquet storage."""
    dates_data = OptionChainService.get_available_dates()
    assert dates_data["symbol"] == "NIFTY"
    assert dates_data["total_sessions"] > 0
    assert dates_data["latest_date"] is not None
    assert isinstance(dates_data["available_dates"], list)


def test_historical_chain_point_in_time_replay():
    """Verifies exact time-travel snapshot retrieval at 12:00:00."""
    dates_data = OptionChainService.get_available_dates()
    latest_date = dates_data["latest_date"]

    res = OptionChainService.get_historical_chain(date_str=latest_date, time_str="12:00:00")
    assert res["status"] == "success"
    assert res["mode"] == "historical"
    assert res["spot_price"] > 0
    assert len(res["chain"]) > 0

    # Verify Support & Resistance Walls
    assert res["r1_resistance"] is not None
    assert res["s1_support"] is not None
    assert res["r1_resistance"] >= res["s1_support"]

    # Verify Total PCR
    assert res["total_pcr"] >= 0.0
    assert res["total_call_oi"] > 0
    assert res["total_put_oi"] > 0


def test_strike_leg_greeks_and_interpretation():
    """Verifies that each strike contains Delta, OI Interpretation, and wall tags."""
    dates_data = OptionChainService.get_available_dates()
    latest_date = dates_data["latest_date"]

    res = OptionChainService.get_historical_chain(date_str=latest_date, time_str="12:00:00")
    sample_row = res["chain"][0]

    # Verify CE leg structure
    ce = sample_row["ce"]
    assert "delta" in ce
    assert 0.0 <= ce["delta"] <= 1.0
    assert ce["oi_interpretation"] in ("LB", "SB", "SC", "LU", "--")
    assert "oi_pct_bar" in ce

    # Verify PE leg structure
    pe = sample_row["pe"]
    assert "delta" in pe
    assert -1.0 <= pe["delta"] <= 0.0
    assert pe["oi_interpretation"] in ("LB", "SB", "SC", "LU", "--")
    assert "oi_pct_bar" in pe

    # Verify Strike PCR
    assert "pcr" in sample_row
    assert sample_row["pcr"] >= 0.0


def test_strike_7day_history_extraction():
    """Verifies that multi-day strike history returns valid OHLC and OI build-up."""
    dates_data = OptionChainService.get_available_dates()
    latest_date = dates_data["latest_date"]
    res = OptionChainService.get_historical_chain(date_str=latest_date, time_str="12:00:00")

    target_strike = res["r1_resistance"]
    stk_history = OptionChainService.get_strike_history(strike=target_strike, end_date=latest_date)

    assert stk_history["status"] == "success"
    assert stk_history["strike"] == target_strike
    assert stk_history["total_days"] > 0
    day0 = stk_history["history"][0]
    assert "date" in day0
    assert "ce" in day0 and "pe" in day0
    assert day0["ce"]["open"] >= 0.0
    assert day0["ce"]["high"] >= day0["ce"]["low"]


def test_api_option_chain_endpoints(app_client):
    """Verifies all REST API endpoints for option chain operations."""
    # 1. Dates endpoint
    r_dates = app_client.get("/api/option-chain/dates")
    assert r_dates.status_code == 200
    dates_json = r_dates.get_json()
    latest_date = dates_json["latest_date"]

    # 2. Historical Option Chain endpoint with custom time
    r_chain = app_client.get(f"/api/option-chain?mode=historical&date={latest_date}&time=12:00:00")
    assert r_chain.status_code == 200
    chain_json = r_chain.get_json()
    assert chain_json["status"] == "success"
    assert "chain" in chain_json

    # 3. Strike 7-day history endpoint
    r1_strike = chain_json["r1_resistance"]
    r_hist = app_client.get(f"/api/strike-history?strike={r1_strike}&date={latest_date}")
    assert r_hist.status_code == 200
    hist_json = r_hist.get_json()
    assert hist_json["status"] == "success"
    assert hist_json["total_days"] > 0
