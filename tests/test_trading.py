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
        engine_type="INTRADAY",
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
