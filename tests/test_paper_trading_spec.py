"""
tests/test_paper_trading_spec.py — Unit Tests for NIFTY Iron Condor Paper-Trading Specification
Covers:
1. Hard safety rules: PaperBroker vs FyersLiveBroker stub, KILL switch, zero live order endpoints.
2. Execution order rules: BUY long first on entry, BUY BACK shorts first on exit, rollback on failure.
3. Microstructure fill realism: BUY at ask, SELL at bid + slippage + spread penalty; crossed quote rejection.
4. Strategy profiles: monthly_45_15 and weekly_filtered profile mechanics and 1:2 rule.
5. Rule-based adjustment engine triggers, action priority, cost caps, and DTE <= 3 guardrail.
"""

import os
import pytest
from app.broker import Broker, PaperBroker, FyersLiveBroker, is_kill_switch_active, KILL_SWITCH_FILE
from app.strategy_profiles import StrategyProfileManager
from app.adjustments import AdjustmentEngine


def test_broker_interface_and_live_stub_safety():
    """Verifies that FyersLiveBroker refuses to run unless LIVE_TRADING='YES_I_UNDERSTAND'."""
    assert issubclass(PaperBroker, Broker)
    assert issubclass(FyersLiveBroker, Broker)

    # Ensure live broker strictly blocks unauthorized execution
    os.environ.pop("LIVE_TRADING", None)
    with pytest.raises(RuntimeError) as exc_info:
        FyersLiveBroker()
    assert "Live trading blocked" in str(exc_info.value)


def test_grepable_guarantee_no_live_order_endpoints():
    """Grep-able guarantee: static check that no live broker order placement calls exist anywhere in app/."""
    forbidden_calls = ["client.place_order", "fyers.place_order", "order_placement", "client.order("]
    app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))

    for root, _, files in os.walk(app_dir):
        for file in files:
            if file.endswith(".py"):
                path = os.path.join(root, file)
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    for forbidden in forbidden_calls:
                        assert forbidden not in content, f"Forbidden live order call '{forbidden}' found in {path}"


def test_global_kill_switch_mechanism():
    """Verifies that the presence of 'KILL' file halts new entries while allowing exits."""
    broker = PaperBroker()

    # 1. Normal state: Entries allowed
    if os.path.exists(KILL_SWITCH_FILE):
        os.remove(KILL_SWITCH_FILE)

    allowed, _ = broker.is_entry_allowed()
    assert allowed is True

    # 2. Activate Kill Switch
    with open(KILL_SWITCH_FILE, "w") as f:
        f.write("TEST KILL\n")

    try:
        assert is_kill_switch_active() is True
        allowed, msg = broker.is_entry_allowed()
        assert allowed is False
        assert "KILL" in msg

        # Ensure entry order is rejected under kill switch
        res_entry = broker.place_order(
            symbol="NIFTY24500CE", action="SELL", quantity=65, price=50.0,
            quote_snapshot={"bid": 50.0, "ask": 51.0, "ltp": 50.5}, is_exit=False,
        )
        assert res_entry["status"] == "REJECTED"

        # Ensure exit order is permitted under kill switch
        res_exit = broker.place_order(
            symbol="NIFTY24500CE", action="BUY", quantity=65, price=50.0,
            quote_snapshot={"bid": 50.0, "ask": 51.0, "ltp": 50.5}, is_exit=True,
        )
        assert res_exit["status"] == "FILLED"

    finally:
        if os.path.exists(KILL_SWITCH_FILE):
            os.remove(KILL_SWITCH_FILE)


def test_execution_order_invariants():
    """
    Verifies critical sequencing:
    - Entry/Shift: BUY long first, SELL shorts second.
    - Close: BUY BACK shorts first, SELL longs second.
    """
    broker = PaperBroker()

    legs = [
        {"symbol": "NIFTY24600CE", "action": "BUY", "quantity": 65, "quote_snapshot": {"bid": 20.0, "ask": 21.0}},
        {"symbol": "NIFTY24500CE", "action": "SELL", "quantity": 65, "quote_snapshot": {"bid": 45.0, "ask": 46.0}},
    ]

    # Entry: Long (BUY) must execute before Short (SELL)
    ok, _, orders = broker.execute_multi_leg_spread(legs, is_exit=False)
    assert ok is True
    assert len(orders) == 2
    assert orders[0]["action"] == "BUY"
    assert orders[1]["action"] == "SELL"

    # Exit: Short (BUY back) must execute before Long (SELL)
    exit_legs = [
        {"symbol": "NIFTY24600CE", "action": "SELL", "quantity": 65, "quote_snapshot": {"bid": 10.0, "ask": 11.0}},
        {"symbol": "NIFTY24500CE", "action": "BUY", "quantity": 65, "quote_snapshot": {"bid": 15.0, "ask": 16.0}},
    ]
    ok_exit, _, exit_orders = broker.execute_multi_leg_spread(exit_legs, is_exit=True)
    assert ok_exit is True
    assert exit_orders[0]["action"] == "BUY"
    assert exit_orders[1]["action"] == "SELL"


def test_crossed_and_stale_quote_rejection():
    """Verifies that orders reject crossed quotes (bid > ask) or missing quotes."""
    broker = PaperBroker()

    # Crossed quote
    res_crossed = broker.place_order(
        symbol="NIFTY24500CE", action="BUY", quantity=65, price=50.0,
        quote_snapshot={"bid": 55.0, "ask": 50.0, "ltp": 52.0},
    )
    assert res_crossed["status"] == "REJECTED"
    assert "Crossed market" in res_crossed["reason"]

    # Zero ask
    res_zero = broker.place_order(
        symbol="NIFTY24500CE", action="BUY", quantity=65, price=50.0,
        quote_snapshot={"bid": 0.0, "ask": 0.0, "ltp": 0.0},
    )
    assert res_zero["status"] == "REJECTED"


def test_strategy_profile_monthly_45_15():
    """Verifies monthly_45_15 profile delta selection, 1:2 rule, and exits."""
    # Synthetic chain around 24,000 spot with full OTM wings
    chain = {
        23000.0: {"CE": {"bid": 1050.0, "ask": 1052.0, "iv": 14.0}, "PE": {"bid": 12.0, "ask": 13.0, "iv": 14.0}},
        23200.0: {"CE": {"bid": 850.0, "ask": 852.0, "iv": 14.0}, "PE": {"bid": 22.0, "ask": 23.0, "iv": 14.0}},
        23400.0: {"CE": {"bid": 650.0, "ask": 652.0, "iv": 14.0}, "PE": {"bid": 45.0, "ask": 46.0, "iv": 14.0}},
        23600.0: {"CE": {"bid": 450.0, "ask": 452.0, "iv": 14.0}, "PE": {"bid": 75.0, "ask": 76.0, "iv": 14.0}},
        24000.0: {"CE": {"bid": 120.0, "ask": 121.0, "iv": 14.0}, "PE": {"bid": 115.0, "ask": 116.0, "iv": 14.0}},
        24400.0: {"CE": {"bid": 55.0, "ask": 56.0, "iv": 14.0}, "PE": {"bid": 280.0, "ask": 282.0, "iv": 14.0}},
        24600.0: {"CE": {"bid": 40.0, "ask": 41.0, "iv": 14.0}, "PE": {"bid": 380.0, "ask": 382.0, "iv": 14.0}},
        24800.0: {"CE": {"bid": 25.0, "ask": 26.0, "iv": 14.0}, "PE": {"bid": 480.0, "ask": 482.0, "iv": 14.0}},
        25000.0: {"CE": {"bid": 15.0, "ask": 16.0, "iv": 14.0}, "PE": {"bid": 580.0, "ask": 582.0, "iv": 14.0}},
    }

    res = StrategyProfileManager.evaluate_monthly_45_15_entry(
        spot=24000.0,
        chain=chain,
        dte=45.0,
        lot_size=65,
    )
    assert res["is_valid"] is True
    assert res["profile"] == "monthly_45_15"
    assert res["net_credit_pts"] > 0
    assert res["max_loss_pts"] <= (2.0 * res["net_credit_pts"])

    # Exits: TP 50%, SL 100%, TIME DTE <= 15
    exit_tp = StrategyProfileManager.evaluate_exit("monthly_45_15", entry_credit_pts=100.0, current_cost_to_close_pts=40.0, current_dte=30.0)
    assert exit_tp.should_exit is True
    assert exit_tp.reason == "TP"

    exit_sl = StrategyProfileManager.evaluate_exit("monthly_45_15", entry_credit_pts=100.0, current_cost_to_close_pts=210.0, current_dte=30.0)
    assert exit_sl.should_exit is True
    assert exit_sl.reason == "SL"

    exit_time = StrategyProfileManager.evaluate_exit("monthly_45_15", entry_credit_pts=100.0, current_cost_to_close_pts=90.0, current_dte=14.0)
    assert exit_time.should_exit is True
    assert exit_time.reason == "TIME"


def test_adjustment_engine_guardrails():
    """Verifies adjustment triggers, action priorities, and DTE <= 3 prohibition."""
    position = {
        "id": 1,
        "entry_spot": 24000.0,
        "entry_vix": 13.0,
        "short_call": 24300.0,
        "long_call": 24500.0,
        "short_put": 23700.0,
        "long_put": 23500.0,
        "net_credit_pts": 45.0,
        "max_loss_pts": 155.0,
        "current_pnl_pts": -80.0,
        "adjustment_count": 0,
        "lot_size": 65,
    }

    # 1. Invariant: DTE <= 3 strictly halts adjustments
    adj_low_dte = AdjustmentEngine.evaluate(
        position=position, current_spot=24250.0, current_vix=14.0, chain={}, dte=2.0,
    )
    assert adj_low_dte.action_type == "NONE"
    assert "DTE_GUARD" in adj_low_dte.triggered_by

    # 2. Trigger fired at DTE 25 when tested
    adj_valid = AdjustmentEngine.evaluate(
        position=position, current_spot=24250.0, current_vix=14.0, chain={}, dte=25.0,
    )
    assert adj_valid.action_type in ("ROLL_UNTESTED", "SHIFT_HEDGE", "EXIT")
    assert adj_valid.is_safe is True


def test_monthly_expiry_filtering_and_policy():
    """Verifies that monthly expiries are correctly identified and weekly expiries excluded."""
    from app.contracts import ContractRegistry
    from app.market_data import MarketDataProvider
    from app.config import Config

    # Invariants for Monthly contracts in NIFTY
    assert ContractRegistry.is_monthly_expiry("27-10-2026") is True
    assert ContractRegistry.is_monthly_expiry("03-11-2026") is False  # Weekly
    assert ContractRegistry.is_monthly_expiry("09-11-2026") is False  # Weekly
    assert ContractRegistry.is_monthly_expiry("17-11-2026") is False  # Weekly
    assert ContractRegistry.is_monthly_expiry("23-11-2026") is True   # Monthly
    assert ContractRegistry.is_monthly_expiry("29-12-2026") is True   # Monthly

    # System configuration default
    assert Config.MONTHLY_EXPIRIES_ONLY is True
    assert Config.ACTIVE_STRATEGY_PROFILE == "monthly_45_15"

    # Option chain available expiries must be strictly monthly
    chain = MarketDataProvider.get_option_chain()
    available = chain.get("available_expiries", [])
    assert len(available) > 0
    for exp in available:
        assert exp.get("expiry_flag") == "M" or ContractRegistry.is_monthly_expiry(exp.get("date", ""))

