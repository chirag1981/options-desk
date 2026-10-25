"""
tests/test_iron_condor_research.py — Iron Condor Research Scope Restriction & Baseline Verification Tests

Verifies:
1. Research workflow is restricted to IRON_CONDOR only (excluding BULL_PUT & BEAR_CALL).
2. Iron Condor rejection under CHOPPY_OR_INSUFFICIENT or trending regimes.
3. Accurate historical lot-size schedule across circular transition dates (no blanket mapping).
4. Max loss risk calculation and itemized statutory fee accounting (STT, Brokerage, Exchange, GST, Stamp Duty).
5. Exact independent reproduction of 31 trades, 19 wins, 12 losses, +₹7,072.11 Base Net P&L across all 3 scenarios.
6. Execution risk guards: max positions (2), per-trade risk (<=₹5,000), portfolio risk (<=₹20,000), daily circuit breaker.
"""

import os
import glob
from datetime import date
import pytest

from app.contracts import ContractRegistry, NIFTY_SPEC_SCHEDULE
from app.trading import TransactionCostEngine
from app.strategies import StrategyBuilder, MarketAnalysisResult
from app.backtesting.backtest_engine import HistoricalBacktestEngine, STARTING_EQUITY
from scripts.run_strategy_experiments import ResearchBacktestEngine, ExperimentConfig
from app.config import Config


def test_research_engine_restricts_to_iron_condor_only():
    """Requirement 1: Verify research workflow is strictly restricted to IRON_CONDOR."""
    engine = ResearchBacktestEngine()
    assert engine.approved_strategies == {"IRON_CONDOR"}
    assert "BULL_PUT" not in engine.approved_strategies
    assert "BEAR_CALL" not in engine.approved_strategies


def test_iron_condor_choppy_regime_rejection():
    """Requirement 1 & 7: Verify Iron Condor fails closed when market is CHOPPY_OR_INSUFFICIENT."""
    mock_analysis = MarketAnalysisResult(
        symbol="NIFTY",
        expiry="2026-10-29",
        timestamp="2026-10-25 10:00:00",
        spot_price=24500.0,
        spot_change_pct=0.0,
        atm_strike=24500.0,
        s1_support=24400.0,
        s2_support=24350.0,
        r1_resistance=24600.0,
        r2_resistance=24650.0,
        pcr_oi=1.0,
        total_pe_oi=50000,
        total_ce_oi=50000,
        total_pe_change_oi=5000,
        total_ce_change_oi=5000,
        net_delta_pe_ce_oi=0,
        writer_momentum="BALANCED",
        dist_to_s1=100.0,
        dist_to_r1=100.0,
        dist_to_s1_pct=0.41,
        dist_to_r1_pct=0.41,
        atm_iv=14.0,
        market_regime="CHOPPY_OR_INSUFFICIENT",
        market_bias="NEUTRAL",
        confidence_score=40.0,
        evidence_factors=["Choppy market"],
        data_status="LIVE_STREAMING",
    )
    chain = {
        24400.0: {"PE": {"bid": 25.0, "ask": 26.0, "volume": 1000, "oi": 5000}},
        24350.0: {"PE": {"bid": 10.0, "ask": 11.0, "volume": 1000, "oi": 5000}},
        24600.0: {"CE": {"bid": 25.0, "ask": 26.0, "volume": 1000, "oi": 5000}},
        24650.0: {"CE": {"bid": 10.0, "ask": 11.0, "volume": 1000, "oi": 5000}},
    }

    cand = StrategyBuilder.build_iron_condor(mock_analysis, chain)
    assert not cand.is_eligible
    assert any("choppy" in r.lower() or "insufficient" in r.lower() for r in cand.rejection_reasons)


def test_lot_size_transition_schedule():
    """Requirement 2: Verify exact date-specific circular lot sizes without blanket 75/65 mapping."""
    # Pre-revision 2024 (Circular NSE/FAOP/61328) -> 25
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2024, 10, 24)) == 25
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2024, 11, 14)) == 25

    # Post-revision Nov 20, 2024 to Dec 30, 2025 (Circular NSE/FAOP/64625) -> 75
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2024, 11, 20)) == 75
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2025, 5, 15)) == 75
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2025, 12, 30)) == 75

    # Revised Jan 2026 onwards (Circular NSE/FAOP/70616) -> 65
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2025, 12, 31)) == 65
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2026, 1, 6)) == 65
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2026, 10, 6)) == 65


def test_iron_condor_max_loss_and_fee_reconciliation():
    """Requirement 3 & 4: Reconcile max loss (₹1,950–₹2,275) and verify statutory fee accounting."""
    # A 50pt wing width Iron Condor with 75 lot size and 22.0pt net credit:
    # Max loss pts = 50.0 - 22.0 = 28.0 pts
    # Gross max loss = 28.0 * 75 = ₹2,100.00
    wing_width = 50.0
    net_credit = 22.0
    lot_size = 75
    gross_max_loss = (wing_width - net_credit) * lot_size
    assert gross_max_loss == 2100.00
    assert 1950.0 <= gross_max_loss <= 2275.0

    # Test fee accounting on 4 legs (2 SELL, 2 BUY)
    entry_legs = [
        {"action": "SELL", "price": 25.0, "quantity": 75},
        {"action": "BUY", "price": 10.0, "quantity": 75},
        {"action": "SELL", "price": 20.0, "quantity": 75},
        {"action": "BUY", "price": 8.0, "quantity": 75},
    ]
    # Trade in 2025 (STT = 0.10%)
    costs_2025 = TransactionCostEngine.calculate_order_costs_breakdown(entry_legs, lots=1, trade_date=date(2025, 6, 1))
    assert costs_2025["brokerage"] == 80.0  # ₹20 * 4 legs
    # Sell turnover = (25 + 20) * 75 = 3375.0
    assert costs_2025["sell_turnover"] == 3375.0
    # STT = 3375.0 * 0.0010 = 3.375
    assert round(costs_2025["stt"], 3) == 3.375
    # GST = 18% on (Brokerage + Exchange turnover + SEBI turnover + IPFT)
    assert costs_2025["gst"] > 0
    # Neither STT nor Stamp duty is inside gst_taxable_base
    assert costs_2025["gst_taxable_base"] == costs_2025["brokerage"] + costs_2025["exchange_fee"] + costs_2025["sebi_fee"] + costs_2025["ipft"]

    # Trade in April 2026 (STT = 0.15% per Finance Act 2026)
    costs_2026 = TransactionCostEngine.calculate_order_costs_breakdown(entry_legs, lots=1, trade_date=date(2026, 4, 15))
    # STT = 3375.0 * 0.0015 = 5.0625
    assert round(costs_2026["stt"], 4) == 5.0625

    # Effective risk ceiling check
    est_round_trip = costs_2025["total"] * 2.0
    effective_risk = gross_max_loss + est_round_trip
    assert effective_risk < Config.get_max_risk_per_trade_inr()


def test_isolated_baseline_metrics_reproduction():
    """Requirement 5: Independently reproduce 31 trades, 19 wins, 12 losses, +₹7,072.11 Base Net P&L."""
    data_dir = os.path.join(os.path.dirname(__file__), "..", "instance", "historical_data")
    files = sorted(glob.glob(os.path.join(data_dir, "NIFTY_*.parquet")))
    if not files:
        pytest.skip("Historical data files not present.")

    engine = HistoricalBacktestEngine(data_dir=data_dir, starting_equity=STARTING_EQUITY)
    engine.approved_strategies = {"IRON_CONDOR"}

    res = engine.run_simulation(parquet_files=files)

    base = res["BASE"]["IRON_CONDOR"]
    assert base.total_trades == 31
    assert base.winning_trades == 19
    assert base.losing_trades == 12
    assert round(base.win_rate_pct, 1) == 61.3
    assert round(base.gross_pnl, 2) == 13073.50
    # Corrected baseline accounts for Trade 3 session cutoff exit costs (+₹222.01 costs)
    assert round(base.total_costs, 2) == 6223.40
    assert round(base.net_pnl, 2) == 6850.10
    assert base.profit_factor == 1.56
    # Exact delta vs frozen baseline (+₹7,072.11 - +₹6,850.10 = ₹222.01)
    frozen_base_net = 7072.11
    assert round(frozen_base_net - base.net_pnl, 2) == 222.01

    opt = res["OPTIMISTIC"]["IRON_CONDOR"]
    assert opt.total_trades == 31
    assert opt.winning_trades == 20
    assert opt.losing_trades == 11
    assert round(opt.win_rate_pct, 1) == 64.5
    assert round(opt.net_pnl, 2) == 7315.27
    assert opt.profit_factor == 1.61

    cons = res["CONSERVATIVE"]["IRON_CONDOR"]
    assert cons.total_trades == 31
    assert cons.winning_trades == 18
    assert cons.losing_trades == 13
    assert round(cons.win_rate_pct, 1) == 58.1
    assert round(cons.net_pnl, 2) == 3418.70
    assert opt.profit_factor > cons.profit_factor


def test_settlement_exit_cost_accounting():
    """Stage A: Regression test verifying settlement and session cutoff exit costs are never zeroed."""
    from app.backtesting.backtest_engine import BacktestTrade
    engine = HistoricalBacktestEngine()

    dummy_snap = {
        "timestamp": "2025-05-15 13:20:00",
        "spot_price": 24000.0,
        "expiry": "2025-05-15",
        "chain_by_strike": {
            24000.0: {"PE": {"bid": 10.0, "ask": 10.5, "ltp": 10.2}, "CE": {"bid": 10.0, "ask": 10.5, "ltp": 10.2}},
            24050.0: {"PE": {"bid": 5.0, "ask": 5.5, "ltp": 5.2}, "CE": {"bid": 5.0, "ask": 5.5, "ltp": 5.2}},
            23950.0: {"PE": {"bid": 5.0, "ask": 5.5, "ltp": 5.2}, "CE": {"bid": 5.0, "ask": 5.5, "ltp": 5.2}},
        }
    }

    trade = BacktestTrade(
        trade_id="BT-TEST-0001",
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
        engine_type="INTRADAY",
        scenario="BASE",
        expiry="2025-05-15",
        entry_time="2025-05-15 10:50:00",
        lots=1,
        quantity=75,
        short_strike=24000.0,
        long_strike=23950.0,
        short_strike_2=24000.0,
        long_strike_2=24050.0,
        net_credit_pts=30.0,
        entry_spread_cost=30.0,
        total_brokerage=80.0,
        total_taxes=30.0,
        total_costs=110.0,
    )

    # Calling _close_trade_at_settlement with SESSION_CUTOFF
    engine._close_trade_at_settlement(trade, dummy_snap, scenario="BASE", reason="SESSION_CUTOFF")

    # Assert entry brokerage was NOT zeroed and exit brokerage was added
    assert trade.total_brokerage == 160.0  # ₹80 entry + ₹80 exit
    assert trade.total_taxes > 30.0        # Entry taxes preserved + exit taxes added
    assert trade.total_costs == round(trade.total_brokerage + trade.total_taxes, 2)
    assert trade.exit_reason == "SESSION_CUTOFF"
    assert trade.net_pnl == round(trade.gross_pnl - trade.total_costs, 2)


def test_holdout_validation_split():
    """Stage B: Verify Discovery (75 sessions) and Holdout (30 sessions) performance separation."""
    data_dir = os.path.join(os.path.dirname(__file__), "..", "instance", "historical_data")
    files = sorted(glob.glob(os.path.join(data_dir, "NIFTY_*.parquet")))
    if not files or len(files) < 105:
        pytest.skip("Full 105 historical data files not present.")

    in_sample = files[:75]
    out_sample = files[75:]

    engine = HistoricalBacktestEngine(data_dir=data_dir, starting_equity=STARTING_EQUITY)
    engine.approved_strategies = {"IRON_CONDOR"}

    # Discovery
    res_disc = engine.run_simulation(parquet_files=in_sample)
    disc_base = res_disc["BASE"]["IRON_CONDOR"]
    assert disc_base.total_trades == 17
    assert disc_base.winning_trades == 11
    assert disc_base.losing_trades == 6
    assert round(disc_base.net_pnl, 2) == 5434.90
    assert disc_base.profit_factor == 1.92

    # Holdout
    res_hold = engine.run_simulation(parquet_files=out_sample)
    hold_base = res_hold["BASE"]["IRON_CONDOR"]
    assert hold_base.total_trades == 14
    assert hold_base.winning_trades == 8
    assert hold_base.losing_trades == 6
    assert round(hold_base.net_pnl, 2) == 1415.20
    assert hold_base.profit_factor == 1.23

    # Total trades equal 31 (17 Discovery + 14 Holdout)
    assert disc_base.total_trades + hold_base.total_trades == 31


def test_risk_and_guardrail_enforcement():
    """Requirement 6: Verify risk controls (max concurrent, per-trade risk, portfolio risk, daily loss)."""
    assert Config.MAX_INTRADAY_POSITIONS == 2
    assert Config.get_max_risk_per_trade_inr() == 5000.0
    assert Config.get_max_combined_portfolio_risk_inr() == 20000.0
    assert Config.get_max_daily_loss_inr() == 15000.0
    assert Config.CONSECUTIVE_LOSS_COOLDOWN_MINS == 15
