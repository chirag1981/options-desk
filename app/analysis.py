"""
app/analysis.py — Open Interest Structure, Key Levels & Market Regime Analysis
Authoritative service for computing:
1. Support Walls (S1, S2) and Resistance Walls (R1, R2).
2. Put-Call Ratio (PCR) and Net Writer Flow (Delta PE OI vs Delta CE OI).
3. Safe distance cushions from spot to major OI concentrations.
4. Deterministic Market Regime: TRENDING_BULLISH, TRENDING_BEARISH, BALANCED_RANGE, CHOPPY.
"""

import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Tuple

log = logging.getLogger("analysis")
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class MarketAnalysisResult:
    """Structured market analysis metrics for options-selling engines."""
    symbol: str
    expiry: str
    timestamp: str
    spot_price: float
    spot_change_pct: float
    atm_strike: float
    s1_support: float
    s2_support: float
    r1_resistance: float
    r2_resistance: float
    pcr_oi: float
    total_pe_oi: int
    total_ce_oi: int
    total_pe_change_oi: int
    total_ce_change_oi: int
    net_delta_pe_ce_oi: int
    writer_momentum: str     # BULLISH_PUT_WRITING, BEARISH_CALL_WRITING, BALANCED
    dist_to_s1: float        # spot - s1
    dist_to_r1: float        # r1 - spot
    dist_to_s1_pct: float
    dist_to_r1_pct: float
    atm_iv: float
    market_regime: str       # TRENDING_BULLISH, TRENDING_BEARISH, BALANCED_RANGE, CHOPPY
    market_bias: str         # BULLISH, BEARISH, NEUTRAL
    confidence_score: float  # 0.0 to 100.0
    evidence_factors: List[str]
    data_status: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MarketAnalyzer:
    """
    Pure, deterministic analyzer for option chain quotes.
    """

    @classmethod
    def analyze_chain(
        cls,
        chain_data: Dict[str, Any],
        symbol: str = "NIFTY",
        strike_step: float = 50.0,
    ) -> MarketAnalysisResult:
        """
        Analyzes normalized option chain and returns MarketAnalysisResult.
        """
        spot = float(chain_data.get("spot_price", 0.0))
        spot_chg = float(chain_data.get("spot_change_pct", 0.0))
        expiry = str(chain_data.get("expiry", ""))
        data_status = str(chain_data.get("data_status", "UNKNOWN"))
        chain = chain_data.get("chain_by_strike", {})

        if spot <= 0:
            raise ValueError(f"Invalid spot price: {spot}")

        atm_strike = round(spot / strike_step) * strike_step

        total_pe_oi = 0
        total_ce_oi = 0
        total_pe_chg = 0
        total_ce_chg = 0

        pe_strikes_oi: List[Tuple[float, int]] = []
        ce_strikes_oi: List[Tuple[float, int]] = []
        atm_iv = 0.0

        for strike, quotes in chain.items():
            stk = float(strike)
            ce = quotes.get("CE") or {}
            pe = quotes.get("PE") or {}

            raw_ce_oi = ce.get("oi")
            raw_pe_oi = pe.get("oi")
            raw_ce_chg = ce.get("change_oi")
            raw_pe_chg = pe.get("change_oi")

            if raw_ce_oi is not None:
                try:
                    ce_oi = int(raw_ce_oi)
                    total_ce_oi += ce_oi
                    if ce_oi > 0:
                        ce_strikes_oi.append((stk, ce_oi))
                except (ValueError, TypeError):
                    pass

            if raw_pe_oi is not None:
                try:
                    pe_oi = int(raw_pe_oi)
                    total_pe_oi += pe_oi
                    if pe_oi > 0:
                        pe_strikes_oi.append((stk, pe_oi))
                except (ValueError, TypeError):
                    pass

            if raw_ce_chg is not None:
                try:
                    total_ce_chg += int(raw_ce_chg)
                except (ValueError, TypeError):
                    pass

            if raw_pe_chg is not None:
                try:
                    total_pe_chg += int(raw_pe_chg)
                except (ValueError, TypeError):
                    pass

            if stk == atm_strike:
                ce_iv = float(ce.get("iv", 0.0) or 0.0)
                pe_iv = float(pe.get("iv", 0.0) or 0.0)
                atm_iv = round((ce_iv + pe_iv) / 2.0 if (ce_iv > 0 or pe_iv > 0) else 0.0, 2)

        # Sort strikes by highest OI to identify major structural walls
        pe_strikes_oi.sort(key=lambda x: x[1], reverse=True)
        ce_strikes_oi.sort(key=lambda x: x[1], reverse=True)

        # S1 is top Put OI strike at or below spot
        s1 = 0.0
        s2 = 0.0
        pe_below = [s for s, oi in pe_strikes_oi if s <= spot]
        if pe_below:
            s1 = pe_below[0]
            if len(pe_below) > 1:
                s2 = pe_below[1]
        elif pe_strikes_oi:
            s1 = pe_strikes_oi[0][0]

        # R1 is top Call OI strike at or above spot
        r1 = 0.0
        r2 = 0.0
        ce_above = [s for s, oi in ce_strikes_oi if s >= spot]
        if ce_above:
            r1 = ce_above[0]
            if len(ce_above) > 1:
                r2 = ce_above[1]
        elif ce_strikes_oi:
            r1 = ce_strikes_oi[0][0]

        pcr = round((total_pe_oi / total_ce_oi), 2) if total_ce_oi > 0 else 1.0
        net_delta = total_pe_chg - total_ce_chg

        # Writer Momentum Bias
        if total_pe_chg > total_ce_chg * 1.25 and total_pe_chg > 0:
            writer_momentum = "BULLISH_PUT_WRITING"
        elif total_ce_chg > total_pe_chg * 1.25 and total_ce_chg > 0:
            writer_momentum = "BEARISH_CALL_WRITING"
        else:
            writer_momentum = "BALANCED"

        dist_s1 = spot - s1 if (spot > 0 and s1 > 0) else 0.0
        dist_r1 = r1 - spot if (spot > 0 and r1 > 0) else 0.0
        dist_s1_pct = round((dist_s1 / spot * 100.0), 2) if spot > 0 else 0.0
        dist_r1_pct = round((dist_r1 / spot * 100.0), 2) if spot > 0 else 0.0

        # Market Regime & Confidence Determination
        regime, bias, confidence, evidence = cls._determine_regime(
            pcr=pcr,
            writer_momentum=writer_momentum,
            spot=spot,
            s1=s1,
            r1=r1,
            dist_s1=dist_s1,
            dist_r1=dist_r1,
            net_delta=net_delta,
        )

        now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

        return MarketAnalysisResult(
            symbol=symbol,
            expiry=expiry,
            timestamp=now_str,
            spot_price=spot,
            spot_change_pct=spot_chg,
            atm_strike=atm_strike,
            s1_support=s1,
            s2_support=s2,
            r1_resistance=r1,
            r2_resistance=r2,
            pcr_oi=pcr,
            total_pe_oi=total_pe_oi,
            total_ce_oi=total_ce_oi,
            total_pe_change_oi=total_pe_chg,
            total_ce_change_oi=total_ce_chg,
            net_delta_pe_ce_oi=net_delta,
            writer_momentum=writer_momentum,
            dist_to_s1=dist_s1,
            dist_to_r1=dist_r1,
            dist_to_s1_pct=dist_s1_pct,
            dist_to_r1_pct=dist_r1_pct,
            atm_iv=atm_iv,
            market_regime=regime,
            market_bias=bias,
            confidence_score=confidence,
            evidence_factors=evidence,
            data_status=data_status,
        )

    @classmethod
    def _determine_regime(
        cls,
        pcr: float,
        writer_momentum: str,
        spot: float,
        s1: float,
        r1: float,
        dist_s1: float,
        dist_r1: float,
        net_delta: int,
    ) -> Tuple[str, str, float, List[str]]:
        factors = []
        confidence = 60.0

        # Strong Bullish Regime
        if pcr >= 1.15 and writer_momentum == "BULLISH_PUT_WRITING" and spot >= s1:
            factors.append(f"Put Writing dominance (Net ΔOI: +{net_delta:,})")
            factors.append(f"Bullish PCR: {pcr:.2f} (above 1.15)")
            if dist_s1 >= 40.0:
                factors.append(f"Safe cushion ({dist_s1:.0f} pts) above Put Wall ({s1:.0f})")
                confidence = 88.0
            else:
                factors.append(f"Tight cushion ({dist_s1:.0f} pts) above Put Wall")
                confidence = 75.0
            return "TRENDING_BULLISH", "BULLISH", confidence, factors

        # Strong Bearish Regime
        if pcr <= 0.85 and writer_momentum == "BEARISH_CALL_WRITING" and spot <= r1:
            factors.append(f"Call Writing dominance (Net ΔOI: {net_delta:,})")
            factors.append(f"Bearish PCR: {pcr:.2f} (below 0.85)")
            if dist_r1 >= 40.0:
                factors.append(f"Safe cushion ({dist_r1:.0f} pts) below Call Wall ({r1:.0f})")
                confidence = 88.0
            else:
                factors.append(f"Tight cushion ({dist_r1:.0f} pts) below Call Wall")
                confidence = 75.0
            return "TRENDING_BEARISH", "BEARISH", confidence, factors

        # Balanced Range-Bound Regime (Iron Condor candidate)
        if 0.90 <= pcr <= 1.10 and s1 < spot < r1 and dist_s1 >= 25.0 and dist_r1 >= 25.0:
            factors.append(f"Balanced PCR: {pcr:.2f}")
            factors.append(f"Spot centered between Put Wall ({s1:.0f}) and Call Wall ({r1:.0f})")
            factors.append("Low directional bias (both wings well-supported)")
            confidence = 82.0
            return "BALANCED_RANGE", "NEUTRAL", confidence, factors

        # Mildly Bullish
        if pcr > 1.05 and spot >= s1:
            factors.append(f"Moderate Put Writing bias (PCR: {pcr:.2f})")
            return "MILDLY_BULLISH", "BULLISH", 70.0, factors

        # Mildly Bearish
        if pcr < 0.95 and spot <= r1:
            factors.append(f"Moderate Call Writing bias (PCR: {pcr:.2f})")
            return "MILDLY_BEARISH", "BEARISH", 70.0, factors

        # Choppy / Mixed Signals
        factors.append(f"Mixed signals: PCR {pcr:.2f}, Writer flow: {writer_momentum}")
        if dist_s1 < 20.0 or dist_r1 < 20.0:
            factors.append("Spot dangerously close to major boundary strike.")
        return "CHOPPY", "NEUTRAL", 50.0, factors
