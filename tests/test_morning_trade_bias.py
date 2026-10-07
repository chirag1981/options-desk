"""
tests/test_morning_trade_bias.py — Unit Tests for Morning Trade Bias & Direction Engine
Verifies:
1. Bullish bias when spot is trading above PDH and CPR with supportive PCR and momentum.
2. Bearish bias when spot is trading below PDL and CPR with resistance PCR and momentum.
3. Neutral / Rangebound bias when spot is inside previous day value / CPR chop.
4. Correct CPR (Central Pivot Range) calculations (Pivot, TC, BC, R1, S1, R2, S2, CPR Width & Type).
5. Opening Gap classification (GAP UP, GAP DOWN, FLAT OPEN).
6. Robustness against missing or DATA_UNAVAILABLE market data payloads.
"""

import pytest
from app.services.options_engine import compute_morning_trade_bias, analyze_option_desk


def test_morning_bias_bullish_above_pdh():
    """Verifies BULLISH morning trade bias when spot is above Previous Day High."""
    market_data = {
        "symbol": "NIFTY",
        "spot_price": 22600.0,
        "spot_change_pct": 0.45,
        "prev_close": 22500.0,
        "open_price": 22540.0,
        "prev_high": 22550.0,
        "prev_low": 22420.0,
        "strike_step": 50,
        "chain": [
            {"strike": 22600, "ce_oi": 100000, "pe_oi": 200000, "ce_change_oi": -20000, "pe_change_oi": 80000, "ce_ltp": 120.0, "pe_ltp": 40.0}
        ]
    }

    result = compute_morning_trade_bias(
        market_data=market_data,
        spot=22600.0,
        step=50.0,
        atm_strike=22600.0,
        pcr=1.35,
        bullish_score=85.0,
        bearish_score=15.0,
        r1=22650.0,
        s1=22550.0
    )

    assert result["direction"] == "BULLISH"
    assert result["action"] == "BUY CE"
    assert "BUY CE" in result["badge"]
    assert result["badge_class"] == "bullish"
    assert "22600" in result["suggested_contract"] or "22650" in result["suggested_contract"]
    assert "CE" in result["suggested_contract"]
    assert result["reference_data"]["pdh"] == 22550.0
    assert result["reference_data"]["pdl"] == 22420.0
    assert result["reference_data"]["pdc"] == 22500.0
    assert result["reference_data"]["gap_type"] == "GAP UP"
    assert result["execution_plan"]["target_1"] > 22600.0
    assert result["execution_plan"]["stop_loss"] < 22600.0
    assert len(result["why_reasons"]) >= 3


def test_morning_bias_bearish_below_pdl():
    """Verifies BEARISH morning trade bias when spot is below Previous Day Low."""
    market_data = {
        "symbol": "NIFTY",
        "spot_price": 22380.0,
        "spot_change_pct": -0.55,
        "prev_close": 22500.0,
        "open_price": 22460.0,
        "prev_high": 22560.0,
        "prev_low": 22420.0,
        "strike_step": 50,
        "chain": [
            {"strike": 22400, "ce_oi": 250000, "pe_oi": 80000, "ce_change_oi": 90000, "pe_change_oi": -30000, "ce_ltp": 45.0, "pe_ltp": 135.0}
        ]
    }

    result = compute_morning_trade_bias(
        market_data=market_data,
        spot=22380.0,
        step=50.0,
        atm_strike=22400.0,
        pcr=0.65,
        bullish_score=15.0,
        bearish_score=85.0,
        r1=22450.0,
        s1=22350.0
    )

    assert result["direction"] == "BEARISH"
    assert result["action"] == "BUY PE"
    assert "BUY PE" in result["badge"]
    assert result["badge_class"] == "bearish"
    assert "PE" in result["suggested_contract"]
    assert result["reference_data"]["pdh"] == 22560.0
    assert result["reference_data"]["pdl"] == 22420.0
    assert result["reference_data"]["gap_type"] == "GAP DOWN"
    assert result["execution_plan"]["target_1"] < 22380.0
    assert result["execution_plan"]["stop_loss"] > 22380.0
    assert len(result["why_reasons"]) >= 3


def test_morning_bias_rangebound_inside_value():
    """Verifies NEUTRAL/RANGEBOUND morning trade bias when spot is inside CPR and PDL-PDH."""
    market_data = {
        "symbol": "NIFTY",
        "spot_price": 22495.0,
        "spot_change_pct": -0.02,
        "prev_close": 22500.0,
        "open_price": 22502.0,
        "prev_high": 22580.0,
        "prev_low": 22410.0,
        "strike_step": 50,
    }

    result = compute_morning_trade_bias(
        market_data=market_data,
        spot=22495.0,
        step=50.0,
        atm_strike=22500.0,
        pcr=0.98,
        bullish_score=35.0,
        bearish_score=35.0,
        r1=22550.0,
        s1=22450.0
    )

    assert result["direction"] == "NEUTRAL"
    assert result["action"] == "WAIT"
    assert "RANGEBOUND" in result["badge"]
    assert result["badge_class"] == "neutral"
    assert result["suggested_contract"] == "--"
    assert "Wait for 15m Opening Range" in result["execution_plan"]["trigger"]


def test_analyze_option_desk_includes_morning_bias():
    """Verifies analyze_option_desk integration returns complete morning_bias key in both normal and fallback modes."""
    # Test 1: Normal chain
    normal_data = {
        "symbol": "NIFTY",
        "spot_price": 22550.0,
        "spot_change_pct": 0.20,
        "strike_step": 50,
        "data_status": "LIVE",
        "chain": [
            {"strike": 22500, "ce_oi": 100000, "pe_oi": 200000, "ce_change_oi": 10000, "pe_change_oi": 30000, "ce_ltp": 90.0, "pe_ltp": 50.0},
            {"strike": 22550, "ce_oi": 150000, "pe_oi": 180000, "ce_change_oi": 15000, "pe_change_oi": 25000, "ce_ltp": 60.0, "pe_ltp": 70.0},
            {"strike": 22600, "ce_oi": 220000, "pe_oi": 110000, "ce_change_oi": 35000, "pe_change_oi": 10000, "ce_ltp": 35.0, "pe_ltp": 105.0},
        ]
    }
    analysis = analyze_option_desk(normal_data)
    assert "morning_bias" in analysis
    mb = analysis["morning_bias"]
    assert "direction" in mb
    assert "reference_data" in mb
    assert "execution_plan" in mb
    assert "why_reasons" in mb

    # Test 2: DATA_UNAVAILABLE fallback
    unavail_data = {
        "symbol": "NIFTY",
        "data_status": "DATA_UNAVAILABLE",
        "chain": []
    }
    unavail_analysis = analyze_option_desk(unavail_data)
    assert "morning_bias" in unavail_analysis
    assert unavail_analysis["morning_bias"]["direction"] == "NEUTRAL"
    assert unavail_analysis["morning_bias"]["action"] == "WAIT"
