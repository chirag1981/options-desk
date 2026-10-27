"""
tests/test_discipline_layer.py — Comprehensive Unit Tests for Discipline Layer & Video Scenario Replays

Tests:
1. Trade Plan contract creation, formatted plan text, and entry risk <= 2% capital limit.
2. Scenario 1: Hedge removed before a spike (blocked by NO_NAKED_SHORTS).
3. Scenario 2: Roll toward spot before a gap (blocked by NO_CREDIT_SEEKING_ROLLS).
4. Scenario 3: Late expensive hedge followed by a reversal (blocked by NO_EXPENSIVE_LATE_HEDGES).
5. Scenario 4: Holding the last 15% of profit into an expiry-day move (auto-exited by PROFIT_TAKE or TIME_EXIT).
6. Scenario 5: Ignoring the planned loss exit (auto-exited by PLANNED_LOSS_EXIT before max loss).
7. Plan Adherence reporting: actual loss vs planned loss, blocked violations count, and exceeded trades audit.
"""

import pytest
from datetime import datetime
from app.discipline import DisciplineRulesEngine
from app.models import TradePlan, DisciplineViolationLog, TradePosition, PositionLeg, db
from app.broker import PaperBroker
from app.adjustments import AdjustmentEngine
from app.strategies import SpreadCandidate, OptionLegSpec


def create_mock_condor_candidate(
    spot: float = 24500.0,
    short_put: float = 24400.0,
    long_put: float = 24350.0,
    short_call: float = 24600.0,
    long_call: float = 24650.0,
    credit_pts: float = 20.0,
    wing_width: float = 50.0,
    lot_size: int = 65,
    lots: int = 1,
) -> SpreadCandidate:
    """Helper to create standardized 4-leg Iron Condor candidate."""
    max_loss_pts = wing_width - credit_pts
    max_profit_inr = credit_pts * lot_size * lots
    max_loss_inr = max_loss_pts * lot_size * lots

    legs = [
        OptionLegSpec(f"NIFTY{int(short_put)}PE", short_put, "PE", "SELL", 30.0, 31.0, 30.0, lot_size * lots),
        OptionLegSpec(f"NIFTY{int(long_put)}PE", long_put, "PE", "BUY", 10.0, 11.0, 10.0, lot_size * lots),
        OptionLegSpec(f"NIFTY{int(short_call)}CE", short_call, "CE", "SELL", 28.0, 29.0, 28.0, lot_size * lots),
        OptionLegSpec(f"NIFTY{int(long_call)}CE", long_call, "CE", "BUY", 8.0, 9.0, 8.0, lot_size * lots),
    ]

    return SpreadCandidate(
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
        engine_type="POSITIONAL",
        symbol="NIFTY",
        expiry="27-NOV-2026",
        lots=lots,
        quantity=lot_size * lots,
        legs=legs,
        net_credit_pts=credit_pts,
        strike_width=wing_width,
        max_profit_inr=max_profit_inr,
        max_loss_pts=max_loss_pts,
        max_loss_inr=max_loss_inr,
        risk_reward_ratio=round(max_profit_inr / max_loss_inr, 2) if max_loss_inr > 0 else 0.0,
        breakevens=[short_put - credit_pts, short_call + credit_pts],
        is_eligible=True,
        rejection_reasons=[],
        confidence_score=85.0,
        rationale="Standard defined-risk Iron Condor candidate.",
        spot_price=spot,
    )


def test_trade_plan_creation_and_text_format(app_ctx):
    """Verify that a TradePlan is correctly constructed, formatted, and enforces <= 2% risk limit."""
    cand = create_mock_condor_candidate(spot=24500.0, credit_pts=20.0, wing_width=50.0, lot_size=65)
    capital = 1_000_000.0  # ₹10,00,000 capital -> 2% limit is ₹20,000

    # Max loss is 30 pts * 65 = ₹1,950 (0.195% of capital) -> Well within 2% limit
    ok, msg, plan = DisciplineRulesEngine.create_trade_plan(cand, capital=capital)
    assert ok is True
    assert plan is not None
    assert plan.net_credit_pts == 20.0
    assert plan.max_profit_inr == 1300.0
    assert plan.max_loss_inr == 1950.0
    assert plan.max_loss_pct_of_capital < 2.0
    assert plan.profit_take_inr == 975.0       # 75% of ₹1,300
    assert plan.planned_loss_exit_inr == 1170.0 # 60% of ₹1,950

    # Verify thesis break calculations (65% distance covered)
    # Call dist = 24600 - 24500 = 100 -> 24500 + 65 = 24565.0
    # Put dist = 24500 - 24400 = 100 -> 24500 - 65 = 24435.0
    assert plan.thesis_break_level_call == 24565.0
    assert plan.thesis_break_level_put == 24435.0

    # Verify text representation matches required format
    plan_text = plan.to_plan_text()
    assert "Spot / expiry / lot size:" in plan_text
    assert "Legs (short call, long call, short put, long put):" in plan_text
    assert "Net credit (pts and ₹):" in plan_text
    assert "Max profit ₹:" in plan_text
    assert "Max loss ₹:" in plan_text
    assert "Max loss as % of capital:" in plan_text
    assert "Profit-take level (70-80% of max profit):" in plan_text
    assert "Planned loss exit (before max loss):" in plan_text
    assert "Thesis-break level (spot):" in plan_text
    assert "Time exit (days before expiry):" in plan_text
    assert "Hedge legs stay on until the shorts are closed: YES" in plan_text
    assert "Rolls allowed only to reduce risk: YES" in plan_text


def test_reject_entry_exceeding_capital_risk_cap(app_ctx):
    """Verify entries whose max loss exceeds 2% of capital are strictly rejected."""
    # 50 lots = 3250 qty. Max loss = 30 pts * 3250 = ₹97,500 (> 2% of ₹10,00,000 capital)
    cand = create_mock_condor_candidate(spot=24500.0, credit_pts=20.0, wing_width=50.0, lot_size=65, lots=50)
    capital = 1_000_000.0  # 2% limit is ₹20,000

    ok, msg, plan = DisciplineRulesEngine.create_trade_plan(cand, capital=capital)
    assert ok is False
    assert "MAX_LOSS_CAP_EXCEEDED" in msg
    assert plan is None

    # Check violation recorded in ledger
    violation = DisciplineViolationLog.query.filter_by(rule_code="MAX_LOSS_CAP_EXCEEDED").first()
    assert violation is not None
    assert "MAX_LOSS_CAP_EXCEEDED" in violation.rule_code


def test_scenario_1_hedge_removed_before_spike_blocked(app_ctx):
    """
    Scenario 1 Replay: Trader attempts to close/remove protective long hedge before a spike while short is open.
    Rule 1 (NO_NAKED_SHORTS) must strictly reject the sequence.
    """
    broker = PaperBroker(capital=1_000_000.0)

    open_legs = [
        {"symbol": "NIFTY24600CE", "option_type": "CE", "action": "SELL", "quantity": 65},
        {"symbol": "NIFTY24650CE", "option_type": "CE", "action": "BUY", "quantity": 65},
    ]

    # Attempting to SELL the protective long hedge first while short CE is still open
    bad_order_sequence = [
        {"symbol": "NIFTY24650CE", "option_type": "CE", "action": "SELL", "quantity": 65, "price": 10.0},
    ]

    ok, msg, filled = broker.execute_multi_leg_spread(
        legs=bad_order_sequence,
        is_exit=True,
        currently_open_legs=open_legs,
        position_id=101,
    )

    assert ok is False
    assert "NO_NAKED_SHORTS" in msg
    assert len(filled) == 0

    # Verify violation was logged
    v = DisciplineViolationLog.query.filter_by(rule_code="NO_NAKED_SHORTS").first()
    assert v is not None
    assert "Short Call is open" in v.description


def test_scenario_2_roll_toward_spot_before_gap_blocked(app_ctx):
    """
    Scenario 2 Replay: Trader attempts to roll short strike inward toward spot for extra credit before a gap.
    Rule 2 (NO_CREDIT_SEEKING_ROLLS) must strictly reject moving short strikes closer to spot.
    """
    cand = create_mock_condor_candidate(spot=24500.0, short_put=24400.0, long_put=24350.0, short_call=24600.0, long_call=24650.0)
    _, _, plan = DisciplineRulesEngine.create_trade_plan(cand, capital=1_000_000.0)

    # Attempt to roll short call closer to spot (from 24600 down to 24550) when spot is 24500
    ok, msg = DisciplineRulesEngine.validate_roll(
        plan=plan,
        current_spot=24500.0,
        proposed_short_call=24550.0,
        position_id=202,
    )

    assert ok is False
    assert "NO_CREDIT_SEEKING_ROLLS" in msg
    assert "closer to spot" in msg

    # Attempt to roll short put closer to spot (from 24400 up to 24450)
    ok_p, msg_p = DisciplineRulesEngine.validate_roll(
        plan=plan,
        current_spot=24500.0,
        proposed_short_put=24450.0,
        position_id=202,
    )
    assert ok_p is False
    assert "NO_CREDIT_SEEKING_ROLLS" in msg_p


def test_scenario_3_late_expensive_hedge_blocked(app_ctx):
    """
    Scenario 3 Replay: Trader attempts to buy an expensive late hedge (> 25% of credit) or ITM hedge.
    Rule 3 (NO_EXPENSIVE_LATE_HEDGES) must strictly reject the purchase.
    """
    cand = create_mock_condor_candidate(spot=24500.0, credit_pts=20.0) # 25% cap is 5.0 pts
    _, _, plan = DisciplineRulesEngine.create_trade_plan(cand, capital=1_000_000.0)

    # 1. Attempt hedge costing 8.0 pts (> 5.0 pts cap)
    ok, msg = DisciplineRulesEngine.validate_late_hedge(
        plan=plan,
        current_spot=24500.0,
        hedge_strike=24620.0,
        option_type="CE",
        hedge_premium_pts=8.0,
        position_id=303,
    )
    assert ok is False
    assert "NO_EXPENSIVE_LATE_HEDGES" in msg
    assert "exceeds 25% cap" in msg

    # 2. Attempt ITM Call hedge (strike 24450 <= spot 24500)
    ok_itm, msg_itm = DisciplineRulesEngine.validate_late_hedge(
        plan=plan,
        current_spot=24500.0,
        hedge_strike=24450.0,
        option_type="CE",
        hedge_premium_pts=3.0,
        position_id=303,
    )
    assert ok_itm is False
    assert "NO_EXPENSIVE_LATE_HEDGES" in msg_itm
    assert "in the money" in msg_itm

    # 3. Attempt alternating whipsaw hedge (CE -> PE -> CE)
    ok_whip, msg_whip = DisciplineRulesEngine.validate_late_hedge(
        plan=plan,
        current_spot=24500.0,
        hedge_strike=24650.0,
        option_type="CE",
        hedge_premium_pts=2.0,
        recent_hedge_sides=["CE", "PE"],
        position_id=303,
    )
    assert ok_whip is False
    assert "alternating hedging" in msg_whip


def test_scenario_4_profit_take_and_time_exit_auto_enforced(app_ctx):
    """
    Scenario 4 Replay: Position reaches 75% profit target or 1 DTE; holding the last 15% into expiry day is blocked.
    Rule 4 (PROFIT_TAKE) and Rule 7 (TIME_EXIT) enforce automatic exit without manual overriding.
    """
    cand = create_mock_condor_candidate(spot=24500.0, credit_pts=20.0, lot_size=65) # max profit = ₹1,300, profit take = ₹975
    _, _, plan = DisciplineRulesEngine.create_trade_plan(cand, capital=1_000_000.0)

    # 1. Profit reaches ₹980 (>= ₹975 profit take)
    exit_profit = DisciplineRulesEngine.evaluate_discipline_exits(
        position=None,
        plan=plan,
        current_spot=24500.0,
        current_pnl_inr=980.0,
        current_dte=15.0,
    )
    assert exit_profit is not None
    assert exit_profit[0] == "PROFIT_TAKE"
    assert "Profit target of ₹975.00" in exit_profit[1]

    # 2. Position held into 1.0 DTE (<= time_exit_dte 1.0)
    exit_time = DisciplineRulesEngine.evaluate_discipline_exits(
        position=None,
        plan=plan,
        current_spot=24500.0,
        current_pnl_inr=400.0,
        current_dte=0.8,
    )
    assert exit_time is not None
    assert exit_time[0] == "TIME_EXIT"

    # 3. Position held into expiry day with hold_to_expiry=False
    exit_exp = DisciplineRulesEngine.evaluate_discipline_exits(
        position=None,
        plan=plan,
        current_spot=24500.0,
        current_pnl_inr=500.0,
        current_dte=10.0,
        is_expiry_day=True,
    )
    assert exit_exp is not None
    assert exit_exp[0] == "EXPIRY_DAY_EXIT"


def test_scenario_5_planned_loss_exit_and_thesis_break_auto_enforced(app_ctx):
    """
    Scenario 5 Replay: Loss hits planned loss exit (60% of max loss) or thesis breaks early.
    Rule 5 (PLANNED_LOSS_EXIT) and Rule 6 (THESIS_BREAK) enforce immediate exit before max loss.
    """
    cand = create_mock_condor_candidate(spot=24500.0, short_put=24400.0, short_call=24600.0, credit_pts=20.0, wing_width=50.0)
    # Max loss = 30 pts * 65 = ₹1,950. Planned loss exit (60%) = ₹1,170
    _, _, plan = DisciplineRulesEngine.create_trade_plan(cand, capital=1_000_000.0, initial_dte=30.0)

    # 1. Loss reaches -₹1,200 (<= -₹1,170 planned loss exit)
    exit_loss = DisciplineRulesEngine.evaluate_discipline_exits(
        position=None,
        plan=plan,
        current_spot=24580.0,
        current_pnl_inr=-1200.0,
        current_dte=20.0,
    )
    assert exit_loss is not None
    assert exit_loss[0] == "PLANNED_LOSS_EXIT"
    assert "Planned loss limit of ₹1,170.00" in exit_loss[1]

    # 2. Thesis break on Call side (Call thesis level = 24565.0) within first 25% of life (DTE >= 22.5)
    exit_thesis = DisciplineRulesEngine.evaluate_discipline_exits(
        position=None,
        plan=plan,
        current_spot=24570.0, # Breached 24565.0
        current_pnl_inr=-300.0,
        current_dte=26.0,     # Elapsed life is (30-26)/30 = 13.3% <= 25%
    )
    assert exit_thesis is not None
    assert exit_thesis[0] == "THESIS_BREAK"
    assert "Call side" in exit_thesis[1]


def test_plan_adherence_report_generation(app_ctx):
    """Verify Plan Adherence report compiles actual vs planned loss, blocked violations, and adherence rate."""
    # Persist sample TradePosition and TradePlan in DB
    pos = TradePosition(
        engine_type="POSITIONAL",
        strategy_id="IRON_CONDOR",
        strategy_name="Iron Condor",
        symbol="NIFTY",
        expiry="27-NOV-2026",
        entry_time=datetime.now(),
        entry_spot=24500.0,
        entry_net_credit_pts=20.0,
        max_loss_inr=1950.0,
        max_profit_inr=1300.0,
        target_credit_pts=6.0,
        stop_loss_pts=30.0,
        lots=1,
        quantity=65,
        status="CLOSED",
    )
    db.session.add(pos)
    db.session.commit()

    cand = create_mock_condor_candidate(spot=24500.0)
    _, _, plan = DisciplineRulesEngine.create_trade_plan(cand, capital=1_000_000.0)
    plan.position_id = pos.id
    db.session.add(plan)
    db.session.commit()

    # Simulate a closed trade outcome audit
    DisciplineRulesEngine.finalize_trade_outcome(pos.id, net_pnl_inr=-800.0, exit_reason="PLANNED_LOSS_EXIT")

    report = DisciplineRulesEngine.generate_plan_adherence_report()
    assert "total_plans_created" in report
    assert "total_violations_blocked" in report
    assert "plan_adherence_rate_pct" in report
    assert "trade_adherence_records" in report
    assert report["total_plans_created"] >= 1
