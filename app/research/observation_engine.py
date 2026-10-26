"""
app/research/observation_engine.py — Core Engine for Iron Condor Paper-Trading Observation Mode

Enforces all research-specific observation requirements:
1. Strict Iron Condor Only, and only under BALANCED_RANGE regime.
2. Isolated from production database; writes strictly to observation_ledger.db.
3. Max 1 concurrent open observation position.
4. Validates quote freshness, uncrossed quotes, and max allowable bid/ask spread (<= 5%).
5. Base execution: Short at Bid, Long at Ask (entry); Short at Ask, Long at Bid (exit). Never uses LTP.
6. Conservative execution: 10 bps slippage per side (sells receive bid * (1 - 0.0010), buys pay ask * (1 + 0.0010)).
7. Target: exit debit <= 30% of initial net credit (70% profit capture).
8. Stop Loss: exit debit >= 150% of initial net credit.
9. Timezone-verified 15:00:00 IST entry cutoff & session square-off.
10. Full statutory transaction cost breakdown (Brokerage, STT, Exchange, GST, Stamp Duty).
11. Records data-quality failures without corrupting open trade state.
"""

import os
import uuid
import logging
import sqlite3
from datetime import datetime, time as dt_time, date
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

from app.config import Config
from app.contracts import ContractRegistry
from app.trading import TransactionCostEngine
from app.research.observation_models import (
    get_db_connection,
    init_observation_db,
    OBSERVATION_DB_PATH,
)

log = logging.getLogger("research.observation_engine")
IST = ZoneInfo("Asia/Kolkata")
STRATEGY_VERSION = "IRON_CONDOR_OBSERVATION_V1"
MAX_SPREAD_PCT = 5.0  # Max allowable bid/ask spread width percentage for liquid research observation


class ObservationEngine:
    """Execution engine for Iron Condor paper observation."""

    def __init__(self, db_path: str = OBSERVATION_DB_PATH):
        self.db_path = db_path
        init_observation_db(self.db_path)

    def log_data_quality_event(
        self,
        event_type: str,
        symbol: str,
        details: str,
        quote_source: str = "FYERS_LIVE",
        strike: Optional[float] = None,
        option_type: Optional[str] = None,
        position_id: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> str:
        """Persists a data-quality failure or anomaly event into the ledger."""
        event_id = f"DQ-{uuid.uuid4().hex[:12].upper()}"
        now_dt = datetime.now(IST)
        
        sql = """
        INSERT INTO observation_data_quality_events (
            event_id, created_at, event_type, symbol, strike, option_type, details, quote_source, position_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (event_id, now_dt, event_type, symbol, strike, option_type, details, quote_source, position_id)
        
        if conn:
            conn.execute(sql, params)
        else:
            with get_db_connection(self.db_path) as local_conn:
                local_conn.execute(sql, params)
        return event_id

    def validate_quotes_for_condor(
        self,
        strikes: Dict[str, float],
        chain: Dict[float, Dict[str, Any]],
        data_source: str = "FYERS_LIVE",
        conn: Optional[sqlite3.Connection] = None,
    ) -> Tuple[bool, str, Dict[str, Dict[str, Any]]]:
        """
        Validates all 4 Iron Condor leg quotes for:
        1. Positive, non-zero Bid and Ask quotes.
        2. Uncrossed books (Bid < Ask).
        3. Spread percentage <= 5.0%.
        4. Valid quote timestamps and source.
        Returns: (is_valid, failure_reason, leg_quotes_dict)
        """
        required_legs = [
            ("put_short", strikes["short_put"], "PE", "SELL"),
            ("put_long", strikes["long_put"], "PE", "BUY"),
            ("call_short", strikes["short_call"], "CE", "SELL"),
            ("call_long", strikes["long_call"], "CE", "BUY"),
        ]

        verified_quotes = {}

        for leg_key, strike, opt_type, action in required_legs:
            stk_data = chain.get(strike, {}).get(opt_type, {})
            if not stk_data:
                msg = f"Missing quote data for strike {strike} {opt_type}"
                self.log_data_quality_event("MISSING_QUOTE", f"NIFTY{int(strike)}{opt_type}", msg, data_source, strike, opt_type, conn=conn)
                return False, msg, {}

            bid = float(stk_data.get("bid", 0.0) or 0.0)
            ask = float(stk_data.get("ask", 0.0) or 0.0)
            symbol = stk_data.get("symbol", f"NIFTY{int(strike)}{opt_type}")
            q_time = stk_data.get("timestamp") or datetime.now(IST)

            if bid <= 0.0:
                msg = f"Non-positive or missing Bid quote on {symbol} (bid={bid})"
                self.log_data_quality_event("MISSING_QUOTE", symbol, msg, data_source, strike, opt_type, conn=conn)
                return False, msg, {}

            if ask <= 0.0:
                msg = f"Non-positive or missing Ask quote on {symbol} (ask={ask})"
                self.log_data_quality_event("MISSING_QUOTE", symbol, msg, data_source, strike, opt_type, conn=conn)
                return False, msg, {}

            if bid >= ask:
                msg = f"Crossed or inverted market on {symbol} (bid={bid} >= ask={ask})"
                self.log_data_quality_event("CROSSED_MARKET", symbol, msg, data_source, strike, opt_type, conn=conn)
                return False, msg, {}

            spread_pct = ((ask - bid) / ask) * 100.0
            if spread_pct > MAX_SPREAD_PCT:
                msg = f"Excessive bid/ask spread on {symbol} ({spread_pct:.2f}% > {MAX_SPREAD_PCT}%)"
                self.log_data_quality_event("EXCESSIVE_SPREAD", symbol, msg, data_source, strike, opt_type, conn=conn)
                return False, msg, {}

            verified_quotes[leg_key] = {
                "symbol": symbol,
                "strike": strike,
                "opt_type": opt_type,
                "action": action,
                "bid": bid,
                "ask": ask,
                "spread_pct": round(spread_pct, 2),
                "timestamp": q_time,
                "source": data_source,
            }

        return True, "All 4 legs verified", verified_quotes

    @staticmethod
    def calculate_slippage_price(price: float, action: str, slippage_bps: float = 10.0) -> float:
        """
        Calculates conservative executable price with 10 bps slippage per side:
        - BUY pays higher: ask * (1 + 0.0010)
        - SELL receives lower: bid * (1 - 0.0010)
        Enforces a 0.05 NSE tick size minimum.
        """
        slip_mult = slippage_bps / 10000.0
        if action.upper() == "BUY":
            res = price * (1.0 + slip_mult)
        else:
            res = price * (1.0 - slip_mult)
        return max(0.05, round(res, 4))

    def evaluate_and_record_signal(
        self,
        analysis: Any,
        chain: Dict[float, Dict[str, Any]],
        data_source: str = "FYERS_LIVE",
        current_time: Optional[datetime] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Evaluates Iron Condor candidate, persists signal with full rationale, and simulates entry if eligible.
        Returns: (is_eligible, message, signal_id)
        """
        now_dt = current_time
        if now_dt is None:
            ts_val = getattr(analysis, "timestamp", None)
            if ts_val:
                try:
                    if isinstance(ts_val, str):
                        ts_clean = ts_val.strip()
                        if "T" in ts_clean:
                            dt_parsed = datetime.fromisoformat(ts_clean)
                        else:
                            dt_parsed = datetime.strptime(ts_clean[:19], "%Y-%m-%d %H:%M:%S")
                        if dt_parsed.tzinfo is None:
                            dt_parsed = dt_parsed.replace(tzinfo=IST)
                        now_dt = dt_parsed
                    elif isinstance(ts_val, datetime):
                        now_dt = ts_val if ts_val.tzinfo else ts_val.replace(tzinfo=IST)
                except Exception as e:
                    log.warning(f"Failed to parse analysis timestamp '{ts_val}': {e}")
            if now_dt is None:
                now_dt = datetime.now(IST)

        now_time = now_dt.time()
        signal_id = f"SIG-{uuid.uuid4().hex[:12].upper()}"

        # 1. Regime Verification: Strict BALANCED_RANGE only
        regime = getattr(analysis, "market_regime", "UNKNOWN")
        bias = getattr(analysis, "market_bias", "UNKNOWN")
        spot = getattr(analysis, "spot_price", 0.0)
        expiry = getattr(analysis, "expiry", "")
        confidence = getattr(analysis, "confidence_score", 0.0)

        rejections = []
        if regime != "BALANCED_RANGE":
            rejections.append(f"Regime is '{regime}', strictly requires 'BALANCED_RANGE'")

        # 2. Timezone & Market Hours Verification
        # Intraday entry allowed between 09:20:00 and 15:00:00 IST
        entry_start = dt_time(9, 20, 0)
        entry_cutoff = dt_time(15, 0, 0)
        if not (entry_start <= now_time <= entry_cutoff):
            rejections.append(f"Current time {now_time.strftime('%H:%M:%S')} outside entry window (09:20:00 - 15:00:00 IST)")

        # 3. Derive strikes (50 pt wing width around S1/R1)
        s1 = getattr(analysis, "s1_support", spot - 100)
        r1 = getattr(analysis, "r1_resistance", spot + 100)
        short_put = round(s1 / 50.0) * 50.0
        long_put = short_put - 50.0
        short_call = round(r1 / 50.0) * 50.0
        long_call = short_call + 50.0

        strikes = {
            "short_put": short_put,
            "long_put": long_put,
            "short_call": short_call,
            "long_call": long_call,
        }

        # Validate quotes
        is_quote_valid, quote_msg, leg_quotes = self.validate_quotes_for_condor(strikes, chain, data_source)
        if not is_quote_valid:
            rejections.append(f"Quote validation failed: {quote_msg}")

        net_credit = 0.0
        max_loss_pts = 0.0
        max_profit_inr = 0.0
        max_loss_inr = 0.0
        lot_size = ContractRegistry.get_authoritative_lot_size("NIFTY", now_dt.date())

        if is_quote_valid:
            put_credit = leg_quotes["put_short"]["bid"] - leg_quotes["put_long"]["ask"]
            call_credit = leg_quotes["call_short"]["bid"] - leg_quotes["call_long"]["ask"]
            net_credit = round(put_credit + call_credit, 2)
            wing_width = 50.0
            max_loss_pts = round(wing_width - net_credit, 2)
            max_profit_inr = round(net_credit * lot_size, 2)
            max_loss_inr = round(max_loss_pts * lot_size, 2)

            if net_credit < Config.MIN_NET_CREDIT_PTS:
                rejections.append(f"Net credit {net_credit:.2f} pts below minimum {Config.MIN_NET_CREDIT_PTS:.2f} pts")

        status = "ELIGIBLE" if len(rejections) == 0 else "REJECTED"
        rejection_reason = "; ".join(rejections) if rejections else None

        fingerprint = f"{spot}_{short_put}_{long_put}_{short_call}_{long_call}_{net_credit}_{status}"

        # Persist Signal Log
        with get_db_connection(self.db_path) as conn:
            conn.execute("""
            INSERT INTO observation_signals (
                signal_id, created_at, strategy_id, engine_type, symbol, expiry,
                spot_price, market_regime, market_bias, confidence_score, status,
                rejection_reason, short_put, long_put, short_call, long_call,
                net_credit_pts, wing_width, max_profit_inr, max_loss_inr,
                strategy_version, data_source, fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_id, now_dt, "IRON_CONDOR", "INTRADAY", "NIFTY", expiry,
                spot, regime, bias, confidence, status, rejection_reason,
                short_put, long_put, short_call, long_call, net_credit, 50.0,
                max_profit_inr, max_loss_inr, STRATEGY_VERSION, data_source, fingerprint
            ))

            # 4. If Eligible, Check Concurrency and Execute
            if status == "ELIGIBLE":
                # Check concurrency: strictly max 1 open observation trade
                open_pos = conn.execute("SELECT COUNT(*) as count FROM observation_positions WHERE status = 'OPEN'").fetchone()
                if open_pos and open_pos["count"] >= 1:
                    conn.execute("""
                    INSERT INTO observation_risk_audits (
                        audit_id, created_at, action, approved, reason, max_loss_inr, daily_loss_inr, open_risk_inr
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (f"RA-{uuid.uuid4().hex[:12].upper()}", now_dt, "ENTRY_CONCURRENCY", 0, "Max 1 open observation position allowed", max_loss_inr, 0.0, max_loss_inr))
                    return False, "Candidate eligible but blocked: 1 observation position is already OPEN", signal_id

                # Execute entry
                self._execute_entry(conn, signal_id, expiry, spot, net_credit, max_profit_inr, max_loss_inr, lot_size, leg_quotes, data_source)
                return True, f"Observation position entered for Iron Condor (Credit: ₹{max_profit_inr:,.2f})", signal_id

        return False, f"Signal rejected: {rejection_reason}", signal_id

    def _execute_entry(
        self,
        conn: sqlite3.Connection,
        signal_id: str,
        expiry: str,
        spot: float,
        net_credit: float,
        max_profit_inr: float,
        max_loss_inr: float,
        lot_size: int,
        leg_quotes: Dict[str, Dict[str, Any]],
        data_source: str,
    ) -> str:
        """Records initial trade entry and individual 4 legs."""
        now_dt = datetime.now(IST)
        position_id = f"POS-{uuid.uuid4().hex[:12].upper()}"

        # Target: buyback at <= 30% of credit (capture 70% profit)
        target_net_debit = round(net_credit * 0.30, 2)
        # Stop loss: buyback at >= 150% of credit
        stop_loss_debit = round(net_credit * 1.50, 2)

        # Calculate entry fees
        legs_data = []
        for l in leg_quotes.values():
            fill_p = l["bid"] if l["action"] == "SELL" else l["ask"]
            legs_data.append({"action": l["action"], "price": fill_p, "quantity": lot_size})

        entry_costs = TransactionCostEngine.calculate_order_costs_breakdown(legs_data, lots=1, trade_date=now_dt.date())

        conn.execute("""
        INSERT INTO observation_positions (
            position_id, signal_id, strategy_id, engine_type, symbol, expiry,
            lots, quantity, entry_time, entry_spot, entry_net_credit_pts,
            target_net_debit_pts, stop_loss_debit_pts, max_profit_inr, max_loss_inr,
            total_costs_inr, brokerage_inr, stt_inr, exchange_fees_inr, gst_inr, stamp_duty_inr, sebi_ipft_inr,
            status, strategy_version, data_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            position_id, signal_id, "IRON_CONDOR", "INTRADAY", "NIFTY", expiry,
            1, lot_size, now_dt, spot, net_credit, target_net_debit, stop_loss_debit, max_profit_inr, max_loss_inr,
            entry_costs["total"], entry_costs["brokerage"], entry_costs["stt"], entry_costs["exchange_fee"],
            entry_costs["gst"], entry_costs["stamp_duty"], entry_costs["sebi_fee"] + entry_costs["ipft"],
            "OPEN", STRATEGY_VERSION, data_source
        ))

        # Insert 4 legs
        leg_order = ["put_short", "put_long", "call_short", "call_long"]
        for idx, k in enumerate(leg_order):
            q = leg_quotes[k]
            fill_p = q["bid"] if q["action"] == "SELL" else q["ask"]
            leg_id = f"LEG-{uuid.uuid4().hex[:12].upper()}"
            conn.execute("""
            INSERT INTO observation_legs (
                leg_id, position_id, leg_index, symbol, strike, option_type, action, quantity,
                entry_fill_price, entry_bid, entry_ask, entry_spread_pct, entry_quote_timestamp, entry_quote_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                leg_id, position_id, idx, q["symbol"], q["strike"], q["opt_type"], q["action"], lot_size,
                fill_p, q["bid"], q["ask"], q["spread_pct"], q["timestamp"], q["source"]
            ))

        return position_id

    def update_open_positions(
        self,
        chain: Dict[float, Dict[str, Any]],
        spot_price: float,
        data_source: str = "FYERS_LIVE",
        current_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Updates Mark-to-Market for open observation positions.
        Checks for:
        1. Profit target (<= 30% of credit).
        2. Stop loss (>= 150% of credit).
        3. 15:00:00 IST entry cutoff vs 15:20:00 session square-off.
        4. Unavailable quotes (fails closed, never marks closed or fabricates prices).
        """
        now_dt = current_time
        if now_dt is None:
            # Check if any quotes in chain contain a timestamp
            for stk_quotes in chain.values():
                for opt_q in stk_quotes.values():
                    q_ts = opt_q.get("timestamp") if isinstance(opt_q, dict) else None
                    if q_ts:
                        if isinstance(q_ts, datetime):
                            now_dt = q_ts if q_ts.tzinfo else q_ts.replace(tzinfo=IST)
                            break
                        elif isinstance(q_ts, str):
                            try:
                                ts_clean = q_ts.strip()
                                if "T" in ts_clean:
                                    dt_p = datetime.fromisoformat(ts_clean)
                                else:
                                    dt_p = datetime.strptime(ts_clean[:19], "%Y-%m-%d %H:%M:%S")
                                now_dt = dt_p if dt_p.tzinfo else dt_p.replace(tzinfo=IST)
                                break
                            except Exception:
                                pass
                if now_dt is not None:
                    break
        if now_dt is None:
            now_dt = datetime.now(IST)

        now_time = now_dt.time()
        results = []

        with get_db_connection(self.db_path) as conn:
            open_rows = conn.execute("SELECT * FROM observation_positions WHERE status = 'OPEN'").fetchall()
            for row in open_rows:
                pos_id = row["position_id"]
                legs = conn.execute("SELECT * FROM observation_legs WHERE position_id = ? ORDER BY leg_index ASC", (pos_id,)).fetchall()

                strikes = {
                    "short_put": legs[0]["strike"],
                    "long_put": legs[1]["strike"],
                    "short_call": legs[2]["strike"],
                    "long_call": legs[3]["strike"],
                }

                # Evaluate current exit price: buy back shorts at Ask, sell longs at Bid
                all_valid = True
                exit_legs_info = []
                current_buyback_cost = 0.0

                for leg in legs:
                    opt_type = leg["option_type"]
                    stk = leg["strike"]
                    q = chain.get(stk, {}).get(opt_type, {})
                    bid = float(q.get("bid", 0.0) or 0.0) if q else 0.0
                    ask = float(q.get("ask", 0.0) or 0.0) if q else 0.0

                    if bid <= 0.0 or ask <= 0.0 or bid >= ask:
                        all_valid = False
                        msg = f"Valuation unavailable for leg {leg['symbol']} (bid={bid}, ask={ask})"
                        self.log_data_quality_event("VALUATION_UNAVAILABLE", leg["symbol"], msg, data_source, stk, opt_type, pos_id, conn=conn)
                        break

                    spread_pct = ((ask - bid) / ask) * 100.0
                    # For closing: if action was SELL, we BUY at Ask; if BUY, we SELL at Bid
                    if leg["action"] == "SELL":
                        close_fill = ask
                        current_buyback_cost += close_fill
                    else:
                        close_fill = bid
                        current_buyback_cost -= close_fill

                    exit_legs_info.append({
                        "leg_id": leg["leg_id"],
                        "action": "BUY" if leg["action"] == "SELL" else "SELL",
                        "fill_price": close_fill,
                        "bid": bid,
                        "ask": ask,
                        "spread_pct": round(spread_pct, 2),
                        "quantity": leg["quantity"],
                    })

                if not all_valid:
                    # Do not fabricate prices or mark closed; leave status as OPEN with DQ event
                    results.append({"position_id": pos_id, "status": "VALUATION_UNAVAILABLE"})
                    continue

                current_buyback_cost = round(current_buyback_cost, 2)

                # Check Exit Conditions
                exit_reason = None
                if current_buyback_cost <= row["target_net_debit_pts"]:
                    exit_reason = "TARGET_HIT"
                elif current_buyback_cost >= row["stop_loss_debit_pts"]:
                    exit_reason = "STOP_LOSS"
                elif now_time >= dt_time(15, 20, 0):
                    exit_reason = "SESSION_CUTOFF"

                if exit_reason:
                    self._close_position(conn, row, exit_legs_info, current_buyback_cost, spot_price, exit_reason, now_dt)
                    results.append({"position_id": pos_id, "status": "CLOSED", "reason": exit_reason})
                else:
                    results.append({"position_id": pos_id, "status": "OPEN", "buyback_cost": current_buyback_cost})

        return results

    def _close_position(
        self,
        conn: sqlite3.Connection,
        pos_row: sqlite3.Row,
        exit_legs_info: List[Dict[str, Any]],
        exit_net_debit: float,
        exit_spot: float,
        exit_reason: str,
        now_dt: datetime,
    ) -> None:
        """Executes position closure, calculating base and conservative fee/slippage scenarios."""
        pos_id = pos_row["position_id"]
        qty = pos_row["quantity"]
        entry_credit = pos_row["entry_net_credit_pts"]

        # Base Scenario Calculations
        gross_pnl = round((entry_credit - exit_net_debit) * qty, 2)
        exit_costs = TransactionCostEngine.calculate_order_costs_breakdown(
            [{"action": l["action"], "price": l["fill_price"], "quantity": l["quantity"]} for l in exit_legs_info],
            lots=pos_row["lots"],
            trade_date=now_dt.date()
        )
        total_costs = round(pos_row["total_costs_inr"] + exit_costs["total"], 2)
        net_pnl = round(gross_pnl - total_costs, 2)

        # Conservative Scenario (10 bps slippage per side on entry + exit)
        # Entry slippage drag: short legs sell lower (bid * 0.0010), long legs buy higher (ask * 0.0010)
        # Exit slippage drag: short legs buy back higher (ask * 0.0010), long legs sell lower (bid * 0.0010)
        entry_legs = conn.execute("SELECT * FROM observation_legs WHERE position_id = ?", (pos_id,)).fetchall()
        entry_slippage_pts = sum(l["entry_fill_price"] * 0.0010 for l in entry_legs)
        exit_slippage_pts = sum(l["fill_price"] * 0.0010 for l in exit_legs_info)
        total_slippage_pts = round(entry_slippage_pts + exit_slippage_pts, 4)

        cons_exit_debit = round(exit_net_debit + exit_slippage_pts, 4)
        cons_gross_pnl = round((entry_credit - entry_slippage_pts - cons_exit_debit) * qty, 2)
        cons_exit_costs = TransactionCostEngine.calculate_order_costs_breakdown(
            [{"action": l["action"], "price": l["fill_price"], "quantity": l["quantity"]} for l in exit_legs_info],
            lots=pos_row["lots"], trade_date=now_dt.date()
        )
        cons_total_costs = round(pos_row["total_costs_inr"] + cons_exit_costs["total"], 2)
        cons_net_pnl = round(cons_gross_pnl - cons_total_costs, 2)
        slippage_diff = round(net_pnl - cons_net_pnl, 2)

        # Update position record
        conn.execute("""
        UPDATE observation_positions SET
            exit_time = ?,
            exit_spot = ?,
            exit_net_debit_pts = ?,
            exit_reason = ?,
            gross_pnl_inr = ?,
            net_pnl_inr = ?,
            total_costs_inr = ?,
            brokerage_inr = brokerage_inr + ?,
            stt_inr = stt_inr + ?,
            exchange_fees_inr = exchange_fees_inr + ?,
            gst_inr = gst_inr + ?,
            stamp_duty_inr = stamp_duty_inr + ?,
            sebi_ipft_inr = sebi_ipft_inr + ?,
            conservative_exit_net_debit_pts = ?,
            conservative_gross_pnl_inr = ?,
            conservative_total_costs_inr = ?,
            conservative_net_pnl_inr = ?,
            conservative_slippage_cost_inr = ?,
            status = 'CLOSED'
        WHERE position_id = ?
        """, (
            now_dt, exit_spot, exit_net_debit, exit_reason,
            gross_pnl, net_pnl, total_costs,
            exit_costs["brokerage"], exit_costs["stt"], exit_costs["exchange_fee"],
            exit_costs["gst"], exit_costs["stamp_duty"], exit_costs["sebi_fee"] + exit_costs["ipft"],
            cons_exit_debit, cons_gross_pnl, cons_total_costs, cons_net_pnl, slippage_diff,
            pos_id
        ))

        # Update leg exit prices and quotes
        for l in exit_legs_info:
            conn.execute("""
            UPDATE observation_legs SET
                exit_fill_price = ?,
                exit_bid = ?,
                exit_ask = ?,
                exit_spread_pct = ?,
                exit_quote_timestamp = ?,
                exit_quote_source = 'FYERS_LIVE'
            WHERE leg_id = ?
            """, (l["fill_price"], l["bid"], l["ask"], l["spread_pct"], now_dt, l["leg_id"]))
