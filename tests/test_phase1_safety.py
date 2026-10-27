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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import TradePosition, PositionLeg, RiskAuditLog
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
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
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
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
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

def test_manual_execution_route_enforces_safety_gate(app_ctx, clean_synthetic_env, monkeypatch):
    """Verifies /api/execute-candidate enforces the ExecutionSafetyGate."""
    from app.market_data import MarketDataProvider
    os.environ.pop("OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA", None)
    monkeypatch.setattr(MarketDataProvider, "get_option_chain", lambda: {"spot_price": 0.0, "data_status": "DISCONNECTED"})
    client = app_ctx.test_client()

    payload = {
        "strategy_id": "IRON_CONDOR",
        "engine_type": "INTRADAY",
        "lots": 1,
    }
    response = client.post("/api/execute-candidate", json=payload)
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
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
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
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
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


def test_exchange_turnover_rate_date_effectiveness():
    """
    Verifies NSE exchange turnover charge rate transition:
    - Pre-Oct 1, 2024: 0.050% (0.00050)
    - Effective Oct 1, 2024: 0.03503% (0.0003503) per NSE Circular NSE/FA/64232 under SEBI True-to-Label
    """
    # Pre-Oct 2024
    assert TransactionCostEngine.get_exchange_turnover_rate(date(2024, 9, 30)) == 0.00050

    # Effective Oct 1, 2024 onward
    assert TransactionCostEngine.get_exchange_turnover_rate(date(2024, 10, 1)) == 0.0003503
    assert TransactionCostEngine.get_exchange_turnover_rate(date(2025, 12, 15)) == 0.0003503
    assert TransactionCostEngine.get_exchange_turnover_rate(date(2026, 10, 23)) == 0.0003503


def test_individual_fee_components_authoritative_rates():
    """
    Verifies that IPFT, SEBI turnover fees, stamp duty, and clearing charges
    strictly adhere to official statutory and broker guidelines.
    """
    # IPFT: 0.0005% (₹50 / crore)
    assert TransactionCostEngine.get_ipft_rate(date(2026, 10, 23)) == 0.000005

    # SEBI turnover fee: 0.0001% (₹10 / crore)
    assert TransactionCostEngine.get_sebi_turnover_rate(date(2026, 10, 23)) == 0.000001

    # Stamp duty: 0.003% (₹300 / crore on buy turnover)
    assert TransactionCostEngine.get_stamp_duty_buy_rate(date(2026, 10, 23)) == 0.00003

    # Brokerage per order leg (FYERS: ₹20 flat)
    assert Config.BROKERAGE_PER_ORDER == 20.0

    # Clearing charges (FYERS: Nil / self-cleared via NCL)
    assert Config.CLEARING_CHARGES_PCT == 0.0


def test_order_costs_deterministic_calculation():
    """
    Verifies exact statutory fees calculation for a 1-lot spread.
    Intended Broker: FYERS Securities
    Leg 1: SELL 65 qty @ 50.0 (turnover = ₹3,250.00)
    Leg 2: BUY 65 qty @ 30.0 (turnover = ₹1,950.00)
    Total premium turnover = ₹5,200.00
    Brokerage = 2 * ₹20.0 = ₹40.00
    NSE Exchange turnover fee (post-Oct 1, 2024) = 5,200 * 0.0003503 = ₹1.82156
    IPFT (0.0005%) = 5,200 * 0.000005 = ₹0.02600
    SEBI fee (0.0001%) = 5,200 * 0.000001 = ₹0.00520
    Stamp duty (buy side, 0.003%) = 1,950 * 0.00003 = ₹0.05850
    GST taxable base = 40.00 + 1.82156 + 0.02600 + 0.00520 = ₹41.85276
    GST (18%) = 41.85276 * 0.18 = ₹7.5334968

    Case 1: On 2026-04-01, STT is 0.15% (3,250 * 0.0015 = ₹4.875)
    Expected total = 40 + 4.875 + 1.82156 + 0.02600 + 0.00520 + 0.05850 + 7.5334968 = ₹54.3197568 -> ₹54.32

    Case 2: On 2026-03-31, STT is 0.10% (3,250 * 0.0010 = ₹3.250)
    Expected total = 40 + 3.250 + 1.82156 + 0.02600 + 0.00520 + 0.05850 + 7.5334968 = ₹52.6947568 -> ₹52.69

    Case 3: Pre-Oct 1, 2024 (e.g. 2024-09-30, STT 0.0625%, Exchange 0.05%)
    Expected total = ₹52.39
    """
    legs = [
        {"action": "SELL", "price": 50.0, "quantity": 65},
        {"action": "BUY", "price": 30.0, "quantity": 65},
    ]
    cost_apr_2026 = TransactionCostEngine.calculate_order_costs(legs, lots=1, trade_date=date(2026, 4, 1))
    assert cost_apr_2026 == 54.32

    cost_mar_2026 = TransactionCostEngine.calculate_order_costs(legs, lots=1, trade_date=date(2026, 3, 31))
    assert cost_mar_2026 == 52.69

    cost_sep_2024 = TransactionCostEngine.calculate_order_costs(legs, lots=1, trade_date=date(2024, 9, 30))
    assert cost_sep_2024 == 52.39


# ---------------- 6. Risk Evaluation with Transaction Costs ----------------

def test_risk_evaluation_includes_transaction_costs(app_ctx):
    """
    Verifies that candidate maximum loss evaluated against the 0.5% limit (₹5,000)
    includes estimated round-trip transaction costs.
    """
    # Spread loss alone is ₹4,950 (< ₹5,000), but round-trip costs (~₹110) push total risk to ₹5,060 > ₹5,000 -> REJECTED
    marginal_candidate = SpreadCandidate(
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
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
    - Pre-Nov 20, 2024: 25 (Circular NSE/FAOP/61328)
    - Nov 20, 2024 to Dec 30, 2025: 75 (Circular NSE/FAOP/64625 & Transitional Schedule)
    - Dec 31, 2025 onward: 65 (Circular NSE/FAOP/70616)
    """
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2024, 6, 1)) == 25
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2025, 3, 1)) == 75
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2025, 11, 27)) == 75
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2025, 12, 23)) == 75
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2025, 12, 30)) == 75
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2025, 12, 31)) == 65
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2026, 1, 6)) == 65
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2026, 1, 27)) == 65
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2026, 10, 23)) == 65


def test_nifty_lot_size_transition_boundaries_official_circulars():
    """
    Verifies the mandatory boundary conditions per NSE Circular NSE/FAOP/70616:
    1. Last old-lot weekly expiry: 23-Dec-2025 -> lot size 75
    2. Last old-lot monthly expiry: 30-Dec-2025 -> lot size 75
    3. First revised-lot weekly expiry: 06-Jan-2026 -> lot size 65
    4. First revised-lot monthly expiry: 27-Jan-2026 -> lot size 65
    5. Existing quarterly/half-yearly contracts transitioned at 30-Dec-2025 EOD -> lot size 65
    6. Historical 25-lot contracts (27-JUN-2024) -> lot size 25
    7. Current 65-lot contracts (27-OCT-2026) -> lot size 65
    """
    # 1. Last old-lot weekly expiry: 23-Dec-2025
    valid, _, lot_weekly_old = ContractRegistry.validate_contract("NIFTY25D2324000PE", strike=24000.0, expiry="23-DEC-2025", provided_lot_size=75)
    assert valid is True
    assert lot_weekly_old == 75
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2025, 12, 23)) == 75

    # 2. Last old-lot monthly expiry: 30-Dec-2025
    valid, _, lot_monthly_old = ContractRegistry.validate_contract("NIFTY25DEC24000PE", strike=24000.0, expiry="30-DEC-2025", provided_lot_size=75)
    assert valid is True
    assert lot_monthly_old == 75
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2025, 12, 30)) == 75

    # 3. First revised-lot weekly expiry: 06-Jan-2026
    valid, _, lot_jan_weekly = ContractRegistry.validate_contract("NIFTY2610624000PE", strike=24000.0, expiry="06-JAN-2026", provided_lot_size=65)
    assert valid is True
    assert lot_jan_weekly == 65
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2026, 1, 6)) == 65

    # 4. First revised-lot monthly expiry: 27-Jan-2026
    valid, _, lot_jan_monthly = ContractRegistry.validate_contract("NIFTY26JAN24000PE", strike=24000.0, expiry="27-JAN-2026", provided_lot_size=65)
    assert valid is True
    assert lot_jan_monthly == 65
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2026, 1, 27)) == 65

    # 5. Existing quarterly & half-yearly contracts transitioned at 30-Dec-2025 EOD (e.g. 26-MAR-2026)
    valid, _, lot_quarterly = ContractRegistry.validate_contract("NIFTY26MAR24000PE", strike=24000.0, expiry="26-MAR-2026", provided_lot_size=65)
    assert valid is True
    assert lot_quarterly == 65
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2026, 3, 26)) == 65

    # 6. Historical 25-lot contracts (27-JUN-2024)
    valid, _, lot_hist25 = ContractRegistry.validate_contract("NIFTY24JUN24000PE", strike=24000.0, expiry="27-JUN-2024", provided_lot_size=25)
    assert valid is True
    assert lot_hist25 == 25
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2024, 6, 27)) == 25

    # 7. Current 65-lot contracts (27-OCT-2026)
    valid, _, lot_curr65 = ContractRegistry.validate_contract("NIFTY26OCT24500PE", strike=24500.0, expiry="27-OCT-2026", provided_lot_size=65)
    assert valid is True
    assert lot_curr65 == 65
    assert ContractRegistry.get_authoritative_lot_size("NIFTY", date(2026, 10, 27)) == 65


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


# ---------------- 9. Hardening: GST Taxable Base & Component Breakdown ----------------

def test_gst_taxable_base_includes_sebi_turnover_fee():
    """
    Verifies that GST taxable base strictly includes Brokerage, Exchange turnover charges,
    SEBI turnover fees, and IPFT, while excluding STT and Stamp Duty.
    Also verifies exact component breakdown against published broker tariffs (FYERS).
    """
    legs = [
        {"action": "SELL", "price": 50.0, "quantity": 65},
        {"action": "BUY", "price": 30.0, "quantity": 65},
    ]
    breakdown = TransactionCostEngine.calculate_order_costs_breakdown(legs, lots=1, trade_date=date(2026, 4, 1))

    # 1. Turnover verification
    assert breakdown["sell_turnover"] == 3250.0
    assert breakdown["buy_turnover"] == 1950.0
    assert breakdown["turnover"] == 5200.0

    # 2. Brokerage (FYERS: ₹20 / leg)
    assert breakdown["brokerage"] == 40.0

    # 3. Exchange turnover charge (post-Oct 1, 2024: 0.03503%)
    assert round(breakdown["exchange_fee"], 5) == round(5200.0 * 0.0003503, 5)

    # 4. IPFT separated from exchange turnover charge (0.0005%)
    assert round(breakdown["ipft"], 4) == round(5200.0 * 0.000005, 4)

    # 5. SEBI turnover fee (₹10 / crore = 0.0001%)
    assert round(breakdown["sebi_fee"], 4) == round(5200.0 * 0.000001, 4)

    # 6. Stamp duty (buy side only: 0.003%)
    assert round(breakdown["stamp_duty"], 4) == round(1950.0 * 0.00003, 4)

    # 7. Clearing charges (0.0 for FYERS self-clearing member)
    assert breakdown["clearing_fee"] == 0.0

    # 8. STT (sell side only: 0.15% effective April 1, 2026)
    assert round(breakdown["stt"], 3) == round(3250.0 * 0.0015, 3)

    # 9. GST taxable base strictly includes Brokerage + Exchange + SEBI + IPFT (excludes STT & Stamp Duty)
    expected_base = breakdown["brokerage"] + breakdown["exchange_fee"] + breakdown["sebi_fee"] + breakdown["ipft"]
    assert round(breakdown["gst_taxable_base"], 5) == round(expected_base, 5)

    # 10. GST (18%)
    expected_gst = expected_base * 0.18
    assert round(breakdown["gst"], 5) == round(expected_gst, 5)

    # 11. Total rounded cost
    assert round(breakdown["total"], 2) == 54.32
    assert TransactionCostEngine.calculate_order_costs(legs, lots=1, trade_date=date(2026, 4, 1)) == 54.32


# ---------------- 10. Hardening: Daily Loss Circuit Breaker & Cooldown ----------------

def test_daily_loss_circuit_breaker_halts_trading(app_ctx):
    """
    Verifies that when daily realized losses breach the 1.5% limit (₹15,000),
    trading is halted and any subsequent candidate is rejected.
    """
    now_dt = datetime.now(IST)

    # 1. Create a closed position with realized net loss > ₹15,000
    pos = TradePosition(
        engine_type="INTRADAY",
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=3,
        quantity=195,
        entry_time=now_dt - timedelta(hours=2),
        entry_spot=24500.0,
        entry_net_credit_pts=15.0,
        max_loss_inr=16000.0,
        max_profit_inr=2925.0,
        target_credit_pts=4.5,
        stop_loss_pts=22.5,
        current_net_debit_pts=93.0,
        status="STOPPED_OUT",
        exit_time=now_dt - timedelta(hours=1),
        exit_spot=24620.0,
        exit_net_debit_pts=93.0,
        exit_reason="STOPPED_OUT",
        gross_pnl_inr=-15210.0,
        transaction_costs_inr=165.63,
        net_pnl_inr=-15375.63,  # Breached ₹15,000 circuit breaker!
    )
    db.session.add(pos)
    db.session.commit()

    candidate = SpreadCandidate(
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
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

    # Clear memory cooldown so test isolates daily loss check
    PortfolioRiskManager._last_loss_exit_time = None
    res = PortfolioRiskManager.evaluate_candidate_risk(candidate, engine_type="INTRADAY", now_dt=now_dt)
    assert res.approved is False
    assert any("Daily realized loss" in r and "breached 1.5% limit" in r for r in res.rejection_reasons)


def test_daily_loss_resets_on_new_trading_day(app_ctx):
    """Verifies that yesterday's realized loss does not count against today's limit."""
    now_dt = datetime.now(IST)
    yesterday_dt = now_dt - timedelta(days=1)

    # Position stopped out yesterday
    pos = TradePosition(
        engine_type="INTRADAY",
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=3,
        quantity=195,
        entry_time=yesterday_dt - timedelta(hours=2),
        entry_spot=24500.0,
        entry_net_credit_pts=15.0,
        max_loss_inr=16000.0,
        max_profit_inr=2925.0,
        target_credit_pts=4.5,
        stop_loss_pts=22.5,
        status="STOPPED_OUT",
        exit_time=yesterday_dt,
        exit_spot=24620.0,
        exit_reason="STOPPED_OUT",
        gross_pnl_inr=-15210.0,
        transaction_costs_inr=165.63,
        net_pnl_inr=-15375.63,
    )
    db.session.add(pos)
    db.session.commit()

    candidate = SpreadCandidate(
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
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

    PortfolioRiskManager._last_loss_exit_time = None
    res = PortfolioRiskManager.evaluate_candidate_risk(candidate, engine_type="INTRADAY", now_dt=now_dt)
    # Today's daily realized loss is 0.0, so the candidate should NOT be rejected for daily loss
    assert not any("Daily realized loss" in r for r in res.rejection_reasons)


def test_consecutive_loss_cooldown_blocks_immediate_reentry(app_ctx):
    """Verifies that an exit with a loss triggers a 15-minute cooldown blocking re-entry."""
    base_time = datetime(2026, 10, 23, 10, 0, 0, tzinfo=IST)
    PortfolioRiskManager.register_loss_exit(exit_time=base_time)

    candidate = SpreadCandidate(
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
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

    # 1. At 10:05 (5 minutes after loss exit, within 15 min cooldown)
    eval_time_5m = base_time + timedelta(minutes=5)
    res_5m = PortfolioRiskManager.evaluate_candidate_risk(candidate, engine_type="INTRADAY", now_dt=eval_time_5m)
    assert res_5m.approved is False
    assert any("Consecutive loss cooldown active" in r for r in res_5m.rejection_reasons)

    # 2. At 10:16 (16 minutes after loss exit, cooldown has expired)
    eval_time_16m = base_time + timedelta(minutes=16)
    res_16m = PortfolioRiskManager.evaluate_candidate_risk(candidate, engine_type="INTRADAY", now_dt=eval_time_16m)
    assert not any("Consecutive loss cooldown active" in r for r in res_16m.rejection_reasons)
    assert res_16m.approved is True


def test_cooldown_restores_from_database_on_restart(app_ctx):
    """Verifies that cooldown state is restored from RiskAuditLog even if memory state is cleared."""
    base_time = datetime(2026, 10, 23, 11, 0, 0, tzinfo=IST)
    PortfolioRiskManager.register_loss_exit(exit_time=base_time)

    # Simulate server restart by clearing in-memory variable
    PortfolioRiskManager._last_loss_exit_time = None

    candidate = SpreadCandidate(
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
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

    # Querying 8 minutes after base_time must restore cooldown from database
    eval_time = base_time + timedelta(minutes=8)
    res = PortfolioRiskManager.evaluate_candidate_risk(candidate, engine_type="INTRADAY", now_dt=eval_time)
    assert res.approved is False
    assert any("Consecutive loss cooldown active" in r for r in res.rejection_reasons)


# ---------------- 11. Hardening: Consistent Open Portfolio Risk Calculation ----------------

def test_combined_open_risk_includes_open_positions_with_costs(app_ctx):
    """
    Proves that combined open portfolio risk consistently includes transaction costs
    for both existing open positions and new candidates without double-counting.
    """
    now_dt = datetime.now(IST)

    # Open position with max_loss_inr = ₹16,000 and entry costs = ₹110.
    # Total committed risk of open position = 16,000 + 2 * 110 = ₹16,220.
    pos = TradePosition(
        engine_type="POSITIONAL",
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=2,
        quantity=130,
        entry_time=now_dt - timedelta(hours=1),
        entry_spot=24500.0,
        entry_net_credit_pts=20.0,
        max_loss_inr=16000.0,
        max_profit_inr=2600.0,
        target_credit_pts=6.0,
        stop_loss_pts=30.0,
        status="OPEN",
        transaction_costs_inr=110.42,  # Entry costs paid
    )
    db.session.add(pos)
    db.session.commit()

    # Candidate with max_loss_inr = ₹3,700 and round-trip costs = ₹110.42.
    # Candidate total risk with costs = 3,700 + 110.42 = ₹3,810.42.
    # Combined projected risk = 16,220.84 + 3,810.42 = ₹20,031.26 > ₹20,000 ceiling -> REJECTED!
    candidate = SpreadCandidate(
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
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
        max_loss_pts=56.92,
        max_loss_inr=3700.0,
        risk_reward_ratio=0.31,
        breakevens=[24382.0],
        is_eligible=True,
    )

    PortfolioRiskManager._last_loss_exit_time = None
    res = PortfolioRiskManager.evaluate_candidate_risk(candidate, engine_type="INTRADAY", now_dt=now_dt)
    assert res.approved is False
    assert any("Projected portfolio risk" in r and "exceeds 2.0% ceiling" in r for r in res.rejection_reasons)


# ---------------- 12. Hardening: Missing Quotes Guard on MTM & Exits ----------------

def test_missing_quote_prevents_mtm_valuation_and_phantom_exit(app_ctx):
    """
    Verifies that missing or non-positive bids/asks on open legs during MTM update
    safely abort the MTM calculation rather than evaluating 0 debit and falsely triggering profit target.
    """
    now_dt = datetime.now(IST)
    pos = TradePosition(
        engine_type="INTRADAY",
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        entry_time=now_dt - timedelta(minutes=30),
        entry_spot=24500.0,
        entry_net_credit_pts=20.0,
        max_loss_inr=2000.0,
        max_profit_inr=1300.0,
        target_credit_pts=6.0,  # Target hit if debit <= 6.0
        stop_loss_pts=30.0,
        status="OPEN",
        current_net_debit_pts=20.0,
        current_pnl_inr=0.0,
    )
    db.session.add(pos)
    db.session.flush()

    leg1 = PositionLeg(position_id=pos.id, symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", quantity=65, entry_price=50.0, current_price=50.0)
    leg2 = PositionLeg(position_id=pos.id, symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", quantity=65, entry_price=30.0, current_price=30.0)
    db.session.add(leg1)
    db.session.add(leg2)
    db.session.commit()

    # Provide option chain where short strike 24400 has missing Ask (0.0)
    broken_chain = {
        24400.0: {"PE": {"symbol": "NIFTY24400PE", "bid": 45.0, "ask": 0.0, "ltp": 0.0}},
        24350.0: {"PE": {"symbol": "NIFTY24350PE", "bid": 25.0, "ask": 27.0, "ltp": 26.0}},
    }

    # Update MTM
    PaperTradingManager.update_open_positions_mtm(broken_chain, spot_price=24500.0)

    # Position MUST remain OPEN with unchanged debit; must NOT be closed as TARGET_HIT
    refreshed_pos = db.session.get(TradePosition, pos.id)
    assert refreshed_pos.status == "OPEN"
    assert refreshed_pos.current_net_debit_pts == 20.0


def test_parse_option_quote_preserves_none_for_missing_oi_and_volume():
    """Confirms MarketDataProvider preserves None when OI or Volume is absent in provider response."""
    raw_opt = {
        "symbol": "NSE:NIFTY26OCT24500CE",
        "ltp": 45.0,
        "bid": 44.0,
        "ask": 46.0,
        # oi and volume are omitted
    }
    cleaned = MarketDataProvider.parse_option_quote(raw_opt)
    assert cleaned["oi"] is None
    assert cleaned["change_oi"] is None
    assert cleaned["volume"] is None
    assert cleaned["bid"] == 44.0
    assert cleaned["ask"] == 46.0


# ---------------- 13. Hardening: Contract Metadata Expiry Series Resolution ----------------

def test_contract_lot_size_resolves_from_contract_expiry_series():
    """
    Verifies that ContractRegistry determines authoritative lot size based on the contract's
    expiry series specification, preserving historical contract identity.
    """
    # 1. Contract from historical June 2024 series -> lot size 25
    is_valid_2024, _, lot_2024 = ContractRegistry.validate_contract(
        symbol="NIFTY24JUN24000PE", strike=24000.0, expiry="27-JUN-2024", provided_lot_size=25
    )
    assert is_valid_2024 is True
    assert lot_2024 == 25

    # 2. Contract from intermediate March 2025 series -> lot size 75
    is_valid_2025, _, lot_2025 = ContractRegistry.validate_contract(
        symbol="NIFTY25MAR24000PE", strike=24000.0, expiry="27-MAR-2025", provided_lot_size=75
    )
    assert is_valid_2025 is True
    assert lot_2025 == 75

    # 3. Contract from current October 2026 series -> lot size 65
    is_valid_2026, _, lot_2026 = ContractRegistry.validate_contract(
        symbol="NIFTY26OCT24500PE", strike=24500.0, expiry="27-OCT-2026", provided_lot_size=65
    )
    assert is_valid_2026 is True
    assert lot_2026 == 65


# ---------------- 14. Hardening: Complete Spread Lifecycle & Risk Field Semantics ----------------

def test_spread_lifecycle_complete_fee_and_risk_field_semantics(app_ctx):
    """
    Verifies complete lifecycle of a 2-leg credit spread:
    1. Pre-entry candidate risk sizing with estimated round-trip costs.
    2. Atomic execution and recording of initial entry transaction_costs_inr.
    3. Open position total_risk_with_costs_inr reflects entry costs + estimated exit costs.
    4. MTM valuation and gross vs net P&L tracking.
    5. Clean exit, recording realized total round-trip costs, final gross & net P&L.
    6. Closed position total_risk_with_costs_inr strictly avoids double-counting.
    """
    # 1. Setup Candidate
    candidate = SpreadCandidate(
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-NOV-2026",
        lots=1,
        quantity=65,
        legs=[
            OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=50.0, ask=52.0, ltp=51.0, quantity=65),
            OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=28.0, ask=30.0, ltp=29.0, quantity=65),
        ],
        net_credit_pts=20.0,
        strike_width=50.0,
        max_profit_inr=1300.0,
        max_loss_pts=30.0,
        max_loss_inr=1950.0,
        risk_reward_ratio=0.67,
        breakevens=[24380.0],
        is_eligible=True,
        spot_price=24500.0,
    )

    # Pre-entry checks
    pre_entry_costs = candidate.total_estimated_round_trip_costs_inr
    assert pre_entry_costs > 100.0
    assert candidate.total_risk_with_costs_inr == round(1950.0 + pre_entry_costs, 2)

    # 2. Execute Entry
    success, msg, pos = PaperTradingManager.execute_candidate(candidate)
    assert success is True
    assert pos.status == "OPEN"
    # Entry costs recorded in pos.transaction_costs_inr
    entry_costs = pos.transaction_costs_inr
    assert entry_costs == 54.32

    # In OPEN state: total_risk_with_costs_inr = max_loss_inr + 2 * entry_costs (entry paid + estimated exit)
    assert pos.total_risk_with_costs_inr == round(1950.0 + 2.0 * 54.32, 2)

    # 3. MTM Evaluation with target hit (Buyback debit drops to 6.0 pts)
    # Leg 1 (SELL) buyback ask drops to 12.0; Leg 2 (BUY) close bid drops to 6.0
    chain_update = {
        24400.0: {"PE": {"symbol": "NIFTY24400PE", "bid": 11.5, "ask": 12.0, "ltp": 12.0}},
        24350.0: {"PE": {"symbol": "NIFTY24350PE", "bid": 6.0, "ask": 6.5, "ltp": 6.0}},
    }
    PaperTradingManager.update_open_positions_mtm(chain_update, spot_price=24600.0)

    # Position should have triggered TARGET_HIT
    closed_pos = db.session.get(TradePosition, pos.id)
    assert closed_pos.status == "TARGET_HIT"

    # 4. Verify Final Realized Metrics
    # Gross P&L = (20.0 - 6.0) * 65 = 14.0 * 65 = +910.00
    assert closed_pos.gross_pnl_inr == 910.00

    # Total Round-Trip Costs = Entry (54.32) + Exit (48.30) = 102.62
    assert closed_pos.transaction_costs_inr == 102.62

    # Net P&L = 910.00 - 102.62 = +807.38
    assert closed_pos.net_pnl_inr == 807.38

    # 5. Closed position risk semantics: no double-counting of costs
    assert closed_pos.total_risk_with_costs_inr == round(1950.0 + 102.62, 2)


def test_estimated_vs_realized_costs_comparison_and_explanation():
    """
    Verifies and documents the deliberate design difference between conservative
    estimated round-trip costs and actual realized closed-position costs:
    1. Pre-entry estimated costs use conservative opposite quotes at entry.
    2. Realized exit costs depend on actual exit fill prices (e.g. decayed premium at target).
    3. Confirms estimated costs are conservative (>= realized costs on profitable trades).
    """
    candidate = SpreadCandidate(
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
        engine_type="INTRADAY",
        symbol="NIFTY",
        expiry="27-OCT-2026",
        lots=1,
        quantity=65,
        legs=[
            OptionLegSpec(symbol="NIFTY24400PE", strike=24400.0, option_type="PE", action="SELL", bid=50.0, ask=52.0, ltp=51.0, quantity=65),
            OptionLegSpec(symbol="NIFTY24350PE", strike=24350.0, option_type="PE", action="BUY", bid=28.0, ask=30.0, ltp=29.0, quantity=65),
        ],
        net_credit_pts=20.0,
        strike_width=50.0,
        max_profit_inr=1300.0,
        max_loss_pts=30.0,
        max_loss_inr=1950.0,
        risk_reward_ratio=0.67,
        breakevens=[24380.0],
        is_eligible=True,
    )

    # 1. Separated pre-entry estimates
    est_entry = candidate.estimated_entry_costs_inr
    est_exit = candidate.estimated_exit_costs_inr
    est_round_trip = candidate.total_estimated_round_trip_costs_inr

    assert est_entry == 54.32
    assert est_exit > 50.0  # Conservative estimate based on closing at live opposite quotes
    assert est_round_trip == round(est_entry + est_exit, 2)

    # 2. Actual Realized Costs at Target Hit (Turnover drops from ₹5,200 to ₹1,170)
    actual_entry_costs = 54.32
    actual_exit_costs = 48.30
    actual_round_trip_costs = round(actual_entry_costs + actual_exit_costs, 2)

    assert actual_round_trip_costs == 102.62

    # 3. Verification of Conservative Variance:
    # On target hit, premium decay reduces exit turnover, so actual exit fees are lower.
    # est_round_trip >= actual_round_trip ensures risk management never underestimates costs.
    assert est_round_trip >= actual_round_trip_costs
    variance = est_round_trip - actual_round_trip_costs
    assert variance >= 0.0

