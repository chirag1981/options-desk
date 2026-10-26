"""
tests/test_strategies.py — Unit Tests for Iron Condor & Gated Iron Butterfly
"""

from app.analysis import MarketAnalyzer
from app.strategies import StrategyBuilder, IntradayEngine, PositionalEngine
from tests.test_analysis import build_mock_chain


def test_iron_condor_spread_construction():
    chain_data = build_mock_chain(spot=24500.0, pcr=1.0, bias_flow="NEUTRAL")
    chain = chain_data["chain_by_strike"]
    # Put wing: Short 24400 PE, Long 24350 PE
    chain[24400.0]["PE"]["bid"] = 40.0
    chain[24400.0]["PE"]["ask"] = 42.0
    chain[24350.0]["PE"]["bid"] = 20.0
    chain[24350.0]["PE"]["ask"] = 22.0
    # Call wing: Short 24650 CE, Long 24700 CE
    chain[24650.0]["CE"]["bid"] = 38.0
    chain[24650.0]["CE"]["ask"] = 40.0
    chain[24700.0]["CE"]["bid"] = 18.0
    chain[24700.0]["CE"]["ask"] = 20.0

    analysis = MarketAnalyzer.analyze_chain(chain_data)
    cand = StrategyBuilder.build_iron_condor(analysis, chain, engine_type="INTRADAY")

    assert cand.strategy_id == "IRON_CONDOR"
    assert cand.has_uncovered_short is False
    assert len(cand.legs) == 4
    assert cand.strike_width == 50.0


def test_iron_butterfly_entry_disabled():
    chain_data = build_mock_chain(spot=24500.0, pcr=1.0)
    analysis = MarketAnalyzer.analyze_chain(chain_data)
    cand = StrategyBuilder.build_iron_butterfly(analysis, chain_data["chain_by_strike"])

    assert cand.strategy_id == "IRON_BUTTERFLY"
    assert cand.is_eligible is False
    assert any("disabled" in r.lower() for r in cand.rejection_reasons)
