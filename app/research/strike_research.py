"""
app/research/strike_research.py — Research-Only Strike Selection Models for NIFTY Iron Condor

Compares:
1. Baseline Open-Interest (S1/R1-anchored) strike selection (preserved as active control).
2. Delta-Based Alternative (~30 delta short strikes, ~15–17 delta protective wings).
3. Expected-Move Alternative (1.0x Expected Move = Spot * IV * sqrt(DTE / 365)).

Strict Invariants:
- Never fabricate delta or IV when genuine inputs are missing.
- Disclose all model assumptions (Black-76, r=7%, q=1.3%).
- All experimental selections run in research mode only.
"""

import math
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple
from statistics import NormalDist

from app.config import Config
from app.contracts import ContractRegistry


norm_dist = NormalDist(mu=0.0, sigma=1.0)


@dataclass
class StrikeSelectionSpec:
    """Standardized representation of a 4-leg Iron Condor strike specification."""
    model_name: str
    short_put_strike: float
    long_put_strike: float
    short_call_strike: float
    long_call_strike: float
    wing_width: float
    is_valid: bool
    rejection_reasons: List[str]
    model_metadata: Dict[str, Any]


class ExperimentalStrikeSelector:
    """
    Evaluates alternative strike placement techniques against the identical market state.
    """

    @classmethod
    def black76_call_delta(cls, forward: float, strike: float, time_to_exp: float, iv: float) -> float:
        """Calculates Black-76 Call option Delta = N(d1)."""
        if forward <= 0 or strike <= 0 or time_to_exp <= 0 or iv <= 0:
            return 0.0
        d1 = (math.log(forward / strike) + (0.5 * (iv ** 2) * time_to_exp)) / (iv * math.sqrt(time_to_exp))
        return float(norm_dist.cdf(d1))

    @classmethod
    def black76_put_delta(cls, forward: float, strike: float, time_to_exp: float, iv: float) -> float:
        """Calculates Black-76 Put option Delta = N(d1) - 1.0 (range: -1.0 to 0.0)."""
        call_d = cls.black76_call_delta(forward, strike, time_to_exp, iv)
        return float(call_d - 1.0)

    @classmethod
    def select_baseline_oi_strikes(
        cls,
        spot: float,
        s1: float,
        r1: float,
        chain: Dict[float, Any],
        wing_width: float = 50.0,
        step: float = 50.0,
    ) -> StrikeSelectionSpec:
        """
        Baseline Method: Anchored to S1 support (Put Wall) and R1 resistance (Call Wall).
        """
        rejections = []
        if spot <= 0 or s1 <= 0 or r1 <= 0:
            rejections.append("Invalid spot or OI wall inputs.")
            return StrikeSelectionSpec("BASELINE_OI", 0, 0, 0, 0, wing_width, False, rejections, {})

        short_put = math.floor(s1 / step) * step
        short_call = math.ceil(r1 / step) * step

        # Balanced cushion check
        put_dist = max(40.0, spot - short_put)
        if r1 > spot and (r1 - spot) <= (put_dist * 2.2) and r1 in chain:
            short_call = math.ceil(r1 / step) * step
        else:
            short_call = math.ceil((spot + max(100.0, put_dist * 1.5)) / step) * step

        long_put = short_put - wing_width
        long_call = short_call + wing_width

        # Validation
        for stk in (short_put, long_put, short_call, long_call):
            if stk not in chain:
                rejections.append(f"Strike {stk} missing from chain.")

        is_valid = (len(rejections) == 0)
        return StrikeSelectionSpec(
            model_name="BASELINE_OI",
            short_put_strike=short_put,
            long_put_strike=long_put,
            short_call_strike=short_call,
            long_call_strike=long_call,
            wing_width=wing_width,
            is_valid=is_valid,
            rejection_reasons=rejections,
            model_metadata={"s1": s1, "r1": r1, "method": "OI_WALL_ANCHORED"},
        )

    @classmethod
    def select_delta_strikes(
        cls,
        spot: float,
        dte: float,
        atm_iv: float,
        chain: Dict[float, Any],
        target_short_delta: float = 0.30,
        target_hedge_delta: float = 0.17,
        r: float = 0.07,
        q: float = 0.013,
        wing_width: float = 50.0,
        step: float = 50.0,
    ) -> StrikeSelectionSpec:
        """
        Delta-Based Alternative:
        - Target ~30 delta short Call, ~-30 delta short Put.
        - Target ~17 delta protective hedges (or wing_width offset).
        - Uses Black-76 forward pricing: F = Spot * exp((r - q) * T).
        """
        rejections = []
        if spot <= 0 or dte <= 0 or atm_iv <= 0:
            rejections.append("Missing or invalid volatility/DTE inputs for delta calculation.")
            return StrikeSelectionSpec("DELTA_BASED", 0, 0, 0, 0, wing_width, False, rejections, {})

        t_exp = max(0.5 / 365.0, dte / 365.0)
        sigma = atm_iv / 100.0 if atm_iv > 1.0 else atm_iv
        forward = spot * math.exp((r - q) * t_exp)

        # Available strikes
        strikes = sorted([float(s) for s in chain.keys() if s > 0])
        if len(strikes) < 8:
            rejections.append(f"Insufficient strike count ({len(strikes)}) in option chain.")
            return StrikeSelectionSpec("DELTA_BASED", 0, 0, 0, 0, wing_width, False, rejections, {})

        # Find Put Short Strike (~ -target_short_delta, e.g. -0.30)
        put_strikes = [s for s in strikes if s < spot]
        if not put_strikes:
            rejections.append("No strikes below spot for Put wing.")
            return StrikeSelectionSpec("DELTA_BASED", 0, 0, 0, 0, wing_width, False, rejections, {})

        best_sp = None
        best_sp_diff = 999.0
        for s in put_strikes:
            delta = abs(cls.black76_put_delta(forward, s, t_exp, sigma))
            diff = abs(delta - target_short_delta)
            if diff < best_sp_diff:
                best_sp_diff = diff
                best_sp = s

        # Find Call Short Strike (~ +target_short_delta, e.g. +0.30)
        call_strikes = [s for s in strikes if s > spot]
        if not call_strikes:
            rejections.append("No strikes above spot for Call wing.")
            return StrikeSelectionSpec("DELTA_BASED", 0, 0, 0, 0, wing_width, False, rejections, {})

        best_sc = None
        best_sc_diff = 999.0
        for s in call_strikes:
            delta = cls.black76_call_delta(forward, s, t_exp, sigma)
            diff = abs(delta - target_short_delta)
            if diff < best_sc_diff:
                best_sc_diff = diff
                best_sc = s

        short_put = best_sp
        short_call = best_sc
        long_put = short_put - wing_width
        long_call = short_call + wing_width

        # Contract existence check
        for stk in (short_put, long_put, short_call, long_call):
            if stk not in chain:
                rejections.append(f"Calculated delta strike {stk} missing from chain.")

        is_valid = (len(rejections) == 0)
        return StrikeSelectionSpec(
            model_name="DELTA_BASED",
            short_put_strike=short_put,
            long_put_strike=long_put,
            short_call_strike=short_call,
            long_call_strike=long_call,
            wing_width=wing_width,
            is_valid=is_valid,
            rejection_reasons=rejections,
            model_metadata={
                "target_short_delta": target_short_delta,
                "target_hedge_delta": target_hedge_delta,
                "forward": round(forward, 2),
                "iv": round(sigma * 100.0, 2),
                "dte": dte,
            },
        )

    @classmethod
    def select_expected_move_strikes(
        cls,
        spot: float,
        dte: float,
        atm_iv: float,
        chain: Dict[float, Any],
        em_multiplier: float = 1.0,
        wing_width: float = 50.0,
        step: float = 50.0,
    ) -> StrikeSelectionSpec:
        """
        Expected-Move Alternative:
        Expected Move = Spot * IV * sqrt(DTE / 365)
        Short Put = Spot - (em_multiplier * EM)
        Short Call = Spot + (em_multiplier * EM)
        """
        rejections = []
        if spot <= 0 or dte <= 0 or atm_iv <= 0:
            rejections.append("Invalid volatility or DTE inputs for Expected Move calculation.")
            return StrikeSelectionSpec("EXPECTED_MOVE", 0, 0, 0, 0, wing_width, False, rejections, {})

        sigma = atm_iv / 100.0 if atm_iv > 1.0 else atm_iv
        t_exp = max(0.5 / 365.0, dte / 365.0)
        expected_move = spot * sigma * math.sqrt(t_exp)

        target_put = spot - (em_multiplier * expected_move)
        target_call = spot + (em_multiplier * expected_move)

        short_put = math.floor(target_put / step) * step
        short_call = math.ceil(target_call / step) * step

        long_put = short_put - wing_width
        long_call = short_call + wing_width

        for stk in (short_put, long_put, short_call, long_call):
            if stk not in chain:
                rejections.append(f"Expected-Move strike {stk} missing from chain.")

        is_valid = (len(rejections) == 0)
        return StrikeSelectionSpec(
            model_name="EXPECTED_MOVE",
            short_put_strike=short_put,
            long_put_strike=long_put,
            short_call_strike=short_call,
            long_call_strike=long_call,
            wing_width=wing_width,
            is_valid=is_valid,
            rejection_reasons=rejections,
            model_metadata={
                "expected_move_pts": round(expected_move, 1),
                "em_multiplier": em_multiplier,
                "iv": round(sigma * 100.0, 2),
            },
        )


# Module-level convenience functions
black76_call_delta = ExperimentalStrikeSelector.black76_call_delta
black76_put_delta = ExperimentalStrikeSelector.black76_put_delta
