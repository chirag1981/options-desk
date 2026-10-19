"""
tests/test_pivot_50_strategy.py — Verification of 50% Range Pivot Gate

Verifies the rule:
- If current spot price is ABOVE 50% pivot level of Day's High and Low:
    * Look for CE (Bullish half of range).
    * PE buying is blocked by 50%_PIVOT_BLOCK.
- If current spot price is BELOW 50% pivot level of Day's High and Low:
    * Look for PE (Bearish half of range).
    * CE buying is blocked by 50%_PIVOT_BLOCK.
- Missing or equal high/low gracefully falls back without false-positive blocking.
"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import options_engine
from app.services.options_engine import analyze_option_desk

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def prime_trading_hours(monkeypatch):
    """Ensures mock runs during prime trading window (10:30 IST) with favorable IV."""
    midday = datetime(2026, 10, 5, 10, 30, tzinfo=IST)
    orig_filter = options_engine.evaluate_iv_and_time_filter
    monkeypatch.setattr(
        options_engine,
        "evaluate_iv_and_time_filter",
        lambda atm_iv, now_dt=None: orig_filter(atm_iv, midday),
    )
    with options_engine._BIAS_LOCK:
        options_engine._BIAS_STATES.clear()


def _mock_chain(spot=23000.0, step=50, is_bearish=True, high=23200.0, low=22800.0) -> dict:
    """
    Constructs a valid mock option chain.
    50% Pivot for (23200 + 22800) / 2 = 23000.0.
    """
    chain = []
    for strike in range(int(spot - 300), int(spot + 350), step):
        if is_bearish:
            ce_oi = 200000 if strike >= spot else 50000
            pe_oi = 40000 if strike >= spot else 80000
            ce_chg = 50000 if strike >= spot else 10000
            pe_chg = -20000 if strike >= spot else 5000
            ce_vol = 80000
            pe_vol = 150000
        else:
            ce_oi = 50000 if strike <= spot else 80000
            pe_oi = 200000 if strike <= spot else 50000
            ce_chg = -20000 if strike <= spot else 5000
            pe_chg = 50000 if strike <= spot else 10000
            ce_vol = 150000
            pe_vol = 80000

        chain.append({
            "strike": strike,
            "ce_ltp": max(10.0, round((spot - strike) * 0.8 + 80.0, 1)),
            "ce_bid": max(9.5, round((spot - strike) * 0.8 + 79.5, 1)),
            "ce_ask": max(10.5, round((spot - strike) * 0.8 + 80.5, 1)),
            "pe_ltp": max(10.0, round((strike - spot) * 0.8 + 80.0, 1)),
            "pe_bid": max(9.5, round((strike - spot) * 0.8 + 79.5, 1)),
            "pe_ask": max(10.5, round((strike - spot) * 0.8 + 80.5, 1)),
            "ce_oi": ce_oi,
            "pe_oi": pe_oi,
            "ce_change_oi": ce_chg,
            "pe_change_oi": pe_chg,
            "ce_volume": ce_vol,
            "pe_volume": pe_vol,
            "ce_iv": 14.0,
            "pe_iv": 14.5,
        })

    return {
        "symbol": "NIFTY",
        "spot_price": spot,
        "spot_change_pct": -0.30 if is_bearish else 0.30,
        "high_price": high,
        "low_price": low,
        "strike_step": step,
        "data_status": "LIVE",
        "chain": chain,
    }


def test_spot_above_50_pivot_blocks_pe():
    """
    When spot is above 50% Pivot level of Day's High and Low,
    PE buying is strictly blocked by 50%_PIVOT_BLOCK even if OI is bearish.
    """
    # High: 23175, Low: 22970 -> 50% Pivot = 23072.5
    # Spot: 23095 (Above 50% Pivot)
    data = _mock_chain(spot=23095.0, is_bearish=True, high=23175.0, low=22970.0)
    res = analyze_option_desk(data)

    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert "50%_PIVOT_BLOCK" in opt["reason"]
    assert "Spot 23,095.0 is above 50% Pivot 23,072.5" in opt["reason"]
    assert "Only CE allowed in upper half" in opt["reason"]


def test_spot_below_50_pivot_allows_pe():
    """
    When spot is below 50% Pivot level of Day's High and Low,
    PE buying is allowed (bearish regime).
    """
    # High: 23175, Low: 22864 -> 50% Pivot = 23019.5
    # Spot: 22880 (Below 50% Pivot)
    data = _mock_chain(spot=22880.0, is_bearish=True, high=23175.0, low=22864.0)
    res = analyze_option_desk(data)

    opt = res["option_buying"]
    assert opt["decision"] == "BUY PE"
    assert opt["raw_values"]["spot_vs_pivot_50"] == "BELOW"
    assert opt["raw_values"]["pivot_50"] == 23019.5


def test_spot_below_50_pivot_blocks_ce():
    """
    When spot is below 50% Pivot level of Day's High and Low,
    CE buying is strictly blocked even if OI is bullish.
    """
    # High: 23200, Low: 22800 -> 50% Pivot = 23000.0
    # Spot: 22920 (Below 50% Pivot)
    data = _mock_chain(spot=22920.0, is_bearish=False, high=23200.0, low=22800.0)
    res = analyze_option_desk(data)

    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert "50%_PIVOT_BLOCK" in opt["reason"]
    assert "Spot 22,920.0 is below 50% Pivot 23,000.0" in opt["reason"]
    assert "Only PE allowed in lower half" in opt["reason"]


def test_spot_above_50_pivot_allows_ce():
    """
    When spot is above 50% Pivot level of Day's High and Low,
    CE buying is allowed (bullish regime).
    """
    # High: 23200, Low: 22800 -> 50% Pivot = 23000.0
    # Spot: 23080 (Above 50% Pivot)
    data = _mock_chain(spot=23080.0, is_bearish=False, high=23200.0, low=22800.0)
    res = analyze_option_desk(data)

    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["raw_values"]["spot_vs_pivot_50"] == "ABOVE"
