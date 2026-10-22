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
from datetime import datetime
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
    """Calculates official statutory NSE transaction fees and taxes for options trading."""

    @classmethod
    def calculate_order_costs(cls, legs: List[Dict[str, Any]], lots: int = 1) -> float:
        """
        Computes statutory fees for entering or exiting spread legs:
        - Brokerage: ₹20 per executed order leg.
        - STT: 0.125% on sell-side option turnover.
        - Exchange Turnover Fee: 0.05% of premium turnover.
        - SEBI Turnover Fee: 0.0001% of premium turnover.
        - Stamp Duty: 0.003% on buy-side option turnover.
        - GST: 18% on (Brokerage + Exchange turnover fees).
        """
        total_brokerage = len(legs) * Config.BROKERAGE_PER_ORDER
        total_stt = 0.0
        total_exchange_fee = 0.0
        total_stamp_duty = 0.0
        total_turnover = 0.0

        for leg in legs:
            action = leg.get("action", "").upper()
            price = float(leg.get("price", leg.get("entry_price", 0.0)) or 0.0)
            qty = int(leg.get("quantity", Config.LOT_SIZE * lots))
            turnover = price * qty
            total_turnover += turnover

            if action == "SELL":
                total_stt += turnover * Config.STT_SELL_PCT
            elif action == "BUY":
                total_stamp_duty += turnover * Config.STAMP_DUTY_BUY_PCT

            total_exchange_fee += turnover * Config.EXCHANGE_TURNOVER_PCT

        total_sebi_fee = total_turnover * Config.SEBI_TURNOVER_PCT
        gst = (total_brokerage + total_exchange_fee) * Config.GST_PCT

        total_charges = total_brokerage + total_stt + total_exchange_fee + total_sebi_fee + total_stamp_duty + gst
        return round(total_charges, 2)


class PaperTradingManager:
    """
    Manages paper positions, simulated order fills, mark-to-market updates, and exits.
    """

    @classmethod
    def execute_candidate(cls, candidate: SpreadCandidate) -> Tuple[bool, str, Optional[TradePosition]]:
        """
        Executes a paper-trade spread candidate atomically.
        """
        if not candidate.is_eligible:
            return False, f"Candidate not eligible: {'; '.join(candidate.rejection_reasons)}", None

        if candidate.has_uncovered_short:
            return False, "CRITICAL: Uncovered short option detected. Execution rejected.", None

        try:
            # 1. Calculate entry costs
            legs_data = [
                {"action": l.action, "price": l.bid if l.action == "SELL" else l.ask, "quantity": l.quantity}
                for l in candidate.legs
            ]
            entry_costs = TransactionCostEngine.calculate_order_costs(legs_data, lots=candidate.lots)

            # 2. Stop loss & target thresholds
            # Target = buyback at 30% of credit (capture 70% profit)
            target_debit = round(candidate.net_credit_pts * 0.30, 2)
            # Stop loss = buyback at 1.5x credit
            stop_loss_debit = round(candidate.net_credit_pts * 1.50, 2)

            now_dt = datetime.now(IST)

            # 3. Create TradePosition record
            position = TradePosition(
                engine_type=candidate.engine_type,
                strategy_id=candidate.strategy_id,
                strategy_name=candidate.strategy_name,
                symbol=candidate.symbol,
                expiry=candidate.expiry,
                lots=candidate.lots,
                quantity=candidate.quantity,
                entry_time=now_dt,
                entry_spot=float(candidate.breakevens[0] if candidate.breakevens else 24500.0),
                entry_net_credit_pts=candidate.net_credit_pts,
                max_loss_inr=candidate.max_loss_inr,
                max_profit_inr=candidate.max_profit_inr,
                target_credit_pts=target_debit,
                stop_loss_pts=stop_loss_debit,
                current_net_debit_pts=candidate.net_credit_pts,
                current_pnl_inr=0.0,
                status="OPEN",
                transaction_costs_inr=entry_costs,
            )
            db.session.add(position)
            db.session.flush()

            # 4. Create individual PositionLeg records
            for l in candidate.legs:
                fill_price = l.bid if l.action == "SELL" else l.ask
                pos_leg = PositionLeg(
                    position_id=position.id,
                    symbol=l.symbol,
                    strike=l.strike,
                    option_type=l.option_type,
                    action=l.action,
                    quantity=l.quantity,
                    entry_price=fill_price,
                    current_price=fill_price,
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
                        current_close_price = float(stk_quotes.get("ask", 0.0) or stk_quotes.get("ltp", 0.0))
                        buyback_cost += current_close_price
                    elif leg.action == "BUY":
                        current_close_price = float(stk_quotes.get("bid", 0.0) or stk_quotes.get("ltp", 0.0))
                        buyback_cost -= current_close_price

                    leg.current_price = current_close_price

                if not all_quotes_valid:
                    continue

                pos.current_net_debit_pts = round(buyback_cost, 2)
                # Gross P&L = (Entry Net Credit - Current Net Debit) * quantity
                gross_pnl = (pos.entry_net_credit_pts - pos.current_net_debit_pts) * pos.quantity
                pos.current_pnl_inr = round(gross_pnl - pos.transaction_costs_inr, 2)

                # 1. Target Check
                if pos.current_net_debit_pts <= pos.target_credit_pts:
                    cls.close_position(pos.id, exit_spot=spot_price, reason="TARGET_HIT")
                # 2. Stop Loss Check
                elif pos.current_net_debit_pts >= pos.stop_loss_pts:
                    cls.close_position(pos.id, exit_spot=spot_price, reason="STOPPED_OUT")
                # 3. Intraday Session Cutoff (15:20 IST)
                elif pos.engine_type == "INTRADAY" and now_time >= Config.INTRADAY_SESSION_CLOSE_TIME:
                    cls.close_position(pos.id, exit_spot=spot_price, reason="SESSION_EXIT")

                updated_positions.append(pos.to_dict())

            db.session.commit()

        except Exception as e:
            db.session.rollback()
            log.error(f"MTM update error: {e}")

        return updated_positions

    @classmethod
    def close_position(cls, position_id: int, exit_spot: Optional[float] = None, reason: str = "MANUAL_CLOSE") -> Tuple[bool, str]:
        """
        Closes an open position and records final realized P&L and fees.
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
            exit_costs = TransactionCostEngine.calculate_order_costs(exit_legs_data, lots=pos.lots)

            total_costs = pos.transaction_costs_inr + exit_costs
            gross_pnl = (pos.entry_net_credit_pts - pos.current_net_debit_pts) * pos.quantity
            net_pnl = gross_pnl - total_costs

            pos.status = reason
            pos.exit_time = now_dt
            pos.exit_spot = exit_spot or pos.entry_spot
            pos.exit_net_debit_pts = pos.current_net_debit_pts
            pos.exit_reason = reason
            pos.gross_pnl_inr = round(gross_pnl, 2)
            pos.transaction_costs_inr = round(total_costs, 2)
            pos.net_pnl_inr = round(net_pnl, 2)

            for l in pos.legs:
                l.exit_price = l.current_price

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

        from app.strategies import AutoStrategySelector
        result = AutoStrategySelector.evaluate_auto_selection(analysis, chain)
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

        if not result.selected_strategies:
            return [], "No strategies currently meet market data criteria for execution."

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
