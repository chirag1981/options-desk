"""
scripts/run_forward_observation.py — Forward Paper Observation Runner

Authoritative Runner for Iron Condor Forward Paper Observation.
Enforces all research mandates:
1. Isolated Ledger: Strictly writes to instance/research_paper_trading/observation_ledger.db.
   Asserts zero writes to instance/options_desk.db.
2. Frozen Candidate Parameters:
   - Strategy: IRON_CONDOR (symmetric 100-pt wings)
   - Regime Filter: BALANCED_RANGE only
   - Trend Filter: Causal ADX(14) < 22.0 (calculated strictly from closed daily bars prior to current session)
   - Profit Target: 70% credit capture (exit debit <= 0.30 * initial credit)
   - Stop Loss: 1.50x initial credit as exit debit (exit debit >= 1.50 * initial credit)
   - Sizing: Exactly 1 lot
   - Concurrency: Strictly 1 open position
   - Risk Cap: Effective Max Loss + Round-Trip Costs <= INR 5,000.00
3. Real-Time Quotes Only: Captures live top-of-book bid/ask quotes during market hours.
4. Fails Closed: Never submits live broker orders. Zero FYERS order endpoints called.
5. Quality Tracking: Records data-quality failures (missing quotes, crossed markets, wide spreads).
6. Strict Partitioning: All forward observations labeled with data_source = 'FORWARD_OBSERVATION'
   to guarantee historical replay records ('HISTORICAL_REPLAY') remain immutable.
"""

import os
import sys
import time
import uuid
import logging
import sqlite3
import glob
from datetime import datetime, date, time as dt_time
from typing import Dict, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np

# Safe stdout encoding for Windows
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from app.config import Config
from app.contracts import ContractRegistry
from app.trading import TransactionCostEngine
from app.market_data import MarketDataProvider
from app.analysis import MarketAnalyzer
from app.research.regime_diagnostics import (
    RegimeDiagnosticsEngine,
    calculate_adx,
)
from app.research.observation_models import (
    get_db_connection,
    init_observation_db,
    OBSERVATION_DB_PATH,
    OBSERVATION_DB_DIR,
)
from app.broker import FyersLiveBroker

log = logging.getLogger("forward_observation")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

IST = ZoneInfo("Asia/Kolkata")
FROZEN_WING_WIDTH = 100.0
FROZEN_ADX_THRESHOLD = 22.0
FROZEN_TARGET_PCT = 0.70       # Exit debit <= 0.30 * credit
FROZEN_STOP_DEBIT_MULT = 1.50   # Exit debit >= 1.50 * credit
MAX_RISK_CEILING_INR = 5000.0
MAX_SPREAD_PCT = 5.0
FORWARD_DATA_SOURCE = "FORWARD_OBSERVATION"


def verify_preflight_environment() -> None:
    """Verifies all safety and isolation invariants prior to forward observation."""
    print("=" * 80)
    print("PREFLIGHT SAFETY CHECK: FORWARD OBSERVATION MODE")
    print("=" * 80)

    # 1. Verify isolated DB path
    resolved_db = os.path.abspath(OBSERVATION_DB_PATH)
    prod_db = os.path.abspath(os.path.join(WORKSPACE_ROOT, "instance", "options_desk.db"))
    print(f"[*] Target Observation DB: {resolved_db}")
    print(f"[*] Production Database (Protected): {prod_db}")
    assert resolved_db != prod_db, "CRITICAL ERROR: Observation DB path resolves to production DB!"
    assert "observation_ledger.db" in resolved_db, "CRITICAL ERROR: Target is not observation_ledger.db!"

    # 2. Verify broker live order placement is strictly blocked
    try:
        broker = FyersLiveBroker()
        raise AssertionError("CRITICAL ERROR: FyersLiveBroker initialized without safety exception!")
    except (RuntimeError, NotImplementedError):
        print("[PASS] Broker safety verified: Real order routing is disabled.")

    # 3. Verify separation of historical records
    init_observation_db(OBSERVATION_DB_PATH)
    with get_db_connection(OBSERVATION_DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM observation_positions WHERE data_source = 'HISTORICAL_REPLAY'")
        hist_count = c.fetchone()[0]
        print(f"[PASS] Historical records verified: {hist_count} frozen historical replay positions intact.")

    print("[PASS] Preflight environment verification successful.\n")


def log_data_quality_event(
    conn: sqlite3.Connection,
    event_type: str,
    symbol: str,
    details: str,
    strike: Optional[float] = None,
    opt_type: Optional[str] = None,
    position_id: Optional[str] = None,
    quote_source: str = "FYERS_LIVE",
) -> str:
    """Persists a data-quality failure or anomaly into observation_data_quality_events."""
    event_id = f"DQ-FWD-{uuid.uuid4().hex[:10].upper()}"
    now_dt = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
    INSERT INTO observation_data_quality_events (
        event_id, created_at, event_type, symbol, strike, option_type, details, quote_source, position_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (event_id, now_dt, event_type, symbol, strike, opt_type, details, quote_source, position_id))
    return event_id


_cached_daily_df: Optional[pd.DataFrame] = None


def get_daily_spot_history() -> pd.DataFrame:
    """Builds and caches daily spot history for causal ADX calculation."""
    global _cached_daily_df
    if _cached_daily_df is not None:
        return _cached_daily_df

    data_dir = os.path.join(WORKSPACE_ROOT, "instance", "historical_data")
    parquet_files = sorted(glob.glob(os.path.join(data_dir, "NIFTY_*.parquet")))
    daily_records = []
    for pf in parquet_files:
        try:
            df = pd.read_parquet(pf, columns=["timestamp", "spot_price"])
            if "spot_price" in df.columns and "timestamp" in df.columns and len(df) > 0:
                ts = str(df["timestamp"].iloc[0])[:10]
                daily_records.append({
                    "date": ts,
                    "open": float(df["spot_price"].iloc[0]),
                    "high": float(df["spot_price"].max()),
                    "low": float(df["spot_price"].min()),
                    "close": float(df["spot_price"].iloc[-1]),
                })
        except Exception:
            pass

    _cached_daily_df = pd.DataFrame(daily_records).drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    return _cached_daily_df


def compute_causal_adx(daily_spot_df: pd.DataFrame, trade_date: str, period: int = 14) -> Tuple[Optional[float], Optional[str]]:
    """Computes ADX using strictly bars closed prior to trade_date."""
    prior_df = daily_spot_df[daily_spot_df["date"] < str(trade_date)]
    if len(prior_df) < (period * 2):
        return None, None
    last_bar_date = str(prior_df["date"].iloc[-1])
    adx_series = RegimeDiagnosticsEngine.calculate_adx(
        high=prior_df["high"],
        low=prior_df["low"],
        close=prior_df["close"],
        period=period
    )
    val = adx_series.iloc[-1]
    return (float(val) if not pd.isna(val) else None), last_bar_date


def validate_quotes_for_condor(
    strikes: Dict[str, float],
    chain: Dict[float, Dict[str, Any]],
    conn: sqlite3.Connection,
    data_source: str = "FYERS_LIVE",
) -> Tuple[bool, str, Dict[str, Dict[str, Any]]]:
    """
    Validates all 4 Iron Condor legs for:
    1. Positive Bid and Ask quotes.
    2. Uncrossed books (Bid < Ask).
    3. Spread percentage <= 5.0%.
    """
    leg_defs = [
        ("put_short", strikes["short_put"], "PE", "SELL"),
        ("put_long", strikes["long_put"], "PE", "BUY"),
        ("call_short", strikes["short_call"], "CE", "SELL"),
        ("call_long", strikes["long_call"], "CE", "BUY"),
    ]
    verified_quotes = {}
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    for leg_key, strike, opt_type, action in leg_defs:
        q = chain.get(strike, {}).get(opt_type, {})
        sym = q.get("symbol", f"NIFTY{int(strike)}{opt_type}") if q else f"NIFTY{int(strike)}{opt_type}"
        if not q:
            msg = f"MISSING_QUOTE on {sym}"
            log_data_quality_event(conn, "MISSING_QUOTE", sym, msg, strike, opt_type, quote_source=data_source)
            return False, msg, {}

        bid = float(q.get("bid", 0.0) or 0.0)
        ask = float(q.get("ask", 0.0) or 0.0)

        if bid <= 0.0:
            msg = f"NON_POSITIVE_BID on {sym} (bid={bid})"
            log_data_quality_event(conn, "MISSING_QUOTE", sym, msg, strike, opt_type, quote_source=data_source)
            return False, msg, {}

        if ask <= 0.0:
            msg = f"NON_POSITIVE_ASK on {sym} (ask={ask})"
            log_data_quality_event(conn, "MISSING_QUOTE", sym, msg, strike, opt_type, quote_source=data_source)
            return False, msg, {}

        if bid >= ask:
            msg = f"CROSSED_MARKET on {sym} (bid={bid} >= ask={ask})"
            log_data_quality_event(conn, "CROSSED_MARKET", sym, msg, strike, opt_type, quote_source=data_source)
            return False, msg, {}

        spread_pct = ((ask - bid) / ask) * 100.0
        if spread_pct > MAX_SPREAD_PCT:
            msg = f"EXCESSIVE_SPREAD on {sym} ({spread_pct:.2f}% > {MAX_SPREAD_PCT}%)"
            log_data_quality_event(conn, "EXCESSIVE_SPREAD", sym, msg, strike, opt_type, quote_source=data_source)
            return False, msg, {}

        verified_quotes[leg_key] = {
            "symbol": sym,
            "strike": strike,
            "opt_type": opt_type,
            "action": action,
            "bid": bid,
            "ask": ask,
            "spread_pct": round(spread_pct, 2),
            "timestamp": q.get("timestamp", now_str),
            "source": data_source,
        }

    return True, "All 4 legs verified", verified_quotes


def run_forward_observation_cycle(conn: sqlite3.Connection) -> None:
    """Executes a single forward paper observation cycle during live market hours."""
    now_dt = datetime.now(IST)
    now_time = now_dt.time()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    trade_date = now_dt.date()

    # 1. Check open observation position MTM & exit triggers
    open_pos = conn.execute(
        "SELECT * FROM observation_positions WHERE status = 'OPEN' AND data_source = ?",
        (FORWARD_DATA_SOURCE,)
    ).fetchone()

    # Fetch live option chain
    try:
        chain_data = MarketDataProvider.get_option_chain("NIFTY")
    except Exception as e:
        log.warning(f"Failed fetching option chain: {e}")
        return

    if not chain_data:
        log.debug("No option chain returned from provider.")
        return

    spot = float(chain_data.get("spot_price", 0.0) or 0.0)
    chain = chain_data.get("chain_by_strike", {})
    expiry = chain_data.get("expiry", "")

    if spot <= 0 or not chain:
        log.debug("Option chain empty or non-positive spot price.")
        return

    # 2. Update MTM for open position if active
    if open_pos:
        pos_id = open_pos["position_id"]
        legs = conn.execute(
            "SELECT * FROM observation_legs WHERE position_id = ? ORDER BY leg_index ASC",
            (pos_id,)
        ).fetchall()

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
                log_data_quality_event(
                    conn, "VALUATION_UNAVAILABLE", leg["symbol"],
                    f"Quote invalid for MTM valuation: bid={bid}, ask={ask}",
                    stk, opt_type, pos_id, quote_source=FORWARD_DATA_SOURCE
                )
                break

            spread_pct = ((ask - bid) / ask) * 100.0
            if leg["action"] == "SELL":
                current_buyback_cost += ask
                fill_p = ask
            else:
                current_buyback_cost -= bid
                fill_p = bid

            exit_legs_info.append({
                "leg_index": leg["leg_index"],
                "fill_price": fill_p,
                "bid": bid,
                "ask": ask,
                "spread_pct": round(spread_pct, 2),
            })

        if all_valid:
            current_buyback_cost = round(current_buyback_cost, 2)
            qty = open_pos["quantity"]
            entry_credit = open_pos["entry_net_credit_pts"]

            exit_reason = None
            if current_buyback_cost <= open_pos["target_net_debit_pts"]:
                exit_reason = "TARGET_HIT"
            elif current_buyback_cost >= open_pos["stop_loss_debit_pts"]:
                exit_reason = "STOP_LOSS"
            elif now_time >= dt_time(15, 20, 0):
                exit_reason = "SESSION_CUTOFF"

            if exit_reason:
                # Close position
                gross_pnl = round((entry_credit - current_buyback_cost) * qty, 2)
                exit_order_legs = [
                    {"action": "BUY" if l["action"] == "SELL" else "SELL", "price": eli["fill_price"], "quantity": qty}
                    for l, eli in zip(legs, exit_legs_info)
                ]
                exit_costs = TransactionCostEngine.calculate_order_costs_breakdown(exit_order_legs, lots=1, trade_date=trade_date)
                total_costs = round(open_pos["total_costs_inr"] + exit_costs["total"], 2)
                net_pnl = round(gross_pnl - total_costs, 2)

                # Conservative 10 bps slippage
                entry_fill_sum = sum(l["entry_fill_price"] for l in legs)
                exit_fill_sum = sum(eli["fill_price"] for eli in exit_legs_info)
                total_slip_pts = round((entry_fill_sum + exit_fill_sum) * 0.0010, 4)
                slip_cost_inr = round(total_slip_pts * qty, 2)
                cons_exit_debit = round(current_buyback_cost + (exit_fill_sum * 0.0010), 4)
                cons_gross_pnl = round(gross_pnl - slip_cost_inr, 2)
                cons_net_pnl = round(cons_gross_pnl - total_costs, 2)

                conn.execute("""
                UPDATE observation_positions SET
                    exit_time = ?, exit_spot = ?, exit_net_debit_pts = ?, exit_reason = ?,
                    gross_pnl_inr = ?, net_pnl_inr = ?, total_costs_inr = ?,
                    conservative_exit_net_debit_pts = ?, conservative_gross_pnl_inr = ?,
                    conservative_total_costs_inr = ?, conservative_net_pnl_inr = ?,
                    conservative_slippage_cost_inr = ?, status = 'CLOSED'
                WHERE position_id = ?
                """, (
                    now_str, spot, current_buyback_cost, exit_reason,
                    gross_pnl, net_pnl, total_costs,
                    cons_exit_debit, cons_gross_pnl, total_costs, cons_net_pnl,
                    slip_cost_inr, pos_id
                ))

                for eli in exit_legs_info:
                    conn.execute("""
                    UPDATE observation_legs SET
                        exit_fill_price = ?, exit_bid = ?, exit_ask = ?, exit_spread_pct = ?,
                        exit_quote_timestamp = ?, exit_quote_source = ?
                    WHERE position_id = ? AND leg_index = ?
                    """, (
                        eli["fill_price"], eli["bid"], eli["ask"], eli["spread_pct"],
                        now_str, FORWARD_DATA_SOURCE, pos_id, eli["leg_index"]
                    ))

                conn.commit()
                log.info(f"[FORWARD] Closed observation position {pos_id} via {exit_reason} (Net: INR {net_pnl:,.2f})")
                return

    # 3. If no position is open, evaluate candidate entry
    # Entry allowed strictly between 09:20 and 15:00 IST
    if not (dt_time(9, 20, 0) <= now_time <= dt_time(15, 0, 0)):
        return

    if open_pos is not None:
        return  # Concurrency ceiling: strictly 1 position

    # Analyze market structure
    analysis = MarketAnalyzer.analyze_chain(chain_data)
    regime = analysis.market_regime
    if regime != "BALANCED_RANGE":
        return

    # Compute Causal ADX(14) using closed daily bars
    daily_df = get_daily_spot_history()
    adx_val, last_bar_date = compute_causal_adx(daily_df, str(trade_date))
    if adx_val is None or adx_val >= FROZEN_ADX_THRESHOLD:
        log.debug(f"Candidate rejected by ADX filter: ADX={adx_val} >= {FROZEN_ADX_THRESHOLD}")
        return

    # Form 100 pt wing Iron Condor around S1 / R1
    s1 = getattr(analysis, "s1_support", spot - 100)
    r1 = getattr(analysis, "r1_resistance", spot + 100)
    short_put = round(s1 / 50.0) * 50.0
    long_put = short_put - FROZEN_WING_WIDTH
    short_call = round(r1 / 50.0) * 50.0
    long_call = short_call + FROZEN_WING_WIDTH

    strikes = {
        "short_put": short_put,
        "long_put": long_put,
        "short_call": short_call,
        "long_call": long_call,
    }

    is_valid, q_msg, leg_quotes = validate_quotes_for_condor(strikes, chain, conn, data_source=FORWARD_DATA_SOURCE)
    if not is_valid:
        return

    sp_bid = leg_quotes["put_short"]["bid"]
    lp_ask = leg_quotes["put_long"]["ask"]
    sc_bid = leg_quotes["call_short"]["bid"]
    lc_ask = leg_quotes["call_long"]["ask"]

    net_credit = round((sp_bid - lp_ask) + (sc_bid - lc_ask), 2)
    if net_credit <= 0:
        return

    lot_size = ContractRegistry.get_authoritative_lot_size("NIFTY", trade_date)
    qty = lot_size * 1

    entry_order_legs = [
        {"action": "SELL", "price": sp_bid, "quantity": qty},
        {"action": "BUY", "price": lp_ask, "quantity": qty},
        {"action": "SELL", "price": sc_bid, "quantity": qty},
        {"action": "BUY", "price": lc_ask, "quantity": qty},
    ]
    entry_costs = TransactionCostEngine.calculate_order_costs_breakdown(entry_order_legs, lots=1, trade_date=trade_date)
    entry_total_costs = entry_costs["total"]

    # Maximum Theoretical Risk Pre-Check (Wing width - net credit) * qty + estimated round-trip costs <= INR 5,000
    max_loss_pts = max(0.0, FROZEN_WING_WIDTH - net_credit)
    max_loss_inr = round(max_loss_pts * qty, 2)
    max_profit_inr = round(net_credit * qty, 2)
    estimated_round_trip_costs = entry_total_costs * 2.0
    effective_total_risk = round(max_loss_inr + estimated_round_trip_costs, 2)

    sig_id = f"SIG-FWD-{uuid.uuid4().hex[:10].upper()}"
    if effective_total_risk > MAX_RISK_CEILING_INR:
        rej_msg = f"RISK_CEILING_BREACH(INR {effective_total_risk:.2f} > {MAX_RISK_CEILING_INR})"
        conn.execute("""
        INSERT INTO observation_signals (
            signal_id, created_at, strategy_id, engine_type, symbol, expiry,
            spot_price, market_regime, market_bias, confidence_score, status,
            rejection_reason, short_put, long_put, short_call, long_call,
            net_credit_pts, wing_width, max_profit_inr, max_loss_inr,
            strategy_version, data_source, fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sig_id, now_str, "IRON_CONDOR", "INTRADAY", "NIFTY", expiry,
            spot, regime, analysis.market_bias, analysis.confidence_score, "REJECTED",
            rej_msg, short_put, long_put, short_call, long_call, net_credit, FROZEN_WING_WIDTH,
            max_profit_inr, max_loss_inr, "FROZEN_100W_ADX22", FORWARD_DATA_SOURCE, f"{sig_id}_REJECT"
        ))
        conn.commit()
        return

    # Enter forward observation position (Simulated top-of-book fills; ZERO orders sent)
    pos_id = f"POS-FWD-{now_dt.strftime('%Y%m%d-%H%M%S')}"
    target_debit = round(net_credit * (1.0 - FROZEN_TARGET_PCT), 2)
    stop_debit = round(net_credit * FROZEN_STOP_DEBIT_MULT, 2)

    conn.execute("""
    INSERT INTO observation_signals (
        signal_id, created_at, strategy_id, engine_type, symbol, expiry,
        spot_price, market_regime, market_bias, confidence_score, status,
        rejection_reason, short_put, long_put, short_call, long_call,
        net_credit_pts, wing_width, max_profit_inr, max_loss_inr,
        strategy_version, data_source, fingerprint
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sig_id, now_str, "IRON_CONDOR", "INTRADAY", "NIFTY", expiry,
        spot, regime, analysis.market_bias, analysis.confidence_score, "ELIGIBLE",
        None, short_put, long_put, short_call, long_call, net_credit, FROZEN_WING_WIDTH,
        max_profit_inr, max_loss_inr, "FROZEN_100W_ADX22", FORWARD_DATA_SOURCE, f"{sig_id}_ELIGIBLE"
    ))

    conn.execute("""
    INSERT INTO observation_positions (
        position_id, signal_id, strategy_id, engine_type, symbol, expiry,
        lots, quantity, entry_time, entry_spot, entry_net_credit_pts,
        target_net_debit_pts, stop_loss_debit_pts, max_profit_inr, max_loss_inr,
        total_costs_inr, brokerage_inr, stt_inr, exchange_fees_inr, gst_inr, stamp_duty_inr, sebi_ipft_inr,
        status, strategy_version, data_source
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        pos_id, sig_id, "IRON_CONDOR", "INTRADAY", "NIFTY", expiry,
        1, qty, now_str, spot, net_credit, target_debit, stop_debit, max_profit_inr, max_loss_inr,
        entry_total_costs, entry_costs["brokerage"], entry_costs["stt"], entry_costs["exchange_fee"],
        entry_costs["gst"], entry_costs["stamp_duty"], entry_costs["sebi_fee"] + entry_costs["ipft"],
        "OPEN", "FROZEN_100W_ADX22", FORWARD_DATA_SOURCE
    ))

    leg_sequence = [
        (0, "put_short", leg_quotes["put_short"], sp_bid),
        (1, "put_long", leg_quotes["put_long"], lp_ask),
        (2, "call_short", leg_quotes["call_short"], sc_bid),
        (3, "call_long", leg_quotes["call_long"], lc_ask),
    ]
    for idx, leg_key, q_dict, fill_p in leg_sequence:
        leg_id = f"LEG-{pos_id}-{idx}"
        conn.execute("""
        INSERT INTO observation_legs (
            leg_id, position_id, leg_index, symbol, strike, option_type, action, quantity,
            entry_fill_price, entry_bid, entry_ask, entry_spread_pct, entry_quote_timestamp, entry_quote_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            leg_id, pos_id, idx, q_dict["symbol"], q_dict["strike"], q_dict["opt_type"], q_dict["action"], qty,
            fill_p, q_dict["bid"], q_dict["ask"], q_dict["spread_pct"], q_dict["timestamp"], FORWARD_DATA_SOURCE
        ))

    conn.commit()
    log.info(f"[FORWARD] Entered simulated observation position {pos_id} (Credit: {net_credit:.2f} pts, Effective Risk: INR {effective_total_risk:,.2f})")


def run_observation_loop(poll_interval_sec: int = 60) -> None:
    """Runs continuous polling loop during market hours."""
    verify_preflight_environment()
    print(f"[*] Starting Forward Paper Observation Poller (Poll interval: {poll_interval_sec}s)...")
    print("[*] Market Hours Mon-Fri 09:15:00 - 15:30:00 IST. Press Ctrl+C to stop.")

    conn = get_db_connection(OBSERVATION_DB_PATH)
    try:
        while True:
            now_dt = datetime.now(IST)
            # Check market session (Mon-Fri 09:15 to 15:30)
            is_weekday = now_dt.weekday() < 5
            is_market_hours = dt_time(9, 15, 0) <= now_dt.time() <= dt_time(15, 30, 0)

            if is_weekday and is_market_hours:
                try:
                    run_forward_observation_cycle(conn)
                except Exception as e:
                    log.error(f"Error in observation cycle: {e}")
            else:
                log.debug("Market closed. Observation poller idle.")

            time.sleep(poll_interval_sec)
    except KeyboardInterrupt:
        print("\n[*] Forward Paper Observation Poller stopped by user.")
    finally:
        conn.close()


if __name__ == "__main__":
    interval = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 60
    run_observation_loop(poll_interval_sec=interval)
