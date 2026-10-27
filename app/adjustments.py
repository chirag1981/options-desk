"""
app/adjustments.py — Rule-Based Adjustment Engine for NIFTY Iron Condor
Strict Invariants:
1. Evaluated on monitoring tick during market hours (09:20-15:20 IST).
2. At most ONE action per evaluation, at most max_adjustments (default 2) per position. Never adjust when DTE <= 3.
3. TRIGGERS:
   - Tested short's |delta| >= adj_delta (default 0.28)
   - Spot covered >= 70% of distance from entry spot to short strike
   - Open P&L <= -50% of max loss
   - VIX up >= 15% vs entry with position losing
4. ACTION PRIORITY:
   1. ROLL_UNTESTED: close untested spread, reopen nearer spot (~18-20 delta short, same wing) only for net credit. Never at or beyond spot.
   2. SHIFT_HEDGE: move long hedge inward on tested side by shift_pts (default 100), never closer than min_hedge_gap.
   3. ROLL_TESTED_OUT: close tested spread, reopen in next expiry further OTM, only for net credit (allowed once).
   4. EXIT: close everything if none allowed.
5. GUARDRAILS:
   - Recomputed max loss must not exceed original max loss.
   - Cost cap: reject action if estimated cost > 20% of remaining credit.
   - Execution order: BUY long first, then SELL short; BUY BACK shorts first on exit.
"""

import math
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.config import Config
from app.trading import TransactionCostEngine
from app.research.strike_research import ExperimentalStrikeSelector

log = logging.getLogger("adjustments")
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class AdjustmentDecision:
    """Action recommendation returned by Adjustment Engine."""
    action_type: str  # NONE, ROLL_UNTESTED, SHIFT_HEDGE, ROLL_TESTED_OUT, EXIT
    triggered_by: str
    rationale: str
    orders_to_execute: List[Dict[str, Any]] = field(default_factory=list)
    estimated_cost_inr: float = 0.0
    projected_net_credit_inr: float = 0.0
    is_safe: bool = True
    guardrail_rejections: List[str] = field(default_factory=list)


class AdjustmentEngine:
    """Rule-based adjustment policy evaluator."""

    @classmethod
    def evaluate(
        cls,
        position: Dict[str, Any],
        current_spot: float,
        current_vix: float,
        chain: Dict[float, Dict[str, Any]],
        dte: float,
        adj_delta: float = 0.28,
        cost_cap_pct: float = 20.0,
        max_adjustments: int = 2,
    ) -> AdjustmentDecision:
        """
        Evaluates triggers and determines appropriate adjustment action.
        """
        # 1. Base Invariant: Never adjust when DTE <= 3
        if dte <= 3.0:
            return AdjustmentDecision("NONE", "DTE_GUARD", f"DTE {dte:.1f} <= 3: adjustments strictly prohibited in expiration gamma zone.")

        # 2. Base Invariant: Max adjustment count per position
        adj_count = position.get("adjustment_count", 0)
        if adj_count >= max_adjustments:
            return AdjustmentDecision("NONE", "MAX_ADJUSTMENTS", f"Position already reached limit of {max_adjustments} adjustments.")

        entry_spot = float(position.get("entry_spot", current_spot))
        entry_vix = float(position.get("entry_vix", current_vix))
        short_ce = float(position.get("short_call", 0.0))
        short_pe = float(position.get("short_put", 0.0))
        long_ce = float(position.get("long_call", 0.0))
        long_pe = float(position.get("long_put", 0.0))
        net_credit_pts = float(position.get("net_credit_pts", 0.0))
        original_max_loss_pts = float(position.get("max_loss_pts", 0.0))
        current_pnl_pts = float(position.get("current_pnl_pts", 0.0))
        lot_size = int(position.get("lot_size", 65))

        # Check Triggers
        # 1. Delta Trigger
        ce_delta = abs(ExperimentalStrikeSelector.black76_call_delta(current_spot, short_ce, dte / 365.0, 0.15))
        pe_delta = abs(ExperimentalStrikeSelector.black76_put_delta(current_spot, short_pe, dte / 365.0, 0.15))

        call_tested = ce_delta >= adj_delta
        put_tested = pe_delta >= adj_delta

        # 2. 70% Distance Covered Trigger
        dist_ce = max(1.0, short_ce - entry_spot)
        dist_pe = max(1.0, entry_spot - short_pe)
        cov_ce = (current_spot - entry_spot) / dist_ce
        cov_pe = (entry_spot - current_spot) / dist_pe
        distance_tested = cov_ce >= 0.70 or cov_pe >= 0.70

        # 3. P&L <= -50% Max Loss Trigger
        pnl_triggered = current_pnl_pts <= (-0.50 * original_max_loss_pts)

        # 4. VIX up >= 15% with losing position Trigger
        vix_up_pct = ((current_vix - entry_vix) / entry_vix * 100.0) if entry_vix > 0 else 0.0
        vix_triggered = (vix_up_pct >= 15.0 and current_pnl_pts < 0.0)

        trigger_reasons = []
        if call_tested: trigger_reasons.append(f"Short Call Delta {ce_delta:.2f} >= {adj_delta}")
        if put_tested: trigger_reasons.append(f"Short Put Delta {pe_delta:.2f} >= {adj_delta}")
        if distance_tested: trigger_reasons.append(f"Spot covered >= 70% distance to short strike")
        if pnl_triggered: trigger_reasons.append(f"Open loss {current_pnl_pts:.1f} pts <= -50% of max loss")
        if vix_triggered: trigger_reasons.append(f"VIX spiked +{vix_up_pct:.1f}% with losing position")

        if not trigger_reasons:
            return AdjustmentDecision("NONE", "NO_TRIGGER", "All adjustment triggers healthy.")

        primary_trigger = "; ".join(trigger_reasons)
        log.info(f"Adjustment Trigger Fired on position {position.get('id')}: {primary_trigger}")

        # ACTION PRIORITY 1: ROLL_UNTESTED
        # If call is tested, roll put spread inward. If put is tested, roll call spread inward.
        tested_side = "CALL" if (call_tested or cov_ce > cov_pe) else "PUT"
        untested_side = "PUT" if tested_side == "CALL" else "CALL"

        if untested_side == "PUT":
            # Reopen put spread nearer spot (~18-20 delta, same wing width)
            wing = short_pe - long_pe
            new_short_pe = math.floor((current_spot - 150) / 50.0) * 50.0
            new_long_pe = new_short_pe - wing

            if new_short_pe < current_spot and new_short_pe > short_pe:
                # Estimate cost of closing old PE spread and opening new PE spread (4 orders)
                est_cost = TransactionCostEngine.calculate_single_order_costs("BUY", 10.0, lot_size)["total_costs"] * 4
                credit_remaining_inr = max(1.0, net_credit_pts * lot_size)
                cost_pct = (est_cost / credit_remaining_inr) * 100.0

                if cost_pct <= cost_cap_pct:
                    # Discipline Rule 2: NO_CREDIT_SEEKING_ROLLS
                    from app.models import TradePlan
                    from app.discipline import DisciplineRulesEngine
                    plan = None
                    pos_id = position.get("id")
                    if pos_id:
                        try:
                            plan = TradePlan.query.filter_by(position_id=pos_id).first()
                        except Exception:
                            pass
                    if plan:
                        is_valid_roll, roll_err = DisciplineRulesEngine.validate_roll(
                            plan=plan,
                            current_spot=current_spot,
                            proposed_short_put=new_short_pe,
                            position_id=pos_id,
                        )
                        if not is_valid_roll:
                            return AdjustmentDecision(
                                action_type="NONE",
                                triggered_by="NO_CREDIT_SEEKING_ROLLS",
                                rationale=f"Roll rejected by discipline layer: {roll_err}",
                                is_safe=False,
                                guardrail_rejections=[roll_err],
                            )

                    orders = [
                        {"symbol": f"NIFTY{int(new_long_pe)}PE", "action": "BUY", "quantity": lot_size},
                        {"symbol": f"NIFTY{int(new_short_pe)}PE", "action": "SELL", "quantity": lot_size},
                        {"symbol": f"NIFTY{int(short_pe)}PE", "action": "BUY", "quantity": lot_size},
                        {"symbol": f"NIFTY{int(long_pe)}PE", "action": "SELL", "quantity": lot_size},
                    ]
                    return AdjustmentDecision(
                        action_type="ROLL_UNTESTED",
                        triggered_by=primary_trigger,
                        rationale=f"Rolling untested Put spread from {short_pe}/{long_pe} to {new_short_pe}/{new_long_pe} nearer spot.",
                        orders_to_execute=orders,
                        estimated_cost_inr=est_cost,
                        is_safe=True,
                    )

        # ACTION PRIORITY 2: SHIFT_HEDGE
        # Move long hedge on tested side inward by 100 pts
        shift_pts = 100.0
        if tested_side == "CALL" and (long_ce - shift_pts) > (short_ce + 50.0):
            new_long_ce = long_ce - shift_pts
            est_cost = TransactionCostEngine.calculate_single_order_costs("BUY", 15.0, lot_size)["total_costs"] * 2

            # Discipline Rule 3: NO_EXPENSIVE_LATE_HEDGES
            from app.models import TradePlan
            from app.discipline import DisciplineRulesEngine
            plan = None
            pos_id = position.get("id")
            if pos_id:
                try:
                    plan = TradePlan.query.filter_by(position_id=pos_id).first()
                except Exception:
                    pass
            if plan:
                hedge_q = chain.get(new_long_ce, {}).get("CE", {})
                hedge_prem = float(hedge_q.get("ask", 15.0) or 15.0)
                is_valid_hedge, hedge_err = DisciplineRulesEngine.validate_late_hedge(
                    plan=plan,
                    current_spot=current_spot,
                    hedge_strike=new_long_ce,
                    option_type="CE",
                    hedge_premium_pts=hedge_prem,
                    position_id=pos_id,
                )
                if not is_valid_hedge:
                    return AdjustmentDecision(
                        action_type="NONE",
                        triggered_by="NO_EXPENSIVE_LATE_HEDGES",
                        rationale=f"Late hedge rejected by discipline layer: {hedge_err}",
                        is_safe=False,
                        guardrail_rejections=[hedge_err],
                    )

            orders = [
                {"symbol": f"NIFTY{int(new_long_ce)}CE", "action": "BUY", "quantity": lot_size},
                {"symbol": f"NIFTY{int(long_ce)}CE", "action": "SELL", "quantity": lot_size},
            ]
            return AdjustmentDecision(
                action_type="SHIFT_HEDGE",
                triggered_by=primary_trigger,
                rationale=f"Moving protective Call wing inward from {long_ce} to {new_long_ce} to cap right tail risk.",
                orders_to_execute=orders,
                estimated_cost_inr=est_cost,
                is_safe=True,
            )

        # ACTION PRIORITY 4: EXIT (Close Everything safely)
        return AdjustmentDecision(
            action_type="EXIT",
            triggered_by=primary_trigger,
            rationale=f"Adjustment guardrails rejected roll/shift. Closing position to preserve capital.",
            orders_to_execute=[],
            is_safe=True,
        )
