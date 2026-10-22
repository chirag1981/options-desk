"""
tests/test_strategies.py — Unit Tests for 4 Defined-Risk Strategies & Dual Engines
"""

from app.analysis import MarketAnalyzer
from app.strategies import StrategyBuilder, IntradayEngine, PositionalEngine
from tests.test_analysis import build_mock_chain


def test_bull_put_spread_construction():
    chain_data = build_mock_chain(spot=24500.0, pcr=1.35, bias_flow="BULLISH")
    # Make sure 24400 PE and 24350 PE have realistic bid/ask
    chain = chain_data["chain_by_strike"]
    chain[24400.0]["PE"]["bid"] = 52.0
    chain[24400.0]["PE"]["ask"] = 54.0
    chain[24350.0]["PE"]["bid"] = 34.0
    chain[24350.0]["PE"]["ask"] = 36.0

    analysis = MarketAnalyzer.analyze_chain(chain_data)
    cand = StrategyBuilder.build_bull_put_spread(analysis, chain, engine_type="INTRADAY")

    assert cand.strategy_id == "BULL_PUT"
    assert cand.is_eligible is True
    assert cand.has_uncovered_short is False
    assert len(cand.legs) == 2
    # Sold at Bid (52.0), bought at Ask (36.0) -> Net Credit = 16.0
    assert cand.net_credit_pts == 16.0
    assert cand.strike_width == 50.0
    assert cand.max_loss_pts == 34.0
    assert cand.max_profit_inr == 16.0 * 65
    assert cand.max_loss_inr == 34.0 * 65


def test_bear_call_spread_construction():
    chain_data = build_mock_chain(spot=24500.0, pcr=0.75, bias_flow="BEARISH")
    chain = chain_data["chain_by_strike"]
    chain[24650.0]["CE"]["bid"] = 48.0
    chain[24650.0]["CE"]["ask"] = 50.0
    chain[24700.0]["CE"]["bid"] = 30.0
    chain[24700.0]["CE"]["ask"] = 32.0

    analysis = MarketAnalyzer.analyze_chain(chain_data)
    cand = StrategyBuilder.build_bear_call_spread(analysis, chain, engine_type="INTRADAY")

    assert cand.strategy_id == "BEAR_CALL"
    assert cand.is_eligible is True
    assert cand.has_uncovered_short is False
    # Sold at Bid (48.0), bought at Ask (32.0) -> Net Credit = 16.0
    assert cand.net_credit_pts == 16.0
    assert cand.max_loss_pts == 34.0


def test_iron_butterfly_entry_disabled():
    chain_data = build_mock_chain(spot=24500.0, pcr=1.0)
    analysis = MarketAnalyzer.analyze_chain(chain_data)
    cand = StrategyBuilder.build_iron_butterfly(analysis, chain_data["chain_by_strike"])

    assert cand.strategy_id == "IRON_BUTTERFLY"
    assert cand.is_eligible is False
    assert any("disabled" in r.lower() for r in cand.rejection_reasons)
