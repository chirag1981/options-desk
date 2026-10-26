"""
app/trading.py — Paper Trading Simulator, Multi-Leg Positions & Lifecycle Manager
Handles:
1. Realistic Simulated Paper Fills at Live Bid/Ask.
2. Statutory NSE Transaction Costs Calculation (Brokerage, STT, Exchange fee, SEBI, GST).
3. Position Lifecycle Management & Mark-to-Market (MTM) Valuation.
4. Stop-loss, Profit-target, and Intraday Session Cutoff Exits.
5. Controlled Adjustment Recommendations.
"""

import logging
import threading
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

from app.config import Config
from app.strategies import SpreadCandidate
from app.models import TradePosition, PositionLeg
from app.risk import PortfolioRiskManager
from app.extensions import db

log = logging.getLogger("trading")
IST = ZoneInfo("Asia/Kolkata")


class TransactionCostEngine:
    """
    Calculates official statutory NSE and broker transaction fees and taxes for options trading.
    Intended Broker: FYERS Securities (Online discount broker).
    
    Authoritative Statutory & Broker Sources:
    1. Brokerage: Flat ₹20 per executed order leg (FYERS Tariff Schedule).
    2. STT (Securities Transaction Tax) on Option Sell Turnover:
       - Prior to Oct 1, 2024: 0.0625% (0.000625)
       - Oct 1, 2024 to March 31, 2026: 0.10% (0.0010) (Finance Act No. 2, 2024)
       - Effective April 1, 2026: 0.15% (0.0015) (Union Budget 2026 / NSE Circular)
    3. NSE Exchange Turnover Charges (Options Premium):
       - Prior to Oct 1, 2024: 0.05% (0.00050)
       - Effective Oct 1, 2024: 0.03503% (0.0003503 or ₹35.03/lakh)
         (NSE Circular Ref: NSE/FA/64232 under SEBI True-to-Label directive SEBI/HO/MRD/TPD-1/P/CIR/2024/92)
    4. IPFT (Investor Protection Fund Trust):
       - 0.0005% (0.000005 or ₹50 per crore) on total premium turnover (NSE Circular).
    5. SEBI Turnover Fee:
       - 0.0001% (0.000001 or ₹10 per crore) on total premium turnover (SEBI (Payment of Fees) Regulations).
    6. Stamp Duty (Indian Stamp Act):
       - 0.003% (0.00003 or ₹300 per crore) on buy-side option turnover.
    7. Clearing Charges:
       - 0.0% (FYERS self-clears through NSE Clearing Limited / NCL).
    8. GST:
       - 18% on (Brokerage + Exchange turnover charges + SEBI turnover fee + IPFT).
    """

    @classmethod
    def get_stt_sell_rate(cls, as_of_date: Optional[date] = None) -> float:
        """Determines the statutory STT rate on option sales based on trade execution date."""
        from datetime import date as dt_date
        if as_of_date is None:
            as_of_date = datetime.now(IST).date()

        if as_of_date < dt_date(2024, 10, 1):
            return 0.000625  # 0.0625%
        elif as_of_date < dt_date(2026, 4, 1):
            return 0.001000  # 0.10%
        else:
            return 0.001500  # 0.15% effective April 1, 2026

    @classmethod
    def get_exchange_turnover_rate(cls, as_of_date: Optional[date] = None) -> float:
        """
        Determines the NSE exchange turnover charge rate on options premium.
        Authoritative Sources:
        - Prior to Oct 1, 2024: 0.05% (0.00050)
        - Effective Oct 1, 2024: 0.03503% (0.0003503 or ₹35.03/lakh)
          (NSE Circular Ref: NSE/FA/64232 under SEBI Circular SEBI/HO/MRD/TPD-1/P/CIR/2024/92)
        """
        from datetime import date as dt_date
        if as_of_date is None:
            as_of_date = datetime.now(IST).date()

        if as_of_date < dt_date(2024, 10, 1):
            return Config.EXCHANGE_TURNOVER_LEGACY_PCT
        return Config.EXCHANGE_TURNOVER_PCT

    @classmethod
    def get_ipft_rate(cls, as_of_date: Optional[date] = None) -> float:
        """Determines the Investor Protection Fund Trust (IPFT) rate on options premium."""
        return Config.IPFT_TURNOVER_PCT

    @classmethod
    def get_sebi_turnover_rate(cls, as_of_date: Optional[date] = None) -> float:
        """Determines the SEBI turnover fee rate on options premium."""
        return Config.SEBI_TURNOVER_PCT

    @classmethod
    def get_stamp_duty_buy_rate(cls, as_of_date: Optional[date] = None) -> float:
        """Determines the Stamp Duty rate on buy-side options premium."""
        return Config.STAMP_DUTY_BUY_PCT

    @classmethod
    def calculate_order_costs_breakdown(
        cls,
        legs: List[Dict[str, Any]],
        lots: int = 1,
        trade_date: Optional[date] = None,
    ) -> Dict[str, float]:
        """
        Computes detailed itemized statutory and broker fees for entering or exiting spread legs.
        Returns exact floating-point breakdown of all cost components.
        """
        total_brokerage = len(legs) * Config.BROKERAGE_PER_ORDER
        total_turnover = 0.0
        buy_turnover = 0.0
        sell_turnover = 0.0

        stt_sell_rate = cls.get_stt_sell_rate(trade_date)
        exchange_rate = cls.get_exchange_turnover_rate(trade_date)
        ipft_rate = cls.get_ipft_rate(trade_date)
        sebi_rate = cls.get_sebi_turnover_rate(trade_date)
        stamp_duty_rate = cls.get_stamp_duty_buy_rate(trade_date)
        authoritative_lot_size = Config.get_lot_size(trade_date)

        for leg in legs:
            action = leg.get("action", "").upper()
            price = float(leg.get("price", leg.get("entry_price", 0.0)) or 0.0)
            qty = int(leg.get("quantity", authoritative_lot_size * lots))
            turnover = price * qty
            total_turnover += turnover

            if action == "SELL":
                sell_turnover += turnover
            elif action == "BUY":
                buy_turnover += turnover

        stt = sell_turnover * stt_sell_rate
        exchange_fee = total_turnover * exchange_rate
        ipft = total_turnover * ipft_rate
        sebi_fee = total_turnover * sebi_rate
        stamp_duty = buy_turnover * stamp_duty_rate
        clearing_fee = total_turnover * Config.CLEARING_CHARGES_PCT

        # GST is 18% on (Brokerage + Exchange turnover fee + SEBI turnover fee + IPFT)
        taxable_base = total_brokerage + exchange_fee + sebi_fee + ipft
        gst = taxable_base * Config.GST_PCT

        total = total_brokerage + stt + exchange_fee + ipft + sebi_fee + stamp_duty + clearing_fee + gst

        return {
            "brokerage": total_brokerage,
            "turnover": total_turnover,
            "buy_turnover": buy_turnover,
            "sell_turnover": sell_turnover,
            "stt": stt,
            "exchange_fee": exchange_fee,
            "ipft": ipft,
            "sebi_fee": sebi_fee,
            "stamp_duty": stamp_duty,
            "clearing_fee": clearing_fee,
            "gst_taxable_base": taxable_base,
            "gst": gst,
            "total": total,
        }

    @classmethod
    def calculate_order_costs(
        cls,
        legs: List[Dict[str, Any]],
        lots: int = 1,
        trade_date: Optional[date] = None,
    ) -> float:
        """
        Computes total statutory and broker fees rounded to 2 decimal places.
        Applies date-effective STT and exchange rates so historical trades are accurately calculated.
        """
        breakdown = cls.calculate_order_costs_breakdown(legs, lots=lots, trade_date=trade_date)
        return round(breakdown["total"], 2)



class PaperTradingManager:
    """
    Manages paper positions, simulated order fills, mark-to-market updates, and exits.
    """
    _execution_lock = threading.Lock()

    @classmethod
    def execute_candidate(cls, candidate: SpreadCandidate, analysis: Optional[Any] = None) -> Tuple[bool, str, Optional[TradePosition]]:
        """
        Executes a paper-trade spread candidate atomically.
        Enforces execution safety gate, portfolio risk limits, positive spot validation,
        and uncovered-short checks non-bypassably.
        """
        if not candidate.is_eligible:
            return False, f"Candidate not eligible: {'; '.join(candidate.rejection_reasons)}", None

        SUPPORTED_STRATEGIES = {"IRON_CONDOR"}
        if candidate.strategy_id not in SUPPORTED_STRATEGIES:
            return False, f"Strategy '{candidate.strategy_id}' is not supported for execution. Supported strategies: {', '.join(sorted(SUPPORTED_STRATEGIES))}.", None

        if candidate.has_uncovered_short:
            return False, "CRITICAL: Uncovered short option detected. Execution rejected.", None

        if not candidate.spot_price or candidate.spot_price <= 0:
            return False, "Invalid or missing spot price for candidate. Execution rejected.", None

        # Authoritative Execution Safety Gate Validation
        from app.safety import ExecutionSafetyGate
        is_safe, safety_msg = ExecutionSafetyGate.validate_execution(candidate, analysis)
        if not is_safe:
            return False, f"Execution safety gate rejected trade: {safety_msg}", None

        # Authoritative Portfolio Risk Evaluation
        from app.risk import PortfolioRiskManager
        risk_eval = PortfolioRiskManager.evaluate_candidate_risk(candidate, engine_type=candidate.engine_type)
        if not risk_eval.approved:
            return False, f"Portfolio risk limit exceeded: {risk_eval.summary}", None

        try:
            with cls._execution_lock:
                existing = TradePosition.query.filter_by(
                    strategy_id=candidate.strategy_id,
                    engine_type=candidate.engine_type,
                    status="OPEN"
                ).first()
                if existing:
                    return False, f"An open position for {candidate.strategy_id} ({candidate.engine_type}) already exists (ID: #{existing.id}). Duplicate entry blocked.", None

                # 1. Calculate itemized entry costs
                legs_data = [
                    {"action": l.action, "price": l.bid if l.action == "SELL" else l.ask, "quantity": l.quantity}
                    for l in candidate.legs
                ]
                now_dt = datetime.now(IST)
                cost_breakdown = TransactionCostEngine.calculate_order_costs_breakdown(legs_data, lots=candidate.lots, trade_date=now_dt.date())
                entry_costs = round(cost_breakdown["total"], 2)

                # 2. Stop loss & target thresholds
                # Target = buyback at 30% of credit (capture 70% profit)
                target_debit = round(candidate.net_credit_pts * 0.30, 2)
                # Stop loss = buyback at 1.5x credit
                stop_loss_debit = round(candidate.net_credit_pts * 1.50, 2)

                # 3. Create TradePosition record with itemized entry costs
                position = TradePosition(
                    engine_type=candidate.engine_type,
                    strategy_id=candidate.strategy_id,
                    strategy_name=candidate.strategy_name,
                    symbol=candidate.symbol,
                    expiry=candidate.expiry,
                    lots=candidate.lots,
                    quantity=candidate.quantity,
                    entry_time=now_dt,
                    entry_spot=float(candidate.spot_price),
                    entry_net_credit_pts=candidate.net_credit_pts,
                    max_loss_inr=candidate.max_loss_inr,
                    max_profit_inr=candidate.max_profit_inr,
                    target_credit_pts=target_debit,
                    stop_loss_pts=stop_loss_debit,
                    current_net_debit_pts=candidate.net_credit_pts,
                    current_pnl_inr=0.0,
                    status="OPEN",
                    transaction_costs_inr=entry_costs,
                    entry_brokerage_inr=cost_breakdown["brokerage"],
                    entry_stt_inr=cost_breakdown["stt"],
                    entry_exchange_fees_inr=cost_breakdown["exchange_fee"],
                    entry_gst_inr=cost_breakdown["gst"],
                    entry_stamp_duty_inr=cost_breakdown["stamp_duty"],
                    entry_sebi_ipft_inr=cost_breakdown["sebi_fee"] + cost_breakdown["ipft"],
                )
                db.session.add(position)
                db.session.flush()

                # 4. Create individual PositionLeg records with full quote evidence
                data_src = getattr(candidate, "data_status", "FYERS_LIVE")
                for l in candidate.legs:
                    fill_price = l.bid if l.action == "SELL" else l.ask
                    bid_val = float(l.bid) if l.bid is not None else None
                    ask_val = float(l.ask) if l.ask is not None else None
                    spread_pct = round(((ask_val - bid_val) / ask_val) * 100.0, 2) if (ask_val and bid_val and ask_val > 0) else None

                    pos_leg = PositionLeg(
                        position_id=position.id,
                        symbol=l.symbol,
                        strike=l.strike,
                        option_type=l.option_type,
                        action=l.action,
                        quantity=l.quantity,
                        entry_price=fill_price,
                        current_price=fill_price,
                        entry_bid=bid_val,
                        entry_ask=ask_val,
                        entry_spread_pct=spread_pct,
                        entry_quote_timestamp=now_dt,
                        entry_data_source=data_src,
                    )
                    db.session.add(pos_leg)

                db.session.commit()
                log.info(f"Opened paper position #{position.id} ({candidate.strategy_name}) for {candidate.engine_type}.")
                return True, f"Successfully executed {candidate.strategy_name} (#{position.id}).", position

        except Exception as e:
            db.session.rollback()
            log.error(f"Paper trade execution failed: {e}")
            return False, f"Execution failed: {str(e)}", None

    @classmethod
    def update_open_positions_mtm(cls, chain_by_strike: Dict[float, Dict[str, Any]], spot_price: float) -> List[Dict[str, Any]]:
        """
        Updates Mark-to-Market (MTM) valuations and checks stop-loss / profit-targets.
        """
        now_dt = datetime.now(IST)
        now_time = now_dt.strftime("%H:%M:%S")
        updated_positions = []

        try:
            open_positions = TradePosition.query.filter_by(status="OPEN").all()
            for pos in open_positions:
                buyback_cost = 0.0
                all_quotes_valid = True

                # Evaluate current price of each leg
                for leg in pos.legs:
                    stk_quotes = chain_by_strike.get(leg.strike, {}).get(leg.option_type, {})
                    if not stk_quotes:
                        all_quotes_valid = False
                        break

                    # To close a position: buy back SELL legs at Ask; sell BUY legs at Bid
                    if leg.action == "SELL":
                        raw_ask = stk_quotes.get("ask")
                        if raw_ask is None or float(raw_ask) <= 0:
                            all_quotes_valid = False
                            break
                        current_close_price = float(raw_ask)
                        buyback_cost += current_close_price
                    elif leg.action == "BUY":
                        raw_bid = stk_quotes.get("bid")
                        if raw_bid is None or float(raw_bid) <= 0:
                            all_quotes_valid = False
                            break
                        current_close_price = float(raw_bid)
                        buyback_cost -= current_close_price

                    leg.current_price = current_close_price

                if not all_quotes_valid:
                    try:
                        from app.models import PositionMTMSnapshot
                        import hashlib
                        minute_tag = now_dt.strftime("%Y%m%d%H%M")
                        mtm_idem = hashlib.sha256(f"{pos.id}_{minute_tag}_unavail".encode()).hexdigest()[:32]
                        if not PositionMTMSnapshot.query.filter_by(idempotency_key=mtm_idem).first():
                            snap_rec = PositionMTMSnapshot(
                                position_id=pos.id,
                                timestamp=now_dt,
                                spot_price=spot_price,
                                net_debit_pts=None,
                                unrealized_gross_pnl_inr=None,
                                known_transaction_costs_inr=pos.transaction_costs_inr,
                                unrealized_net_pnl_inr=None,
                                data_quality_status="VALUATION_UNAVAILABLE",
                                leg_prices_json=None,
                                idempotency_key=mtm_idem,
                            )
                            db.session.add(snap_rec)
                    except Exception as ex_u:
                        log.warning(f"Failed to record unavailable MTM snapshot: {ex_u}")
                    continue

                pos.current_net_debit_pts = round(buyback_cost, 2)
                # Gross P&L = (Entry Net Credit - Current Net Debit) * quantity
                gross_pnl = (pos.entry_net_credit_pts - pos.current_net_debit_pts) * pos.quantity
                pos.current_pnl_inr = round(gross_pnl - pos.transaction_costs_inr, 2)

                today_str = now_dt.strftime("%d-%b-%Y").upper()
                is_expiry_day = (pos.expiry.upper() in (today_str, now_dt.strftime("%Y-%m-%d")))

                # 1. Target Check
                if pos.current_net_debit_pts <= pos.target_credit_pts:
                    cls.close_position(pos.id, exit_spot=spot_price, reason="TARGET_HIT")
                # 2. Stop Loss Check
                elif pos.current_net_debit_pts >= pos.stop_loss_pts:
                    cls.close_position(pos.id, exit_spot=spot_price, reason="STOPPED_OUT")
                # 3. Intraday Session Cutoff (15:20 IST)
                elif pos.engine_type == "INTRADAY" and now_time >= Config.INTRADAY_SESSION_CLOSE_TIME:
                    cls.close_position(pos.id, exit_spot=spot_price, reason="SESSION_EXIT")
                # 4. Positional Pre-Expiry Exit (14:30 IST on expiry day)
                elif pos.engine_type == "POSITIONAL" and is_expiry_day and now_time >= Config.POSITIONAL_PRE_EXPIRY_EXIT_TIME:
                    cls.close_position(pos.id, exit_spot=spot_price, reason="PRE_EXPIRY_EXIT")

                # 5. Persist append-only MTM snapshot
                try:
                    from app.models import PositionMTMSnapshot
                    import json, hashlib
                    leg_snaps = {
                        l.symbol: {
                            "strike": l.strike,
                            "option_type": l.option_type,
                            "action": l.action,
                            "price": l.current_price,
                        }
                        for l in pos.legs
                    }
                    minute_tag = now_dt.strftime("%Y%m%d%H%M")
                    mtm_idem = hashlib.sha256(f"{pos.id}_{minute_tag}".encode()).hexdigest()[:32]
                    existing_snap = PositionMTMSnapshot.query.filter_by(idempotency_key=mtm_idem).first()
                    if not existing_snap:
                        snap_rec = PositionMTMSnapshot(
                            position_id=pos.id,
                            timestamp=now_dt,
                            spot_price=spot_price,
                            net_debit_pts=pos.current_net_debit_pts,
                            unrealized_gross_pnl_inr=round(gross_pnl, 2),
                            known_transaction_costs_inr=pos.transaction_costs_inr,
                            unrealized_net_pnl_inr=pos.current_pnl_inr,
                            data_quality_status="OK",
                            leg_prices_json=json.dumps(leg_snaps),
                            idempotency_key=mtm_idem,
                        )
                        db.session.add(snap_rec)
                except Exception as ex_mtm:
                    log.warning(f"Failed to record MTM snapshot for pos #{pos.id}: {ex_mtm}")

                updated_positions.append(pos.to_dict())

            db.session.commit()

        except Exception as e:
            db.session.rollback()
            log.error(f"MTM update error: {e}")

        return updated_positions

    @classmethod
    def close_position(cls, position_id: int, exit_spot: Optional[float] = None, reason: str = "MANUAL_CLOSE", chain_by_strike: Optional[Dict[float, Dict[str, Any]]] = None) -> Tuple[bool, str]:
        """
        Closes an open position and records final realized P&L, itemized fees, and leg exit quotes.
        """
        try:
            pos = db.session.get(TradePosition, position_id)
            if not pos or pos.status != "OPEN":
                return False, "Position not found or already closed."

            now_dt = datetime.now(IST)
            exit_legs_data = [
                {"action": "BUY" if l.action == "SELL" else "SELL", "price": l.current_price, "quantity": l.quantity}
                for l in pos.legs
            ]
            exit_costs_breakdown = TransactionCostEngine.calculate_order_costs_breakdown(exit_legs_data, lots=pos.lots, trade_date=now_dt.date())
            exit_costs = round(exit_costs_breakdown["total"], 2)

            total_costs = round(pos.transaction_costs_inr + exit_costs, 2)
            gross_pnl = round((pos.entry_net_credit_pts - pos.current_net_debit_pts) * pos.quantity, 2)
            net_pnl = round(gross_pnl - total_costs, 2)

            pos.status = reason
            pos.exit_time = now_dt
            pos.exit_spot = exit_spot or pos.entry_spot
            pos.exit_net_debit_pts = pos.current_net_debit_pts
            pos.exit_reason = reason
            pos.gross_pnl_inr = gross_pnl
            pos.transaction_costs_inr = total_costs
            pos.net_pnl_inr = net_pnl

            # Record itemized exit costs
            pos.exit_brokerage_inr = exit_costs_breakdown["brokerage"]
            pos.exit_stt_inr = exit_costs_breakdown["stt"]
            pos.exit_exchange_fees_inr = exit_costs_breakdown["exchange_fee"]
            pos.exit_gst_inr = exit_costs_breakdown["gst"]
            pos.exit_stamp_duty_inr = exit_costs_breakdown["stamp_duty"]
            pos.exit_sebi_ipft_inr = exit_costs_breakdown["sebi_fee"] + exit_costs_breakdown["ipft"]

            for l in pos.legs:
                l.exit_price = l.current_price
                if chain_by_strike:
                    q = chain_by_strike.get(l.strike, {}).get(l.option_type, {})
                    if q:
                        b = float(q.get("bid", 0.0) or 0.0)
                        a = float(q.get("ask", 0.0) or 0.0)
                        l.exit_bid = b if b > 0 else None
                        l.exit_ask = a if a > 0 else None
                        l.exit_spread_pct = round(((a - b) / a) * 100.0, 2) if (a > 0 and b > 0) else None
                        l.exit_quote_timestamp = now_dt
                        l.exit_data_source = "FYERS_LIVE"
                if not l.exit_quote_timestamp:
                    l.exit_quote_timestamp = now_dt
                    l.exit_data_source = "FYERS_LIVE"

            db.session.commit()

            # If stopped out with a loss, trigger cooldown
            if net_pnl < 0:
                PortfolioRiskManager.register_loss_exit(now_dt)

            log.info(f"Closed position #{pos.id} ({reason}). Net P&L: ₹{net_pnl:,.2f}.")
            return True, f"Position #{pos.id} closed ({reason}). Net P&L: ₹{net_pnl:,.2f}."

        except Exception as e:
            db.session.rollback()
            log.error(f"Failed to close position: {e}")
            return False, f"Failed closing position: {str(e)}"


class AutoTradeEngine:
    """
    Manages automatic execution of data-selected defined-risk spreads in paper trading.
    Enforces risk guards, duplicate prevention, and dynamic UI toggle.
    """
    _auto_trade_enabled: bool = False

    @classmethod
    def is_enabled(cls) -> bool:
        return cls._auto_trade_enabled or getattr(Config, "AUTO_TRADE_ENABLED", False)

    @classmethod
    def set_enabled(cls, enabled: bool) -> bool:
        cls._auto_trade_enabled = bool(enabled)
        log.info(f"AutoTradeEngine state set to: {cls._auto_trade_enabled}")
        return cls._auto_trade_enabled

    @classmethod
    def process_auto_trades(cls, analysis, chain) -> List[Dict[str, Any]]:
        """
        Evaluates auto-selected candidates and executes eligible paper trades if auto-trade is enabled.
        """
        if not cls.is_enabled():
            return []

        if getattr(analysis, "data_status", "") != "FYERS_LIVE":
            log.info("Auto-trade dormant: requires live FYERS market data stream.")
            return []

        from app.strategies import AutoStrategySelector
        result = AutoStrategySelector.evaluate_auto_selection(analysis, chain)
        if not result.selected_strategies or result.selection_type == "NONE":
            return []

        executed = []

        for candidate in result.selected_strategies:
            if not candidate.is_eligible:
                continue

            # Check if this strategy and engine is already actively open
            existing = TradePosition.query.filter_by(
                strategy_id=candidate.strategy_id,
                engine_type=candidate.engine_type,
                status="OPEN"
            ).first()

            if existing:
                continue  # Already actively held

            # Authoritative Execution Safety Gate Validation
            from app.safety import ExecutionSafetyGate
            is_safe, safety_msg = ExecutionSafetyGate.validate_execution(candidate, analysis, chain)
            if not is_safe:
                log.info(f"Auto-trade skipped {candidate.strategy_name} ({candidate.engine_type}) via safety gate: {safety_msg}")
                continue

            # Evaluate Portfolio Risk
            risk_eval = PortfolioRiskManager.evaluate_candidate_risk(candidate, engine_type=candidate.engine_type)
            if not risk_eval.approved:
                log.info(f"Auto-trade skipped {candidate.strategy_name} ({candidate.engine_type}): {risk_eval.summary}")
                continue

            # Execute Paper Trade
            success, msg, pos = PaperTradingManager.execute_candidate(candidate)
            if success and pos:
                log.info(f"⚡ AUTO-TRADE EXECUTED: Position #{pos.id} {pos.strategy_name} ({pos.engine_type})")
                executed.append(pos.to_dict())

        return executed

    @classmethod
    def execute_all_selected(cls, analysis, chain) -> Tuple[List[Dict[str, Any]], str]:
        """
        1-Click immediate execution of all currently auto-selected strategies.
        Returns (executed_positions, status_message).
        """
        from app.strategies import AutoStrategySelector
        result = AutoStrategySelector.evaluate_auto_selection(analysis, chain)
        executed = []
        already_open = []
        risk_blocked = []

        if not result.selected_strategies or result.selection_type == "NONE":
            return [], f"No strategies currently meet market data criteria for execution (Selection: {result.selection_type})."

        for candidate in result.selected_strategies:
            if not candidate.is_eligible:
                continue

            existing = TradePosition.query.filter_by(
                strategy_id=candidate.strategy_id,
                engine_type=candidate.engine_type,
                status="OPEN"
            ).first()
            if existing:
                already_open.append(f"{candidate.strategy_name} ({candidate.engine_type}) [Position #{existing.id}]")
                continue

            # Authoritative Execution Safety Gate Validation
            from app.safety import ExecutionSafetyGate
            is_safe, safety_msg = ExecutionSafetyGate.validate_execution(candidate, analysis, chain)
            if not is_safe:
                risk_blocked.append(f"{candidate.strategy_name} ({candidate.engine_type}) [Safety Gate]: {safety_msg}")
                continue

            risk_eval = PortfolioRiskManager.evaluate_candidate_risk(candidate, engine_type=candidate.engine_type)
            if not risk_eval.approved:
                risk_blocked.append(f"{candidate.strategy_name} ({candidate.engine_type}): {'; '.join(risk_eval.rejection_reasons)}")
                continue

            success, msg, pos = PaperTradingManager.execute_candidate(candidate)
            if success and pos:
                log.info(f"MANUAL 1-CLICK AUTO-EXECUTE: Position #{pos.id} {pos.strategy_name} ({pos.engine_type})")
                executed.append(pos.to_dict())

        if executed:
            return executed, f"Successfully executed {len(executed)} auto-selected position(s)."

        if already_open and not risk_blocked:
            return [], f"Auto-selected setup is already open and active in your portfolio: {', '.join(already_open)}. Duplicate entry prevented."

        if risk_blocked:
            return [], f"Execution blocked by portfolio risk guard: {'; '.join(risk_blocked)}."

        return [], "No eligible auto-selected strategies ready to execute at this time."
