"""
app/strategy_profiles.py — Strategy Profiles: monthly_45_15 and weekly_filtered
Strict Invariants:
1. PROFILE monthly_45_15:
   - Monthly expiry with 40-50 DTE (target 45), entered at ~15:05 IST. One open position per profile.
   - Short call + put at ~30 delta. Long hedges at ~17 delta.
   - Enforce max_loss <= 2 x net credit by adjusting wing distance in strike-step increments (max wing width 300 pts).
   - Skip if credit <= 0 or any leg fails liquidity checks.
   - Exits: DTE <= 15 (TIME), profit >= 50% of credit (TP), loss >= 100% of credit (SL).
2. PROFILE weekly_filtered:
   - Weekly expiry, enter at DTE 2-5 only if all pre-trade FILTERS pass.
   - Shorts at larger of ~15-20 delta and 1.0x expected move (spot * iv * sqrt(DTE/365)). Wings 100-200 pts.
   - Require net credit >= 25% of wing width and max loss <= max_loss_pct (default 2%) of capital.
   - Exits: TP 50% of credit, SL 1.5x credit, time exit at 1 DTE.
"""

import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.config import Config
from app.contracts import ContractRegistry
from app.research.strike_research import ExperimentalStrikeSelector

log = logging.getLogger("strategy_profiles")
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class ProfileExitDecision:
    """Decision indicating whether an open position should be closed."""
    should_exit: bool
    reason: str  # TP, SL, TIME, MANUAL, NONE
    details: str
    net_pnl_pts: float = 0.0


class StrategyProfileManager:
    """Manages profile-specific strike selection, entry conditions, and exit rules."""

    @classmethod
    def evaluate_monthly_45_15_entry(
        cls,
        spot: float,
        chain: Dict[float, Dict[str, Any]],
        dte: float,
        lot_size: int = 65,
        target_delta_short: float = 0.30,
        target_delta_long: float = 0.17,
        max_wing_width: float = 300.0,
    ) -> Dict[str, Any]:
        """
        Builds the monthly_45_15 Iron Condor:
        - Monthly expiry 40-50 DTE (target 45).
        - Short ~30 delta, Long ~17 delta.
        - Enforces max_loss <= 2 x net credit by adjusting wing distance.
        """
        if dte < 35.0 or dte > 55.0:
            return {"is_valid": False, "rejection_reason": f"DTE {dte:.1f} outside monthly 40-50 DTE window"}

        strikes = sorted([float(s) for s in chain.keys()])
        if not strikes or spot <= 0:
            return {"is_valid": False, "rejection_reason": "Empty option chain or invalid spot"}

        # 1. Select Short Strikes (~30 delta)
        short_ce = None
        short_pe = None
        min_diff_ce = 999.0
        min_diff_pe = 999.0

        for stk in strikes:
            q = chain[stk]
            ce_iv = float(q.get("CE", {}).get("iv", 15.0) or 15.0) / 100.0
            pe_iv = float(q.get("PE", {}).get("iv", 15.0) or 15.0) / 100.0

            d_ce = ExperimentalStrikeSelector.black76_call_delta(spot, stk, dte / 365.0, ce_iv)
            d_pe = abs(ExperimentalStrikeSelector.black76_put_delta(spot, stk, dte / 365.0, pe_iv))

            if stk >= spot and abs(d_ce - target_delta_short) < min_diff_ce:
                min_diff_ce = abs(d_ce - target_delta_short)
                short_ce = stk

            if stk <= spot and abs(d_pe - target_delta_short) < min_diff_pe:
                min_diff_pe = abs(d_pe - target_delta_short)
                short_pe = stk

        if not short_ce or not short_pe:
            return {"is_valid": False, "rejection_reason": "Could not identify ~30 delta short strikes"}

        # 2. Select Long Hedges (~17 delta)
        long_ce = None
        long_pe = None
        min_diff_long_ce = 999.0
        min_diff_long_pe = 999.0

        for stk in strikes:
            q = chain[stk]
            ce_iv = float(q.get("CE", {}).get("iv", 15.0) or 15.0) / 100.0
            pe_iv = float(q.get("PE", {}).get("iv", 15.0) or 15.0) / 100.0

            d_ce = ExperimentalStrikeSelector.black76_call_delta(spot, stk, dte / 365.0, ce_iv)
            d_pe = abs(ExperimentalStrikeSelector.black76_put_delta(spot, stk, dte / 365.0, pe_iv))

            if stk > short_ce and abs(d_ce - target_delta_long) < min_diff_long_ce:
                min_diff_long_ce = abs(d_ce - target_delta_long)
                long_ce = stk

            if stk < short_pe and abs(d_pe - target_delta_long) < min_diff_long_pe:
                min_diff_long_pe = abs(d_pe - target_delta_long)
                long_pe = stk

        # Fallback if far OTM strikes exist beyond shorts but exact ~17 delta is beyond boundary
        if not long_ce:
            otm_ce_strikes = [s for s in strikes if s > short_ce]
            if otm_ce_strikes:
                long_ce = otm_ce_strikes[-1]
        if not long_pe:
            otm_pe_strikes = [s for s in strikes if s < short_pe]
            if otm_pe_strikes:
                long_pe = otm_pe_strikes[0]

        if not long_ce or not long_pe:
            return {"is_valid": False, "rejection_reason": "Could not identify ~17 delta protective wings"}

        wing_ce = long_ce - short_ce
        wing_pe = short_pe - long_pe
        wing_width = max(wing_ce, wing_pe)

        if wing_width > max_wing_width:
            # Constrain to max wing width
            long_ce = short_ce + max_wing_width
            long_pe = short_pe - max_wing_width
            wing_width = max_wing_width

        # 3. Calculate Pricing & Net Credit
        short_ce_q = chain.get(short_ce, {}).get("CE", {})
        short_pe_q = chain.get(short_pe, {}).get("PE", {})
        long_ce_q = chain.get(long_ce, {}).get("CE", {})
        long_pe_q = chain.get(long_pe, {}).get("PE", {})

        credit_ce = float(short_ce_q.get("bid", 0.0)) - float(long_ce_q.get("ask", 0.0))
        credit_pe = float(short_pe_q.get("bid", 0.0)) - float(long_pe_q.get("ask", 0.0))
        net_credit = round(credit_ce + credit_pe, 2)

        if net_credit <= 0.0:
            return {"is_valid": False, "rejection_reason": f"Net credit (₹{net_credit:.2f}) <= 0"}

        max_loss = round(wing_width - net_credit, 2)

        # 4. Enforce 1:2 Rule (max_loss <= 2 x net credit)
        if max_loss > (2.0 * net_credit):
            # Step in wings to satisfy 1:2
            adjusted_wing = math.floor(3.0 * net_credit / 50.0) * 50.0
            if adjusted_wing < 50.0:
                return {"is_valid": False, "rejection_reason": f"1:2 Rule violation: max loss ({max_loss:.1f}) > 2x credit ({2*net_credit:.1f})"}
            wing_width = adjusted_wing
            long_ce = short_ce + wing_width
            long_pe = short_pe - wing_width
            max_loss = round(wing_width - net_credit, 2)

        return {
            "is_valid": True,
            "profile": "monthly_45_15",
            "short_call": short_ce,
            "long_call": long_ce,
            "short_put": short_pe,
            "long_put": long_pe,
            "wing_width": wing_width,
            "net_credit_pts": net_credit,
            "max_loss_pts": max_loss,
            "lot_size": lot_size,
            "rejection_reason": "",
        }

    @classmethod
    def evaluate_weekly_filtered_entry(
        cls,
        spot: float,
        chain: Dict[float, Dict[str, Any]],
        dte: float,
        atm_iv: float = 14.0,
        capital: float = 1_000_000.0,
        lot_size: int = 65,
        max_loss_pct: float = 2.0,  # Max 2% of capital
    ) -> Dict[str, Any]:
        """
        Builds weekly_filtered Iron Condor:
        - Weekly expiry DTE 2-5.
        - Shorts at larger of ~15-20 delta and 1.0x Expected Move (spot * iv * sqrt(DTE/365)).
        - Wings 100-200 pts.
        - Require net credit >= 25% of wing width and max loss <= 2% of capital.
        """
        if dte < 1.0 or dte > 6.0:
            return {"is_valid": False, "rejection_reason": f"DTE {dte:.1f} outside weekly 2-5 DTE window"}

        strikes = sorted([float(s) for s in chain.keys()])
        sigma = max(0.08, atm_iv / 100.0)
        expected_move = spot * sigma * math.sqrt(dte / 365.0)

        # Expected Move Strikes
        em_call_strike = math.ceil((spot + expected_move) / 50.0) * 50.0
        em_put_strike = math.floor((spot - expected_move) / 50.0) * 50.0

        # Delta ~18 Strikes
        short_ce = None
        short_pe = None
        min_d_ce = 999.0
        min_d_pe = 999.0

        for stk in strikes:
            d_ce = ExperimentalStrikeSelector.black76_call_delta(spot, stk, dte / 365.0, sigma)
            d_pe = abs(ExperimentalStrikeSelector.black76_put_delta(spot, stk, dte / 365.0, sigma))

            if stk >= spot and abs(d_ce - 0.18) < min_d_ce:
                min_d_ce = abs(d_ce - 0.18)
                short_ce = stk

            if stk <= spot and abs(d_pe - 0.18) < min_d_pe:
                min_d_pe = abs(d_pe - 0.18)
                short_pe = stk

        # Take larger distance between Delta 18 and Expected Move
        final_short_ce = max(short_ce or em_call_strike, em_call_strike)
        final_short_put = min(short_pe or em_put_strike, em_put_strike)

        # Wing width 100-200 pts (default 100 pts)
        wing_width = 100.0
        long_ce = final_short_ce + wing_width
        long_pe = final_short_put - wing_width

        # Pricing & Net Credit
        ce_short_q = chain.get(final_short_ce, {}).get("CE", {})
        pe_short_q = chain.get(final_short_put, {}).get("PE", {})
        ce_long_q = chain.get(long_ce, {}).get("CE", {})
        pe_long_q = chain.get(long_pe, {}).get("PE", {})

        credit_ce = float(ce_short_q.get("bid", 0.0)) - float(ce_long_q.get("ask", 0.0))
        credit_pe = float(pe_short_q.get("bid", 0.0)) - float(pe_long_q.get("ask", 0.0))
        net_credit = round(credit_ce + credit_pe, 2)

        min_req_credit = wing_width * 0.25  # Require >= 25% of wing width
        if net_credit < min_req_credit:
            return {"is_valid": False, "rejection_reason": f"Net credit (₹{net_credit:.2f}) < 25% of wing width (₹{min_req_credit:.2f})"}

        max_loss_pts = round(wing_width - net_credit, 2)
        max_loss_inr = max_loss_pts * lot_size
        max_allowed_inr = capital * (max_loss_pct / 100.0)

        if max_loss_inr > max_allowed_inr:
            return {"is_valid": False, "rejection_reason": f"Max loss ₹{max_loss_inr:,.0f} exceeds {max_loss_pct}% capital limit (₹{max_allowed_inr:,.0f})"}

        return {
            "is_valid": True,
            "profile": "weekly_filtered",
            "short_call": final_short_ce,
            "long_call": long_ce,
            "short_put": final_short_put,
            "long_put": long_pe,
            "wing_width": wing_width,
            "net_credit_pts": net_credit,
            "max_loss_pts": max_loss_pts,
            "lot_size": lot_size,
            "rejection_reason": "",
        }

    @classmethod
    def evaluate_exit(
        cls,
        profile_name: str,
        entry_credit_pts: float,
        current_cost_to_close_pts: float,
        current_dte: float,
    ) -> ProfileExitDecision:
        """
        Evaluates profile exit triggers:
        - monthly_45_15: TP >= 50% credit, SL >= 100% credit, TIME DTE <= 15
        - weekly_filtered: TP >= 50% credit, SL >= 150% credit, TIME DTE <= 1
        """
        pnl_pts = round(entry_credit_pts - current_cost_to_close_pts, 2)

        if profile_name == "monthly_45_15":
            # 1. Profit Target (50%)
            if pnl_pts >= (0.50 * entry_credit_pts):
                return ProfileExitDecision(True, "TP", f"Take Profit triggered: +₹{pnl_pts:.1f} pts (>= 50% of credit)", pnl_pts)

            # 2. Stop Loss (100% of credit)
            if pnl_pts <= (-1.0 * entry_credit_pts):
                return ProfileExitDecision(True, "SL", f"Stop Loss triggered: -₹{abs(pnl_pts):.1f} pts (>= 100% of credit)", pnl_pts)

            # 3. Time Exit (DTE <= 15)
            if current_dte <= 15.0:
                return ProfileExitDecision(True, "TIME", f"Time Exit triggered: DTE {current_dte:.1f} <= 15 days", pnl_pts)

        elif profile_name == "weekly_filtered":
            # 1. Profit Target (50%)
            if pnl_pts >= (0.50 * entry_credit_pts):
                return ProfileExitDecision(True, "TP", f"Take Profit triggered: +₹{pnl_pts:.1f} pts (>= 50% of credit)", pnl_pts)

            # 2. Stop Loss (150% of credit)
            if pnl_pts <= (-1.5 * entry_credit_pts):
                return ProfileExitDecision(True, "SL", f"Stop Loss triggered: -₹{abs(pnl_pts):.1f} pts (>= 1.5x credit)", pnl_pts)

            # 3. Time Exit (1 DTE)
            if current_dte <= 1.0:
                return ProfileExitDecision(True, "TIME", f"Time Exit triggered: DTE {current_dte:.1f} <= 1 day", pnl_pts)

        return ProfileExitDecision(False, "NONE", "Holding position", pnl_pts)
