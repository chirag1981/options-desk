"""
tests/test_risk.py — Unit Tests for PortfolioRiskManager & Hard Limit Enforcements
"""

import pytest
from app import create_app
from app.config import Config
from app.extensions import db
from app.risk import PortfolioRiskManager
from app.strategies import SpreadCandidate, OptionLegSpec


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


def test_zero_naked_options_rejection(app_ctx):
    """Verifies that an uncovered short option is rejected immediately."""
    naked_candidate = SpreadCandidate(
        strategy_id="NAKED_CALL",
        strategy_name="Naked Short Call",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        legs=[
            OptionLegSpec(
                symbol="NIFTY24600CE",
                strike=24600.0,
                option_type="CE",
                action="SELL",
                bid=40.0,
                ask=42.0,
                ltp=41.0,
                quantity=65,
            )
        ],
        net_credit_pts=40.0,
        strike_width=0.0,
        max_profit_inr=2600.0,
        max_loss_pts=100.0,
        max_loss_inr=6500.0,
        risk_reward_ratio=0.4,
        breakevens=[24640.0],
        is_eligible=True,
    )

    res = PortfolioRiskManager.evaluate_candidate_risk(naked_candidate, engine_type="INTRADAY")
    assert res.approved is False
    assert any("naked" in r.lower() for r in res.rejection_reasons)


def test_per_trade_risk_budget_cap(app_ctx):
    """Verifies that trades exceeding 0.5% account equity are blocked."""
    excessive_candidate = SpreadCandidate(
        strategy_id="BULL_PUT",
        strategy_name="Bull Put Spread",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        legs=[
            OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=50.0, ask=52.0, ltp=51.0, quantity=65),
            OptionLegSpec(symbol="NIFTY24200PE", strike=24200.0, option_type="PE", action="BUY", bid=10.0, ask=12.0, ltp=11.0, quantity=65),
        ],
        net_credit_pts=38.0,
        strike_width=200.0,
        max_profit_inr=2470.0,
        max_loss_pts=162.0,
        max_loss_inr=10530.0,  # Exceeds 0.5% of 10 Lakh = ₹5,000!
        risk_reward_ratio=0.23,
        breakevens=[24362.0],
        is_eligible=True,
    )

    res = PortfolioRiskManager.evaluate_candidate_risk(excessive_candidate, engine_type="INTRADAY")
    assert res.approved is False
    assert any("exceeds" in r.lower() for r in res.rejection_reasons)
