"""
tests/test_trading.py — Unit & Integration Tests for Paper Trading Execution & MTM Lifecycle
"""

import pytest
from app.config import Config
from app.models import db, TradePosition, PositionLeg
from app.trading import PaperTradingManager, TransactionCostEngine
from app.strategies import SpreadCandidate, OptionLegSpec


def _build_test_ic_candidate(engine_type="INTRADAY", lots=1, spot_price=24500.0, legs=None, max_loss_inr=None):
    qty = 65 * lots
    if legs is None:
        legs = [
            OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=40.0, ask=41.0, ltp=40.5, quantity=qty),
            OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=25.0, ask=26.0, ltp=25.5, quantity=qty),
            OptionLegSpec(symbol="NIFTY24600CE", strike=24600.0, option_type="CE", action="SELL", bid=40.0, ask=41.0, ltp=40.5, quantity=qty),
            OptionLegSpec(symbol="NIFTY24650CE", strike=24650.0, option_type="CE", action="BUY", bid=25.0, ask=26.0, ltp=25.5, quantity=qty),
        ]
    return SpreadCandidate(
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
        engine_type=engine_type,
        symbol="NIFTY",
        expiry="27-NOV-2026",
        lots=lots,
        quantity=qty,
        legs=legs,
        net_credit_pts=28.0,
        strike_width=50.0,
        max_profit_inr=28.0 * qty,
        max_loss_pts=22.0,
        max_loss_inr=max_loss_inr if max_loss_inr is not None else 22.0 * qty,
        risk_reward_ratio=1.27,
        breakevens=[24372.0, 24628.0],
        is_eligible=True,
        spot_price=spot_price,
    )


def test_transaction_cost_calculator_realistic():
    legs = [
        {"action": "SELL", "price": 40.0, "quantity": 65},
        {"action": "BUY", "price": 25.0, "quantity": 65},
        {"action": "SELL", "price": 40.0, "quantity": 65},
        {"action": "BUY", "price": 25.0, "quantity": 65},
    ]
    cost = TransactionCostEngine.calculate_order_costs(legs, lots=1)
    assert cost > 80.0
    assert cost < 140.0


def test_paper_execution_and_mtm_lifecycle(app_ctx):
    candidate = _build_test_ic_candidate(engine_type="POSITIONAL")

    # 1. Execute paper order
    ok, msg, pos = PaperTradingManager.execute_candidate(candidate)
    assert ok is True
    assert pos.status == "OPEN"
    assert pos.quantity == 65
    assert len(pos.legs) == 4

    # 2. Update MTM with favorable quote (Target Hit)
    mock_chain = {
        24400.0: {"PE": {"bid": 10.0, "ask": 12.0, "ltp": 11.0}},
        24350.0: {"PE": {"bid": 6.0, "ask": 7.0, "ltp": 6.5}},
        24600.0: {"CE": {"bid": 10.0, "ask": 12.0, "ltp": 11.0}},
        24650.0: {"CE": {"bid": 6.0, "ask": 7.0, "ltp": 6.5}},
    }
    # Current debit = (12 - 6) + (12 - 6) = 12.0 pts
    PaperTradingManager.update_open_positions_mtm(mock_chain, spot_price=24500.0)

    # Check position state
    saved_pos = db.session.get(TradePosition, pos.id)
    assert saved_pos.current_net_debit_pts == 12.0
    assert saved_pos.current_pnl_inr > 0  # Profit captured!

    # 3. Close position manually
    close_ok, close_msg = PaperTradingManager.close_position(pos.id, exit_spot=24500.0, reason="MANUAL_CLOSE")
    assert close_ok is True
    closed_pos = db.session.get(TradePosition, pos.id)
    assert closed_pos.status == "MANUAL_CLOSE"
    assert closed_pos.exit_time is not None
    assert closed_pos.net_pnl_inr > 0


def test_unsupported_strategy_rejected_server_side(app_ctx):
    """Verifies that unapproved strategy IDs are rejected server-side by both API and manager."""
    client = app_ctx.test_client()

    for invalid_strat in ("BULL_PUT", "BEAR_CALL", "SHORT_STRADDLE", "IRON_BUTTERFLY", "STRANGLE", "CUSTOM_UNKNOWN"):
        # 1. API endpoint rejection
        res = client.post("/api/execute-candidate", json={"strategy_id": invalid_strat, "engine_type": "INTRADAY"})
        assert res.status_code == 400
        data = res.get_json()
        assert data["status"] == "error"
        assert "Unsupported or disabled strategy" in data["message"]

        # 2. PaperTradingManager direct rejection
        cand = SpreadCandidate(
            strategy_id=invalid_strat,
            strategy_name=invalid_strat,
            engine_type="INTRADAY",
            symbol="NIFTY",
            expiry="27-OCT-2026",
            lots=1,
            quantity=65,
            legs=[],
            net_credit_pts=15.0,
            strike_width=50.0,
            max_profit_inr=15.0 * 65,
            max_loss_pts=35.0,
            max_loss_inr=35.0 * 65,
            risk_reward_ratio=0.43,
            breakevens=[24485.0],
            is_eligible=True,
            spot_price=24500.0,
        )
        ok, msg, pos = PaperTradingManager.execute_candidate(cand)
        assert ok is False
        assert "not supported for execution" in msg
        assert pos is None


def test_missing_or_stale_quotes_prevent_phantom_mtm_and_exits(app_ctx):
    """Verifies that missing or zero/stale quotes do not fabricate P&L or cause phantom exits."""
    candidate = _build_test_ic_candidate(engine_type="INTRADAY")

    ok, _, pos = PaperTradingManager.execute_candidate(candidate)
    assert ok is True
    initial_entry_costs = pos.transaction_costs_inr
    assert initial_entry_costs > 0

    # Test Case A: Missing quotes completely for strike
    empty_chain = {}
    PaperTradingManager.update_open_positions_mtm(empty_chain, spot_price=24500.0)
    pos_after_empty = db.session.get(TradePosition, pos.id)
    assert pos_after_empty.status == "OPEN"
    assert pos_after_empty.current_net_debit_pts == 28.0  # unchanged
    assert pos_after_empty.current_pnl_inr == 0.0          # no fabricated P&L

    # Test Case B: Stale / non-positive quotes on one leg
    stale_chain = {
        24400.0: {"PE": {"bid": 0.0, "ask": 0.0, "ltp": 0.0}},
        24350.0: {"PE": {"bid": 25.0, "ask": 26.0, "ltp": 25.5}},
        24600.0: {"CE": {"bid": 40.0, "ask": 41.0, "ltp": 40.5}},
        24650.0: {"CE": {"bid": 25.0, "ask": 26.0, "ltp": 25.5}},
    }
    PaperTradingManager.update_open_positions_mtm(stale_chain, spot_price=24500.0)
    pos_after_stale = db.session.get(TradePosition, pos.id)
    assert pos_after_stale.status == "OPEN"
    assert pos_after_stale.current_net_debit_pts == 28.0  # unchanged, no phantom exit


def test_full_lifecycle_fee_and_pnl_reconciliation(app_ctx):
    """Verifies that fees and P&L reconcile with mathematical exactness across the lifecycle."""
    candidate = _build_test_ic_candidate(engine_type="INTRADAY")

    # 1. Entry
    ok, msg, pos = PaperTradingManager.execute_candidate(candidate)
    assert ok is True
    assert pos.status == "OPEN"
    entry_fees = pos.transaction_costs_inr
    assert entry_fees > 0
    assert len(pos.legs) == 4

    # 2. MTM update
    valid_chain = {
        24400.0: {"PE": {"bid": 15.0, "ask": 16.0, "ltp": 15.5}},
        24350.0: {"PE": {"bid": 8.0, "ask": 9.0, "ltp": 8.5}},
        24600.0: {"CE": {"bid": 15.0, "ask": 16.0, "ltp": 15.5}},
        24650.0: {"CE": {"bid": 8.0, "ask": 9.0, "ltp": 8.5}},
    }
    # Total debit = (16 - 8) + (16 - 8) = 16.0 pts
    orig_close_time = Config.INTRADAY_SESSION_CLOSE_TIME
    try:
        Config.INTRADAY_SESSION_CLOSE_TIME = "23:59:59"
        PaperTradingManager.update_open_positions_mtm(valid_chain, spot_price=24550.0)
        updated_pos = db.session.get(TradePosition, pos.id)
        assert updated_pos.current_net_debit_pts == 16.0
        expected_gross_unrealized = (28.0 - 16.0) * 65
        assert updated_pos.current_pnl_inr == round(expected_gross_unrealized - entry_fees, 2)
    finally:
        Config.INTRADAY_SESSION_CLOSE_TIME = orig_close_time

    # 3. Manual Close
    close_ok, _ = PaperTradingManager.close_position(pos.id, exit_spot=24550.0, reason="MANUAL_CLOSE")
    assert close_ok is True
    closed = db.session.get(TradePosition, pos.id)

    # Verify reconciliation arithmetic
    assert closed.status == "MANUAL_CLOSE"
    assert closed.exit_net_debit_pts == 16.0
    expected_gross = round((28.0 - 16.0) * 65, 2)  # 780.00
    assert closed.gross_pnl_inr == expected_gross
    assert closed.transaction_costs_inr > entry_fees  # entry fees + exit fees recorded
    expected_net = round(closed.gross_pnl_inr - closed.transaction_costs_inr, 2)
    assert closed.net_pnl_inr == expected_net


def test_concurrent_duplicate_execution_rejected(app_ctx):
    """Verifies that duplicate execution for same strategy and engine is safely blocked."""
    candidate = _build_test_ic_candidate(engine_type="INTRADAY")

    # First execution succeeds
    ok1, msg1, pos1 = PaperTradingManager.execute_candidate(candidate)
    assert ok1 is True
    assert pos1 is not None
    assert pos1.status == "OPEN"

    # Second duplicate execution attempt fails safely
    ok2, msg2, pos2 = PaperTradingManager.execute_candidate(candidate)
    assert ok2 is False
    assert pos2 is None
    assert "already exists" in msg2
    assert "Duplicate entry blocked" in msg2

    # Database still contains exactly 1 OPEN position for IRON_CONDOR INTRADAY
    positions = TradePosition.query.filter_by(strategy_id="IRON_CONDOR", engine_type="INTRADAY", status="OPEN").all()
    assert len(positions) == 1


def test_direct_execute_candidate_safeguards_and_spot_validation(app_ctx):
    """
    Verifies that direct calls to PaperTradingManager.execute_candidate():
    1. Reject non-positive or missing spot price without plausible-value fallbacks.
    2. Cannot bypass ExecutionSafetyGate quote/contract checks.
    3. Cannot bypass PortfolioRiskManager limits.
    """
    # Test A: Missing spot price (0.0) -> MUST reject clearly
    zero_spot_cand = _build_test_ic_candidate(engine_type="INTRADAY", spot_price=0.0)
    ok_zero, msg_zero, pos_zero = PaperTradingManager.execute_candidate(zero_spot_cand)
    assert ok_zero is False
    assert pos_zero is None
    assert "spot price" in msg_zero.lower()

    # Test B: Inverted/Crossed market on leg -> MUST be rejected by ExecutionSafetyGate
    crossed_legs = [
        OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=50.0, ask=48.0, ltp=49.0, quantity=65),
        OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=25.0, ask=26.0, ltp=25.5, quantity=65),
        OptionLegSpec(symbol="NIFTY24600CE", strike=24600.0, option_type="CE", action="SELL", bid=40.0, ask=41.0, ltp=40.5, quantity=65),
        OptionLegSpec(symbol="NIFTY24650CE", strike=24650.0, option_type="CE", action="BUY", bid=25.0, ask=26.0, ltp=25.5, quantity=65),
    ]
    crossed_cand = _build_test_ic_candidate(engine_type="INTRADAY", legs=crossed_legs)
    ok_cross, msg_cross, _ = PaperTradingManager.execute_candidate(crossed_cand)
    assert ok_cross is False
    assert "safety gate" in msg_cross.lower() or "crossed" in msg_cross.lower()

    # Test C: Exceeds max trade risk ceiling -> MUST be rejected by PortfolioRiskManager
    huge_loss_cand = _build_test_ic_candidate(engine_type="INTRADAY", lots=5, max_loss_inr=10075.0)
    ok_risk, msg_risk, _ = PaperTradingManager.execute_candidate(huge_loss_cand)
    assert ok_risk is False
    assert "portfolio risk" in msg_risk.lower() or "risk limit" in msg_risk.lower()
