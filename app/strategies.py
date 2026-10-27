"""
app/strategies.py — Defined-Risk Iron Condor Options-Selling Strategy & Dual Trading Engines
Implements:
1. Iron Condor (IRON_CONDOR)
2. Iron Butterfly (IRON_BUTTERFLY - entry gated)
3. IntradayEngine & PositionalEngine classes with independent state and lifecycle.
"""

import math
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

from app.config import Config
from app.analysis import MarketAnalysisResult

log = logging.getLogger("strategies")
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class OptionLegSpec:
    """Specification for a single option leg in a defined-risk spread."""
    symbol: str
    strike: float
    option_type: str  # CE or PE
    action: str       # SELL or BUY
    bid: float
    ask: float
    ltp: float
    quantity: int = 65

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["price"] = self.bid if self.action == "SELL" else self.ask
        return d


@dataclass
class SpreadCandidate:
    """Defined-risk spread structure with full payoff and risk metrics."""
    strategy_id: str
    strategy_name: str
    engine_type: str
    symbol: str
    expiry: str
    lots: int
    quantity: int
    legs: List[OptionLegSpec]
    net_credit_pts: float
    strike_width: float
    max_profit_inr: float
    max_loss_pts: float
    max_loss_inr: float
    risk_reward_ratio: float
    breakevens: List[float]
    is_eligible: bool
    rejection_reasons: List[str] = field(default_factory=list)
    confidence_score: float = 0.0
    rationale: str = ""
    spot_price: float = 0.0
    data_status: Optional[str] = None

    @property
    def has_uncovered_short(self) -> bool:
        """Enforces 100% protective wing coverage across every short leg."""
        short_ce = sum(l.quantity for l in self.legs if l.action == "SELL" and l.option_type == "CE")
        long_ce = sum(l.quantity for l in self.legs if l.action == "BUY" and l.option_type == "CE")
        short_pe = sum(l.quantity for l in self.legs if l.action == "SELL" and l.option_type == "PE")
        long_pe = sum(l.quantity for l in self.legs if l.action == "BUY" and l.option_type == "PE")
        return (short_ce > long_ce) or (short_pe > long_pe)

    @property
    def estimated_costs_inr(self) -> float:
        """Calculates exact statutory NSE brokerage, turnover fees and taxes for this strategy entry."""
        from app.trading import TransactionCostEngine
        legs_dict = [l.to_dict() for l in self.legs]
        return TransactionCostEngine.calculate_order_costs(legs_dict, self.lots)

    @property
    def estimated_entry_costs_inr(self) -> float:
        """Calculates estimated entry statutory transaction costs at entry bid/ask."""
        from app.trading import TransactionCostEngine
        legs_dict = [l.to_dict() for l in self.legs]
        return TransactionCostEngine.calculate_order_costs(legs_dict, self.lots)

    @property
    def estimated_exit_costs_inr(self) -> float:
        """
        Calculates conservative estimated exit transaction costs assuming position closing
        at current opposite quotes (buying back SELL legs at Ask, selling BUY legs at Bid).
        """
        from app.trading import TransactionCostEngine
        exit_legs_dict = [
            {"action": "BUY" if l.action == "SELL" else "SELL", "price": l.ask if l.action == "SELL" else l.bid, "quantity": l.quantity}
            for l in self.legs
        ]
        return TransactionCostEngine.calculate_order_costs(exit_legs_dict, self.lots)

    @property
    def total_estimated_round_trip_costs_inr(self) -> float:
        """Calculates round-trip entry + exit estimated statutory transaction costs."""
        return round(self.estimated_entry_costs_inr + self.estimated_exit_costs_inr, 2)

    @property
    def total_risk_with_costs_inr(self) -> float:
        """Theoretical maximum loss plus applicable estimated round-trip transaction costs."""
        return round(self.max_loss_inr + self.total_estimated_round_trip_costs_inr, 2)

    @property
    def estimated_margin_inr(self) -> float:
        """
        Estimated NSE SPAN + Exposure Margin required to deploy this defined-risk spread.
        Accounts for SEBI hedged benefit:
        - 4-leg Iron Condor: ~₹52,000 / lot (only higher wing margin + exposure)
        """
        lots = max(1, self.lots)
        if self.strategy_id == "IRON_CONDOR":
            return round(52000.0 * lots, 2)
        else:
            return round(52000.0 * lots, 2)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["legs"] = [l.to_dict() for l in self.legs]
        d["has_uncovered_short"] = self.has_uncovered_short
        d["estimated_costs_inr"] = self.estimated_costs_inr
        d["estimated_entry_costs_inr"] = self.estimated_entry_costs_inr
        d["estimated_exit_costs_inr"] = self.estimated_exit_costs_inr
        d["total_estimated_round_trip_costs_inr"] = self.total_estimated_round_trip_costs_inr
        d["total_risk_with_costs_inr"] = self.total_risk_with_costs_inr
        d["estimated_margin_inr"] = self.estimated_margin_inr
        d["net_credit_inr"] = round(self.net_credit_pts * self.quantity, 2)
        return d


class StrategyBuilder:
    """
    Constructs multi-leg defined-risk spreads using conservative quote-side pricing:
    - Short legs are sold strictly at live Bid.
    - Long protective legs are bought strictly at live Ask.
    """

    @classmethod
    def build_iron_condor(
        cls,
        analysis: MarketAnalysisResult,
        chain: Dict[float, Dict[str, Any]],
        engine_type: str = "INTRADAY",
        lots: int = 1,
        wing_width: Optional[float] = None,
    ) -> SpreadCandidate:
        """
        Builds self-contained 4-leg Iron Condor (Put Spread wing + Call Spread wing).
        Selects short strikes dynamically based on strike-wise Open Interest change (ΔOI),
        institutional writing defense zones, and cumulative support/resistance walls.
        Strictly requires BALANCED_RANGE or neutral regime with positive risk/reward.
        """
        from app.contracts import ContractRegistry
        rejections = []
        if analysis.market_regime == "CHOPPY_OR_INSUFFICIENT":
            rejections.append("Market regime is choppy or data insufficient; cannot confirm balanced range.")
        elif analysis.market_regime not in ("BALANCED_RANGE", "SIDEWAYS") and analysis.market_bias != "NEUTRAL":
            rejections.append(f"Market regime ({analysis.market_regime}) is not balanced range-bound.")

        spot = analysis.spot_price
        s1 = analysis.s1_support
        r1 = analysis.r1_resistance
        step = Config.STRIKE_STEP
        series_lot_size = ContractRegistry.get_contract_lot_size("NIFTY", analysis.expiry)
        lot_size = series_lot_size * lots

        # Determine effective wing width
        if wing_width is None or wing_width <= 0:
            actual_wing = float(getattr(Config, "DEFAULT_WING_WIDTH", 50.0))
        else:
            actual_wing = float(wing_width)
        actual_wing = max(actual_wing, step)
        actual_wing = round(actual_wing / step) * step

        # --- Dynamic Strike Selection based on strike-wise Open Interest Change (ΔOI) ---
        put_oi_insights = []
        call_oi_insights = []

        # 1. Put Wing: Seek active institutional Put writing defense zone below spot
        base_short_put = math.floor(s1 / step) * step if s1 > 0 else (math.floor(spot / step) * step - step)
        s1_quote = chain.get(s1, {}).get("PE", {})
        s1_pe_chg = int(s1_quote.get("change_oi") or 0)
        s1_unwinding = (s1 > 0 and s1_pe_chg < 0)

        eligible_puts = []
        for s_raw, quotes in chain.items():
            stk = float(s_raw)
            if stk <= spot and (spot - stk) >= Config.MIN_SPOT_CUSHION_PTS:
                pe_quote = quotes.get("PE") or {}
                if (stk - actual_wing) in chain or chain.get(stk - actual_wing):
                    pe_oi = int(pe_quote.get("oi") or 0)
                    pe_chg = int(pe_quote.get("change_oi") or 0)
                    eligible_puts.append((stk, pe_chg, pe_oi))

        if eligible_puts:
            if s1_unwinding:
                # If S1 has unwinding, step short put OTM below S1 to avoid breakout risk
                safer_puts = [p for p in eligible_puts if p[0] < s1]
                target_puts = safer_puts if safer_puts else eligible_puts
                put_oi_insights.append(f"Put unwinding at S1 ({int(s1)} PE, ΔOI: {s1_pe_chg:,}) -> shifted short put OTM")
            else:
                target_puts = eligible_puts

            positive_puts = [p for p in target_puts if p[1] > 0]
            if positive_puts:
                # Select the strike with strongest active Put writing defense, breaking ties by cumulative OI
                positive_puts.sort(key=lambda x: (x[1], x[2]), reverse=True)
                selected_put_strike = positive_puts[0][0]
                put_oi_insights.append(f"Active Put writing support at {int(selected_put_strike)} PE (ΔOI: +{positive_puts[0][1]:,})")
            else:
                target_puts.sort(key=lambda x: x[2], reverse=True)
                selected_put_strike = target_puts[0][0]
        else:
            selected_put_strike = base_short_put

        short_put_strike = selected_put_strike

        # 2. Call Wing: Seek active institutional Call writing defense zone above spot
        base_short_call = math.ceil(r1 / step) * step if r1 > 0 else (math.ceil(spot / step) * step + step)
        r1_quote = chain.get(r1, {}).get("CE", {})
        r1_ce_chg = int(r1_quote.get("change_oi") or 0)
        r1_unwinding = (r1 > 0 and r1_ce_chg < 0)

        eligible_calls = []
        for s_raw, quotes in chain.items():
            stk = float(s_raw)
            if stk >= spot and (stk - spot) >= Config.MIN_SPOT_CUSHION_PTS:
                ce_quote = quotes.get("CE") or {}
                if (stk + actual_wing) in chain or chain.get(stk + actual_wing):
                    ce_oi = int(ce_quote.get("oi") or 0)
                    ce_chg = int(ce_quote.get("change_oi") or 0)
                    eligible_calls.append((stk, ce_chg, ce_oi))

        if eligible_calls:
            if r1_unwinding:
                # If R1 has unwinding, step short call OTM above R1 to avoid breakout risk
                safer_calls = [c for c in eligible_calls if c[0] > r1]
                target_calls = safer_calls if safer_calls else eligible_calls
                call_oi_insights.append(f"Call unwinding at R1 ({int(r1)} CE, ΔOI: {r1_ce_chg:,}) -> shifted short call OTM")
            else:
                target_calls = eligible_calls

            positive_calls = [c for c in target_calls if c[1] > 0]
            if positive_calls:
                # Select the strike with strongest active Call writing resistance, breaking ties by cumulative OI
                positive_calls.sort(key=lambda x: (x[1], x[2]), reverse=True)
                selected_call_strike = positive_calls[0][0]
                call_oi_insights.append(f"Active Call writing resistance at {int(selected_call_strike)} CE (ΔOI: +{positive_calls[0][1]:,})")
            else:
                target_calls.sort(key=lambda x: x[2], reverse=True)
                selected_call_strike = target_calls[0][0]
        else:
            selected_call_strike = base_short_call

        # In historical replay mode, preserve exact baseline strike calculation for deterministic backtest reproduction
        if getattr(analysis, "data_status", "") == "HISTORICAL_REPLAY":
            short_put_strike = base_short_put
            short_call_strike = base_short_call
        else:
            short_put_strike = selected_put_strike
            short_call_strike = selected_call_strike

            # 3. Writer Momentum Directional Cushion:
            if analysis.writer_momentum == "BEARISH_CALL_WRITING":
                if (spot - short_put_strike) < 1.5 * Config.MIN_SPOT_CUSHION_PTS:
                    short_put_strike -= step
                    put_oi_insights.append("Bearish call writer pressure -> cushioned Put wing 1 strike further OTM")
            elif analysis.writer_momentum == "BULLISH_PUT_WRITING":
                if (short_call_strike - spot) < 1.5 * Config.MIN_SPOT_CUSHION_PTS:
                    short_call_strike += step
                    call_oi_insights.append("Bullish put writer pressure -> cushioned Call wing 1 strike further OTM")

        # 4. Long Wing Strikes based on actual wing width
        long_put_strike = short_put_strike - actual_wing
        long_call_strike = short_call_strike + actual_wing

        # Validate contract specifications
        for stk in (short_put_strike, long_put_strike, short_call_strike, long_call_strike):
            is_valid_c, c_err, _ = ContractRegistry.validate_contract(
                "NIFTY", stk, analysis.expiry, provided_lot_size=series_lot_size
            )
            if not is_valid_c:
                rejections.append(c_err)

        if spot < short_put_strike:
            rejections.append(f"Spot price ({spot}) breached below proposed short put strike ({short_put_strike}).")
        if (spot - short_put_strike) < Config.MIN_SPOT_CUSHION_PTS:
            rejections.append(f"Put spot cushion ({spot - short_put_strike:.1f} pts) below minimum {Config.MIN_SPOT_CUSHION_PTS} pts.")

        if spot > short_call_strike:
            rejections.append(f"Spot price ({spot}) breached above proposed short call strike ({short_call_strike}).")
        if (short_call_strike - spot) < Config.MIN_SPOT_CUSHION_PTS:
            rejections.append(f"Call spot cushion ({short_call_strike - spot:.1f} pts) below minimum {Config.MIN_SPOT_CUSHION_PTS} pts.")

        # Quotes: Put Wing
        sp_q = chain.get(short_put_strike, {}).get("PE", {})
        lp_q = chain.get(long_put_strike, {}).get("PE", {})
        if not sp_q or not lp_q:
            rejections.append(f"Missing Put quotes for strikes {short_put_strike} or {long_put_strike}.")
            return cls._empty_candidate("IRON_CONDOR", "Iron Condor", engine_type, analysis, rejections)

        sp_bid = float(sp_q.get("bid", 0.0) or 0.0)
        lp_ask = float(lp_q.get("ask", 0.0) or 0.0)
        if sp_bid <= 0:
            rejections.append(f"Missing or non-positive Bid on short put {short_put_strike} PE.")
        if lp_ask <= 0:
            rejections.append(f"Missing or non-positive Ask on long put {long_put_strike} PE.")
        if sp_bid > 0 and lp_ask > 0 and sp_bid <= lp_ask:
            rejections.append(f"Crossed/zero credit on put wing: Short Bid (₹{sp_bid:.2f}) <= Long Ask (₹{lp_ask:.2f}).")

        # Quotes: Call Wing
        sc_q = chain.get(short_call_strike, {}).get("CE", {})
        lc_q = chain.get(long_call_strike, {}).get("CE", {})
        if not sc_q or not lc_q:
            rejections.append(f"Missing Call quotes for strikes {short_call_strike} or {long_call_strike}.")
            return cls._empty_candidate("IRON_CONDOR", "Iron Condor", engine_type, analysis, rejections)

        sc_bid = float(sc_q.get("bid", 0.0) or 0.0)
        lc_ask = float(lc_q.get("ask", 0.0) or 0.0)
        if sc_bid <= 0:
            rejections.append(f"Missing or non-positive Bid on short call {short_call_strike} CE.")
        if lc_ask <= 0:
            rejections.append(f"Missing or non-positive Ask on long call {long_call_strike} CE.")
        if sc_bid > 0 and lc_ask > 0 and sc_bid <= lc_ask:
            rejections.append(f"Crossed/zero credit on call wing: Short Bid (₹{sc_bid:.2f}) <= Long Ask (₹{lc_ask:.2f}).")

        put_credit = round(sp_bid - lp_ask, 2)
        call_credit = round(sc_bid - lc_ask, 2)

        if put_credit < Config.MIN_NET_CREDIT_PTS:
            rejections.append(f"Net credit ({put_credit:.2f} pts) below minimum {Config.MIN_NET_CREDIT_PTS:.2f} pts.")
        put_ratio = (put_credit / actual_wing * 100.0) if actual_wing > 0 else 0.0
        if put_ratio < Config.MIN_CREDIT_SPREAD_PCT:
            rejections.append(f"Credit ratio ({put_ratio:.1f}%) below minimum {Config.MIN_CREDIT_SPREAD_PCT:.1f}% of spread width.")

        if call_credit < Config.MIN_NET_CREDIT_PTS:
            rejections.append(f"Net credit ({call_credit:.2f} pts) below minimum {Config.MIN_NET_CREDIT_PTS:.2f} pts.")
        call_ratio = (call_credit / actual_wing * 100.0) if actual_wing > 0 else 0.0
        if call_ratio < Config.MIN_CREDIT_SPREAD_PCT:
            rejections.append(f"Credit ratio ({call_ratio:.1f}%) below minimum {Config.MIN_CREDIT_SPREAD_PCT:.1f}% of spread width.")

        combined_credit = round(put_credit + call_credit, 2)
        wing_width = actual_wing

        if combined_credit >= wing_width:
            rejections.append(f"Combined credit ({combined_credit:.2f} pts) cannot equal or exceed wing width ({wing_width:.2f} pts).")
            max_loss_pts = 0.0
        else:
            max_loss_pts = round(wing_width - combined_credit, 2)

        max_profit_inr = round(combined_credit * lot_size, 2)
        max_loss_inr = round(max_loss_pts * lot_size, 2)
        rr_ratio = round(max_profit_inr / max_loss_inr, 2) if max_loss_inr > 0 else 0.0

        legs = [
            OptionLegSpec(
                symbol=sp_q.get("symbol", f"NIFTY{int(short_put_strike)}PE"),
                strike=short_put_strike,
                option_type="PE",
                action="SELL",
                bid=sp_bid,
                ask=float(sp_q.get("ask", 0.0) or 0.0),
                ltp=float(sp_q.get("ltp", sp_bid)),
                quantity=lot_size,
            ),
            OptionLegSpec(
                symbol=lp_q.get("symbol", f"NIFTY{int(long_put_strike)}PE"),
                strike=long_put_strike,
                option_type="PE",
                action="BUY",
                bid=float(lp_q.get("bid", 0.0) or 0.0),
                ask=lp_ask,
                ltp=float(lp_q.get("ltp", lp_ask)),
                quantity=lot_size,
            ),
            OptionLegSpec(
                symbol=sc_q.get("symbol", f"NIFTY{int(short_call_strike)}CE"),
                strike=short_call_strike,
                option_type="CE",
                action="SELL",
                bid=sc_bid,
                ask=float(sc_q.get("ask", 0.0) or 0.0),
                ltp=float(sc_q.get("ltp", sc_bid)),
                quantity=lot_size,
            ),
            OptionLegSpec(
                symbol=lc_q.get("symbol", f"NIFTY{int(long_call_strike)}CE"),
                strike=long_call_strike,
                option_type="CE",
                action="BUY",
                bid=float(lc_q.get("bid", 0.0) or 0.0),
                ask=lc_ask,
                ltp=float(lc_q.get("ltp", lc_ask)),
                quantity=lot_size,
            ),
        ]

        breakevens = [round(short_put_strike - combined_credit, 1), round(short_call_strike + combined_credit, 1)]

        is_eligible = (len(rejections) == 0 and len(legs) == 4)
        oi_context = []
        if put_oi_insights:
            oi_context.append(put_oi_insights[-1])
        if call_oi_insights:
            oi_context.append(call_oi_insights[-1])
        oi_suffix = f" [{'; '.join(oi_context)}]" if oi_context else ""

        rationale = (
            f"Iron Condor 4-Leg: Put wing {int(short_put_strike)}/{int(long_put_strike)} PE, "
            f"Call wing {int(short_call_strike)}/{int(long_call_strike)} CE.{oi_suffix} "
            f"Net Credit: {combined_credit:.2f} pts (₹{max_profit_inr:,.2f}), Max Loss: ₹{max_loss_inr:,.2f}."
        ) if is_eligible else f"Rejections: {'; '.join(rejections)}"

        return SpreadCandidate(
            strategy_id="IRON_CONDOR",
            strategy_name="Iron Condor",
            engine_type=engine_type,
            symbol=analysis.symbol,
            expiry=analysis.expiry,
            lots=lots,
            quantity=lot_size,
            legs=legs,
            net_credit_pts=combined_credit,
            strike_width=wing_width,
            max_profit_inr=max_profit_inr,
            max_loss_pts=max_loss_pts,
            max_loss_inr=max_loss_inr,
            risk_reward_ratio=rr_ratio,
            breakevens=breakevens,
            is_eligible=is_eligible,
            rejection_reasons=rejections,
            confidence_score=analysis.confidence_score,
            rationale=rationale,
            spot_price=analysis.spot_price,
            data_status=getattr(analysis, "data_status", None),
        )

    @classmethod
    def build_iron_butterfly(
        cls,
        analysis: MarketAnalysisResult,
        chain: Dict[float, Dict[str, Any]],
        engine_type: str = "INTRADAY",
        lots: int = 1,
    ) -> SpreadCandidate:
        """Iron Butterfly 4-leg structure (Entry gated per specification)."""
        rejections = ["Iron Butterfly active entry is disabled pending specification threshold review."]
        return cls._empty_candidate("IRON_BUTTERFLY", "Iron Butterfly", engine_type, analysis, rejections)

    @classmethod
    def _empty_candidate(
        cls,
        strat_id: str,
        name: str,
        engine_type: str,
        analysis: MarketAnalysisResult,
        rejections: List[str],
    ) -> SpreadCandidate:
        return SpreadCandidate(
            strategy_id=strat_id,
            strategy_name=name,
            engine_type=engine_type,
            symbol=analysis.symbol,
            expiry=analysis.expiry,
            lots=1,
            quantity=Config.LOT_SIZE,
            legs=[],
            net_credit_pts=0.0,
            strike_width=0.0,
            max_profit_inr=0.0,
            max_loss_pts=0.0,
            max_loss_inr=0.0,
            risk_reward_ratio=0.0,
            breakevens=[],
            is_eligible=False,
            rejection_reasons=rejections,
            confidence_score=0.0,
            rationale=f"Rejection: {'; '.join(rejections)}",
            spot_price=analysis.spot_price,
            data_status=getattr(analysis, "data_status", None),
        )


class IntradayEngine:
    """
    Dedicated Intraday Selling Engine:
    - Focuses on 5-minute candle structure and intraday session limits.
    - Automatic cutoff at 15:20 IST.
    """
    engine_type = "INTRADAY"

    @classmethod
    def evaluate(cls, analysis: MarketAnalysisResult, chain: Dict[float, Dict[str, Any]], wing_width: float = 100.0) -> List[SpreadCandidate]:
        candidates = []
        if analysis and analysis.timestamp:
            ts_str = str(analysis.timestamp).strip()
            if " " in ts_str:
                now_time = ts_str.split(" ")[1][:8]
            elif "T" in ts_str:
                now_time = ts_str.split("T")[1][:8]
            elif ":" in ts_str and len(ts_str) >= 5:
                now_time = ts_str[:8]
            else:
                now_time = datetime.now(IST).strftime("%H:%M:%S")
        else:
            now_time = datetime.now(IST).strftime("%H:%M:%S")

        # Session Time Guards
        if now_time < Config.INTRADAY_ENTRY_START or now_time > Config.INTRADAY_ENTRY_CUTOFF:
            return [StrategyBuilder._empty_candidate(
                "INTRADAY_TIME_GUARD", "Intraday Engine", "INTRADAY", analysis,
                [f"Outside intraday trading window ({Config.INTRADAY_ENTRY_START} to {Config.INTRADAY_ENTRY_CUTOFF})"]
            )]

        # Generate candidates based on market regime
        if analysis.market_regime == "BALANCED_RANGE":
            candidates.append(StrategyBuilder.build_iron_condor(analysis, chain, engine_type="INTRADAY", wing_width=wing_width))
        else:
            candidates.append(StrategyBuilder._empty_candidate(
                "NO_REGIME_MATCH", "Intraday Engine", "INTRADAY", analysis,
                [f"Regime {analysis.market_regime} not supported for Iron Condor (requires BALANCED_RANGE)"]
            ))

        return candidates


class PositionalEngine:
    """
    Dedicated Positional Selling Engine:
    - Trades daily trends and holds defined-risk credit spreads overnight.
    - Pre-expiry exit enforced before 14:30 IST on expiry day.
    """
    engine_type = "POSITIONAL"

    @classmethod
    def evaluate(cls, analysis: MarketAnalysisResult, chain: Dict[float, Dict[str, Any]], wing_width: float = 100.0) -> List[SpreadCandidate]:
        candidates = []

        # Positional requires higher confidence threshold (>= 75%)
        if analysis.confidence_score < 75.0:
            return [StrategyBuilder._empty_candidate(
                "POSITIONAL_CONFIDENCE_GUARD", "Positional Engine", "POSITIONAL", analysis,
                [f"Confidence score ({analysis.confidence_score:.0f}%) below positional minimum 75%"]
            )]

        if analysis.market_regime == "BALANCED_RANGE":
            candidates.append(StrategyBuilder.build_iron_condor(analysis, chain, engine_type="POSITIONAL", wing_width=wing_width))
        else:
            candidates.append(StrategyBuilder._empty_candidate(
                "NO_REGIME_MATCH", "Positional Engine", "POSITIONAL", analysis,
                [f"Market regime ({analysis.market_regime}) not suitable for positional Iron Condor"]
            ))

        return candidates


@dataclass
class AutoSelectionResult:
    """Outcome of data-driven auto strategy selection."""
    selection_type: str  # "SINGLE", "MULTIPLE", or "NONE"
    selected_strategies: List[SpreadCandidate]
    primary_strategy: Optional[SpreadCandidate]
    engine_types: List[str]  # ["INTRADAY"], ["POSITIONAL"], or ["INTRADAY", "POSITIONAL"]
    selection_rationale: str
    data_evidence: List[str]
    market_regime: str
    market_bias: str
    timestamp: str
    no_trade_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selection_type": self.selection_type,
            "selected_strategies": [s.to_dict() for s in self.selected_strategies],
            "primary_strategy": self.primary_strategy.to_dict() if self.primary_strategy else None,
            "engine_types": self.engine_types,
            "selection_rationale": self.selection_rationale,
            "data_evidence": self.data_evidence,
            "market_regime": self.market_regime,
            "market_bias": self.market_bias,
            "timestamp": self.timestamp,
            "no_trade_reasons": self.no_trade_reasons,
        }


class AutoStrategySelector:
    """
    Data-Driven Strategy Selection Engine:
    Selects the optimal strategy based on:
    - Market Bias & Regime (Iron Condor on BALANCED_RANGE)
    - Put-Call Ratio (PCR)
    - Net Delta OI Writer Momentum
    - S1 Floor and R1 Ceiling Wall Proximity
    - Session Time Horizon (Intraday vs Positional)
    """

    @classmethod
    def evaluate_auto_selection(
        cls,
        analysis: MarketAnalysisResult,
        chain: Dict[float, Dict[str, Any]],
        wing_width: float = 100.0,
    ) -> AutoSelectionResult:
        if analysis and analysis.timestamp:
            ts_str = str(analysis.timestamp).strip()
            if " " in ts_str:
                now_str = ts_str.split(" ")[1][:8]
            elif "T" in ts_str:
                now_str = ts_str.split("T")[1][:8]
            elif ":" in ts_str and len(ts_str) >= 5:
                now_str = ts_str[:8]
            else:
                now_str = datetime.now(IST).strftime("%H:%M:%S")
        else:
            now_str = datetime.now(IST).strftime("%H:%M:%S")
        evidence = []

        # 1. Evaluate approved strategy (Iron Condor) on both Intraday and Positional horizons
        candidates_with_score: List[Tuple[SpreadCandidate, float, str]] = []

        ic_intra = StrategyBuilder.build_iron_condor(analysis, chain, engine_type="INTRADAY", wing_width=wing_width)
        ic_pos = StrategyBuilder.build_iron_condor(analysis, chain, engine_type="POSITIONAL", wing_width=wing_width)
        score_ic, rat_ic = cls._score_iron_condor(analysis, ic_intra)
        is_replay = getattr(analysis, "data_status", "") == "HISTORICAL_REPLAY"
        if (not Config.MONTHLY_EXPIRIES_ONLY or is_replay) and ic_intra.is_eligible and analysis.market_regime in ("BALANCED_RANGE", "SIDEWAYS"):
            candidates_with_score.append((ic_intra, score_ic, rat_ic))
        if (not is_replay) and ic_pos.is_eligible and analysis.market_regime in ("BALANCED_RANGE", "SIDEWAYS"):
            candidates_with_score.append((ic_pos, score_ic, rat_ic))

        # Sort eligible candidates by suitability score
        candidates_with_score.sort(key=lambda x: x[1], reverse=True)

        if not candidates_with_score:
            no_trade_reasons = []
            if analysis.market_regime != "BALANCED_RANGE":
                no_trade_reasons.append(f"Market regime is {analysis.market_regime} (Iron Condor requires BALANCED_RANGE): {'; '.join(analysis.evidence_factors)}")
            if analysis.data_quality_warnings:
                no_trade_reasons.extend(analysis.data_quality_warnings)

            # Collect individual strategy rejection reasons
            for c in [ic_intra, ic_pos]:
                if not c.is_eligible and c.rejection_reasons:
                    no_trade_reasons.append(f"{c.strategy_name} ({c.engine_type}) rejected: {'; '.join(c.rejection_reasons)}")

            if not no_trade_reasons:
                no_trade_reasons.append("No strategy meets suitability thresholds or positive risk/reward criteria.")

            return AutoSelectionResult(
                selection_type="NONE",
                selected_strategies=[],
                primary_strategy=None,
                engine_types=[],
                selection_rationale=f"NO TRADE: {no_trade_reasons[0]}",
                data_evidence=[f"Market bias is {analysis.market_bias}", f"Regime is {analysis.market_regime}", f"PCR: {analysis.pcr_oi:.2f}"],
                market_regime=analysis.market_regime,
                market_bias=analysis.market_bias,
                timestamp=now_str,
                no_trade_reasons=no_trade_reasons,
            )

        # Select primary candidate
        selected_candidates = []
        best_cand, best_score, best_rat = candidates_with_score[0]
        selected_candidates.append(best_cand)
        evidence.append(best_rat)

        # Check for secondary complementary strategy (e.g. Intraday + Positional Iron Condor)
        for cand, score, rat in candidates_with_score[1:]:
            is_duplicate = (best_cand.strategy_id == cand.strategy_id and best_cand.engine_type == cand.engine_type)
            if not is_duplicate and score >= 60.0:
                selected_candidates.append(cand)
                evidence.append(rat)
                break  # Maximum 2 concurrent auto-selected setups

        selection_type = "MULTIPLE" if len(selected_candidates) > 1 else "SINGLE"
        engines = list(dict.fromkeys(s.engine_type for s in selected_candidates))

        if selection_type == "MULTIPLE":
            names = " + ".join(f"{s.strategy_name} ({s.engine_type})" for s in selected_candidates)
            rationale = f"Dual setup auto-selected: {names}."
        else:
            rationale = f"Single strategy auto-selected: {best_cand.strategy_name} ({best_cand.engine_type}) based on {best_rat}."

        return AutoSelectionResult(
            selection_type=selection_type,
            selected_strategies=selected_candidates,
            primary_strategy=best_cand,
            engine_types=engines,
            selection_rationale=rationale,
            data_evidence=evidence,
            market_regime=analysis.market_regime,
            market_bias=analysis.market_bias,
            timestamp=now_str,
        )

    @classmethod
    def _score_iron_condor(cls, a: MarketAnalysisResult, c: SpreadCandidate) -> Tuple[float, str]:
        score = 0.0
        reasons = []
        if a.market_regime in ("BALANCED_RANGE", "SIDEWAYS"):
            score += 40.0
            reasons.append(f"Confirmed balanced range regime ({a.market_regime})")
        elif a.market_bias == "NEUTRAL":
            score += 15.0
            reasons.append(f"Neutral bias without confirmed balanced range ({a.market_regime})")
        if 0.85 <= a.pcr_oi <= 1.15:
            score += 25.0
            reasons.append(f"Balanced PCR ({a.pcr_oi:.2f})")
        if a.s1_support < a.spot_price < a.r1_resistance:
            score += 15.0
            reasons.append(f"Spot centered between S1 ({a.s1_support:.0f}) and R1 ({a.r1_resistance:.0f})")
        if c.net_credit_pts >= 25.0:
            score += 20.0
            reasons.append(f"Solid 4-leg credit ({c.net_credit_pts:.1f}pts)")
        return score, "; ".join(reasons) or "Neutral market structure"
