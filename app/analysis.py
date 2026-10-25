"""
app/analysis.py — Open Interest Structure, Key Levels & Market Regime Analysis
Authoritative service for computing:
1. Support Walls (S1, S2) and Resistance Walls (R1, R2).
2. Put-Call Ratio (PCR) and Net Writer Flow (Delta PE OI vs Delta CE OI).
3. Safe distance cushions from spot to major OI concentrations.
4. Deterministic Market Regime: TRENDING_BULLISH, TRENDING_BEARISH, BALANCED_RANGE, CHOPPY.
"""

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Tuple, Optional

from app.config import Config

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
    writer_momentum: str     # BULLISH_PUT_WRITING, BEARISH_CALL_WRITING, BALANCED, UNKNOWN
    dist_to_s1: float        # spot - s1
    dist_to_r1: float        # r1 - spot
    dist_to_s1_pct: float
    dist_to_r1_pct: float
    atm_iv: float
    market_regime: str       # TRENDING_BULLISH, TRENDING_BEARISH, BALANCED_RANGE, MILDLY_BULLISH, MILDLY_BEARISH, CHOPPY_OR_INSUFFICIENT
    market_bias: str         # BULLISH, BEARISH, NEUTRAL
    confidence_score: float  # Rule-based heuristic confidence score (0.0 to 100.0), not a statistical probability of profit
    evidence_factors: List[str]
    data_status: str
    data_quality_warnings: List[str] = field(default_factory=list)

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
        has_pe_oi = False
        has_ce_oi = False
        has_pe_chg = False
        has_ce_chg = False
        warnings: List[str] = []

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
                    has_ce_oi = True
                    if ce_oi > 0:
                        ce_strikes_oi.append((stk, ce_oi))
                except (ValueError, TypeError):
                    pass

            if raw_pe_oi is not None:
                try:
                    pe_oi = int(raw_pe_oi)
                    total_pe_oi += pe_oi
                    has_pe_oi = True
                    if pe_oi > 0:
                        pe_strikes_oi.append((stk, pe_oi))
                except (ValueError, TypeError):
                    pass

            if raw_ce_chg is not None:
                try:
                    total_ce_chg += int(raw_ce_chg)
                    has_ce_chg = True
                except (ValueError, TypeError):
                    pass

            if raw_pe_chg is not None:
                try:
                    total_pe_chg += int(raw_pe_chg)
                    has_pe_chg = True
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

        # Data Quality & OI Validation
        if not has_pe_oi and not has_ce_oi:
            warnings.append("Missing Open Interest data across entire option chain.")
        elif not has_pe_oi:
            warnings.append("Missing Put Open Interest data across option chain.")
        elif not has_ce_oi:
            warnings.append("Missing Call Open Interest data across option chain.")
        elif total_pe_oi == 0 and total_ce_oi == 0:
            warnings.append("Zero Open Interest across entire option chain.")

        if not has_ce_oi or total_ce_oi == 0:
            warnings.append("Zero Call Open Interest; Put-Call Ratio cannot be calculated safely.")
            pcr = 0.0
        elif not has_pe_oi or total_pe_oi == 0:
            warnings.append("Zero Put Open Interest; Put-Call Ratio cannot be calculated safely.")
            pcr = 0.0
        else:
            pcr = round((total_pe_oi / total_ce_oi), 2)

        if not has_pe_chg and not has_ce_chg:
            warnings.append("Historical change in Open Interest (ΔOI) is unavailable from feed.")
            net_delta = 0
            writer_momentum = "BALANCED"
        else:
            net_delta = total_pe_chg - total_ce_chg
            if total_pe_chg > total_ce_chg * 1.25 and total_pe_chg > 0:
                writer_momentum = "BULLISH_PUT_WRITING"
            elif total_ce_chg > total_pe_chg * 1.25 and total_ce_chg > 0:
                writer_momentum = "BEARISH_CALL_WRITING"
            else:
                writer_momentum = "BALANCED"

        if s1 <= 0 or r1 <= 0 or s1 >= r1:
            warnings.append(f"Invalid structural levels: S1 ({s1}) >= R1 ({r1}) or non-positive.")

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
            warnings=warnings,
            total_pe_chg=total_pe_chg,
            total_ce_chg=total_ce_chg,
        )

        raw_ts = chain_data.get("timestamp")
        if raw_ts and str(raw_ts).strip():
            now_str = str(raw_ts).strip()
        else:
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
            data_quality_warnings=warnings,
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
        warnings: Optional[List[str]] = None,
        total_pe_chg: int = 0,
        total_ce_chg: int = 0,
    ) -> Tuple[str, str, float, List[str]]:
        warnings = warnings or []
        factors: List[str] = []
        has_missing_delta = any("Historical change in Open Interest" in w for w in warnings)

        def _finalize(reg: str, b: str, conf: float) -> Tuple[str, str, float, List[str]]:
            if has_missing_delta and reg != "CHOPPY_OR_INSUFFICIENT":
                factors.append("ΔOI change data unavailable from broker feed; directional confidence reduced.")
                conf = min(conf, 65.0)
            return reg, b, conf, factors

        # 1. Critical Data Defect Guard (Zero/Missing OI or Invalid S1/R1)
        critical_warnings = [
            w for w in warnings
            if "Zero Open Interest" in w
            or "Missing Open Interest" in w
            or "Zero Call" in w
            or "Zero Put" in w
            or "Missing Put" in w
            or "Missing Call" in w
            or "Invalid structural" in w
        ]
        if critical_warnings:
            factors.extend(critical_warnings)
            return "CHOPPY_OR_INSUFFICIENT", "NEUTRAL", 25.0, factors

        # 2. Contradictory Input Checks
        if pcr >= 1.15 and spot < s1:
            factors.append(f"Contradictory signals: High PCR ({pcr:.2f}) but spot ({spot:.1f}) breached below Put Wall ({s1:.0f}).")
            return "CHOPPY_OR_INSUFFICIENT", "NEUTRAL", 45.0, factors

        if pcr <= 0.85 and spot > r1:
            factors.append(f"Contradictory signals: Low PCR ({pcr:.2f}) but spot ({spot:.1f}) breached above Call Wall ({r1:.0f}).")
            return "CHOPPY_OR_INSUFFICIENT", "NEUTRAL", 45.0, factors

        if pcr >= 1.15 and writer_momentum == "BEARISH_CALL_WRITING" and total_ce_chg > total_pe_chg * 1.5:
            factors.append(f"Contradictory signals: High PCR ({pcr:.2f}) contradicts heavy Call OI expansion (+{total_ce_chg:,}).")
            return "CHOPPY_OR_INSUFFICIENT", "NEUTRAL", 45.0, factors

        if pcr <= 0.85 and writer_momentum == "BULLISH_PUT_WRITING" and total_pe_chg > total_ce_chg * 1.5:
            factors.append(f"Contradictory signals: Low PCR ({pcr:.2f}) contradicts heavy Put OI expansion (+{total_pe_chg:,}).")
            return "CHOPPY_OR_INSUFFICIENT", "NEUTRAL", 45.0, factors

        # 3. Structural Boundary Cushion Breaches
        if spot < s1:
            factors.append(f"Spot price ({spot:.1f}) breached below S1 Put Wall ({s1:.0f}).")
            return "CHOPPY_OR_INSUFFICIENT", "NEUTRAL", 40.0, factors
        if spot > r1:
            factors.append(f"Spot price ({spot:.1f}) breached above R1 Call Wall ({r1:.0f}).")
            return "CHOPPY_OR_INSUFFICIENT", "NEUTRAL", 40.0, factors

        # 4. Ordered Deterministic Regimes
        # Rule 1: TRENDING_BULLISH (PCR >= 1.15, positive delta or Put OI expansion, spot >= s1, cushion >= 20)
        if pcr >= 1.15 and (writer_momentum == "BULLISH_PUT_WRITING" or net_delta > 0) and spot >= s1 and dist_s1 >= Config.MIN_SPOT_CUSHION_PTS:
            factors.append(f"Put OI expansion (Net ΔOI: +{net_delta:,})")
            factors.append(f"Bullish PCR: {pcr:.2f} (>= 1.15)")
            if dist_s1 >= 40.0:
                factors.append(f"Safe cushion ({dist_s1:.0f} pts) above Put Wall ({s1:.0f})")
                confidence = 88.0
            else:
                factors.append(f"Moderate cushion ({dist_s1:.0f} pts) above Put Wall ({s1:.0f})")
                confidence = 75.0
            return _finalize("TRENDING_BULLISH", "BULLISH", confidence)

        # Rule 2: TRENDING_BEARISH (PCR <= 0.85, negative delta or Call OI expansion, spot <= r1, cushion >= 20)
        if pcr <= 0.85 and (writer_momentum == "BEARISH_CALL_WRITING" or net_delta < 0) and spot <= r1 and dist_r1 >= Config.MIN_SPOT_CUSHION_PTS:
            factors.append(f"Call OI expansion (Net ΔOI: {net_delta:,})")
            factors.append(f"Bearish PCR: {pcr:.2f} (<= 0.85)")
            if dist_r1 >= 40.0:
                factors.append(f"Safe cushion ({dist_r1:.0f} pts) below Call Wall ({r1:.0f})")
                confidence = 88.0
            else:
                factors.append(f"Moderate cushion ({dist_r1:.0f} pts) below Call Wall ({r1:.0f})")
                confidence = 75.0
            return _finalize("TRENDING_BEARISH", "BEARISH", confidence)

        # Rule 3: BALANCED_RANGE (0.90 <= PCR <= 1.10, spot safely centered between S1 and R1)
        if 0.90 <= pcr <= 1.10 and s1 < spot < r1 and dist_s1 >= Config.MIN_SPOT_CUSHION_PTS and dist_r1 >= Config.MIN_SPOT_CUSHION_PTS:
            factors.append(f"Balanced PCR: {pcr:.2f} (0.90 to 1.10)")
            factors.append(f"Spot centered between Put Wall ({s1:.0f}) and Call Wall ({r1:.0f})")
            factors.append("Low directional bias (both wings well-supported)")
            confidence = 82.0
            return _finalize("BALANCED_RANGE", "NEUTRAL", confidence)

        # Rule 4: MILDLY_BULLISH (PCR > 1.05, spot >= s1, cushion >= 20)
        if pcr > 1.05 and spot >= s1 and dist_s1 >= Config.MIN_SPOT_CUSHION_PTS:
            factors.append(f"Moderate Put OI concentration (PCR: {pcr:.2f})")
            return _finalize("MILDLY_BULLISH", "BULLISH", 70.0)

        # Rule 5: MILDLY_BEARISH (PCR < 0.95, spot <= r1, cushion >= 20)
        if pcr < 0.95 and spot <= r1 and dist_r1 >= Config.MIN_SPOT_CUSHION_PTS:
            factors.append(f"Moderate Call OI concentration (PCR: {pcr:.2f})")
            return _finalize("MILDLY_BEARISH", "BEARISH", 70.0)

        # Rule 6: CHOPPY_OR_INSUFFICIENT (Default for mixed signals, narrow cushions, or neutral range)
        factors.append(f"Mixed or boundary proximity signals (PCR: {pcr:.2f}, Writer flow: {writer_momentum})")
        if dist_s1 < Config.MIN_SPOT_CUSHION_PTS or dist_r1 < Config.MIN_SPOT_CUSHION_PTS:
            factors.append(f"Spot cushion ({min(dist_s1, dist_r1):.1f} pts) below required {Config.MIN_SPOT_CUSHION_PTS:.1f} pts minimum.")
        return _finalize("CHOPPY_OR_INSUFFICIENT", "NEUTRAL", 50.0)
