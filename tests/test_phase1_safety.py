"""
tests/test_phase1_safety.py — Comprehensive Acceptance Test Suite for Phase 1
Verifies:
1. Reconciled Account Equity (₹10 Lakhs) & Derived Rupee Limits.
2. Authoritative ExecutionSafetyGate across all entry paths.
3. Test Isolation Flags (TESTING=True AND OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA=1).
4. Zero Quote Fabrication, Rejection of Crossed Markets and Missing Bids/Asks.
5. Date-Effective Statutory STT & Transaction Costs (Before vs. After April 1, 2026).
6. Total Risk Evaluation including estimated round-trip transaction costs.
7. Contract Metadata Registry (Versioned Lot Sizes: 25 -> 75 -> 65, Strike Validation).
8. Strict Non-Negotiable Invariants (Paper-only, Iron Butterfly disabled, no live orders).
"""

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo
import pytest

from app import create_app
from app.config import Config
from app.extensions import db
from app.contracts import ContractRegistry
from app.trading import TransactionCostEngine, PaperTradingManager, AutoTradeEngine
from app.safety import ExecutionSafetyGate
from app.risk import PortfolioRiskManager
from app.analysis import MarketAnalyzer, MarketAnalysisResult
from app.performance import PerformanceAnalytics
from app.strategies import StrategyBuilder, SpreadCandidate, OptionLegSpec
from app.market_data import MarketDataProvider

IST = ZoneInfo("Asia/Kolkata")


class Phase1TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


@pytest.fixture
def app_ctx():
    """Provides isolated in-memory test database and application context."""
    app = create_app(Phase1TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def clean_synthetic_env():
    """Ensures test isolation environment variables are cleanly managed."""
    original_val = os.environ.get("OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA")
    yield
    if original_val is None:
        os.environ.pop("OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA", None)
    else:
        os.environ["OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA"] = original_val


# ---------------- 1. Account Equity & Risk Limits ----------------

def test_account_equity_and_rupee_limits():
    """Verifies that ₹10 Lakh equity derives exact approved rupee risk ceilings."""
    assert Config.TOTAL_ACCOUNT_EQUITY == 1000000.0

    # 0.5% max risk per trade -> ₹5,000
    assert Config.get_max_risk_per_trade_inr() == 5000.0

    # 2.0% max combined open risk -> ₹20,000
    assert Config.get_max_combined_portfolio_risk_inr() == 20000.0

    # 1.5% daily drawdown circuit breaker -> ₹15,000
    assert Config.get_max_daily_loss_inr() == 15000.0

    # Capital allocations (50% / 50%)
    assert Config.INTRADAY_CAPITAL_ALLOCATION == 500000.0
    assert Config.POSITIONAL_CAPITAL_ALLOCATION == 500000.0


def test_portfolio_summary_contains_authoritative_limits(app_ctx):
    """Verifies that PerformanceAnalytics exposes exact derived monetary ceilings."""
    summary = PerformanceAnalytics.get_portfolio_summary()
    assert summary["total_account_equity"] == 1000000.0
    assert summary["max_trade_risk_inr"] == 5000.0
    assert summary["max_portfolio_risk_inr"] == 20000.0
    assert summary["daily_loss_circuit_inr"] == 15000.0


# ---------------- 2. Execution Safety Gate & Test Isolation ----------------

def build_test_chain(spot=24500.0, data_status="TEST_MOCK"):
    chain = {}
    atm = round(spot / 50.0) * 50.0
    for i in range(-5, 6):
        stk = atm + (i * 50.0)
        chain[stk] = {
            "CE": {"symbol": f"NIFTY{int(stk)}CE", "ltp": 50.0, "bid": 49.0, "ask": 51.0, "oi": 20000, "change_oi": 2000, "iv": 14.5},
            "PE": {"symbol": f"NIFTY{int(stk)}PE", "ltp": 50.0, "bid": 49.0, "ask": 51.0, "oi": 20000, "change_oi": 2000, "iv": 15.0},
        }
    return {
        "spot_price": spot,
        "spot_change_pct": 0.45,
        "expiry": "27-OCT-2026",
        "chain_by_strike": chain,
        "data_status": data_status,
    }


def test_safety_gate_rejects_simulated_stream_without_isolation(app_ctx, clean_synthetic_env):
    """Simulated/demo stream is rejected when isolation flag is unset."""
    os.environ.pop("OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA", None)

    cand = SpreadCandidate(
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
        max_profit_inr=1170.0,
        max_loss_pts=32.0,
        max_loss_inr=2080.0,
        risk_reward_ratio=0.56,
        breakevens=[24382.0],
        is_eligible=True,
    )

    chain_sim = build_test_chain(data_status="SIMULATED_DEMO")
    analysis_sim = MarketAnalyzer.analyze_chain(chain_sim)

    # Must be rejected because data_status is SIMULATED_DEMO and isolation flag is not set
    is_safe, msg = ExecutionSafetyGate.validate_execution(cand, analysis_sim)
    assert is_safe is False
    assert "Live FYERS market data stream required" in msg


def test_safety_gate_allows_synthetic_only_under_both_flags(app_ctx, clean_synthetic_env):
    """Synthetic test data is strictly isolated: requires app.config['TESTING'] AND env var."""
    os.environ["OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA"] = "1"

    cand = SpreadCandidate(
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
        max_profit_inr=1170.0,
        max_loss_pts=32.0,
        max_loss_inr=2080.0,
        risk_reward_ratio=0.56,
        breakevens=[24382.0],
        is_eligible=True,
    )

    chain_test = build_test_chain(data_status="TEST_MOCK")
    analysis_test = MarketAnalyzer.analyze_chain(chain_test)

    # When both TESTING=True and OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA=1
    is_safe, msg = ExecutionSafetyGate.validate_execution(cand, analysis_test)
    assert is_safe is True

    # If env var is disabled, it must fail even if TESTING=True
    os.environ["OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA"] = "0"
    is_safe, msg = ExecutionSafetyGate.validate_execution(cand, analysis_test)
    assert is_safe is False


# ---------------- 3. Execution Paths Rejection of Invalid / Simulated Data ----------------

def test_manual_execution_route_enforces_safety_gate(app_ctx, clean_synthetic_env):
    """Verifies /api/execute-candidate enforces the ExecutionSafetyGate."""
    os.environ.pop("OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA", None)
    client = app_ctx.test_client()

    payload = {
        "strategy_id": "BULL_PUT",
        "engine_type": "INTRADAY",
        "lots": 1,
    }
    response = client.post("/api/execute-candidate", json=payload)
    # Since live FYERS stream is not active in this test runner, it must reject with 400
    assert response.status_code == 400
    data = response.get_json()
    assert data["status"] == "error"


def test_auto_trade_paths_enforce_safety_gate(app_ctx, clean_synthetic_env):
    """Verifies both execute_all_selected and process_auto_trades enforce safety gate."""
    os.environ.pop("OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA", None)

    chain_sim = build_test_chain(data_status="SIMULATED_DEMO")
    analysis_sim = MarketAnalyzer.analyze_chain(chain_sim)

    # 1. execute_all_selected
    executed, msg = AutoTradeEngine.execute_all_selected(analysis_sim, {})
    assert len(executed) == 0

    # 2. process_auto_trades
    AutoTradeEngine.set_enabled(True)
    executed_trades = AutoTradeEngine.process_auto_trades(analysis_sim, {})
    assert len(executed_trades) == 0


# ---------------- 4. Quotes Validation & Zero Price Fabrication ----------------

def test_missing_bid_blocks_candidate(app_ctx, clean_synthetic_env):
    """A missing or zero bid on a short leg blocks the candidate."""
    os.environ["OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA"] = "1"

    cand = SpreadCandidate(
        strategy_id="BULL_PUT",
        strategy_name="Bull Put Spread",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        legs=[
            OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=0.0, ask=52.0, ltp=51.0, quantity=65),
            OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=30.0, ask=32.0, ltp=31.0, quantity=65),
        ],
        net_credit_pts=18.0,
        strike_width=50.0,
        max_profit_inr=1170.0,
        max_loss_pts=32.0,
        max_loss_inr=2080.0,
        risk_reward_ratio=0.56,
        breakevens=[24382.0],
        is_eligible=True,
    )
    analysis = MarketAnalyzer.analyze_chain(build_test_chain(data_status="TEST_MOCK"))

    is_safe, msg = ExecutionSafetyGate.validate_execution(cand, analysis)
    assert is_safe is False
    assert "Missing or non-positive executable Bid" in msg


def test_crossed_market_rejection(app_ctx, clean_synthetic_env):
    """Crossed markets (bid >= ask) are rejected."""
    os.environ["OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA"] = "1"

    cand = SpreadCandidate(
        strategy_id="BULL_PUT",
        strategy_name="Bull Put Spread",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        legs=[
            OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=55.0, ask=50.0, ltp=52.0, quantity=65), # bid > ask!
            OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=30.0, ask=32.0, ltp=31.0, quantity=65),
        ],
        net_credit_pts=18.0,
        strike_width=50.0,
        max_profit_inr=1170.0,
        max_loss_pts=32.0,
        max_loss_inr=2080.0,
        risk_reward_ratio=0.56,
        breakevens=[24382.0],
        is_eligible=True,
    )
    analysis = MarketAnalyzer.analyze_chain(build_test_chain(data_status="TEST_MOCK"))

    is_safe, msg = ExecutionSafetyGate.validate_execution(cand, analysis)
    assert is_safe is False
    assert "Crossed or inverted market" in msg


def test_zero_price_fabrication_in_market_data_cleaning():
    """Confirms MarketDataProvider does not fabricate synthetic bid/ask offsets from LTP."""
    raw_opt = {
        "symbol": "NSE:NIFTY26OCT24500CE",
        "ltp": 45.0,
        "bid": 0.0,  # Missing
        "ask": 0.0,  # Missing
        "oi": 150000,
        "volume": 25000,
    }
    cleaned = MarketDataProvider.parse_option_quote(raw_opt)
    # Must NOT fabricate bid = 44.0 or ask = 46.0!
    assert cleaned["bid"] == 0.0
    assert cleaned["ask"] == 0.0
    assert cleaned["ltp"] == 45.0


# ---------------- 5. Date-Effective STT & Fee Engine ----------------

def test_stt_rate_date_effectiveness():
    """
    Verifies statutory STT rate on option sales:
    - Pre-Oct 1, 2024: 0.0625%
    - Oct 1, 2024 to March 31, 2026: 0.10% (Finance Act 2024)
    - April 1, 2026 onward: 0.15% (Union Budget 2026 / NSE circular)
    """
    # Pre-Oct 2024
    assert TransactionCostEngine.get_stt_sell_rate(date(2024, 9, 30)) == 0.000625

    # Intermediate regime (0.10%)
    assert TransactionCostEngine.get_stt_sell_rate(date(2024, 10, 1)) == 0.001000
    assert TransactionCostEngine.get_stt_sell_rate(date(2025, 12, 15)) == 0.001000
    assert TransactionCostEngine.get_stt_sell_rate(date(2026, 3, 31)) == 0.001000

    # April 1, 2026 onward (0.15%)
    assert TransactionCostEngine.get_stt_sell_rate(date(2026, 4, 1)) == 0.001500
    assert TransactionCostEngine.get_stt_sell_rate(date(2026, 10, 23)) == 0.001500


def test_order_costs_deterministic_calculation():
    """
    Verifies exact statutory fees calculation for a 1-lot spread.
    Leg 1: SELL 65 qty @ 50.0 (turnover = 3250)
    Leg 2: BUY 65 qty @ 30.0 (turnover = 1950)
    Total premium turnover = 5200.
    Brokerage = 2 * 20 = 40.
    Exchange turnover = 5200 * 0.0005 = 2.60.
    GST (18% on Brokerage + Exchange) = 0.18 * 42.60 = 7.668.
    SEBI = 5200 * 0.000001 = 0.0052.
    Stamp duty (buy side) = 1950 * 0.00003 = 0.0585.
    STT (sell side) on 2026-04-01: 3250 * 0.0015 = 4.875.
    Expected total = 40 + 2.60 + 7.668 + 0.0052 + 0.0585 + 4.875 = 55.2067 -> 55.21.
    """
    legs = [
        {"action": "SELL", "price": 50.0, "quantity": 65},
        {"action": "BUY", "price": 30.0, "quantity": 65},
    ]
    cost_apr_2026 = TransactionCostEngine.calculate_order_costs(legs, lots=1, trade_date=date(2026, 4, 1))
    assert cost_apr_2026 == 55.21

    # On 2026-03-31, STT is 0.10% (3250 * 0.001 = 3.25)
    # Expected total = 40 + 2.60 + 7.668 + 0.0052 + 0.0585 + 3.25 = 53.5817 -> 53.58.
    cost_mar_2026 = TransactionCostEngine.calculate_order_costs(legs, lots=1, trade_date=date(2026, 3, 31))
    assert cost_mar_2026 == 53.58


# ---------------- 6. Risk Evaluation with Transaction Costs ----------------

def test_risk_evaluation_includes_transaction_costs(app_ctx):
    """
    Verifies that candidate maximum loss evaluated against the 0.5% limit (₹5,000)
    includes estimated round-trip transaction costs.
    """
    # Spread loss alone is ₹4,950 (< ₹5,000), but round-trip costs (~₹110) push total risk to ₹5,060 > ₹5,000 -> REJECTED
    marginal_candidate = SpreadCandidate(
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
        max_profit_inr=1170.0,
        max_loss_pts=76.15,
        max_loss_inr=4950.0,  # Theoretical spread max loss is under 5000
        risk_reward_ratio=0.56,
        breakevens=[24382.0],
        is_eligible=True,
    )

    eval_res = PortfolioRiskManager.evaluate_candidate_risk(marginal_candidate, engine_type="INTRADAY")
    assert eval_res.approved is False
    assert any("exceeds 0.5% limit" in r for r in eval_res.rejection_reasons)


# ---------------- 7. Contract Registry & Dynamic Lot Sizes ----------------

def test_contract_registry_versioned_lot_sizes():
    """
    Verifies authoritative NIFTY lot sizes across official NSE historical revision dates:
    - Pre-Nov 20, 2024: 25
    - Nov 20, 2024 to Oct 27, 2025: 75
    - Oct 28, 2025 onward: 65 (Circular NSE/FAOP/70616)
    """
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2024, 6, 1)) == 25
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2025, 3, 1)) == 75
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2026, 10, 23)) == 65


def test_contract_validation_rejects_invalid_strike_or_lot():
    """Contract validation rejects invalid non-step strikes or lot size mismatches."""
    # Invalid strike: 24523.5 is not a multiple of 50.0
    valid, msg, _ = ContractRegistry.validate_contract("NIFTY24523.5PE", strike=24523.5, expiry="27-OCT-2026", provided_lot_size=65)
    assert valid is False
    assert "Invalid strike" in msg

    # Invalid lot size: provided 50 instead of 65
    valid, msg, _ = ContractRegistry.validate_contract("NIFTY24500PE", strike=24500.0, expiry="27-OCT-2026", provided_lot_size=50)
    assert valid is False
    assert "does not match authoritative NSE lot size" in msg


# ---------------- 8. Invariants Preservation ----------------

def test_iron_butterfly_remains_strictly_disabled():
    """Iron Butterfly active entry must remain completely disabled."""
    analysis = MarketAnalyzer.analyze_chain(build_test_chain(data_status="TEST_MOCK"))
    ib_cand = StrategyBuilder.build_iron_butterfly(analysis, {})
    assert ib_cand.is_eligible is False
    assert any("Iron Butterfly active entry is disabled" in r for r in ib_cand.rejection_reasons)
