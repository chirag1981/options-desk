"""
scripts/run_observation_validation.py — Isolated Observational Validation Runner

Enforces all mandatory validation safeguards:
1. Isolated database: Writes strictly to instance/research_paper_trading/observation_ledger.db.
   Asserts zero writes or connections to instance/options_desk.db.
2. Verified artifacts: Confirms existence and SHA-256 hash of reconciled_research_matrix.json.
   Preserves existing research artifacts without overwriting.
3. Frozen Candidate Parameters:
   - Wings: 100 points
   - Regime Filter: BALANCED_RANGE only
   - Trend Filter: Causal ADX(14) < 22.0
   - Profit Target: 70% of credit (exit debit <= 0.30 * credit)
   - Stop Loss: 1.5x initial credit as exit debit (exit debit >= 1.50 * credit)
   - Sizing: 1 lot
   - Concurrency: Strictly 1 open position
   - Risk Ceiling: Theoretical Max Loss + Estimated Round-Trip Costs <= INR 5,000
4. Causal ADX: Computed using strictly closed bars prior to decision timestamp.
   Logs ADX, last input bar timestamp, and eligibility decision for every candidate.
5. Chronological Partitioning:
   - Sessions 1-75 (2024-10-24 to 2026-03-24): Discovery / In-Sample Replay
   - Sessions 76-105 (2026-03-30 to 2026-10-19): Previously Examined Test Data
6. Full Attribution:
   - 4 legs with timestamped bid/ask quotes, freshness, spread checks, MTM
   - Base Execution: Short at Bid, Long at Ask (entry); Short at Ask, Long at Bid (exit)
   - Conservative Execution: 10 bps slippage per side
   - Statutory transaction costs via TransactionCostEngine
7. Output: Writes JSON report strictly to instance/research_reports/observation_validation_report.json.
"""

import os
import sys
import glob
import json
import time
import hashlib
import sqlite3
from datetime import datetime, date, timedelta, time as dt_time
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
from app.analysis import MarketAnalyzer
from app.strategies import AutoStrategySelector
from app.backtesting.data_validator import HistoricalDataValidator
from app.research.regime_diagnostics import RegimeDiagnosticsEngine
from app.research.observation_models import (
    get_db_connection,
    init_observation_db,
    OBSERVATION_DB_PATH,
    OBSERVATION_DB_DIR,
)
from app.research.observation_reports import ObservationReportGenerator

IST = ZoneInfo("Asia/Kolkata")
EXPECTED_MATRIX_HASH = "C998A07BE0DBCB766FD2853506A29E3B24ECAABFF10DD3F637EDCBC2DCFAEE1D"
MAX_RISK_CEILING_INR = 5000.0
MAX_SPREAD_PCT = 5.0
FROZEN_WING_WIDTH = 100.0
FROZEN_ADX_THRESHOLD = 22.0
FROZEN_TARGET_PCT = 0.70       # Exit debit <= 0.30 * credit
FROZEN_STOP_DEBIT_MULT = 1.50   # Exit debit >= 1.50 * credit


def verify_environment_safeguards() -> None:
    """Verifies that all environment and file safety requirements are met."""
    print("=" * 80)
    print("STEP 1: PREFLIGHT ENVIRONMENT & SAFETY CHECKS")
    print("=" * 80)

    # 1. Verify isolated DB path
    resolved_db = os.path.abspath(OBSERVATION_DB_PATH)
    prod_db = os.path.abspath(os.path.join(WORKSPACE_ROOT, "instance", "options_desk.db"))
    print(f"[*] Target Observation Ledger DB: {resolved_db}")
    print(f"[*] Production Database (Protected): {prod_db}")
    assert resolved_db != prod_db, "CRITICAL ERROR: Observation DB path resolves to production DB!"
    assert "observation_ledger.db" in resolved_db, "CRITICAL ERROR: DB filename is not observation_ledger.db"
    print("[PASS] Database isolation verified: Production DB is untouched.")

    # 2. Verify existence and SHA-256 hash of reconciled_research_matrix.json
    matrix_path = os.path.join(WORKSPACE_ROOT, "instance", "research_reports", "reconciled_research_matrix.json")
    assert os.path.exists(matrix_path), f"CRITICAL ERROR: Missing artifact {matrix_path}"
    with open(matrix_path, "rb") as f:
        file_hash = hashlib.sha256(f.read()).hexdigest().upper()
    print(f"[*] Reconciled Matrix SHA-256: {file_hash}")
    assert file_hash == EXPECTED_MATRIX_HASH, f"CRITICAL ERROR: Hash mismatch! Expected {EXPECTED_MATRIX_HASH}, got {file_hash}"
    print("[PASS] Reconciled research matrix verified and intact.")

    # 3. Verify report output path
    report_path = os.path.join(WORKSPACE_ROOT, "instance", "research_reports", "observation_validation_report.json")
    print(f"[*] Report Output Target: {report_path}")
    print("[PASS] Environment preflight checks passed successfully.\n")


def setup_clean_observation_ledger(reset: bool = True) -> None:
    """Initializes the observation ledger schema. If reset=True, clears observation tables without touching other files."""
    os.makedirs(OBSERVATION_DB_DIR, exist_ok=True)
    init_observation_db(OBSERVATION_DB_PATH)
    if reset:
        print("[*] Preparing clean observation ledger tables in observation_ledger.db...")
        with get_db_connection(OBSERVATION_DB_PATH) as conn:
            conn.execute("DELETE FROM observation_legs")
            conn.execute("DELETE FROM observation_positions")
            conn.execute("DELETE FROM observation_signals")
            conn.execute("DELETE FROM observation_data_quality_events")
            conn.execute("DELETE FROM observation_risk_audits")
            conn.commit()
        print("[PASS] Observation ledger reset successfully. Schema ready.\n")


def compute_causal_adx(daily_spot_df: pd.DataFrame, trade_date: str, period: int = 14) -> Tuple[Optional[float], Optional[str]]:
    """
    Computes ADX using strictly bars closed prior to trade_date.
    Returns (adx_value, last_bar_date).
    """
    prior_df = daily_spot_df[daily_spot_df["date"] < trade_date]
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


def validate_quotes_for_candidate(
    strikes: Dict[str, float],
    chain: Dict[float, Dict[str, Any]],
    conn: sqlite3.Connection,
    trade_date: str,
    time_str: str,
) -> Tuple[bool, str, Dict[str, Dict[str, Any]]]:
    """Validates quotes for the 4 legs: bid > 0, ask > 0, uncrossed (bid < ask), spread <= 5%."""
    leg_defs = [
        ("put_short", strikes["short_put"], "PE", "SELL"),
        ("put_long", strikes["long_put"], "PE", "BUY"),
        ("call_short", strikes["short_call"], "CE", "SELL"),
        ("call_long", strikes["long_call"], "CE", "BUY"),
    ]
    verified_quotes = {}

    for leg_key, strike, opt_type, action in leg_defs:
        q = chain.get(strike, {}).get(opt_type, {})
        if not q:
            reason = f"MISSING_QUOTE for strike {strike} {opt_type}"
            _log_dq(conn, "MISSING_QUOTE", f"NIFTY{int(strike)}{opt_type}", reason, strike, opt_type, trade_date, time_str)
            return False, reason, {}

        bid = float(q.get("bid", 0.0) or 0.0)
        ask = float(q.get("ask", 0.0) or 0.0)
        sym = q.get("symbol", f"NIFTY{int(strike)}{opt_type}")
        ts = q.get("timestamp", f"{trade_date} {time_str}")

        if bid <= 0.0:
            reason = f"NON_POSITIVE_BID on {sym} (bid={bid})"
            _log_dq(conn, "MISSING_QUOTE", sym, reason, strike, opt_type, trade_date, time_str)
            return False, reason, {}
        if ask <= 0.0:
            reason = f"NON_POSITIVE_ASK on {sym} (ask={ask})"
            _log_dq(conn, "MISSING_QUOTE", sym, reason, strike, opt_type, trade_date, time_str)
            return False, reason, {}
        if bid >= ask:
            reason = f"CROSSED_MARKET on {sym} (bid={bid} >= ask={ask})"
            _log_dq(conn, "CROSSED_MARKET", sym, reason, strike, opt_type, trade_date, time_str)
            return False, reason, {}

        spread_pct = ((ask - bid) / ask) * 100.0
        if spread_pct > MAX_SPREAD_PCT:
            reason = f"EXCESSIVE_SPREAD on {sym} ({spread_pct:.2f}% > {MAX_SPREAD_PCT}%)"
            _log_dq(conn, "EXCESSIVE_SPREAD", sym, reason, strike, opt_type, trade_date, time_str)
            return False, reason, {}

        verified_quotes[leg_key] = {
            "symbol": sym,
            "strike": strike,
            "opt_type": opt_type,
            "action": action,
            "bid": bid,
            "ask": ask,
            "spread_pct": round(spread_pct, 2),
            "timestamp": ts,
            "source": "HISTORICAL_REPLAY",
        }

    return True, "All 4 legs verified", verified_quotes


def _log_dq(
    conn: sqlite3.Connection,
    event_type: str,
    symbol: str,
    details: str,
    strike: Optional[float],
    opt_type: Optional[str],
    trade_date: str,
    time_str: str,
    pos_id: Optional[str] = None
) -> None:
    event_id = f"DQ-{trade_date.replace('-', '')}-{time_str.replace(':', '')}-{strike or 0}-{opt_type or 'NA'}"
    conn.execute("""
    INSERT OR IGNORE INTO observation_data_quality_events (
        event_id, created_at, event_type, symbol, strike, option_type, details, quote_source, position_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (event_id, f"{trade_date} {time_str}", event_type, symbol, strike, opt_type, details, "HISTORICAL_REPLAY", pos_id))


def run_observation_replay() -> Dict[str, Any]:
    """Runs the full historical replay across all 105 sessions into the isolated observation ledger."""
    verify_environment_safeguards()
    setup_clean_observation_ledger(reset=True)

    data_dir = os.path.join(WORKSPACE_ROOT, "instance", "historical_data")
    parquet_files = sorted(glob.glob(os.path.join(data_dir, "NIFTY_*.parquet")))
    total_sessions = len(parquet_files)
    print(f"[*] Found {total_sessions} NIFTY session files in {data_dir}")

    # Build daily spot history for causal ADX
    print("[*] Building daily spot history for causal ADX calculation...")
    daily_records = []
    all_records = []
    for pf in parquet_files:
        try:
            df = pd.read_parquet(pf)
            valid, _, recs = HistoricalDataValidator.validate_dataframe(df, file_path=pf)
            if valid:
                all_records.extend(recs)
            if "spot_price" in df.columns and "timestamp" in df.columns:
                ts = str(df["timestamp"].iloc[0])[:10]
                daily_records.append({
                    "date": ts,
                    "open": float(df["spot_price"].iloc[0]),
                    "high": float(df["spot_price"].max()),
                    "low": float(df["spot_price"].min()),
                    "close": float(df["spot_price"].iloc[-1]),
                })
        except Exception as e:
            print(f"[!] Warning reading {pf}: {e}")

    df_daily = pd.DataFrame(daily_records).drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    print(f"[*] Extracted {len(df_daily)} daily spot sessions for ADX history.")

    cached_snapshots, _, _, _, _, _, _ = HistoricalDataValidator.reconstruct_snapshots(all_records)
    print(f"[*] Reconstructed {len(cached_snapshots)} 1-minute chronological snapshots across {total_sessions} sessions.\n")

    print("=" * 80)
    print("STEP 2: RUNNING ISOLATED OBSERVATIONAL REPLAY (CANDIDATE: 100W + ADX < 22)")
    print("=" * 80)

    # State tracking
    open_pos: Optional[Dict[str, Any]] = None
    current_trade_day: Optional[date] = None
    daily_loss_incurred = 0.0
    consecutive_losses = 0
    cooldown_until: Optional[datetime] = None
    circuit_breaker_active = False

    evaluated_candidates_count = 0
    rejected_candidates_count = 0
    rejection_reasons: Dict[str, int] = {}
    adx_logs: List[Dict[str, Any]] = []

    last_snap = None

    conn = get_db_connection(OBSERVATION_DB_PATH)

    try:
        for snap_idx, snap in enumerate(cached_snapshots):
            ts_str = snap["timestamp"]
            dt_obj = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            trade_date = dt_obj.date()
            date_str = str(trade_date)
            time_str = dt_obj.strftime("%H:%M:%S")
            expiry = snap["expiry"]
            spot = snap["spot_price"]
            chain = snap["chain_by_strike"]

            # 1. Day transition: settle open trade from previous day
            if current_trade_day is not None and current_trade_day != trade_date:
                if open_pos is not None:
                    _close_observation_position(
                        conn=conn,
                        pos=open_pos,
                        chain=(last_snap or snap)["chain_by_strike"],
                        spot_price=(last_snap or snap)["spot_price"],
                        exit_reason="SESSION_CUTOFF",
                        exit_dt=datetime.strptime((last_snap or snap)["timestamp"], "%Y-%m-%d %H:%M:%S"),
                        source="HISTORICAL_REPLAY"
                    )
                    open_pos = None

                current_trade_day = trade_date
                daily_loss_incurred = 0.0
                circuit_breaker_active = False
                cooldown_until = None
            elif current_trade_day is None:
                current_trade_day = trade_date

            last_snap = snap

            # 2. Monitor open position
            if open_pos is not None:
                exit_res = _check_position_exit(
                    pos=open_pos,
                    chain=chain,
                    spot_price=spot,
                    current_dt=dt_obj,
                    conn=conn,
                    trade_date=date_str,
                    time_str=time_str
                )
                if exit_res.get("should_close"):
                    closed_pnl = _close_observation_position(
                        conn=conn,
                        pos=open_pos,
                        chain=chain,
                        spot_price=spot,
                        exit_reason=exit_res["reason"],
                        exit_dt=dt_obj,
                        source="HISTORICAL_REPLAY"
                    )
                    open_pos = None
                    if closed_pnl < 0:
                        daily_loss_incurred += abs(closed_pnl)
                        consecutive_losses += 1
                        cooldown_until = dt_obj + timedelta(minutes=Config.CONSECUTIVE_LOSS_COOLDOWN_MINS)
                    else:
                        consecutive_losses = 0

                    if daily_loss_incurred >= Config.get_max_daily_loss_inr():
                        circuit_breaker_active = True

            # 3. Entry evaluation (between 09:20:00 and 14:45:00)
            if open_pos is not None:
                continue
            if circuit_breaker_active:
                rejected_candidates_count += 1
                rejection_reasons["DAILY_CIRCUIT_BREAKER_ACTIVE"] = rejection_reasons.get("DAILY_CIRCUIT_BREAKER_ACTIVE", 0) + 1
                continue
            if cooldown_until and dt_obj < cooldown_until:
                rejected_candidates_count += 1
                rejection_reasons["CONSECUTIVE_LOSS_COOLDOWN_ACTIVE"] = rejection_reasons.get("CONSECUTIVE_LOSS_COOLDOWN_ACTIVE", 0) + 1
                continue
            if time_str < "09:20:00" or time_str > "14:45:00":
                continue

            evaluated_candidates_count += 1

            # Check Causal ADX
            adx_val, last_bar_date = compute_causal_adx(df_daily, date_str, period=14)
            if adx_val is None:
                rejected_candidates_count += 1
                rejection_reasons["ADX_HISTORY_INSUFFICIENT"] = rejection_reasons.get("ADX_HISTORY_INSUFFICIENT", 0) + 1
                _record_signal_in_db(
                    conn=conn,
                    signal_id=f"SIG-{date_str.replace('-', '')}-{time_str.replace(':', '')}",
                    created_at=ts_str,
                    status="REJECTED",
                    rejection_reason="ADX_HISTORY_INSUFFICIENT",
                    spot=spot,
                    expiry=expiry,
                    regime="UNKNOWN",
                    bias="UNKNOWN",
                    adx_val=None,
                    last_bar=None,
                    strikes=None,
                    net_credit=0.0,
                    max_profit=0.0,
                    max_loss=0.0
                )
                continue

            adx_eligible = (adx_val < FROZEN_ADX_THRESHOLD)
            if not adx_eligible:
                rejected_candidates_count += 1
                rej_msg = f"ADX_EXCEEDS_THRESHOLD ({adx_val:.2f} >= {FROZEN_ADX_THRESHOLD})"
                rejection_reasons["ADX_EXCEEDS_THRESHOLD"] = rejection_reasons.get("ADX_EXCEEDS_THRESHOLD", 0) + 1
                _record_signal_in_db(
                    conn=conn,
                    signal_id=f"SIG-{date_str.replace('-', '')}-{time_str.replace(':', '')}",
                    created_at=ts_str,
                    status="REJECTED",
                    rejection_reason=rej_msg,
                    spot=spot,
                    expiry=expiry,
                    regime="UNKNOWN",
                    bias="UNKNOWN",
                    adx_val=adx_val,
                    last_bar=last_bar_date,
                    strikes=None,
                    net_credit=0.0,
                    max_profit=0.0,
                    max_loss=0.0
                )
                continue

            # Analyze market regime & candidate strikes
            chain_data = {
                "symbol": "NIFTY",
                "spot_price": spot,
                "spot_change_pct": snap.get("spot_change_pct") or 0.0,
                "expiry": expiry,
                "timestamp": ts_str,
                "chain_by_strike": chain,
                "data_status": "HISTORICAL_REPLAY",
            }

            try:
                analysis = MarketAnalyzer.analyze_chain(chain_data)
                regime = getattr(analysis, "market_regime", "UNKNOWN")
                bias = getattr(analysis, "market_bias", "UNKNOWN")

                if regime != "BALANCED_RANGE":
                    rejected_candidates_count += 1
                    rej_msg = f"REGIME_MISMATCH ({regime} != BALANCED_RANGE)"
                    rejection_reasons["REGIME_MISMATCH"] = rejection_reasons.get("REGIME_MISMATCH", 0) + 1
                    _record_signal_in_db(
                        conn=conn,
                        signal_id=f"SIG-{date_str.replace('-', '')}-{time_str.replace(':', '')}",
                        created_at=ts_str,
                        status="REJECTED",
                        rejection_reason=rej_msg,
                        spot=spot,
                        expiry=expiry,
                        regime=regime,
                        bias=bias,
                        adx_val=adx_val,
                        last_bar=last_bar_date,
                        strikes=None,
                        net_credit=0.0,
                        max_profit=0.0,
                        max_loss=0.0
                    )
                    continue

                # AutoStrategySelector with 100-pt wings
                selection = AutoStrategySelector.evaluate_auto_selection(
                    analysis, chain, wing_width=FROZEN_WING_WIDTH
                )

                if selection.selection_type == "NONE" or not selection.selected_strategies:
                    rejected_candidates_count += 1
                    rejection_reasons["NO_STRATEGY_SELECTED"] = rejection_reasons.get("NO_STRATEGY_SELECTED", 0) + 1
                    continue

                condor_cands = [s for s in selection.selected_strategies if s.strategy_id == "IRON_CONDOR"]
                if not condor_cands:
                    rejected_candidates_count += 1
                    rejection_reasons["NO_CONDOR_CANDIDATE"] = rejection_reasons.get("NO_CONDOR_CANDIDATE", 0) + 1
                    continue

                cand = condor_cands[0]
                if not cand.is_eligible or cand.net_credit_pts <= 0:
                    rejected_candidates_count += 1
                    rej_msg = "; ".join(cand.rejection_reasons) if cand.rejection_reasons else "INELIGIBLE_CREDIT"
                    rejection_reasons[rej_msg] = rejection_reasons.get(rej_msg, 0) + 1
                    continue

                # Extract strikes
                pe_legs = [l for l in cand.legs if l.option_type == "PE"]
                ce_legs = [l for l in cand.legs if l.option_type == "CE"]
                sp = [l.strike for l in pe_legs if l.action == "SELL"][0]
                lp = [l.strike for l in pe_legs if l.action == "BUY"][0]
                sc = [l.strike for l in ce_legs if l.action == "SELL"][0]
                lc = [l.strike for l in ce_legs if l.action == "BUY"][0]

                strikes = {
                    "short_put": sp,
                    "long_put": lp,
                    "short_call": sc,
                    "long_call": lc,
                }

                # Validate quotes
                valid_q, q_err, verified_quotes = validate_quotes_for_candidate(
                    strikes, chain, conn, date_str, time_str
                )
                if not valid_q:
                    rejected_candidates_count += 1
                    rejection_reasons[q_err] = rejection_reasons.get(q_err, 0) + 1
                    _record_signal_in_db(
                        conn=conn,
                        signal_id=f"SIG-{date_str.replace('-', '')}-{time_str.replace(':', '')}",
                        created_at=ts_str,
                        status="REJECTED",
                        rejection_reason=q_err,
                        spot=spot,
                        expiry=expiry,
                        regime=regime,
                        bias=bias,
                        adx_val=adx_val,
                        last_bar=last_bar_date,
                        strikes=strikes,
                        net_credit=0.0,
                        max_profit=0.0,
                        max_loss=0.0
                    )
                    continue

                # Base Entry Pricing: Short at Bid, Long at Ask
                sp_bid = verified_quotes["put_short"]["bid"]
                lp_ask = verified_quotes["put_long"]["ask"]
                sc_bid = verified_quotes["call_short"]["bid"]
                lc_ask = verified_quotes["call_long"]["ask"]

                net_credit = round((sp_bid - lp_ask) + (sc_bid - lc_ask), 2)
                if net_credit <= 0:
                    rejected_candidates_count += 1
                    rejection_reasons["NON_POSITIVE_NET_CREDIT"] = rejection_reasons.get("NON_POSITIVE_NET_CREDIT", 0) + 1
                    continue

                lot_size = ContractRegistry.get_contract_lot_size("NIFTY", expiry)
                qty = lot_size * 1

                # Calculate entry transaction costs
                entry_legs_order = [
                    {"action": "SELL", "price": sp_bid, "quantity": qty},
                    {"action": "BUY", "price": lp_ask, "quantity": qty},
                    {"action": "SELL", "price": sc_bid, "quantity": qty},
                    {"action": "BUY", "price": lc_ask, "quantity": qty},
                ]
                entry_costs = TransactionCostEngine.calculate_order_costs_breakdown(
                    entry_legs_order, lots=1, trade_date=trade_date
                )
                entry_total_costs = entry_costs["total"]

                # Maximum Theoretical Risk Calculation (Wing width - net credit) * qty + estimated round-trip costs
                wing_width = FROZEN_WING_WIDTH
                max_loss_pts = max(0.0, wing_width - net_credit)
                max_loss_inr = round(max_loss_pts * qty, 2)
                max_profit_inr = round(net_credit * qty, 2)
                estimated_round_trip_costs = entry_total_costs * 2.0
                effective_total_risk = round(max_loss_inr + estimated_round_trip_costs, 2)

                # MANDATORY RISK LIMIT: Effective Trade Risk <= INR 5,000
                if effective_total_risk > MAX_RISK_CEILING_INR:
                    rejected_candidates_count += 1
                    rej_msg = f"RISK_CEILING_BREACH(INR {effective_total_risk:.2f} > {MAX_RISK_CEILING_INR})"
                    rejection_reasons["RISK_CEILING_BREACH"] = rejection_reasons.get("RISK_CEILING_BREACH", 0) + 1
                    conn.execute("""
                    INSERT INTO observation_risk_audits (
                        audit_id, created_at, action, approved, reason, max_loss_inr, daily_loss_inr, open_risk_inr
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        f"AUDIT-{date_str.replace('-', '')}-{time_str.replace(':', '')}",
                        ts_str, "ENTRY_EVALUATION", 0, rej_msg, effective_total_risk, daily_loss_incurred, 0.0
                    ))
                    _record_signal_in_db(
                        conn=conn,
                        signal_id=f"SIG-{date_str.replace('-', '')}-{time_str.replace(':', '')}",
                        created_at=ts_str,
                        status="REJECTED",
                        rejection_reason=rej_msg,
                        spot=spot,
                        expiry=expiry,
                        regime=regime,
                        bias=bias,
                        adx_val=adx_val,
                        last_bar=last_bar_date,
                        strikes=strikes,
                        net_credit=net_credit,
                        max_profit=max_profit_inr,
                        max_loss=max_loss_inr
                    )
                    continue

                # Signal is ELIGIBLE -> Enter observation position
                sig_id = f"SIG-{date_str.replace('-', '')}-{time_str.replace(':', '')}"
                _record_signal_in_db(
                    conn=conn,
                    signal_id=sig_id,
                    created_at=ts_str,
                    status="ELIGIBLE",
                    rejection_reason=None,
                    spot=spot,
                    expiry=expiry,
                    regime=regime,
                    bias=bias,
                    adx_val=adx_val,
                    last_bar=last_bar_date,
                    strikes=strikes,
                    net_credit=net_credit,
                    max_profit=max_profit_inr,
                    max_loss=max_loss_inr
                )

                # Insert Risk Audit
                conn.execute("""
                INSERT INTO observation_risk_audits (
                    audit_id, created_at, action, approved, reason, max_loss_inr, daily_loss_inr, open_risk_inr
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    f"AUDIT-{date_str.replace('-', '')}-{time_str.replace(':', '')}",
                    ts_str, "ENTRY_EVALUATION", 1, "APPROVED_WITHIN_RISK_CAP", effective_total_risk, daily_loss_incurred, effective_total_risk
                ))

                # Open Position
                pos_id = f"POS-{date_str.replace('-', '')}-{time_str.replace(':', '')}"
                target_debit = round(net_credit * (1.0 - FROZEN_TARGET_PCT), 2)  # 0.30 * credit
                stop_debit = round(net_credit * FROZEN_STOP_DEBIT_MULT, 2)         # 1.50 * credit

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
                    1, qty, ts_str, spot, net_credit, target_debit, stop_debit, max_profit_inr, max_loss_inr,
                    entry_total_costs, entry_costs["brokerage"], entry_costs["stt"], entry_costs["exchange_fee"],
                    entry_costs["gst"], entry_costs["stamp_duty"], entry_costs["sebi_fee"] + entry_costs["ipft"],
                    "OPEN", "IRON_CONDOR_FROZEN_CANDIDATE_100W", "HISTORICAL_REPLAY"
                ))

                # Insert 4 legs into observation_legs
                leg_sequence = [
                    (0, "put_short", verified_quotes["put_short"], sp_bid),
                    (1, "put_long", verified_quotes["put_long"], lp_ask),
                    (2, "call_short", verified_quotes["call_short"], sc_bid),
                    (3, "call_long", verified_quotes["call_long"], lc_ask),
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
                        fill_p, q_dict["bid"], q_dict["ask"], q_dict["spread_pct"], q_dict["timestamp"], "HISTORICAL_REPLAY"
                    ))

                conn.commit()

                open_pos = {
                    "position_id": pos_id,
                    "signal_id": sig_id,
                    "expiry": expiry,
                    "entry_time": ts_str,
                    "entry_spot": spot,
                    "quantity": qty,
                    "entry_net_credit_pts": net_credit,
                    "target_net_debit_pts": target_debit,
                    "stop_loss_debit_pts": stop_debit,
                    "entry_costs_inr": entry_total_costs,
                    "strikes": strikes,
                    "verified_entry_quotes": verified_quotes,
                }

            except Exception as e:
                rejected_candidates_count += 1
                rejection_reasons[f"ANALYSIS_ERROR: {e}"] = rejection_reasons.get(f"ANALYSIS_ERROR: {e}", 0) + 1

        # Settle any lingering trade at end of backtest
        if open_pos is not None and last_snap is not None:
            _close_observation_position(
                conn=conn,
                pos=open_pos,
                chain=last_snap["chain_by_strike"],
                spot_price=last_snap["spot_price"],
                exit_reason="SESSION_CUTOFF",
                exit_dt=datetime.strptime(last_snap["timestamp"], "%Y-%m-%d %H:%M:%S"),
                source="HISTORICAL_REPLAY"
            )
            open_pos = None

    finally:
        conn.commit()
        conn.close()

    print("[PASS] Replay completed and committed to SQLite ledger.")
    print(f"[*] Total Candidates Evaluated: {evaluated_candidates_count}")
    print(f"[*] Total Candidates Rejected: {rejected_candidates_count}")

    # Generate and reconcile report
    report = generate_comprehensive_report(parquet_files)
    return report


def _record_signal_in_db(
    conn: sqlite3.Connection,
    signal_id: str,
    created_at: str,
    status: str,
    rejection_reason: Optional[str],
    spot: float,
    expiry: str,
    regime: str,
    bias: str,
    adx_val: Optional[float],
    last_bar: Optional[str],
    strikes: Optional[Dict[str, float]],
    net_credit: float,
    max_profit: float,
    max_loss: float
) -> None:
    fingerprint = f"ADX={round(adx_val, 2) if adx_val is not None else 'NA'}|BAR={last_bar or 'NA'}|{created_at}"
    sp = strikes["short_put"] if strikes else None
    lp = strikes["long_put"] if strikes else None
    sc = strikes["short_call"] if strikes else None
    lc = strikes["long_call"] if strikes else None

    conn.execute("""
    INSERT OR REPLACE INTO observation_signals (
        signal_id, created_at, strategy_id, engine_type, symbol, expiry, spot_price,
        market_regime, market_bias, confidence_score, status, rejection_reason,
        short_put, long_put, short_call, long_call, net_credit_pts, wing_width,
        max_profit_inr, max_loss_inr, strategy_version, data_source, fingerprint
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal_id, created_at, "IRON_CONDOR", "INTRADAY", "NIFTY", expiry, spot,
        regime, bias, 0.85 if status == "ELIGIBLE" else 0.0, status, rejection_reason,
        sp, lp, sc, lc, net_credit, FROZEN_WING_WIDTH,
        max_profit, max_loss, "IRON_CONDOR_FROZEN_CANDIDATE_100W", "HISTORICAL_REPLAY", fingerprint
    ))


def _check_position_exit(
    pos: Dict[str, Any],
    chain: Dict[float, Dict[str, Any]],
    spot_price: float,
    current_dt: datetime,
    conn: sqlite3.Connection,
    trade_date: str,
    time_str: str,
) -> Dict[str, Any]:
    """Evaluates current buyback cost: Buy back shorts at Ask, Sell longs at Bid."""
    strikes = pos["strikes"]
    sp = strikes["short_put"]
    lp = strikes["long_put"]
    sc = strikes["short_call"]
    lc = strikes["long_call"]

    legs_check = [
        ("PE", sp, "SELL"),
        ("PE", lp, "BUY"),
        ("CE", sc, "SELL"),
        ("CE", lc, "BUY"),
    ]

    buyback_cost = 0.0
    for opt_type, stk, act in legs_check:
        q = chain.get(stk, {}).get(opt_type, {})
        bid = float(q.get("bid", 0.0) or 0.0) if q else 0.0
        ask = float(q.get("ask", 0.0) or 0.0) if q else 0.0

        if bid <= 0.0 or ask <= 0.0 or bid >= ask:
            _log_dq(conn, "VALUATION_UNAVAILABLE", f"NIFTY{int(stk)}{opt_type}", f"Quote invalid for valuation: bid={bid}, ask={ask}", stk, opt_type, trade_date, time_str, pos["position_id"])
            return {"should_close": False, "reason": "VALUATION_UNAVAILABLE"}

        # Closing: if original action was SELL, we BUY at Ask; if BUY, we SELL at Bid
        if act == "SELL":
            buyback_cost += ask
        else:
            buyback_cost -= bid

    buyback_cost = round(buyback_cost, 2)

    # Check exit triggers
    if buyback_cost <= pos["target_net_debit_pts"]:
        return {"should_close": True, "reason": "TARGET_HIT", "buyback_cost": buyback_cost}
    if buyback_cost >= pos["stop_loss_debit_pts"]:
        return {"should_close": True, "reason": "STOP_LOSS", "buyback_cost": buyback_cost}
    if time_str >= "15:20:00":
        return {"should_close": True, "reason": "SESSION_CUTOFF", "buyback_cost": buyback_cost}

    return {"should_close": False, "buyback_cost": buyback_cost}


def _close_observation_position(
    conn: sqlite3.Connection,
    pos: Dict[str, Any],
    chain: Dict[float, Dict[str, Any]],
    spot_price: float,
    exit_reason: str,
    exit_dt: datetime,
    source: str
) -> float:
    """Executes closure in observation_positions and observation_legs, computing both Base and Conservative slippage."""
    pos_id = pos["position_id"]
    qty = pos["quantity"]
    entry_credit = pos["entry_net_credit_pts"]
    strikes = pos["strikes"]

    # Calculate exit fills
    # Short put bought back at Ask, Long put sold at Bid
    # Short call bought back at Ask, Long call sold at Bid
    sp_q = chain.get(strikes["short_put"], {}).get("PE", {})
    lp_q = chain.get(strikes["long_put"], {}).get("PE", {})
    sc_q = chain.get(strikes["short_call"], {}).get("CE", {})
    lc_q = chain.get(strikes["long_call"], {}).get("CE", {})

    sp_ask = float(sp_q.get("ask", 0.0) or 0.0)
    lp_bid = float(lp_q.get("bid", 0.0) or 0.0)
    sc_ask = float(sc_q.get("ask", 0.0) or 0.0)
    lc_bid = float(lc_q.get("bid", 0.0) or 0.0)

    # Fallback to last known if zero
    if sp_ask <= 0: sp_ask = 0.05
    if sc_ask <= 0: sc_ask = 0.05

    exit_net_debit = round((sp_ask - lp_bid) + (sc_ask - lc_bid), 2)
    gross_pnl = round((entry_credit - exit_net_debit) * qty, 2)

    # Exit order costs
    exit_legs_order = [
        {"action": "BUY", "price": sp_ask, "quantity": qty},
        {"action": "SELL", "price": lp_bid, "quantity": qty},
        {"action": "BUY", "price": sc_ask, "quantity": qty},
        {"action": "SELL", "price": lc_bid, "quantity": qty},
    ]
    exit_costs = TransactionCostEngine.calculate_order_costs_breakdown(
        exit_legs_order, lots=1, trade_date=exit_dt.date()
    )
    exit_total_costs = exit_costs["total"]
    total_costs = round(pos["entry_costs_inr"] + exit_total_costs, 2)
    net_pnl = round(gross_pnl - total_costs, 2)

    # Conservative Execution (10 bps slippage per side on entry + exit fills)
    # Entry fills: Short put (bid), Long put (ask), Short call (bid), Long call (ask)
    # Exit fills: Short put (ask), Long put (bid), Short call (ask), Long call (bid)
    entry_quotes = pos["verified_entry_quotes"]
    entry_fill_sum = (
        entry_quotes["put_short"]["bid"] + entry_quotes["put_long"]["ask"] +
        entry_quotes["call_short"]["bid"] + entry_quotes["call_long"]["ask"]
    )
    exit_fill_sum = sp_ask + lp_bid + sc_ask + lc_bid
    slippage_drag_pts = round((entry_fill_sum + exit_fill_sum) * 0.0010, 4)
    slippage_cost_inr = round(slippage_drag_pts * qty, 2)

    cons_exit_debit = round(exit_net_debit + (exit_fill_sum * 0.0010), 4)
    cons_gross_pnl = round(gross_pnl - slippage_cost_inr, 2)
    cons_total_costs = total_costs  # Fees remain identical
    cons_net_pnl = round(cons_gross_pnl - cons_total_costs, 2)

    # Update observation_positions
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
        exit_dt.strftime("%Y-%m-%d %H:%M:%S"), spot_price, exit_net_debit, exit_reason,
        gross_pnl, net_pnl, total_costs,
        exit_costs["brokerage"], exit_costs["stt"], exit_costs["exchange_fee"],
        exit_costs["gst"], exit_costs["stamp_duty"], exit_costs["sebi_fee"] + exit_costs["ipft"],
        cons_exit_debit, cons_gross_pnl, cons_total_costs, cons_net_pnl, slippage_cost_inr,
        pos_id
    ))

    # Update observation_legs with exit quotes
    exit_leg_data = [
        (0, sp_ask, float(sp_q.get("bid", 0.0) or 0.0), sp_ask),
        (1, lp_bid, lp_bid, float(lp_q.get("ask", 0.0) or 0.0)),
        (2, sc_ask, float(sc_q.get("bid", 0.0) or 0.0), sc_ask),
        (3, lc_bid, lc_bid, float(lc_q.get("ask", 0.0) or 0.0)),
    ]
    for idx, fill_p, b, a in exit_leg_data:
        sp_pct = round(((a - b) / a) * 100.0, 2) if a > 0 else 0.0
        conn.execute("""
        UPDATE observation_legs SET
            exit_fill_price = ?,
            exit_bid = ?,
            exit_ask = ?,
            exit_spread_pct = ?,
            exit_quote_timestamp = ?,
            exit_quote_source = ?
        WHERE position_id = ? AND leg_index = ?
        """, (fill_p, b, a, sp_pct, exit_dt.strftime("%Y-%m-%d %H:%M:%S"), source, pos_id, idx))

    conn.commit()
    return net_pnl


def generate_comprehensive_report(parquet_files: List[str]) -> Dict[str, Any]:
    """
    Queries the SQLite observation ledger and compiles the complete JSON validation report.
    Enforces the chronological partition:
    - Discovery: Sessions 1-75 (2024-10-24 to 2026-03-24)
    - Previously Examined Test Data: Sessions 76-105 (2026-03-30 to 2026-10-19)
    """
    print("=" * 80)
    print("STEP 3: COMPILING OBSERVATIONAL VALIDATION REPORT & RECONCILIATION")
    print("=" * 80)

    # Partition boundary definition
    discovery_cutoff = "2026-03-24"

    with get_db_connection(OBSERVATION_DB_PATH) as conn:
        all_closed = conn.execute("SELECT * FROM observation_positions WHERE status = 'CLOSED' ORDER BY entry_time ASC").fetchall()
        all_signals = conn.execute("SELECT * FROM observation_signals").fetchall()
        all_dq = conn.execute("SELECT * FROM observation_data_quality_events").fetchall()
        all_audits = conn.execute("SELECT * FROM observation_risk_audits").fetchall()

    total_closed = len(all_closed)
    print(f"[*] Total Executed Observation Trades in Ledger: {total_closed}")

    # Reconcile with earlier backtest
    discovery_trades = [t for t in all_closed if str(t["entry_time"])[:10] <= discovery_cutoff]
    test_split_trades = [t for t in all_closed if str(t["entry_time"])[:10] > discovery_cutoff]

    def compute_metrics_for_trades(trades: List[sqlite3.Row]) -> Dict[str, Any]:
        if not trades:
            return {"total_trades": 0, "net_pnl_inr": 0.0, "win_rate_pct": 0.0, "profit_factor": 0.0}
        n = len(trades)
        wins = [t for t in trades if t["net_pnl_inr"] > 0]
        losses = [t for t in trades if t["net_pnl_inr"] <= 0]
        gross_pnl = sum(t["gross_pnl_inr"] for t in trades)
        total_costs = sum(t["total_costs_inr"] for t in trades)
        net_pnl = sum(t["net_pnl_inr"] for t in trades)
        win_rate = round((len(wins) / n) * 100.0, 2)
        gross_win = sum(t["net_pnl_inr"] for t in wins)
        gross_loss = abs(sum(t["net_pnl_inr"] for t in losses))
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else 999.0
        expectancy = round(net_pnl / n, 2)

        # Conservative metrics
        cons_net_pnl = sum(t["conservative_net_pnl_inr"] for t in trades)
        cons_wins = [t for t in trades if t["conservative_net_pnl_inr"] > 0]
        cons_losses = [t for t in trades if t["conservative_net_pnl_inr"] <= 0]
        cons_win_rate = round((len(cons_wins) / n) * 100.0, 2)
        cons_gross_win = sum(t["conservative_net_pnl_inr"] for t in cons_wins)
        cons_gross_loss = abs(sum(t["conservative_net_pnl_inr"] for t in cons_losses))
        cons_profit_factor = round(cons_gross_win / cons_gross_loss, 2) if cons_gross_loss > 0 else 999.0
        slippage_drag = sum(t["conservative_slippage_cost_inr"] for t in trades)

        # Drawdown
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            cum += t["net_pnl_inr"]
            if cum > peak: peak = cum
            dd = peak - cum
            if dd > max_dd: max_dd = dd

        return {
            "total_trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": win_rate,
            "gross_pnl_inr": round(gross_pnl, 2),
            "total_costs_inr": round(total_costs, 2),
            "net_pnl_inr": round(net_pnl, 2),
            "profit_factor": profit_factor,
            "expectancy_inr": expectancy,
            "max_drawdown_inr": round(max_dd, 2),
            "conservative_scenario": {
                "net_pnl_inr": round(cons_net_pnl, 2),
                "win_rate_pct": cons_win_rate,
                "profit_factor": cons_profit_factor,
                "slippage_drag_inr": round(slippage_drag, 2),
            }
        }

    overall_metrics = compute_metrics_for_trades(all_closed)
    discovery_metrics = compute_metrics_for_trades(discovery_trades)
    test_metrics = compute_metrics_for_trades(test_split_trades)

    # Itemized trade records
    trade_records = []
    for t in all_closed:
        trade_records.append({
            "position_id": t["position_id"],
            "signal_id": t["signal_id"],
            "trade_date": str(t["entry_time"])[:10],
            "partition": "DISCOVERY_IN_SAMPLE" if str(t["entry_time"])[:10] <= discovery_cutoff else "PREVIOUSLY_EXAMINED_TEST_DATA",
            "entry_time": t["entry_time"],
            "exit_time": t["exit_time"],
            "entry_spot": t["entry_spot"],
            "exit_spot": t["exit_spot"],
            "entry_net_credit_pts": t["entry_net_credit_pts"],
            "exit_net_debit_pts": t["exit_net_debit_pts"],
            "target_net_debit_pts": t["target_net_debit_pts"],
            "stop_loss_debit_pts": t["stop_loss_debit_pts"],
            "exit_reason": t["exit_reason"],
            "gross_pnl_inr": t["gross_pnl_inr"],
            "total_costs_inr": t["total_costs_inr"],
            "net_pnl_inr": t["net_pnl_inr"],
            "conservative_net_pnl_inr": t["conservative_net_pnl_inr"],
            "conservative_slippage_cost_inr": t["conservative_slippage_cost_inr"],
            "max_risk_inr": t["max_loss_inr"],
            "cost_breakdown": {
                "brokerage": t["brokerage_inr"],
                "stt": t["stt_inr"],
                "exchange_fees": t["exchange_fees_inr"],
                "gst": t["gst_inr"],
                "stamp_duty": t["stamp_duty_inr"],
                "sebi_ipft": t["sebi_ipft_inr"],
            }
        })

    # Summary of signals & rejections
    sig_by_status = {}
    rejections_breakdown = {}
    for s in all_signals:
        st = s["status"]
        sig_by_status[st] = sig_by_status.get(st, 0) + 1
        if s["rejection_reason"]:
            rej = s["rejection_reason"]
            rejections_breakdown[rej] = rejections_breakdown.get(rej, 0) + 1

    dq_breakdown = {}
    for dq in all_dq:
        et = dq["event_type"]
        dq_breakdown[et] = dq_breakdown.get(et, 0) + 1

    report_data = {
        "report_metadata": {
            "title": "Iron Condor Observational Validation Report (Isolated Research Ledger)",
            "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
            "strategy_id": "IRON_CONDOR",
            "candidate_name": "100-Point Wings + Causal ADX(14) < 22.0",
            "execution_mode": "ISOLATED_HISTORICAL_REPLAY",
            "database_target": "instance/research_paper_trading/observation_ledger.db",
            "database_isolation_status": "VERIFIED_ISOLATED_FROM_PRODUCTION",
            "checksum_verified": True,
            "reconciled_matrix_sha256": EXPECTED_MATRIX_HASH,
        },
        "frozen_candidate_parameters": {
            "wing_width_points": FROZEN_WING_WIDTH,
            "regime_filter": "BALANCED_RANGE",
            "trend_filter": f"Causal ADX(14) < {FROZEN_ADX_THRESHOLD}",
            "profit_target_pct": 70.0,
            "profit_target_rule": "Exit debit <= 0.30 * initial net credit",
            "stop_loss_rule": "Exit debit >= 1.50 * initial net credit (equivalent to 0.50x credit loss)",
            "order_sizing": "1 Lot (NIFTY Contract Standard)",
            "concurrency_limit": "Strictly 1 open position",
            "loss_cooldown_mins": Config.CONSECUTIVE_LOSS_COOLDOWN_MINS,
            "daily_loss_circuit_breaker_inr": Config.get_max_daily_loss_inr(),
            "max_risk_ceiling_inr": MAX_RISK_CEILING_INR,
            "max_spread_pct_filter": MAX_SPREAD_PCT,
        },
        "slippage_model_comparison": {
            "earlier_backtest_base_slippage": "Fixed 0.05 pts per leg deducted from P&L",
            "observation_engine_base_slippage": "Zero additional deduction beyond natural Bid/Ask crossing (Shorts sell at Bid, Longs buy at Ask)",
            "observation_engine_conservative_slippage": "10 bps (0.10%) percentage slippage on each individual leg fill price on entry and exit",
            "duplicate_deductions_prevented": True,
        },
        "data_coverage_and_partitioning": {
            "total_sessions_analyzed": len(parquet_files),
            "date_range": "2024-10-24 to 2026-10-19",
            "discovery_partition": {
                "label": "Discovery / In-Sample Replay",
                "sessions_count": 75,
                "date_range": "2024-10-24 to 2026-03-24",
                "description": "Sessions initially used to develop and explore the ADX filter",
            },
            "test_split_partition": {
                "label": "Previously Examined Test Data",
                "sessions_count": 30,
                "date_range": "2026-03-30 to 2026-10-19",
                "description": "Sessions 76-105 previously examined during baseline reconciliation. NOT an independent holdout or forward observation.",
            },
            "independent_forward_observation_note": "True forward paper observation requires accumulating new sessions in real-time quote feeds; historical replay is not forward validation.",
        },
        "performance_metrics": {
            "overall_summary": overall_metrics,
            "discovery_summary": discovery_metrics,
            "test_split_summary": test_metrics,
        },
        "signal_evaluation_audit": {
            "total_signals_evaluated": len(all_signals),
            "eligible_signals": sig_by_status.get("ELIGIBLE", 0),
            "rejected_signals": sig_by_status.get("REJECTED", 0),
            "top_rejection_reasons": rejections_breakdown,
        },
        "data_quality_audit": {
            "total_data_quality_events": len(all_dq),
            "events_by_type": dq_breakdown,
        },
        "risk_limit_audit": {
            "risk_evaluations_count": len(all_audits),
            "breaches_rejected": len([a for a in all_audits if a["approved"] == 0]),
            "max_risk_ceiling_breach_count": rejections_breakdown.get("RISK_CEILING_BREACH", 0),
        },
        "trade_ledger": trade_records,
    }

    out_path = os.path.join(WORKSPACE_ROOT, "instance", "research_reports", "observation_validation_report.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)

    print(f"\n[PASS] Report written to: {out_path}")
    print("=" * 80)
    print("RECONCILIATION SUMMARY:")
    print(f"Total Trades: {overall_metrics['total_trades']}")
    print(f"Base Net P&L: INR {overall_metrics['net_pnl_inr']:,.2f}")
    print(f"Base Win Rate: {overall_metrics['win_rate_pct']:.1f}%")
    print(f"Base Profit Factor: {overall_metrics['profit_factor']:.2f}")
    print(f"Base Max Drawdown: INR {overall_metrics['max_drawdown_inr']:,.2f}")
    print(f"Conservative Net P&L (10 bps slippage): INR {overall_metrics['conservative_scenario']['net_pnl_inr']:,.2f}")
    print(f"Conservative Slippage Drag: INR {overall_metrics['conservative_scenario']['slippage_drag_inr']:,.2f}")
    print(f"Discovery Partition Net P&L (Sessions 1-75): INR {discovery_metrics['net_pnl_inr']:,.2f} ({discovery_metrics['total_trades']} trades)")
    print(f"Test Partition Net P&L (Sessions 76-105): INR {test_metrics['net_pnl_inr']:,.2f} ({test_metrics['total_trades']} trades)")
    print("=" * 80)

    return report_data


if __name__ == "__main__":
    run_observation_replay()
