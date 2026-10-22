"""
tests/test_analysis.py — Unit Tests for MarketAnalyzer & OI Structure
"""

import pytest
from app.analysis import MarketAnalyzer


def build_mock_chain(spot=24500.0, pcr=1.2, bias_flow="BULLISH"):
    chain = {}
    atm = round(spot / 50.0) * 50.0

    for i in range(-5, 6):
        stk = atm + (i * 50.0)
        is_s1 = (stk == atm - 100.0)
        is_r1 = (stk == atm + 150.0)

        pe_oi = 50000 if is_s1 else 15000
        ce_oi = int((50000 if is_r1 else 15000) / pcr)

        pe_chg = 12000 if (bias_flow == "BULLISH" and stk <= atm) else 2000
        ce_chg = 12000 if (bias_flow == "BEARISH" and stk >= atm) else 2000

        chain[stk] = {
            "CE": {"symbol": f"NIFTY{int(stk)}CE", "ltp": 50.0, "bid": 49.0, "ask": 51.0, "oi": ce_oi, "change_oi": ce_chg, "iv": 14.5},
            "PE": {"symbol": f"NIFTY{int(stk)}PE", "ltp": 50.0, "bid": 49.0, "ask": 51.0, "oi": pe_oi, "change_oi": pe_chg, "iv": 15.0},
        }

    return {
        "spot_price": spot,
        "spot_change_pct": 0.45,
        "expiry": "27-OCT-2026",
        "chain_by_strike": chain,
        "data_status": "TEST_MOCK",
    }


def test_bullish_market_analysis():
    chain_data = build_mock_chain(spot=24500.0, pcr=1.35, bias_flow="BULLISH")
    res = MarketAnalyzer.analyze_chain(chain_data)

    assert res.spot_price == 24500.0
    assert res.atm_strike == 24500.0
    assert res.s1_support == 24400.0
    assert res.r1_resistance == 24650.0
    assert res.pcr_oi > 1.0
    assert res.market_bias == "BULLISH"
    assert res.market_regime == "TRENDING_BULLISH"
    assert res.dist_to_s1 == 100.0


def test_bearish_market_analysis():
    chain_data = build_mock_chain(spot=24500.0, pcr=0.75, bias_flow="BEARISH")
    res = MarketAnalyzer.analyze_chain(chain_data)

    assert res.pcr_oi < 0.85
    assert res.market_bias == "BEARISH"
    assert res.market_regime == "TRENDING_BEARISH"
    assert res.dist_to_r1 == 150.0


def test_invalid_spot_rejection():
    with pytest.raises(ValueError):
        MarketAnalyzer.analyze_chain({"spot_price": 0.0, "chain_by_strike": {}})
