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


def test_iron_condor_strike_selection_based_on_oi_change():
    """Verify that Iron Condor short strike selection adapts dynamically based on strike-wise ΔOI."""
    chain_data = build_mock_chain(spot=24500.0, pcr=1.0, bias_flow="NEUTRAL")
    chain = chain_data["chain_by_strike"]

    # 1. Base S1 is 24400 (50k OI), but S1 is experiencing unwinding (-5000 ΔOI)
    # Active Put writing is aggressively building at 24350 (+25,000 ΔOI)
    chain[24400.0]["PE"]["change_oi"] = -5000
    chain[24350.0]["PE"]["change_oi"] = 25000
    chain[24350.0]["PE"]["bid"] = 28.0
    chain[24350.0]["PE"]["ask"] = 30.0
    chain[24300.0]["PE"]["bid"] = 14.0
    chain[24300.0]["PE"]["ask"] = 16.0

    # 2. Base R1 is 24650 (50k OI), but Call writers are unwinding (-8000 ΔOI)
    # Active Call writing is building at 24700 (+30,000 ΔOI)
    chain[24650.0]["CE"]["change_oi"] = -8000
    chain[24700.0]["CE"]["change_oi"] = 30000
    chain[24700.0]["CE"]["bid"] = 26.0
    chain[24700.0]["CE"]["ask"] = 28.0
    chain[24750.0]["CE"]["bid"] = 12.0
    chain[24750.0]["CE"]["ask"] = 14.0

    analysis = MarketAnalyzer.analyze_chain(chain_data)
    cand = StrategyBuilder.build_iron_condor(analysis, chain, engine_type="INTRADAY", wing_width=50.0)

    # Short Put should adapt from unwinding 24400 down to 24350 (peak fresh writing)
    put_leg_short = [leg for leg in cand.legs if leg.option_type == "PE" and leg.action == "SELL"][0]
    put_leg_long = [leg for leg in cand.legs if leg.option_type == "PE" and leg.action == "BUY"][0]
    assert put_leg_short.strike == 24350.0
    assert put_leg_long.strike == 24300.0

    # Short Call should adapt from unwinding 24650 up to 24700 (peak fresh writing)
    call_leg_short = [leg for leg in cand.legs if leg.option_type == "CE" and leg.action == "SELL"][0]
    call_leg_long = [leg for leg in cand.legs if leg.option_type == "CE" and leg.action == "BUY"][0]
    assert call_leg_short.strike == 24700.0
    assert call_leg_long.strike == 24750.0

    # Rationale must reflect ΔOI selection
    assert "Put unwinding at S1" in cand.rationale or "Active Put writing" in cand.rationale
    assert "Call unwinding at R1" in cand.rationale or "Active Call writing" in cand.rationale


def test_auto_trade_engine_default_enabled():
    """Verify AutoTradeEngine defaults to True and supports explicit toggle."""
    from app.trading import AutoTradeEngine
    AutoTradeEngine._auto_trade_enabled = None
    assert AutoTradeEngine.is_enabled() is True

    # Test explicit toggle to False
    AutoTradeEngine.set_enabled(False)
    assert AutoTradeEngine.is_enabled() is False

    # Test explicit toggle to True
    AutoTradeEngine.set_enabled(True)
    assert AutoTradeEngine.is_enabled() is True

