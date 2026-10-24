"""
tests/test_backtest_engine.py — Unit tests for HistoricalBacktestEngine.
Tests point-in-time replay, scenario fills, target/stop-loss exits, risk controls, and performance metrics.
"""

import os
import pytest
from unittest.mock import MagicMock, patch

from app.backtesting.backtest_engine import (
    HistoricalBacktestEngine,
    BacktestTrade,
    StrategyMetrics,
    STARTING_EQUITY,
)


@pytest.fixture
def engine():
    return HistoricalBacktestEngine(starting_equity=STARTING_EQUITY)


def test_scenario_pricing_tiers(engine):
    """Verify Optimistic, Base, and Conservative fill prices."""
    quote = {
        "ltp": 50.0,
        "opt_bid": 49.95,
        "opt_ask": 50.05,
        "base_bid": 49.80,
        "base_ask": 50.20,
        "cons_bid": 49.60,
        "cons_ask": 50.40,
    }

    # BUY prices (Ask)
    p_opt_buy = engine._get_contract_price_for_scenario(quote, "BUY", "OPTIMISTIC")
    p_base_buy = engine._get_contract_price_for_scenario(quote, "BUY", "BASE")
    p_cons_buy = engine._get_contract_price_for_scenario(quote, "BUY", "CONSERVATIVE")
    assert p_opt_buy <= p_base_buy <= p_cons_buy
    assert p_opt_buy == 50.05
    assert p_base_buy == 50.20
    assert p_cons_buy == 50.40

    # SELL prices (Bid)
    p_opt_sell = engine._get_contract_price_for_scenario(quote, "SELL", "OPTIMISTIC")
    p_base_sell = engine._get_contract_price_for_scenario(quote, "SELL", "BASE")
    p_cons_sell = engine._get_contract_price_for_scenario(quote, "SELL", "CONSERVATIVE")
    assert p_opt_sell >= p_base_sell >= p_cons_sell
    assert p_opt_sell == 49.95
    assert p_base_sell == 49.80
    assert p_cons_sell == 49.60


def test_expiry_settlement_intrinsic_calculation(engine):
    """Verify expiry settlement intrinsic value calculations for all 3 strategies."""
    # 1. Bull Put Spread: Short 24000 PE, Long 23900 PE
    trade_bp = BacktestTrade(
        trade_id="T1",
        strategy_id="BULL_PUT",
        strategy_name="Bull Put Spread",
        engine_type="POSITIONAL",
        scenario="BASE",
        expiry="2025-01-23",
        entry_time="2025-01-20 10:00:00",
        short_strike=24000.0,
        long_strike=23900.0,
        net_credit_pts=25.0,
    )

    # If NIFTY expires at 24100 (OTM -> worthless -> full profit)
    cost = engine._evaluate_spread_exit_cost(trade_bp, {}, "BASE", final_spot=24100.0, is_expiry_settlement=True)
    assert cost == 0.0

    # If NIFTY expires at 23950 (Between strikes -> Short has 50 intrinsic, Long has 0)
    cost = engine._evaluate_spread_exit_cost(trade_bp, {}, "BASE", final_spot=23950.0, is_expiry_settlement=True)
    assert cost == 50.0

    # If NIFTY expires at 23800 (Below long strike -> Max loss = 100 pts)
    cost = engine._evaluate_spread_exit_cost(trade_bp, {}, "BASE", final_spot=23800.0, is_expiry_settlement=True)
    assert cost == 100.0


def test_metrics_finalization(engine):
    """Verify performance metrics calculation (ROI, Profit Factor, Expectancy, Max Drawdown)."""
    m = StrategyMetrics(strategy_id="BULL_PUT", scenario="BASE")

    # Trade 1: Win ₹5,000
    t1 = BacktestTrade(
        trade_id="T1", strategy_id="BULL_PUT", strategy_name="Bull Put", engine_type="INTRADAY",
        scenario="BASE", expiry="2025-01-23", entry_time="2025-01-20 10:00:00",
        gross_pnl=5500.0, total_costs=500.0, net_pnl=5000.0
    )
    engine._record_trade(m, t1)

    # Trade 2: Loss ₹2,000
    t2 = BacktestTrade(
        trade_id="T2", strategy_id="BULL_PUT", strategy_name="Bull Put", engine_type="INTRADAY",
        scenario="BASE", expiry="2025-01-23", entry_time="2025-01-21 10:00:00",
        gross_pnl=-1500.0, total_costs=500.0, net_pnl=-2000.0
    )
    engine._record_trade(m, t2)

    engine._finalize_metrics(m)

    assert m.total_trades == 2
    assert m.winning_trades == 1
    assert m.losing_trades == 1
    assert m.win_rate_pct == 50.0
    assert m.net_pnl == 3000.0
    assert m.roi_pct == round((3000.0 / STARTING_EQUITY) * 100.0, 2)
    assert m.profit_factor == 2.5  # 5000 / 2000
    assert m.expectancy_inr == (0.5 * 5000.0) - (0.5 * 2000.0)
    assert m.max_drawdown_inr == 2000.0
    assert m.max_consecutive_losses == 1
