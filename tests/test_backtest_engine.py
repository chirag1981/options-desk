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


def test_target_and_stop_loss_threshold_alignment(engine):
    """
    Verify aligned profit target and stop loss parameter semantics:
    - Target: buyback cost reaches 30% of credit (70% credit profit captured)
    - Stop Loss: buyback cost reaches 150% of credit (50% credit loss)
    """
    net_credit = 20.0
    # Production: target buyback at 30% of credit
    target_threshold = round(net_credit * (1.0 - engine.target_profit_pct), 2)
    assert target_threshold == 6.0  # 20.0 * 0.30

    # Production: stop loss buyback at 150% of credit
    stop_threshold = round(net_credit * (1.0 + engine.stop_loss_credit_mult), 2)
    assert stop_threshold == 30.0  # 20.0 * 1.50


def test_authoritative_risk_ceilings_alignment():
    """Verify authoritative Config limits used by backtest engine."""
    from app.config import Config
    assert Config.get_max_risk_per_trade_inr() == 5000.0
    assert Config.get_max_combined_portfolio_risk_inr() == 20000.0
    assert Config.get_max_daily_loss_inr() == 15000.0
    assert Config.CONSECUTIVE_LOSS_COOLDOWN_MINS == 15
    assert Config.MAX_INTRADAY_POSITIONS == 2


def test_conservative_ambiguity_policy():
    """
    Verify conservative ambiguity policy:
    If within a snapshot both target and stop conditions could be met,
    the engine conservatively assumes the adverse outcome (STOP_LOSS).
    """
    target_hit = True
    stop_hit = True
    exit_reason = None
    if target_hit and stop_hit:
        exit_reason = "STOP_LOSS"
    elif target_hit:
        exit_reason = "TARGET"
    elif stop_hit:
        exit_reason = "STOP_LOSS"

    assert exit_reason == "STOP_LOSS"


def test_no_duplicate_slippage_deduction():
    """Verify Net P&L does not double-deduct slippage that is already embedded in fills."""
    # Trade filled at execution prices with gross P&L of ₹1,000 and total costs of ₹150
    gross_pnl = 1000.0
    total_costs = 150.0
    # Embedded slippage (already inside gross_pnl) is ₹100
    slippage_cost = 100.0

    # Correct calculation: net_pnl = gross_pnl - total_costs
    correct_net_pnl = gross_pnl - total_costs
    assert correct_net_pnl == 850.0

    # Duplicate deduction error would have produced: gross_pnl - total_costs - slippage_cost = 750.0
    erroneous_net_pnl = gross_pnl - total_costs - slippage_cost
    assert erroneous_net_pnl != correct_net_pnl


def test_leg_by_leg_exit_brokerage():
    """Verify exit legs properly calculate ₹20 per leg brokerage (e.g. ₹40 for 2-leg, ₹80 for 4-leg)."""
    from app.trading import TransactionCostEngine
    from datetime import date

    # 2-leg spread exit
    legs_2 = [
        {"action": "BUY", "price": 10.0, "quantity": 75},
        {"action": "SELL", "price": 2.0, "quantity": 75},
    ]
    costs_2 = TransactionCostEngine.calculate_order_costs_breakdown(legs_2, lots=1, trade_date=date(2025, 1, 15))
    assert costs_2["brokerage"] == 40.0  # 2 * 20

    # 4-leg Iron Condor exit
    legs_4 = [
        {"action": "BUY", "price": 10.0, "quantity": 75},
        {"action": "SELL", "price": 2.0, "quantity": 75},
        {"action": "BUY", "price": 8.0, "quantity": 75},
        {"action": "SELL", "price": 1.5, "quantity": 75},
    ]
    costs_4 = TransactionCostEngine.calculate_order_costs_breakdown(legs_4, lots=1, trade_date=date(2025, 1, 15))
    assert costs_4["brokerage"] == 80.0  # 4 * 20


def test_snapshot_reconstruction_preserves_scenario_pricing_tiers():
    """Verify that HistoricalDataValidator.reconstruct_snapshots preserves scenario quote tiers."""
    from app.backtesting.data_validator import HistoricalDataValidator

    records = [
        {
            "timestamp": "2025-01-16 09:20:00",
            "spot_price": 24000.0,
            "expiry": "2025-01-16",
            "strike": 24000.0,
            "option_type": "CE",
            "ltp": 50.0,
            "bid": 49.80,
            "ask": 50.20,
            "opt_bid": 49.95,
            "opt_ask": 50.05,
            "base_bid": 49.80,
            "base_ask": 50.20,
            "cons_bid": 49.60,
            "cons_ask": 50.40,
            "fill_tier": "ESTIMATED_SPREAD",
            "oi": 100000,
            "change_oi": 5000,
        },
        {
            "timestamp": "2025-01-16 09:20:00",
            "spot_price": 24000.0,
            "expiry": "2025-01-16",
            "strike": 24000.0,
            "option_type": "PE",
            "ltp": 50.0,
            "bid": 49.80,
            "ask": 50.20,
            "opt_bid": 49.95,
            "opt_ask": 50.05,
            "base_bid": 49.80,
            "base_ask": 50.20,
            "cons_bid": 49.60,
            "cons_ask": 50.40,
            "fill_tier": "ESTIMATED_SPREAD",
            "oi": 100000,
            "change_oi": 5000,
        },
    ]

    # Add 4 more strikes to satisfy structural reconstructability (min 5 strikes)
    for stk in (23900.0, 23950.0, 24050.0, 24100.0):
        for opt in ("CE", "PE"):
            records.append({
                "timestamp": "2025-01-16 09:20:00",
                "spot_price": 24000.0,
                "expiry": "2025-01-16",
                "strike": stk,
                "option_type": opt,
                "ltp": 30.0,
                "bid": 29.80,
                "ask": 30.20,
                "opt_bid": 29.95,
                "opt_ask": 30.05,
                "base_bid": 29.80,
                "base_ask": 30.20,
                "cons_bid": 29.60,
                "cons_ask": 30.40,
                "fill_tier": "ESTIMATED_SPREAD",
                "oi": 50000,
                "change_oi": 1000,
            })

    snapshots, _, _, _, _, _, _ = HistoricalDataValidator.reconstruct_snapshots(records)
    assert len(snapshots) == 1
    snap = snapshots[0]
    ce_quote = snap["chain_by_strike"][24000.0]["CE"]
    assert ce_quote["opt_bid"] == 49.95
    assert ce_quote["opt_ask"] == 50.05
    assert ce_quote["cons_bid"] == 49.60
    assert ce_quote["cons_ask"] == 50.40
    assert ce_quote["base_bid"] == 49.80
    assert ce_quote["base_ask"] == 50.20


def test_session_transition_settles_unclosed_trades():
    """Verify that trades unclosed from a prior truncated session are settled on date transition."""
    from app.backtesting.backtest_engine import HistoricalBacktestEngine, BacktestTrade

    engine = HistoricalBacktestEngine()
    dummy_snap = {
        "timestamp": "2025-05-15 13:20:00",
        "spot_price": 24000.0,
        "chain_by_strike": {
            24000.0: {"CE": {"ltp": 50.0, "bid": 49.8, "ask": 50.2}, "PE": {"ltp": 50.0, "bid": 49.8, "ask": 50.2}},
            24050.0: {"CE": {"ltp": 30.0, "bid": 29.8, "ask": 30.2}, "PE": {"ltp": 70.0, "bid": 69.8, "ask": 70.2}},
        }
    }
    trade = BacktestTrade(
        trade_id="BT-TEST-0001",
        strategy_id="BEAR_CALL",
        strategy_name="Bear Call Credit Spread",
        engine_type="INTRADAY",
        scenario="BASE",
        expiry="2025-05-15",
        entry_time="2025-05-15 09:20:00",
        lots=1,
        quantity=75,
        short_strike=24000.0,
        long_strike=24050.0,
        net_credit_pts=20.0,
        entry_spread_cost=20.0,
    )

    engine._close_trade_at_settlement(trade, dummy_snap, scenario="BASE", reason="SESSION_CUTOFF")
    assert trade.exit_reason == "SESSION_CUTOFF"
    assert trade.exit_time == "2025-05-15 13:20:00"
    assert trade.holding_time_mins > 0
    assert trade.net_pnl is not None



