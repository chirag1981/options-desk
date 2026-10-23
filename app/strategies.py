"""
app/strategies.py — Four Defined-Risk Options-Selling Strategies & Dual Trading Engines
Implements:
1. Bull Put Credit Spread (BULL_PUT)
2. Bear Call Credit Spread (BEAR_CALL)
3. Iron Condor (IRON_CONDOR)
4. Iron Butterfly (IRON_BUTTERFLY)
5. IntradayEngine & PositionalEngine classes with independent state and lifecycle.
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
        - 2-leg spread (Bull Put / Bear Call): ~₹38,000 / lot
        - 4-leg Iron Condor: ~₹52,000 / lot (only higher wing margin + exposure)
        """
        lots = max(1, self.lots)
        if self.strategy_id == "IRON_CONDOR":
            return round(52000.0 * lots, 2)
        elif self.strategy_id in ("BULL_PUT", "BEAR_CALL"):
            return round(38000.0 * lots, 2)
        else:
            return round(42000.0 * lots, 2)

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
    def build_bull_put_spread(
        cls,
        analysis: MarketAnalysisResult,
        chain: Dict[float, Dict[str, Any]],
        engine_type: str = "INTRADAY",
        lots: int = 1,
    ) -> SpreadCandidate:
        from app.contracts import ContractRegistry
        rejections = []
        spot = analysis.spot_price
        s1 = analysis.s1_support
        step = Config.STRIKE_STEP
        lot_size = Config.get_lot_size() * lots

        # Select Short Put at or below S1; Long Put 1 step lower
        short_strike = math.floor(s1 / step) * step
        long_strike = short_strike - step

        # Validate contract specifications
        for stk in (short_strike, long_strike):
            is_valid_c, c_err, _ = ContractRegistry.validate_contract(
                "NIFTY", stk, analysis.expiry, provided_lot_size=Config.get_lot_size()
            )
            if not is_valid_c:
                rejections.append(c_err)

        if analysis.market_bias not in ("BULLISH", "STRONG_BULLISH"):
            rejections.append(f"Market bias ({analysis.market_bias}) is not bullish.")
        if spot < short_strike:
            rejections.append(f"Spot price ({spot}) breached below proposed short strike ({short_strike}).")
        if (spot - short_strike) < Config.MIN_SPOT_CUSHION_PTS:
            rejections.append(f"Spot cushion ({spot - short_strike:.1f} pts) is below minimum {Config.MIN_SPOT_CUSHION_PTS} pts.")

        short_q = chain.get(short_strike, {}).get("PE", {})
        long_q = chain.get(long_strike, {}).get("PE", {})

        if not short_q or not long_q:
            rejections.append(f"Missing Put quotes for strikes {short_strike} or {long_strike}.")
            return cls._empty_candidate("BULL_PUT", "Bull Put Credit Spread", engine_type, analysis, rejections)

        raw_bid = short_q.get("bid")
        raw_ask = long_q.get("ask")

        if raw_bid is None or float(raw_bid) <= 0:
            rejections.append(f"Missing or non-positive executable Bid quote on short strike {short_strike} PE.")
            sell_price = 0.0
        else:
            sell_price = float(raw_bid)

        if raw_ask is None or float(raw_ask) <= 0:
            rejections.append(f"Missing or non-positive executable Ask quote on long strike {long_strike} PE.")
            buy_price = 0.0
        else:
            buy_price = float(raw_ask)

        if sell_price > 0 and buy_price > 0 and sell_price <= buy_price:
            rejections.append(f"Crossed/zero credit spread: Short Bid (₹{sell_price:.2f}) <= Long Ask (₹{buy_price:.2f}).")

        net_credit = round(sell_price - buy_price, 2)
        strike_width = short_strike - long_strike
        max_loss_pts = round(strike_width - net_credit, 2)

        if net_credit < Config.MIN_NET_CREDIT_PTS:
            rejections.append(f"Net credit ({net_credit:.2f} pts) below minimum {Config.MIN_NET_CREDIT_PTS:.2f} pts.")

        credit_ratio = (net_credit / strike_width * 100.0) if strike_width > 0 else 0.0
        if credit_ratio < Config.MIN_CREDIT_SPREAD_PCT:
            rejections.append(f"Credit ratio ({credit_ratio:.1f}%) below minimum {Config.MIN_CREDIT_SPREAD_PCT:.1f}% of spread width.")

        max_profit_inr = round(net_credit * lot_size, 2)
        max_loss_inr = round(max_loss_pts * lot_size, 2)
        rr_ratio = round(max_profit_inr / max_loss_inr, 2) if max_loss_inr > 0 else 0.0
        breakeven = round(short_strike - net_credit, 1)

        legs = [
            OptionLegSpec(
                symbol=short_q.get("symbol", f"NIFTY{int(short_strike)}PE"),
                strike=short_strike,
                option_type="PE",
                action="SELL",
                bid=sell_price,
                ask=float(short_q.get("ask", 0.0) or 0.0),
                ltp=float(short_q.get("ltp", sell_price)),
                quantity=lot_size,
            ),
            OptionLegSpec(
                symbol=long_q.get("symbol", f"NIFTY{int(long_strike)}PE"),
                strike=long_strike,
                option_type="PE",
                action="BUY",
                bid=float(long_q.get("bid", 0.0) or 0.0),
                ask=buy_price,
                ltp=float(long_q.get("ltp", buy_price)),
                quantity=lot_size,
            ),
        ]

        is_eligible = (len(rejections) == 0)
        rationale = (
            f"Bull Put Spread: Sold {int(short_strike)} PE at ₹{sell_price:.2f}, bought {int(long_strike)} PE at ₹{buy_price:.2f}. "
            f"Net Credit: {net_credit:.2f} pts (₹{max_profit_inr:,.2f}), Max Loss: ₹{max_loss_inr:,.2f}."
        ) if is_eligible else f"Rejections: {'; '.join(rejections)}"

        return SpreadCandidate(
            strategy_id="BULL_PUT",
            strategy_name="Bull Put Credit Spread",
            engine_type=engine_type,
            symbol=analysis.symbol,
            expiry=analysis.expiry,
            lots=lots,
            quantity=lot_size,
            legs=legs,
            net_credit_pts=net_credit,
            strike_width=strike_width,
            max_profit_inr=max_profit_inr,
            max_loss_pts=max_loss_pts,
            max_loss_inr=max_loss_inr,
            risk_reward_ratio=rr_ratio,
            breakevens=[breakeven],
            is_eligible=is_eligible,
            rejection_reasons=rejections,
            confidence_score=analysis.confidence_score,
            rationale=rationale,
            spot_price=spot,
        )

    @classmethod
    def build_bear_call_spread(
        cls,
        analysis: MarketAnalysisResult,
        chain: Dict[float, Dict[str, Any]],
        engine_type: str = "INTRADAY",
        lots: int = 1,
    ) -> SpreadCandidate:
        from app.contracts import ContractRegistry
        rejections = []
        spot = analysis.spot_price
        r1 = analysis.r1_resistance
        step = Config.STRIKE_STEP
        lot_size = Config.get_lot_size() * lots

        # Select Short Call at or above R1; Long Call 1 step higher
        short_strike = math.ceil(r1 / step) * step
        long_strike = short_strike + step

        # Validate contract specifications
        for stk in (short_strike, long_strike):
            is_valid_c, c_err, _ = ContractRegistry.validate_contract(
                "NIFTY", stk, analysis.expiry, provided_lot_size=Config.get_lot_size()
            )
            if not is_valid_c:
                rejections.append(c_err)

        if analysis.market_bias not in ("BEARISH", "STRONG_BEARISH"):
            rejections.append(f"Market bias ({analysis.market_bias}) is not bearish.")
        if spot > short_strike:
            rejections.append(f"Spot price ({spot}) breached above proposed short strike ({short_strike}).")
        if (short_strike - spot) < Config.MIN_SPOT_CUSHION_PTS:
            rejections.append(f"Spot cushion ({short_strike - spot:.1f} pts) is below minimum {Config.MIN_SPOT_CUSHION_PTS} pts.")

        short_q = chain.get(short_strike, {}).get("CE", {})
        long_q = chain.get(long_strike, {}).get("CE", {})

        if not short_q or not long_q:
            rejections.append(f"Missing Call quotes for strikes {short_strike} or {long_strike}.")
            return cls._empty_candidate("BEAR_CALL", "Bear Call Credit Spread", engine_type, analysis, rejections)

        raw_bid = short_q.get("bid")
        raw_ask = long_q.get("ask")

        if raw_bid is None or float(raw_bid) <= 0:
            rejections.append(f"Missing or non-positive executable Bid quote on short strike {short_strike} CE.")
            sell_price = 0.0
        else:
            sell_price = float(raw_bid)

        if raw_ask is None or float(raw_ask) <= 0:
            rejections.append(f"Missing or non-positive executable Ask quote on long strike {long_strike} CE.")
            buy_price = 0.0
        else:
            buy_price = float(raw_ask)

        if sell_price > 0 and buy_price > 0 and sell_price <= buy_price:
            rejections.append(f"Crossed/zero credit spread: Short Bid (₹{sell_price:.2f}) <= Long Ask (₹{buy_price:.2f}).")

        net_credit = round(sell_price - buy_price, 2)
        strike_width = long_strike - short_strike
        max_loss_pts = round(strike_width - net_credit, 2)

        if net_credit < Config.MIN_NET_CREDIT_PTS:
            rejections.append(f"Net credit ({net_credit:.2f} pts) below minimum {Config.MIN_NET_CREDIT_PTS:.2f} pts.")

        credit_ratio = (net_credit / strike_width * 100.0) if strike_width > 0 else 0.0
        if credit_ratio < Config.MIN_CREDIT_SPREAD_PCT:
            rejections.append(f"Credit ratio ({credit_ratio:.1f}%) below minimum {Config.MIN_CREDIT_SPREAD_PCT:.1f}% of spread width.")

        max_profit_inr = round(net_credit * lot_size, 2)
        max_loss_inr = round(max_loss_pts * lot_size, 2)
        rr_ratio = round(max_profit_inr / max_loss_inr, 2) if max_loss_inr > 0 else 0.0
        breakeven = round(short_strike + net_credit, 1)

        legs = [
            OptionLegSpec(
                symbol=short_q.get("symbol", f"NIFTY{int(short_strike)}CE"),
                strike=short_strike,
                option_type="CE",
                action="SELL",
                bid=sell_price,
                ask=float(short_q.get("ask", 0.0) or 0.0),
                ltp=float(short_q.get("ltp", sell_price)),
                quantity=lot_size,
            ),
            OptionLegSpec(
                symbol=long_q.get("symbol", f"NIFTY{int(long_strike)}CE"),
                strike=long_strike,
                option_type="CE",
                action="BUY",
                bid=float(long_q.get("bid", 0.0) or 0.0),
                ask=buy_price,
                ltp=float(long_q.get("ltp", buy_price)),
                quantity=lot_size,
            ),
        ]

        is_eligible = (len(rejections) == 0)
        rationale = (
            f"Bear Call Spread: Sold {int(short_strike)} CE at ₹{sell_price:.2f}, bought {int(long_strike)} CE at ₹{buy_price:.2f}. "
            f"Net Credit: {net_credit:.2f} pts (₹{max_profit_inr:,.2f}), Max Loss: ₹{max_loss_inr:,.2f}."
        ) if is_eligible else f"Rejections: {'; '.join(rejections)}"

        return SpreadCandidate(
            strategy_id="BEAR_CALL",
            strategy_name="Bear Call Credit Spread",
            engine_type=engine_type,
            symbol=analysis.symbol,
            expiry=analysis.expiry,
            lots=lots,
            quantity=lot_size,
            legs=legs,
            net_credit_pts=net_credit,
            strike_width=strike_width,
            max_profit_inr=max_profit_inr,
            max_loss_pts=max_loss_pts,
            max_loss_inr=max_loss_inr,
            risk_reward_ratio=rr_ratio,
            breakevens=[breakeven],
            is_eligible=is_eligible,
            rejection_reasons=rejections,
            confidence_score=analysis.confidence_score,
            rationale=rationale,
            spot_price=spot,
        )

    @classmethod
    def build_iron_condor(
        cls,
        analysis: MarketAnalysisResult,
        chain: Dict[float, Dict[str, Any]],
        engine_type: str = "INTRADAY",
        lots: int = 1,
    ) -> SpreadCandidate:
        rejections = []
        if analysis.market_regime not in ("BALANCED_RANGE", "SIDEWAYS") and analysis.market_bias != "NEUTRAL":
            rejections.append(f"Market regime ({analysis.market_regime}) is not balanced range-bound.")

        # Build Put side and Call side
        put_cand = cls.build_bull_put_spread(analysis, chain, engine_type=engine_type, lots=lots)
        call_cand = cls.build_bear_call_spread(analysis, chain, engine_type=engine_type, lots=lots)

        # For condor, individual bias rejections are relaxed because market is range-bound
        put_rejs = [r for r in put_cand.rejection_reasons if "bias" not in r.lower()]
        call_rejs = [r for r in call_cand.rejection_reasons if "bias" not in r.lower()]

        rejections.extend(put_rejs)
        rejections.extend(call_rejs)

        combined_credit = round(put_cand.net_credit_pts + call_cand.net_credit_pts, 2)
        wing_width = max(put_cand.strike_width, call_cand.strike_width)

        if combined_credit >= wing_width:
            rejections.append(f"Combined credit ({combined_credit:.2f} pts) cannot equal or exceed wing width ({wing_width:.2f} pts).")
            max_loss_pts = 0.0
        else:
            max_loss_pts = round(wing_width - combined_credit, 2)

        lot_size = Config.LOT_SIZE * lots
        max_profit_inr = round(combined_credit * lot_size, 2)
        max_loss_inr = round(max_loss_pts * lot_size, 2)
        rr_ratio = round(max_profit_inr / max_loss_inr, 2) if max_loss_inr > 0 else 0.0

        legs = put_cand.legs + call_cand.legs
        breakevens = (
            [round(put_cand.legs[0].strike - combined_credit, 1), round(call_cand.legs[0].strike + combined_credit, 1)]
            if len(legs) == 4 else []
        )

        is_eligible = (len(rejections) == 0 and len(legs) == 4)
        rationale = (
            f"Iron Condor 4-Leg: Put wing {int(put_cand.legs[0].strike)}/{int(put_cand.legs[1].strike)} PE, "
            f"Call wing {int(call_cand.legs[0].strike)}/{int(call_cand.legs[1].strike)} CE. "
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
        )


class IntradayEngine:
    """
    Dedicated Intraday Selling Engine:
    - Focuses on 5-minute candle structure and intraday session limits.
    - Automatic cutoff at 15:20 IST.
    """
    engine_type = "INTRADAY"

    @classmethod
    def evaluate(cls, analysis: MarketAnalysisResult, chain: Dict[float, Dict[str, Any]]) -> List[SpreadCandidate]:
        candidates = []
        now_time = datetime.now(IST).strftime("%H:%M:%S")

        # Session Time Guards
        if now_time < Config.INTRADAY_ENTRY_START or now_time > Config.INTRADAY_ENTRY_CUTOFF:
            return [StrategyBuilder._empty_candidate(
                "INTRADAY_TIME_GUARD", "Intraday Engine", "INTRADAY", analysis,
                [f"Outside intraday trading window ({Config.INTRADAY_ENTRY_START} to {Config.INTRADAY_ENTRY_CUTOFF})"]
            )]

        # Generate candidates based on market regime
        if analysis.market_bias in ("BULLISH", "STRONG_BULLISH"):
            candidates.append(StrategyBuilder.build_bull_put_spread(analysis, chain, engine_type="INTRADAY"))
        elif analysis.market_bias in ("BEARISH", "STRONG_BEARISH"):
            candidates.append(StrategyBuilder.build_bear_call_spread(analysis, chain, engine_type="INTRADAY"))
        elif analysis.market_regime == "BALANCED_RANGE":
            candidates.append(StrategyBuilder.build_iron_condor(analysis, chain, engine_type="INTRADAY"))
        else:
            candidates.append(StrategyBuilder._empty_candidate(
                "NO_REGIME_MATCH", "Intraday Engine", "INTRADAY", analysis,
                [f"Choppy or indeterminate market regime ({analysis.market_regime})"]
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
    def evaluate(cls, analysis: MarketAnalysisResult, chain: Dict[float, Dict[str, Any]]) -> List[SpreadCandidate]:
        candidates = []

        # Positional requires higher confidence threshold (>= 75%)
        if analysis.confidence_score < 75.0:
            return [StrategyBuilder._empty_candidate(
                "POSITIONAL_CONFIDENCE_GUARD", "Positional Engine", "POSITIONAL", analysis,
                [f"Confidence score ({analysis.confidence_score:.0f}%) below positional minimum 75%"]
            )]

        if analysis.market_bias in ("BULLISH", "STRONG_BULLISH"):
            candidates.append(StrategyBuilder.build_bull_put_spread(analysis, chain, engine_type="POSITIONAL"))
        elif analysis.market_bias in ("BEARISH", "STRONG_BEARISH"):
            candidates.append(StrategyBuilder.build_bear_call_spread(analysis, chain, engine_type="POSITIONAL"))
        elif analysis.market_regime == "BALANCED_RANGE":
            candidates.append(StrategyBuilder.build_iron_condor(analysis, chain, engine_type="POSITIONAL"))
        else:
            candidates.append(StrategyBuilder._empty_candidate(
                "NO_REGIME_MATCH", "Positional Engine", "POSITIONAL", analysis,
                [f"Market regime ({analysis.market_regime}) not suitable for overnight positional holding"]
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
        }


class AutoStrategySelector:
    """
    Data-Driven Strategy Selection Engine:
    Selects the optimal strategy (or multiple strategies) based on:
    - Market Bias & Regime
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
    ) -> AutoSelectionResult:
        now_str = datetime.now(IST).strftime("%H:%M:%S")
        evidence = []

        # 1. Evaluate all 3 approved strategies on both Intraday and Positional horizons
        candidates_with_score: List[Tuple[SpreadCandidate, float, str]] = []

        # Strategy 1: Bull Put Spread (Intraday & Positional)
        bp_intra = StrategyBuilder.build_bull_put_spread(analysis, chain, engine_type="INTRADAY")
        bp_pos = StrategyBuilder.build_bull_put_spread(analysis, chain, engine_type="POSITIONAL")
        score_bp, rat_bp = cls._score_bull_put(analysis, bp_intra)
        if bp_intra.is_eligible:
            candidates_with_score.append((bp_intra, score_bp, rat_bp))
        if bp_pos.is_eligible:
            candidates_with_score.append((bp_pos, score_bp * 0.95, rat_bp))

        # Strategy 2: Bear Call Spread (Intraday & Positional)
        bc_intra = StrategyBuilder.build_bear_call_spread(analysis, chain, engine_type="INTRADAY")
        bc_pos = StrategyBuilder.build_bear_call_spread(analysis, chain, engine_type="POSITIONAL")
        score_bc, rat_bc = cls._score_bear_call(analysis, bc_intra)
        if bc_intra.is_eligible:
            candidates_with_score.append((bc_intra, score_bc, rat_bc))
        if bc_pos.is_eligible:
            candidates_with_score.append((bc_pos, score_bc * 0.95, rat_bc))

        # Strategy 3: Iron Condor (Intraday & Positional)
        ic_intra = StrategyBuilder.build_iron_condor(analysis, chain, engine_type="INTRADAY")
        ic_pos = StrategyBuilder.build_iron_condor(analysis, chain, engine_type="POSITIONAL")
        score_ic, rat_ic = cls._score_iron_condor(analysis, ic_intra)
        if ic_intra.is_eligible:
            candidates_with_score.append((ic_intra, score_ic, rat_ic))
        if ic_pos.is_eligible:
            candidates_with_score.append((ic_pos, score_ic * 0.95, rat_ic))

        # Sort eligible candidates by suitability score
        candidates_with_score.sort(key=lambda x: x[1], reverse=True)

        if not candidates_with_score:
            return AutoSelectionResult(
                selection_type="NONE",
                selected_strategies=[],
                primary_strategy=None,
                engine_types=[],
                selection_rationale="No strategy currently meets market data criteria or session risk thresholds.",
                data_evidence=[f"Market bias is {analysis.market_bias}", f"Regime is {analysis.market_regime}", f"PCR: {analysis.pcr_oi:.2f}"],
                market_regime=analysis.market_regime,
                market_bias=analysis.market_bias,
                timestamp=now_str,
            )

        # Select primary candidate
        selected_candidates = []
        best_cand, best_score, best_rat = candidates_with_score[0]
        selected_candidates.append(best_cand)
        evidence.append(best_rat)

        # Check for secondary complementary strategy (e.g. Intraday Bull Put + Positional Bull Put, or Intraday + Positional Iron Condor)
        for cand, score, rat in candidates_with_score[1:]:
            # Never combine conflicting directional strategies (e.g. Bull Put + Bear Call)
            is_conflicting = (
                (best_cand.strategy_id == "BULL_PUT" and cand.strategy_id == "BEAR_CALL") or
                (best_cand.strategy_id == "BEAR_CALL" and cand.strategy_id == "BULL_PUT")
            )
            # Avoid duplicate same strategy and same engine
            is_duplicate = (best_cand.strategy_id == cand.strategy_id and best_cand.engine_type == cand.engine_type)

            if not is_conflicting and not is_duplicate and score >= 60.0:
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
    def _score_bull_put(cls, a: MarketAnalysisResult, c: SpreadCandidate) -> Tuple[float, str]:
        score = 0.0
        reasons = []
        if a.market_bias in ("BULLISH", "STRONG_BULLISH"):
            score += 40.0
            reasons.append(f"Bullish bias ({a.market_bias})")
        if a.total_pe_change_oi > a.total_ce_change_oi:
            score += 25.0
            reasons.append(f"Put writing surge (+{a.total_pe_change_oi / 1000:.0f}k PE OI)")
        if a.pcr_oi >= 1.0:
            score += 15.0
            reasons.append(f"Supportive PCR ({a.pcr_oi:.2f})")
        if a.spot_price > a.s1_support:
            score += 10.0
            reasons.append(f"Spot {a.spot_price - a.s1_support:.0f}pts above S1 floor")
        if c.net_credit_pts >= 15.0:
            score += 10.0
            reasons.append(f"Net credit {c.net_credit_pts:.1f}pts")
        return score, "; ".join(reasons) or "Neutral market structure"

    @classmethod
    def _score_bear_call(cls, a: MarketAnalysisResult, c: SpreadCandidate) -> Tuple[float, str]:
        score = 0.0
        reasons = []
        if a.market_bias in ("BEARISH", "STRONG_BEARISH"):
            score += 40.0
            reasons.append(f"Bearish bias ({a.market_bias})")
        if a.total_ce_change_oi > a.total_pe_change_oi:
            score += 25.0
            reasons.append(f"Call writing overhead (+{a.total_ce_change_oi / 1000:.0f}k CE OI)")
        if a.pcr_oi <= 0.95:
            score += 15.0
            reasons.append(f"Heavy PCR ceiling ({a.pcr_oi:.2f})")
        if a.spot_price < a.r1_resistance:
            score += 10.0
            reasons.append(f"Spot {a.r1_resistance - a.spot_price:.0f}pts below R1 ceiling")
        if c.net_credit_pts >= 15.0:
            score += 10.0
            reasons.append(f"Net credit {c.net_credit_pts:.1f}pts")
        return score, "; ".join(reasons) or "Neutral market structure"

    @classmethod
    def _score_iron_condor(cls, a: MarketAnalysisResult, c: SpreadCandidate) -> Tuple[float, str]:
        score = 0.0
        reasons = []
        if a.market_regime == "BALANCED_RANGE" or a.market_bias == "NEUTRAL":
            score += 40.0
            reasons.append(f"Range-bound regime ({a.market_regime})")
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
