"""
app/services/options_scheduler.py — Background Telemetry, Signal Dispatcher & Review Scheduler
- Enforces single-instance process lock.
- Operates strictly in Asia/Kolkata timezone, skipping cycles outside 09:15-15:30 IST, on market holidays, or when data_status != LIVE.
- Executes analyze_option_desk per cycle for each primary index symbol.
- Logs decision evaluation telemetry (every cycle including WAIT) with full pillar flags, raw values, and data quality.
- Records signals only when high-conviction criteria are met and bid/ask quotes are valid.
- Records daily ATM IV observations into daily_atm_iv during market hours.
- Passes fetched market data chains as market_data_cache to update_active_signals.
- Runs backfill_forward_labels periodically (~every 15 mins).
- Runs review_unreviewed_closed_trades at scheduler startup.
- Generates and persists daily review reports to daily_review_reports table after 15:45 IST.
"""

import os
import time
import json
import sqlite3
import threading
import logging
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo
from config.trading_holidays import NSE_HOLIDAYS, is_market_holiday, is_special_session
from app.services.fyers_options_service import fetch_option_chain_data, INDEX_CONFIGS
from app.services.options_engine import analyze_option_desk, ENGINE_CONFIG
from app.services.options_signal_service import (
    _get_db,
    record_signal,
    update_active_signals,
    log_decision,
    backfill_forward_labels,
    record_daily_atm_iv,
    record_trending_oi_snapshot,
    cleanup_old_telemetry,
    acquire_scheduler_exclusive_lock,
    heartbeat_scheduler_lock,
    release_scheduler_exclusive_lock,
    get_intraday_spot_momentum,
)
from app.services.trade_analyzer_agent import (
    review_unreviewed_closed_trades,
    generate_daily_review_report,
)

log = logging.getLogger("options_scheduler")

IST = ZoneInfo("Asia/Kolkata")

_scheduler_thread = None
_stop_scheduler = threading.Event()
LOCK_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../instance/options_scheduler.lock"))

# Thread-safe per-symbol pending entry confirmation state
_CONFIRMATION_LOCK = threading.Lock()
_PENDING_ENTRY_CONFIRMATIONS: dict[str, dict] = {}


def get_pending_entry_confirmation(symbol: str) -> dict:
    """Returns the current pending entry confirmation state for a symbol."""
    with _CONFIRMATION_LOCK:
        if symbol not in _PENDING_ENTRY_CONFIRMATIONS:
            _PENDING_ENTRY_CONFIRMATIONS[symbol] = {
                "candidate_side": None,
                "confirmation_count": 0,
                "required": ENGINE_CONFIG.get("CONSECUTIVE_CONFIRMATIONS_REQUIRED", 2),
                "first_pass_time": None,
                "last_cycle": "",
            }
        return dict(_PENDING_ENTRY_CONFIRMATIONS[symbol])


def update_pending_entry_confirmation(
    symbol: str, candidate_side: str | None, req_confs: int = 2, now_dt: datetime | None = None
) -> tuple[int, bool, float | None]:
    """
    Updates consecutive entry confirmation state for symbol.
    Returns (confirmation_count, is_fully_confirmed, entry_latency_sec).
    - If candidate_side is CE/PE and matches previous pending candidate, increments count.
    - If candidate_side changes, is None (or gate fails/WAIT), resets count.
    - Tracks elapsed seconds from initial raw gate pass to confirmed entry.
    """
    if now_dt is None:
        now_dt = datetime.now(IST)

    with _CONFIRMATION_LOCK:
        if symbol not in _PENDING_ENTRY_CONFIRMATIONS:
            _PENDING_ENTRY_CONFIRMATIONS[symbol] = {
                "candidate_side": None,
                "confirmation_count": 0,
                "required": req_confs,
                "first_pass_time": None,
                "last_cycle": "",
            }

        state = _PENDING_ENTRY_CONFIRMATIONS[symbol]
        state["required"] = req_confs

        if candidate_side in ("CE", "PE"):
            if state["candidate_side"] == candidate_side:
                state["confirmation_count"] += 1
            else:
                state["candidate_side"] = candidate_side
                state["confirmation_count"] = 1
                state["first_pass_time"] = now_dt

            if state.get("first_pass_time") is None:
                state["first_pass_time"] = now_dt

            is_confirmed = (state["confirmation_count"] >= req_confs)
            return state["confirmation_count"], is_confirmed
        else:
            state["candidate_side"] = None
            state["confirmation_count"] = 0
            state["first_pass_time"] = None
            return 0, False


def get_pending_entry_latency(symbol: str, now_dt: datetime | None = None) -> float | None:
    """Returns the time in seconds from first raw gate pass to execution for a confirmed candidate."""
    if now_dt is None:
        now_dt = datetime.now(IST)
    with _CONFIRMATION_LOCK:
        state = _PENDING_ENTRY_CONFIRMATIONS.get(symbol)
        if state and state.get("first_pass_time"):
            return round((now_dt - state["first_pass_time"]).total_seconds(), 2)
    return None


def reset_pending_entry_confirmation(symbol: str):
    """Resets confirmation state after a signal is successfully recorded or on data failure."""
    with _CONFIRMATION_LOCK:
        _PENDING_ENTRY_CONFIRMATIONS[symbol] = {
            "candidate_side": None,
            "confirmation_count": 0,
            "required": ENGINE_CONFIG.get("CONSECUTIVE_CONFIRMATIONS_REQUIRED", 2),
            "first_pass_time": None,
            "last_cycle": "",
        }


def _acquire_process_lock() -> bool:
    """Acquires a real exclusive process lock with PID liveness check and heartbeat."""
    return acquire_scheduler_exclusive_lock(lock_id="options_scheduler", lease_timeout_sec=30)


def _release_process_lock():
    """Releases the exclusive process lock on shutdown."""
    release_scheduler_exclusive_lock(lock_id="options_scheduler")


def is_market_hours(now_dt: datetime | None = None) -> bool:
    """Checks if current time is within Indian Market Hours (Mon-Fri 09:15 - 15:30 IST), accounting for holidays and special sessions."""
    if now_dt is None:
        now_dt = datetime.now(IST)

    today = now_dt.date()
    cur_time = now_dt.time()

    # Check for special trading sessions (e.g. Diwali Muhurat Trading)
    is_special, _, session_times = is_special_session(today)
    if is_special and session_times:
        start_t, end_t = session_times
        return start_t <= cur_time <= end_t

    # Weekdays: Mon(0) to Fri(4)
    if now_dt.weekday() >= 5:
        return False

    if is_market_holiday(today):
        return False

    return dtime(9, 15) <= cur_time <= dtime(15, 30)


def persist_daily_report(report_data: dict) -> bool:
    """Saves daily review report to daily_review_reports table."""
    try:
        report_date = report_data.get("date") or datetime.now(IST).strftime("%Y-%m-%d")
        now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        report_json = json.dumps(report_data)

        with _get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT INTO daily_review_reports (report_date, report_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(report_date) DO UPDATE SET
                    report_json = excluded.report_json,
                    created_at = excluded.created_at
            """, (report_date, report_json, now_str))
            conn.execute("COMMIT")
            log.info(f"Persisted daily review report for {report_date}.")
            return True
    except Exception as e:
        log.warning(f"Could not persist daily review report: {e}")
        return False


def _refresh_worker():
    """Periodic worker that polls primary indices every 180 seconds (3 mins)."""
    if not _acquire_process_lock():
        log.warning("Another options scheduler process is already running. Exiting worker thread.")
        return

    log.info("Options Desk scheduler started (Asia/Kolkata timezone).")

    # Run startup review of any unreviewed closed trades
    try:
        unreviewed_count = review_unreviewed_closed_trades()
        if unreviewed_count > 0:
            log.info(f"[STARTUP] Reviewed {unreviewed_count} previously closed unreviewed trades.")
    except Exception as e:
        log.warning(f"[STARTUP] review_unreviewed_closed_trades error: {e}")

    last_fwd_backfill_time = 0.0
    daily_report_saved_for_date = None

    try:
        while not _stop_scheduler.is_set():
            now_dt = datetime.now(IST)
            now_time = now_dt.time()
            today_date_str = now_dt.strftime("%Y-%m-%d")
            in_market = is_market_hours(now_dt)

            heartbeat_scheduler_lock("options_scheduler")

            if not in_market:
                # Reset pending confirmations so pre-market / stale data does not accumulate counts
                with _CONFIRMATION_LOCK:
                    _PENDING_ENTRY_CONFIRMATIONS.clear()

                # Process any active signals for EOD closure or trade management
                try:
                    update_active_signals(market_data_cache={})
                except Exception as e:
                    log.warning(f"Error updating active signals out of market: {e}")

                # Generate daily review report after 15:45 IST
                if now_time >= dtime(15, 45) and daily_report_saved_for_date != today_date_str:
                    try:
                        rep = generate_daily_review_report(today_date_str)
                        if persist_daily_report(rep):
                            daily_report_saved_for_date = today_date_str
                            cleanup_old_telemetry(retention_days=400)
                    except Exception as e:
                        log.warning(f"Error generating daily review report: {e}")

                # Sleep 60 seconds when market is closed before checking again
                for _ in range(60):
                    if _stop_scheduler.is_set():
                        break
                    time.sleep(1)
                continue

            market_data_cache = {}

            try:
                for sym in list(INDEX_CONFIGS.keys()):
                    if _stop_scheduler.is_set():
                        break
                    time.sleep(0.3)  # Gentle pacing to avoid burst rate spikes
                    try:
                        raw = fetch_option_chain_data(symbol=sym, force_refresh=True)
                        data_status = raw.get("data_status", "LIVE")
                        market_data_cache[sym] = raw

                        # Skip execution if data is stale or not LIVE
                        if data_status != "LIVE":
                            reset_pending_entry_confirmation(sym)
                            log.info(f"Skipping strategy execution for {sym}: data_status is {data_status}")
                            continue

                        # Run analytical engine (Item 10: update_state=True in scheduler only)
                        analysis = analyze_option_desk(raw, update_state=True)
                        if "error" in analysis:
                            reset_pending_entry_confirmation(sym)
                            continue

                        # Periodically store Trending OI observation (ATM ± 5 strikes)
                        record_trending_oi_snapshot(sym, analysis)

                        opt_buying = analysis.get("option_buying", {})
                        cand_decision = opt_buying.get("decision", "WAIT")
                        score = opt_buying.get("setup_score", 0)
                        trade_plan = opt_buying.get("trade_plan", {})
                        pillar_flags = opt_buying.get("pillar_flags", {})
                        raw_values = opt_buying.get("raw_values", {})
                        data_quality = opt_buying.get("data_quality", {})
                        market_bias = analysis.get("market_bias", {})

                        req_confs = ENGINE_CONFIG.get("CONSECUTIVE_CONFIRMATIONS_REQUIRED", 2)

                        # Evaluate final entry persistence:
                        # Candidate BUY must pass all mandatory gates for CONSECUTIVE_CONFIRMATIONS_REQUIRED cycles
                        entry_latency_sec = None
                        if cand_decision in ("BUY CE", "BUY PE", "CE", "PE"):
                            cand_side = "CE" if "CE" in cand_decision else "PE"
                            conf_count, is_confirmed = update_pending_entry_confirmation(sym, cand_side, req_confs, now_dt)
                            if is_confirmed:
                                entry_latency_sec = get_pending_entry_latency(sym, now_dt)
                                final_decision = f"BUY {cand_side}"
                                decision_reason = f"{opt_buying.get('reason', '')} [Confirmed {conf_count}/{req_confs} consecutive evaluations]"
                            else:
                                final_decision = "WAIT"
                                decision_reason = f"Candidate BUY {cand_side} pending confirmation ({conf_count}/{req_confs} evaluations). Preserving entry discipline."
                        else:
                            cand_side = None
                            conf_count, is_confirmed = update_pending_entry_confirmation(sym, None, req_confs, now_dt)
                            final_decision = "WAIT"
                            decision_reason = opt_buying.get("reason", "")

                        spot_val = float(market_bias.get("spot_price", 0.0) or raw.get("spot_price", 0.0))
                        spot_chg = float(market_bias.get("spot_change_pct", 0.0) or raw.get("spot_change_pct", 0.0))
                        atm_strike = float(analysis.get("key_levels", {}).get("atm_strike", 0.0))
                        atm_iv = market_bias.get("atm_iv")
                        iv_pct = market_bias.get("iv_percentile")
                        vix_val = market_bias.get("vix")
                        dte_val = market_bias.get("dte")
                        pcr_val = float(market_bias.get("pcr", 1.0) or 1.0)
                        bull_score = float(market_bias.get("bullish_score", 0.0))
                        bear_score = float(market_bias.get("bearish_score", 0.0))
                        bias_str = str(market_bias.get("bias", "NEUTRAL")).upper()

                        # Store daily ATM IV observation during market hours
                        if in_market and atm_iv is not None and float(atm_iv) > 0.01:
                            record_daily_atm_iv(sym, float(atm_iv), today_date_str)

                        # Item 9: Compute intraday spot momentum (% vs ~15 mins ago)
                        intra_mom = get_intraday_spot_momentum(sym, window_minutes=15)
                        window_pcr = analysis.get("key_levels", {}).get("window_pcr")

                        contract_name = trade_plan.get("contract_name") if trade_plan else ""
                        contract_ltp = float(trade_plan.get("ltp", 0.0) or trade_plan.get("entry_price", 0.0)) if trade_plan else 0.0
                        contract_bid = float(trade_plan.get("bid_at_entry", 0.0)) if trade_plan else 0.0
                        contract_ask = float(trade_plan.get("ask_at_entry", 0.0) or trade_plan.get("entry_price", 0.0)) if trade_plan else 0.0
                        spread_pct = float(trade_plan.get("bid_ask_spread_pct", 0.0)) if trade_plan else 0.0
                        spread_paid = float(trade_plan.get("spread_paid", 0.0)) if trade_plan else 0.0
                        moneyness = trade_plan.get("moneyness", "ATM") if trade_plan else "ATM"

                        # 1. Log decision evaluation telemetry (every cycle including WAIT)
                        decision_payload = {
                            "symbol": sym,
                            "spot_price": spot_val,
                            "spot_change_pct": spot_chg,
                            "atm_strike": atm_strike,
                            "atm_iv": atm_iv,
                            "iv_percentile": iv_pct,
                            "vix": vix_val,
                            "dte": dte_val,
                            "pcr": pcr_val,
                            "window_pcr": window_pcr,
                            "bullish_score": bull_score,
                            "bearish_score": bear_score,
                            "bias": bias_str,
                            "pillar_flags": pillar_flags,
                            "raw_values": {
                                **raw_values,
                                "candidate_side": cand_side,
                                "confirmation_count": conf_count,
                                "required_confirmations": req_confs,
                                "intraday_momentum_pct": intra_mom,
                            },
                            "data_quality": data_quality,
                            "chosen_contract": contract_name,
                            "expiry": trade_plan.get("expiry") if trade_plan else "",
                            "strike": trade_plan.get("strike") if trade_plan else 0.0,
                            "type": trade_plan.get("type") if trade_plan else final_decision,
                            "contract_ltp": contract_ltp,
                            "contract_bid": contract_bid,
                            "contract_ask": contract_ask,
                            "bid_ask_spread_pct": spread_pct,
                            "spread_paid": spread_paid,
                            "setup_score": score,
                            "candidate_side": cand_side,
                            "confirmation_count": conf_count,
                            "required_confirmations": req_confs,
                            "decision": final_decision,
                            "decision_reason": decision_reason,
                            "entry_latency_sec": entry_latency_sec,
                            "intraday_momentum_pct": intra_mom,
                        }
                        log_decision(decision_payload)

                        # 2. Auto-record signal only during active market hours, LIVE data, fully confirmed entry, and valid bid/ask
                        has_valid_quotes = (contract_bid > 0.05 and contract_ask > 0.05)
                        if (
                            in_market
                            and data_status == "LIVE"
                            and data_quality.get("valid", True)
                            and has_valid_quotes
                            and final_decision in ("BUY CE", "BUY PE", "CE", "PE")
                        ):
                            passed_checks = [c.get("name") for c in opt_buying.get("checklist", []) if c.get("passed")]
                            evidence_tag = f"Score: {score}/100 | PCR: {pcr_val:.2f} | Bias: {bias_str} | Confirmed: {conf_count}/{req_confs} | Checks: {', '.join(passed_checks[:3])}"

                            sig_payload = {
                                "symbol": sym,
                                "type": cand_side,
                                "contract_name": contract_name,
                                "strike": trade_plan.get("strike"),
                                "expiry": trade_plan.get("expiry"),
                                "spot_price": spot_val,
                                "entry_price": contract_ask,
                                "ask_at_entry": contract_ask,
                                "bid_at_entry": contract_bid,
                                "spread_paid": spread_paid,
                                "stop_loss": trade_plan.get("stop_loss"),
                                "target_1": trade_plan.get("target_1"),
                                "target_2": trade_plan.get("target_2"),
                                "risk_reward": trade_plan.get("risk_reward", "1:2.0"),
                                "setup_score": score,
                                "pcr": pcr_val,
                                "bias": bias_str,
                                "atm_iv": atm_iv,
                                "dte": dte_val,
                                "moneyness": moneyness,
                                "pillar_flags": pillar_flags,
                                "raw_values": {
                                    **raw_values,
                                    "candidate_side": cand_side,
                                    "confirmation_count": conf_count,
                                    "required_confirmations": req_confs,
                                    "intraday_momentum_pct": intra_mom,
                                },
                                "data_quality": data_quality,
                                "entry_latency_sec": entry_latency_sec,
                                "reason": f"{opt_buying.get('reason', '')} [{evidence_tag}]"
                            }
                            # Explicit Paper Trade Signal Recording (No broker order execution)
                            log.info(f"[PAPER_TRADE_ONLY] Dispatching paper signal for {sym} {trade_plan.get('contract_name')}")
                            record_signal(sig_payload, is_paper_trade=True)
                            reset_pending_entry_confirmation(sym)
                    except Exception as e:
                        log.warning(f"Background refresh for {sym} failed: {e}")
                    time.sleep(1)

                # Update active signals with market data cache
                try:
                    update_active_signals(market_data_cache=market_data_cache)
                except Exception as e:
                    log.warning(f"Error updating active signals: {e}")

                # Periodically backfill forward labels every ~15 minutes
                cur_monotonic = time.monotonic()
                if (cur_monotonic - last_fwd_backfill_time) >= 900.0:
                    try:
                        backfill_forward_labels(batch_size=50)
                        last_fwd_backfill_time = cur_monotonic
                    except Exception as e:
                        log.warning(f"Error backfilling forward labels: {e}")

                # Generate daily review report after 15:45 IST
                if now_time >= dtime(15, 45) and daily_report_saved_for_date != today_date_str:
                    try:
                        rep = generate_daily_review_report(today_date_str)
                        if persist_daily_report(rep):
                            daily_report_saved_for_date = today_date_str
                    except Exception as e:
                        log.warning(f"Error generating daily review report: {e}")

            except Exception as e:
                log.error(f"Error in options background worker cycle: {e}")

            for _ in range(180):
                if _stop_scheduler.is_set():
                    break
                time.sleep(1)
    finally:
        _release_process_lock()
        log.info("Options scheduler worker cleanly terminated.")


def start_options_scheduler(app=None):
    """Starts background thread if not already running."""
    global _scheduler_thread
    if _scheduler_thread is None or not _scheduler_thread.is_alive():
        _stop_scheduler.clear()
        _scheduler_thread = threading.Thread(target=_refresh_worker, daemon=True, name="OptionsDeskUpdater")
        _scheduler_thread.start()
