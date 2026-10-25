"""
tests/test_trading.py — Unit Tests for PaperTradingManager & Transaction Costs
"""

import pytest
from app import create_app
from app.config import Config
from app.extensions import db
from app.trading import TransactionCostEngine, PaperTradingManager
from app.strategies import SpreadCandidate, OptionLegSpec
from app.models import TradePosition


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


@pytest.fixture
def app_ctx():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_transaction_cost_calculations():
    # 2 leg spread
    legs = [
        {"action": "SELL", "price": 50.0, "quantity": 65},
        {"action": "BUY", "price": 30.0, "quantity": 65},
    ]
    cost = TransactionCostEngine.calculate_order_costs(legs, lots=1)
    # Brokerage is ₹40 + STT on sell + Turnover fees + 18% GST -> ~₹47-52
    assert cost > 40.0
    assert cost < 70.0


def test_paper_execution_and_mtm_lifecycle(app_ctx):
    candidate = SpreadCandidate(
        strategy_id="BULL_PUT",
        strategy_name="Bull Put Spread",
        engine_type="POSITIONAL",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        legs=[
            OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=50.0, ask=52.0, ltp=51.0, quantity=65),
            OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=30.0, ask=32.0, ltp=31.0, quantity=65),
        ],
        net_credit_pts=18.0,
        strike_width=50.0,
        max_profit_inr=18.0 * 65,
        max_loss_pts=32.0,
        max_loss_inr=32.0 * 65,
        risk_reward_ratio=0.56,
        breakevens=[24382.0],
        is_eligible=True,
        spot_price=24500.0,
    )

    # 1. Execute paper order
    ok, msg, pos = PaperTradingManager.execute_candidate(candidate)
    assert ok is True
    assert pos.status == "OPEN"
    assert pos.quantity == 65
    assert len(pos.legs) == 2

    # 2. Update MTM with favorable quote (Target Hit)
    mock_chain = {
        24400.0: {"PE": {"bid": 10.0, "ask": 12.0, "ltp": 11.0}},
        24350.0: {"PE": {"bid": 6.0, "ask": 7.0, "ltp": 6.5}},
    }
    # Current debit = buyback short at 12.0 - sell long at 6.0 = 6.0 pts
    # Target was 30% of 18.0 = 5.4 pts, close to target
    PaperTradingManager.update_open_positions_mtm(mock_chain, spot_price=24550.0)

    # Check position state
    saved_pos = db.session.get(TradePosition, pos.id)
    assert saved_pos.current_net_debit_pts == 6.0
    assert saved_pos.current_pnl_inr > 0  # Profit captured!

    # 3. Close position manually
    close_ok, close_msg = PaperTradingManager.close_position(pos.id, exit_spot=24550.0, reason="MANUAL_CLOSE")
    assert close_ok is True
    closed_pos = db.session.get(TradePosition, pos.id)
    assert closed_pos.status == "MANUAL_CLOSE"
    assert closed_pos.exit_time is not None
    assert closed_pos.net_pnl_inr > 0


def test_unsupported_strategy_rejected_server_side(app_ctx):
    """Verifies that unapproved strategy IDs are rejected server-side by both API and manager."""
    client = app_ctx.test_client()

    for invalid_strat in ("SHORT_STRADDLE", "IRON_BUTTERFLY", "STRANGLE", "CUSTOM_UNKNOWN"):
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
        )
        ok, msg, pos = PaperTradingManager.execute_candidate(cand)
        assert ok is False
        assert "not supported for execution" in msg
        assert pos is None


def test_missing_or_stale_quotes_prevent_phantom_mtm_and_exits(app_ctx):
    """Verifies that missing or zero/stale quotes do not fabricate P&L or cause phantom exits."""
    candidate = SpreadCandidate(
        strategy_id="BEAR_CALL",
        strategy_name="Bear Call Spread",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        legs=[
            OptionLegSpec(symbol="NIFTY24600CE", strike=24600.0, option_type="CE", action="SELL", bid=45.0, ask=46.0, ltp=45.5, quantity=65),
            OptionLegSpec(symbol="NIFTY24650CE", strike=24650.0, option_type="CE", action="BUY", bid=25.0, ask=26.0, ltp=25.5, quantity=65),
        ],
        net_credit_pts=19.0,
        strike_width=50.0,
        max_profit_inr=19.0 * 65,
        max_loss_pts=31.0,
        max_loss_inr=31.0 * 65,
        risk_reward_ratio=0.61,
        breakevens=[24619.0],
        is_eligible=True,
        spot_price=24500.0,
    )

    ok, _, pos = PaperTradingManager.execute_candidate(candidate)
    assert ok is True
    initial_entry_costs = pos.transaction_costs_inr
    assert initial_entry_costs > 0

    # Test Case A: Missing quotes completely for strike
    empty_chain = {}
    PaperTradingManager.update_open_positions_mtm(empty_chain, spot_price=24500.0)
    pos_after_empty = db.session.get(TradePosition, pos.id)
    assert pos_after_empty.status == "OPEN"
    assert pos_after_empty.current_net_debit_pts == 19.0  # unchanged
    assert pos_after_empty.current_pnl_inr == 0.0          # no fabricated P&L

    # Test Case B: Stale / non-positive quotes on one leg
    stale_chain = {
        24600.0: {"CE": {"bid": 0.0, "ask": 0.0, "ltp": 0.0}},
        24650.0: {"CE": {"bid": 25.0, "ask": 26.0, "ltp": 25.5}},
    }
    PaperTradingManager.update_open_positions_mtm(stale_chain, spot_price=24500.0)
    pos_after_stale = db.session.get(TradePosition, pos.id)
    assert pos_after_stale.status == "OPEN"
    assert pos_after_stale.current_net_debit_pts == 19.0  # unchanged, no phantom exit


def test_full_lifecycle_fee_and_pnl_reconciliation(app_ctx):
    """Verifies that fees and P&L reconcile with mathematical exactness across the lifecycle."""
    candidate = SpreadCandidate(
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        legs=[
            OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=40.0, ask=41.0, ltp=40.5, quantity=65),
            OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=25.0, ask=26.0, ltp=25.5, quantity=65),
            OptionLegSpec(symbol="NIFTY24700CE", strike=24700.0, option_type="CE", action="SELL", bid=40.0, ask=41.0, ltp=40.5, quantity=65),
            OptionLegSpec(symbol="NIFTY24750CE", strike=24750.0, option_type="CE", action="BUY", bid=25.0, ask=26.0, ltp=25.5, quantity=65),
        ],
        net_credit_pts=28.0,  # (40 - 26) + (40 - 26) = 28.0
        strike_width=50.0,
        max_profit_inr=28.0 * 65,
        max_loss_pts=22.0,
        max_loss_inr=22.0 * 65,
        risk_reward_ratio=1.27,
        breakevens=[24372.0, 24728.0],
        is_eligible=True,
        spot_price=24500.0,
    )

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
        24700.0: {"CE": {"bid": 15.0, "ask": 16.0, "ltp": 15.5}},
        24750.0: {"CE": {"bid": 8.0, "ask": 9.0, "ltp": 8.5}},
    }
    # Current buyback cost:
    # 24400 PE: buy at Ask (16.0)
    # 24350 PE: sell at Bid (8.0) -> net 8.0 pts
    # 24700 CE: buy at Ask (16.0)
    # 24750 CE: sell at Bid (8.0) -> net 8.0 pts
    # Total debit = 16.0 pts
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
    candidate = SpreadCandidate(
        strategy_id="BULL_PUT",
        strategy_name="Bull Put Spread",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        legs=[
            OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=45.0, ask=46.0, ltp=45.5, quantity=65),
            OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=25.0, ask=26.0, ltp=25.5, quantity=65),
        ],
        net_credit_pts=19.0,
        strike_width=50.0,
        max_profit_inr=19.0 * 65,
        max_loss_pts=31.0,
        max_loss_inr=31.0 * 65,
        risk_reward_ratio=0.61,
        breakevens=[24381.0],
        is_eligible=True,
        spot_price=24500.0,
    )

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

    # Database still contains exactly 1 OPEN position for BULL_PUT INTRADAY
    positions = TradePosition.query.filter_by(strategy_id="BULL_PUT", engine_type="INTRADAY", status="OPEN").all()
    assert len(positions) == 1


def test_direct_execute_candidate_safeguards_and_spot_validation(app_ctx):
    """
    Regression Test for Findings 5 & 6:
    Verifies that direct calls to PaperTradingManager.execute_candidate():
    1. Reject non-positive or missing spot price without plausible-value fallbacks.
    2. Cannot bypass ExecutionSafetyGate quote/contract checks.
    3. Cannot bypass PortfolioRiskManager limits.
    """
    base_legs = [
        OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=45.0, ask=46.0, ltp=45.5, quantity=65),
        OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=25.0, ask=26.0, ltp=25.5, quantity=65),
    ]

    # Test A: Missing spot price (0.0) -> MUST reject clearly
    zero_spot_cand = SpreadCandidate(
        strategy_id="BULL_PUT",
        strategy_name="Bull Put Spread",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        legs=base_legs,
        net_credit_pts=19.0,
        strike_width=50.0,
        max_profit_inr=19.0 * 65,
        max_loss_pts=31.0,
        max_loss_inr=31.0 * 65,
        risk_reward_ratio=0.61,
        breakevens=[24381.0],
        is_eligible=True,
        spot_price=0.0,
    )
    ok_zero, msg_zero, pos_zero = PaperTradingManager.execute_candidate(zero_spot_cand)
    assert ok_zero is False
    assert pos_zero is None
    assert "spot price" in msg_zero.lower()

    # Test B: Inverted/Crossed market on leg -> MUST be rejected by ExecutionSafetyGate
    crossed_legs = [
        OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=50.0, ask=48.0, ltp=49.0, quantity=65),
        OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=25.0, ask=26.0, ltp=25.5, quantity=65),
    ]
    crossed_cand = SpreadCandidate(
        strategy_id="BULL_PUT",
        strategy_name="Bull Put Spread",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        legs=crossed_legs,
        net_credit_pts=19.0,
        strike_width=50.0,
        max_profit_inr=19.0 * 65,
        max_loss_pts=31.0,
        max_loss_inr=31.0 * 65,
        risk_reward_ratio=0.61,
        breakevens=[24381.0],
        is_eligible=True,
        spot_price=24500.0,
    )
    ok_cross, msg_cross, _ = PaperTradingManager.execute_candidate(crossed_cand)
    assert ok_cross is False
    assert "safety gate" in msg_cross.lower() or "crossed" in msg_cross.lower()

    # Test C: Exceeds max trade risk ceiling -> MUST be rejected by PortfolioRiskManager
    huge_loss_cand = SpreadCandidate(
        strategy_id="BULL_PUT",
        strategy_name="Bull Put Spread",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=5,
        quantity=325,
        legs=[
            OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=45.0, ask=46.0, ltp=45.5, quantity=325),
            OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=25.0, ask=26.0, ltp=25.5, quantity=325),
        ],
        net_credit_pts=19.0,
        strike_width=50.0,
        max_profit_inr=19.0 * 325,
        max_loss_pts=31.0,
        max_loss_inr=31.0 * 325,  # 10,075 INR > 5,000 INR limit
        risk_reward_ratio=0.61,
        breakevens=[24381.0],
        is_eligible=True,
        spot_price=24500.0,
    )
    ok_risk, msg_risk, _ = PaperTradingManager.execute_candidate(huge_loss_cand)
    assert ok_risk is False
    assert "portfolio risk" in msg_risk.lower() or "risk limit" in msg_risk.lower()

