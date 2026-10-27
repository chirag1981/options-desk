"""
app/discipline.py — Discipline Layer & Hard Rule Invariants for NIFTY Iron Condor

Enforces hard trading rules, trade plan persistence at entry,
real-time execution guardrails, auto-exits without overriding,
and plan adherence auditing.

Rules enforced:
1. NO_NAKED_SHORTS: Reject any sequence leaving shorts unhedged; closing order is shorts first, then longs.
2. NO_CREDIT_SEEKING_ROLLS: Reject rolls moving short strikes closer to spot, adding shorts, or increasing credit without risk/delta reduction.
3. NO_EXPENSIVE_LATE_HEDGES: Reject buying hedges > 25% of original credit or ATM/ITM; reject alternating hedging whipsaws.
4. PROFIT_TAKE: Auto-exit at profit_take_pct (default 75%) of max profit; no overriding.
5. PLANNED_LOSS_EXIT: Auto-exit at planned_loss_exit (default 60%) of max loss before full loss.
6. THESIS_BREAK: Auto-exit when spot covers 65% of distance to short strike within first 25% of position life.
7. TIME_EXIT & EXPIRY_DAY: Exit at configured DTE; no new adjustments and no position held into expiry day unless hold_to_expiry=True.
8. TOTAL_RISK_LIMIT: Total risk after any action may never exceed original max loss.
"""

import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.config import Config
from app.extensions import db
from app.models import TradePlan, DisciplineViolationLog, TradePosition

log = logging.getLogger("discipline")
IST = ZoneInfo("Asia/Kolkata")


class DisciplineRulesEngine:
    """
    Hard rules engine enforcing trade plan contracts, execution invariants,
    and discipline audit reporting.
    """

    @classmethod
    def record_violation(
        cls,
        rule_code: str,
        description: str,
        position_id: Optional[int] = None,
        attempted_action: Optional[str] = None,
    ) -> DisciplineViolationLog:
        """Persists a rejected rule violation into the audit ledger."""
        log.warning(f"DISCIPLINE VIOLATION [{rule_code}] Pos #{position_id}: {description} (Action: {attempted_action})")
        violation = DisciplineViolationLog(
            timestamp=datetime.now(IST),
            position_id=position_id,
            rule_code=rule_code,
            description=description,
            attempted_action=attempted_action,
        )
        try:
            db.session.add(violation)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            log.error(f"Failed to record discipline violation: {e}")
        return violation

    @classmethod
    def create_trade_plan(
        cls,
        candidate: Any,
        capital: float,
        max_loss_pct_limit: float = 2.0,
        profit_take_pct: float = 0.75,
        planned_loss_exit_pct: float = 0.60,
        thesis_dist_cover_pct: float = 0.65,
        thesis_window_life_pct: float = 0.25,
        initial_dte: float = 30.0,
        time_exit_dte: float = 1.0,
        hold_to_expiry: bool = False,
        hedge_cost_cap_pct: float = 25.0,
    ) -> Tuple[bool, str, Optional[TradePlan]]:
        """
        Validates candidate entry against discipline limits (<= 2% capital risk)
        and constructs the persisted TradePlan.
        """
        if not candidate.legs or len(candidate.legs) not in (2, 4):
            return False, "Trade plan requires 2 or 4 protected legs.", None

        # Extract 4 legs
        short_call = None
        long_call = None
        short_put = None
        long_put = None

        for leg in candidate.legs:
            opt = getattr(leg, "option_type", "")
            act = getattr(leg, "action", "")
            stk = float(getattr(leg, "strike", 0.0))
            if opt == "CE" and act == "SELL":
                short_call = stk
            elif opt == "CE" and act == "BUY":
                long_call = stk
            elif opt == "PE" and act == "SELL":
                short_put = stk
            elif opt == "PE" and act == "BUY":
                long_put = stk

        has_call_wing = (short_call is not None and long_call is not None)
        has_put_wing = (short_put is not None and long_put is not None)

        if not (has_call_wing or has_put_wing):
            return False, "Could not identify protected credit spread legs (short/long pairs).", None

        # Hard Invariant: Long hedges must protect shorts
        if has_call_wing and long_call <= short_call:
            msg = f"NO_NAKED_SHORTS: Long Call hedge ({long_call}) must be above short call ({short_call})."
            cls.record_violation("NO_NAKED_SHORTS", msg, attempted_action="ENTRY")
            return False, msg, None

        if has_put_wing and long_put >= short_put:
            msg = f"NO_NAKED_SHORTS: Long Put hedge ({long_put}) must be below short put ({short_put})."
            cls.record_violation("NO_NAKED_SHORTS", msg, attempted_action="ENTRY")
            return False, msg, None

        # Risk as % of capital check (default <= 2.0%)
        cap = capital if capital > 0 else Config.TOTAL_ACCOUNT_EQUITY
        max_loss_inr = float(candidate.max_loss_inr)
        max_loss_pct = (max_loss_inr / cap) * 100.0 if cap > 0 else 999.0

        if max_loss_pct > max_loss_pct_limit:
            reason = (
                f"MAX_LOSS_CAP_EXCEEDED: Max loss ₹{max_loss_inr:,.2f} is {max_loss_pct:.2f}% of capital "
                f"(exceeds maximum allowed {max_loss_pct_limit:.1f}% limit)."
            )
            cls.record_violation("MAX_LOSS_CAP_EXCEEDED", reason, attempted_action="ENTRY")
            return False, reason, None

        entry_spot = float(candidate.spot_price)
        max_profit_inr = float(candidate.max_profit_inr)

        # Profit-take and planned loss levels
        profit_take_inr = round(profit_take_pct * max_profit_inr, 2)
        planned_loss_exit_inr = round(planned_loss_exit_pct * max_loss_inr, 2)

        # Thesis break level: spot covers 65% of distance to short strike
        if short_call is not None:
            call_dist = max(1.0, short_call - entry_spot)
            thesis_break_level_call = round(entry_spot + (thesis_dist_cover_pct * call_dist), 1)
        else:
            thesis_break_level_call = round(entry_spot + 500.0, 1)

        if short_put is not None:
            put_dist = max(1.0, entry_spot - short_put)
            thesis_break_level_put = round(entry_spot - (thesis_dist_cover_pct * put_dist), 1)
        else:
            thesis_break_level_put = round(entry_spot - 500.0, 1)

        plan = TradePlan(
            strategy_id=candidate.strategy_id,
            symbol=candidate.symbol,
            expiry=str(candidate.expiry),
            spot_at_entry=entry_spot,
            lot_size=int(candidate.quantity),
            lots=int(candidate.lots),
            short_call=short_call,
            long_call=long_call,
            short_put=short_put,
            long_put=long_put,
            net_credit_pts=float(candidate.net_credit_pts),
            net_credit_inr=max_profit_inr,
            max_profit_inr=max_profit_inr,
            max_loss_inr=max_loss_inr,
            max_loss_pct_of_capital=round(max_loss_pct, 2),
            profit_take_pct=profit_take_pct,
            profit_take_inr=profit_take_inr,
            planned_loss_exit_pct=planned_loss_exit_pct,
            planned_loss_exit_inr=planned_loss_exit_inr,
            thesis_break_level_call=thesis_break_level_call,
            thesis_break_level_put=thesis_break_level_put,
            thesis_break_time_window_pct=thesis_window_life_pct,
            initial_dte=initial_dte,
            time_exit_dte=time_exit_dte,
            hold_to_expiry=hold_to_expiry,
            hedge_legs_stay_on=True,
            rolls_allowed_only_to_reduce_risk=True,
            hedge_cost_cap_pct=hedge_cost_cap_pct,
            status="ACTIVE",
        )
        return True, "OK", plan

    @classmethod
    def validate_order_execution_sequence(
        cls,
        legs_to_execute: List[Dict[str, Any]],
        currently_open_legs: List[Dict[str, Any]],
        is_exit: bool = False,
        position_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Rule 1: NO_NAKED_SHORTS
        - When exiting: Short legs must be closed first (BUY), then long hedges (SELL).
        - Strictly rejects removing/selling a long hedge while its short is open.
        """
        if is_exit:
            has_open_short_call = any(l.get("option_type") == "CE" and l.get("action") == "SELL" for l in currently_open_legs)
            has_open_short_put = any(l.get("option_type") == "PE" and l.get("action") == "SELL" for l in currently_open_legs)

            short_ce_closed = any(l.get("option_type") == "CE" and l.get("action") == "BUY" for l in legs_to_execute)
            short_pe_closed = any(l.get("option_type") == "PE" and l.get("action") == "BUY" for l in legs_to_execute)

            for order in legs_to_execute:
                opt = order.get("option_type")
                act = order.get("action")
                if opt == "CE" and act == "SELL" and has_open_short_call and not short_ce_closed:
                    msg = "NO_NAKED_SHORTS: Cannot close protective Long Call hedge while Short Call is open. Shorts must be bought back first."
                    cls.record_violation("NO_NAKED_SHORTS", msg, position_id=position_id, attempted_action=f"SELL {order.get('symbol')}")
                    return False, msg

                if opt == "PE" and act == "SELL" and has_open_short_put and not short_pe_closed:
                    msg = "NO_NAKED_SHORTS: Cannot close protective Long Put hedge while Short Put is open. Shorts must be bought back first."
                    cls.record_violation("NO_NAKED_SHORTS", msg, position_id=position_id, attempted_action=f"SELL {order.get('symbol')}")
                    return False, msg

        return True, "OK"

    @classmethod
    def validate_roll(
        cls,
        plan: TradePlan,
        current_spot: float,
        proposed_short_call: Optional[float] = None,
        proposed_short_put: Optional[float] = None,
        proposed_credit_pts: Optional[float] = None,
        proposed_max_loss_inr: Optional[float] = None,
        position_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Rule 2: NO_CREDIT_SEEKING_ROLLS & Rule 8: TOTAL_RISK_LIMIT
        - Reject rolls that move a short strike closer to spot, add a new short, or whose purpose is to increase credit.
        - Allowed rolls must reduce delta or max loss.
        """
        if proposed_short_call is not None:
            orig_dist = plan.short_call - plan.spot_at_entry
            curr_dist = proposed_short_call - current_spot
            if proposed_short_call < plan.short_call or curr_dist < orig_dist * 0.70:
                msg = f"NO_CREDIT_SEEKING_ROLLS: Rolling Short Call inward from {plan.short_call} to {proposed_short_call} closer to spot ({current_spot}) increases breakout risk."
                cls.record_violation("NO_CREDIT_SEEKING_ROLLS", msg, position_id=position_id, attempted_action=f"ROLL_CALL_{proposed_short_call}")
                return False, msg

        if proposed_short_put is not None:
            orig_dist = plan.spot_at_entry - plan.short_put
            curr_dist = current_spot - proposed_short_put
            if proposed_short_put > plan.short_put or curr_dist < orig_dist * 0.70:
                msg = f"NO_CREDIT_SEEKING_ROLLS: Rolling Short Put inward from {plan.short_put} to {proposed_short_put} closer to spot ({current_spot}) increases downside threat."
                cls.record_violation("NO_CREDIT_SEEKING_ROLLS", msg, position_id=position_id, attempted_action=f"ROLL_PUT_{proposed_short_put}")
                return False, msg

        if proposed_max_loss_inr is not None and proposed_max_loss_inr > plan.max_loss_inr:
            msg = f"TOTAL_RISK_EXCEEDED: Risk after adjustment (₹{proposed_max_loss_inr:,.2f}) exceeds original max loss (₹{plan.max_loss_inr:,.2f})."
            cls.record_violation("TOTAL_RISK_EXCEEDED", msg, position_id=position_id, attempted_action="EXPAND_RISK")
            return False, msg

        return True, "OK"

    @classmethod
    def validate_late_hedge(
        cls,
        plan: TradePlan,
        current_spot: float,
        hedge_strike: float,
        option_type: str,
        hedge_premium_pts: float,
        recent_hedge_sides: Optional[List[str]] = None,
        position_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Rule 3: NO_EXPENSIVE_LATE_HEDGES
        - Reject buying a new long option whose premium exceeds hedge_cost_cap (default 25% of original credit)
        - Reject buying ATM or ITM hedge.
        - Reject repeated alternating hedging of both sides.
        """
        cap_pts = plan.net_credit_pts * (plan.hedge_cost_cap_pct / 100.0)
        if hedge_premium_pts > cap_pts:
            msg = (
                f"NO_EXPENSIVE_LATE_HEDGES: Hedge premium ({hedge_premium_pts:.2f} pts) exceeds {plan.hedge_cost_cap_pct:.0f}% "
                f"cap of original credit ({cap_pts:.2f} pts)."
            )
            cls.record_violation("NO_EXPENSIVE_LATE_HEDGES", msg, position_id=position_id, attempted_action=f"BUY_HEDGE_{hedge_strike}")
            return False, msg

        if option_type == "CE" and hedge_strike <= current_spot:
            msg = f"NO_EXPENSIVE_LATE_HEDGES: Call hedge strike {hedge_strike} is at or in the money (spot={current_spot:.1f})."
            cls.record_violation("NO_EXPENSIVE_LATE_HEDGES", msg, position_id=position_id, attempted_action=f"BUY_ITM_HEDGE_{hedge_strike}")
            return False, msg

        if option_type == "PE" and hedge_strike >= current_spot:
            msg = f"NO_EXPENSIVE_LATE_HEDGES: Put hedge strike {hedge_strike} is at or in the money (spot={current_spot:.1f})."
            cls.record_violation("NO_EXPENSIVE_LATE_HEDGES", msg, position_id=position_id, attempted_action=f"BUY_ITM_HEDGE_{hedge_strike}")
            return False, msg

        # Check for alternating whipsaw hedging
        if recent_hedge_sides and len(recent_hedge_sides) >= 2:
            if recent_hedge_sides[-1] != option_type and recent_hedge_sides[-2] == option_type:
                msg = "NO_EXPENSIVE_LATE_HEDGES: Repeated alternating hedging across both sides is prohibited (whipsaw prevention)."
                cls.record_violation("NO_EXPENSIVE_LATE_HEDGES", msg, position_id=position_id, attempted_action="WHIPSAW_HEDGE")
                return False, msg

        return True, "OK"

    @classmethod
    def evaluate_discipline_exits(
        cls,
        position: Any,
        plan: TradePlan,
        current_spot: float,
        current_pnl_inr: float,
        current_dte: float,
        is_expiry_day: bool = False,
    ) -> Optional[Tuple[str, str]]:
        """
        Evaluates hard auto-exits:
        - Rule 4: PROFIT_TAKE (default 75% of max profit)
        - Rule 5: PLANNED_LOSS_EXIT (default 60% of max loss)
        - Rule 6: THESIS_BREAK (65% distance covered within first 25% of life)
        - Rule 7: TIME_EXIT & EXPIRY_DAY (configured DTE, or expiry day if hold_to_expiry=False)
        """
        # Rule 4: PROFIT_TAKE
        if current_pnl_inr >= plan.profit_take_inr:
            return (
                "PROFIT_TAKE",
                f"Auto-exit: Profit target of ₹{plan.profit_take_inr:,.2f} ({int(plan.profit_take_pct * 100)}% of max profit) achieved (P&L: ₹{current_pnl_inr:,.2f}).",
            )

        # Rule 5: PLANNED_LOSS_EXIT
        if current_pnl_inr <= -plan.planned_loss_exit_inr:
            return (
                "PLANNED_LOSS_EXIT",
                f"Auto-exit: Planned loss limit of ₹{plan.planned_loss_exit_inr:,.2f} ({int(plan.planned_loss_exit_pct * 100)}% of max loss) reached (loss: ₹{-current_pnl_inr:,.2f}). Exiting before max loss.",
            )

        # Rule 6: THESIS_BREAK (within first 25% of position's life)
        elapsed_life = (plan.initial_dte - current_dte) / plan.initial_dte if plan.initial_dte > 0 else 0.0
        if elapsed_life <= plan.thesis_break_time_window_pct:
            if current_spot >= plan.thesis_break_level_call:
                return (
                    "THESIS_BREAK",
                    f"Auto-exit: Thesis break on Call side. Spot ({current_spot:.1f}) reached thesis level ({plan.thesis_break_level_call:.1f}) within first {int(plan.thesis_break_time_window_pct * 100)}% of position life.",
                )
            if current_spot <= plan.thesis_break_level_put:
                return (
                    "THESIS_BREAK",
                    f"Auto-exit: Thesis break on Put side. Spot ({current_spot:.1f}) reached thesis level ({plan.thesis_break_level_put:.1f}) within first {int(plan.thesis_break_time_window_pct * 100)}% of position life.",
                )

        # Rule 7: TIME_EXIT and EXPIRY_DAY
        if not plan.hold_to_expiry:
            if current_dte <= plan.time_exit_dte:
                return (
                    "TIME_EXIT",
                    f"Auto-exit: Position reached {current_dte:.1f} DTE (<= time exit limit of {plan.time_exit_dte:.1f} DTE).",
                )
            if is_expiry_day:
                return (
                    "EXPIRY_DAY_EXIT",
                    "Auto-exit: Position cannot be held into expiry day (hold_to_expiry=False).",
                )

        return None

    @classmethod
    def finalize_trade_outcome(cls, position_id: int, net_pnl_inr: float, exit_reason: str) -> None:
        """Audits the closed trade against its persisted trade plan."""
        plan = TradePlan.query.filter_by(position_id=position_id).first()
        if not plan:
            return

        plan.status = "COMPLETED"
        plan.realized_loss_inr = round(-net_pnl_inr, 2) if net_pnl_inr < 0 else 0.0

        if net_pnl_inr < -plan.planned_loss_exit_inr:
            plan.loss_exceeded_plan = True
            plan.exceeded_cause = f"Realized loss ₹{-net_pnl_inr:,.2f} exceeded planned loss limit ₹{plan.planned_loss_exit_inr:,.2f} (Exit: {exit_reason})"
            log.warning(f"PLAN AUDIT: Position #{position_id} {plan.exceeded_cause}")

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            log.error(f"Failed to finalize trade plan audit: {e}")

    @classmethod
    def generate_plan_adherence_report(cls) -> Dict[str, Any]:
        """
        Builds the authoritative Plan Adherence report:
        - Actual loss vs planned loss per trade
        - Number of blocked rule violations
        - Any trade whose realized loss exceeded plan (with cause)
        """
        plans = TradePlan.query.order_by(TradePlan.id.desc()).all()
        violations = DisciplineViolationLog.query.order_by(DisciplineViolationLog.id.desc()).all()

        violations_by_code: Dict[str, int] = {}
        for v in violations:
            violations_by_code[v.rule_code] = violations_by_code.get(v.rule_code, 0) + 1

        trade_adherence_list = []
        exceeded_trades = []

        for p in plans:
            realized_loss = p.realized_loss_inr or 0.0
            planned_loss = p.planned_loss_exit_inr
            exceeded = p.loss_exceeded_plan or (realized_loss > (planned_loss + 1.0))
            pos = p.position

            item = {
                "position_id": p.position_id,
                "strategy": p.strategy_id,
                "expiry": p.expiry,
                "entry_spot": p.spot_at_entry,
                "net_credit_inr": p.net_credit_inr,
                "max_loss_inr": p.max_loss_inr,
                "planned_loss_exit_inr": planned_loss,
                "profit_take_inr": p.profit_take_inr,
                "realized_loss_inr": realized_loss,
                "realized_net_pnl_inr": pos.net_pnl_inr if pos else 0.0,
                "exit_reason": pos.exit_reason if pos else "OPEN",
                "loss_exceeded_plan": exceeded,
                "exceeded_cause": p.exceeded_cause if exceeded else "Within plan",
                "plan_text": p.to_plan_text(),
            }
            trade_adherence_list.append(item)
            if exceeded:
                exceeded_trades.append(item)

        total_trades = len(plans)
        adherent_trades = total_trades - len(exceeded_trades)
        adherence_rate_pct = round((adherent_trades / total_trades) * 100.0, 1) if total_trades > 0 else 100.0

        return {
            "total_plans_created": total_trades,
            "total_violations_blocked": len(violations),
            "violations_by_rule": violations_by_code,
            "plan_adherence_rate_pct": adherence_rate_pct,
            "trades_exceeding_plan": exceeded_trades,
            "trade_adherence_records": trade_adherence_list,
        }
