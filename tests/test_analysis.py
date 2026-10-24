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


def test_balanced_range_market_analysis():
    chain_data = build_mock_chain(spot=24500.0, pcr=1.0, bias_flow="BALANCED")
    res = MarketAnalyzer.analyze_chain(chain_data)
    assert res.market_regime == "BALANCED_RANGE"
    assert res.market_bias == "NEUTRAL"
    assert res.confidence_score == 82.0
    assert 0.90 <= res.pcr_oi <= 1.10


def test_mildly_bullish_and_mildly_bearish_regimes():
    # Mildly Bullish: PCR = 1.12 (between 1.10 and 1.15) with spot above S1
    chain_bull = build_mock_chain(spot=24500.0, pcr=1.12, bias_flow="BALANCED")
    res_bull = MarketAnalyzer.analyze_chain(chain_bull)
    assert res_bull.market_regime == "MILDLY_BULLISH"
    assert res_bull.market_bias == "BULLISH"

    # Mildly Bearish: PCR = 0.88 (between 0.85 and 0.90) with spot below R1
    chain_bear = build_mock_chain(spot=24500.0, pcr=0.88, bias_flow="BALANCED")
    res_bear = MarketAnalyzer.analyze_chain(chain_bear)
    assert res_bear.market_regime == "MILDLY_BEARISH"
    assert res_bear.market_bias == "BEARISH"


def test_zero_and_missing_oi_safety():
    # Test A: Zero OI on all strikes
    chain_zero = build_mock_chain(spot=24500.0, pcr=1.0)
    for stk in chain_zero["chain_by_strike"].values():
        stk["CE"]["oi"] = 0
        stk["PE"]["oi"] = 0
    res_zero = MarketAnalyzer.analyze_chain(chain_zero)
    assert res_zero.market_regime == "CHOPPY_OR_INSUFFICIENT"
    assert any("Zero Open Interest" in w for w in res_zero.data_quality_warnings)

    # Test B: Zero Call OI (prevents divide-by-zero, sets PCR=0.0 safely)
    chain_zero_ce = build_mock_chain(spot=24500.0, pcr=1.0)
    for stk in chain_zero_ce["chain_by_strike"].values():
        stk["CE"]["oi"] = 0
    res_zero_ce = MarketAnalyzer.analyze_chain(chain_zero_ce)
    assert res_zero_ce.pcr_oi == 0.0
    assert res_zero_ce.market_regime == "CHOPPY_OR_INSUFFICIENT"
    assert any("Zero Call Open Interest" in w for w in res_zero_ce.data_quality_warnings)


def test_missing_delta_oi_handled_safely():
    chain_no_delta = build_mock_chain(spot=24500.0, pcr=1.0)
    for stk in chain_no_delta["chain_by_strike"].values():
        stk["CE"]["change_oi"] = None
        stk["PE"]["change_oi"] = None
    res = MarketAnalyzer.analyze_chain(chain_no_delta)
    assert res.net_delta_pe_ce_oi == 0
    assert any("Historical change in Open Interest" in w for w in res.data_quality_warnings)


def test_contradictory_indicators_detection():
    # Case A: Bullish PCR (1.30) but all Put OI is above spot (S1=24600 > Spot=24500)
    chain_contra_a = build_mock_chain(spot=24500.0, pcr=1.30, bias_flow="BULLISH")
    for s, stk in chain_contra_a["chain_by_strike"].items():
        if s <= 24500.0:
            stk["PE"]["oi"] = 0  # Zero Put OI below spot
        elif s == 24600.0:
            stk["PE"]["oi"] = 80000
    res_a = MarketAnalyzer.analyze_chain(chain_contra_a)
    assert res_a.market_regime == "CHOPPY_OR_INSUFFICIENT"
    assert any("Contradictory signals" in f or "breached" in f for f in res_a.evidence_factors)

    # Case B: Bearish PCR (0.70) but all Call OI is below spot (S1=24300 < R1=24400 < Spot=24500)
    chain_contra_b = build_mock_chain(spot=24500.0, pcr=0.70, bias_flow="BEARISH")
    for s, stk in chain_contra_b["chain_by_strike"].items():
        if s >= 24500.0:
            stk["CE"]["oi"] = 0  # Zero Call OI above spot
        elif s == 24400.0:
            stk["CE"]["oi"] = 80000
        elif s == 24300.0:
            stk["PE"]["oi"] = 80000  # S1 at 24300 so S1 < R1
    res_b = MarketAnalyzer.analyze_chain(chain_contra_b)
    assert res_b.market_regime == "CHOPPY_OR_INSUFFICIENT"
    assert any("Contradictory signals" in f or "breached" in f for f in res_b.evidence_factors)

    # Case C: High PCR (1.25) but Call OI heavily surging (>1.5x Put OI change)
    chain_contra_c = build_mock_chain(spot=24500.0, pcr=1.25, bias_flow="BULLISH")
    for stk in chain_contra_c["chain_by_strike"].values():
        stk["CE"]["change_oi"] = 30000
        stk["PE"]["change_oi"] = 2000
    res_c = MarketAnalyzer.analyze_chain(chain_contra_c)
    assert res_c.market_regime == "CHOPPY_OR_INSUFFICIENT"
    assert any("Contradictory signals" in f for f in res_c.evidence_factors)


def test_invalid_support_resistance_levels_rejected():
    # Invert S1 and R1 by having only Put OI at 24700 and only Call OI at 24300
    chain_inv = {
        "spot_price": 24500.0,
        "spot_change_pct": 0.0,
        "expiry": "27-OCT-2026",
        "chain_by_strike": {
            24700.0: {"PE": {"oi": 50000, "bid": 10.0, "ask": 12.0}, "CE": {"oi": 0, "bid": 10.0, "ask": 12.0}},
            24300.0: {"CE": {"oi": 50000, "bid": 10.0, "ask": 12.0}, "PE": {"oi": 0, "bid": 10.0, "ask": 12.0}},
        },
        "data_status": "TEST_MOCK",
    }
    res = MarketAnalyzer.analyze_chain(chain_inv)
    assert res.market_regime == "CHOPPY_OR_INSUFFICIENT"
    assert any("Invalid structural levels" in w for w in res.data_quality_warnings)


def test_deterministic_analysis_output():
    chain_data = build_mock_chain(spot=24500.0, pcr=1.35, bias_flow="BULLISH")
    res1 = MarketAnalyzer.analyze_chain(chain_data)
    res2 = MarketAnalyzer.analyze_chain(chain_data)

    dict1 = res1.to_dict()
    dict2 = res2.to_dict()
    # Timestamps are based on evaluation run, compare all analytical metrics
    dict1.pop("timestamp")
    dict2.pop("timestamp")
    assert dict1 == dict2


def test_auto_strategy_selector_no_trade_evidence():
    from app.strategies import AutoStrategySelector
    # Build choppy chain that triggers NO TRADE
    chain_contra = build_mock_chain(spot=24350.0, pcr=1.30, bias_flow="BULLISH")
    analysis = MarketAnalyzer.analyze_chain(chain_contra)
    auto_res = AutoStrategySelector.evaluate_auto_selection(analysis, chain_contra["chain_by_strike"])

    assert auto_res.selection_type == "NONE"
    assert auto_res.primary_strategy is None
    assert auto_res.selection_rationale.startswith("NO TRADE:")
    assert len(auto_res.no_trade_reasons) > 0
