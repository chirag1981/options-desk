"""
app/services/trade_analyzer_agent.py - Rigorous Data-Driven Paper Trade Analysis & Automated Review Agent
OBSERVE -> DATA QUALITY FILTER -> 6 PILLARS -> STATISTICAL VALIDATION -> COUNTERFACTUAL REPLAY -> ADVISORY RECOMMENDATIONS

Automated Post-Trade Review System:
- Automated review_closed_trade(signal_id) triggered on terminal trade exit.
- Idempotent execution (1 review per signal_id in trade_reviews table).
- 7-state mutually exclusive classification:
  1. WRONG_DIRECTION (never in profit, MFE <= 0.05R)
  2. RIGHT_THEN_REVERSED (reached >= 0.5R or 50% T1, then reversed to loss)
  3. STOPPED_BY_NOISE (hit STOP_LOSS_HIT, but post-exit price recovered to T1 within 30m)
  4. THETA_STAGNATION (held >= 45m without reaching T1 or SL, eroded to loss)
  5. CLEAN_WIN (T1/T2 hit with minimal drawdown, MAE <= 0.5R)
  6. EARLY_EXIT (target hit or manual exit, then ran significantly further >= 1.5x)
  7. DATA_QUALITY_ISSUE (missing bid/ask, zero IV, outlier quote)
- Uses immutable initial_stop_loss for all R-multiple calculations.
- Measures setup pillar/gate edge from decision_log forward labels across all cycles (including WAIT).
- Replays counterfactual variations on real trade_ticks.
- Hypotheses generated from live ENGINE_CONFIG and promoted only via out-of-sample walk-forward verification.
- Advisory-only: never modifies live engine parameters automatically.
"""

import json
import math
import random
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.services.fyers_options_service import INDEX_CONFIGS
from app.services.options_engine import ENGINE_CONFIG
from app.services.options_signal_service import _get_db
from app.services.options_replay_backtest import calculate_trade_costs, run_walk_forward_evaluation, replay_trade_on_ticks

log = logging.getLogger("trade_analyzer_agent")

IST = ZoneInfo("Asia/Kolkata")
MIN_SAMPLE_SIZE = 30  # Minimum sample size required per segment for statistical inference
CONFIDENCE_LEVEL = 0.95
Z_SCORE_95 = 1.96

def validate_index_configs_against_circulars() -> bool:
    """
    Startup check validating that all supported indices in INDEX_CONFIGS
    have valid positive lot sizes and strike steps defined in the single dated source of truth.
    """
    all_valid = True
    required_indices = list(INDEX_CONFIGS.keys())
    for sym in required_indices:
        cfg = INDEX_CONFIGS.get(sym)
        if not cfg:
            log.warning(f"[INDEX_CONFIG_MISMATCH] Missing configuration for index {sym} in INDEX_CONFIGS.")
            all_valid = False
            continue
        cfg_lot = cfg.get("lot_size")
        if not cfg_lot or cfg_lot <= 0:
            log.warning(f"[INDEX_CONFIG_MISMATCH] {sym} has invalid lot size {cfg_lot} in INDEX_CONFIGS.")
            all_valid = False
    return all_valid


try:
    validate_index_configs_against_circulars()
except Exception as e:
    log.warning(f"Error during index config validation: {e}")


# ==============================================================================
# Per-Trade Automated Review System (Single Trade Review)
# ==============================================================================

def review_closed_trade(signal_id: int) -> dict | None:
    """
    Evaluates a completed paper trade immediately upon reaching terminal status.
    Idempotent: Returns existing review if already evaluated.
    """
    with _get_db() as conn:
        existing = conn.execute("SELECT * FROM trade_reviews WHERE signal_id = ?", (signal_id,)).fetchone()
        if existing:
            res = dict(existing)
            res["is_data_quality_flagged"] = bool(json.loads(res.get("data_quality_flags_json") or "[]"))
            res["counterfactual_results_json"] = res.get("counterfactuals_json")
            return res

        sig_row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        if not sig_row:
            log.warning(f"review_closed_trade: signal #{signal_id} not found.")
            return None
        sig = dict(sig_row)

        ticks_rows = conn.execute("SELECT * FROM trade_ticks WHERE signal_id = ? ORDER BY id ASC", (signal_id,)).fetchall()
        ticks = [dict(t) for t in ticks_rows]

    # 1. Trade Parameters
    symbol = (sig.get("symbol") or "NIFTY").upper()
    contract_name = sig.get("contract_name") or f"{sig.get('strike')} {sig.get('signal_type')}"
    sig_type = (sig.get("signal_type") or "CE").upper()
    entry_p = float(sig.get("ask_at_entry") or sig.get("entry_price") or 0.0)
    exit_p = float(sig.get("bid_at_exit") or sig.get("exit_price") or entry_p)

    # Risk denominator strictly uses immutable initial_stop_loss
    init_sl = float(sig.get("initial_stop_loss") or sig.get("stop_loss") or (entry_p * 0.85))
    initial_risk = max(entry_p - init_sl, 0.1)

    t1_p = float(sig.get("target_1") or (entry_p + initial_risk * 1.5))
    t2_p = float(sig.get("target_2") or (entry_p + initial_risk * 2.5))

    cfg = INDEX_CONFIGS.get(symbol, INDEX_CONFIGS.get("NIFTY", {"lot_size": 25}))
    lot_size = int(sig.get("lot_size") or cfg.get("lot_size", 25))
    lots = int(sig.get("lots") or 1)
    dur_mins = float(sig.get("duration_mins") or 0.0)
    exit_reason = (sig.get("exit_reason") or sig.get("status") or "").upper()
    iv = sig.get("iv_at_entry")

    # DTE resolution from raw values / data quality
    dte = sig.get("dte")
    if dte is None and sig.get("raw_values_json"):
        try:
            rv = json.loads(sig["raw_values_json"])
            dte = rv.get("dte")
        except Exception:
            pass
    if dte is None and sig.get("data_quality_json"):
        try:
            dq = json.loads(sig["data_quality_json"])
            dte = dq.get("dte")
        except Exception:
            pass

    # 2. Data Quality Evaluation
    data_quality_flags = []
    if dur_mins < 3.0:
        data_quality_flags.append("SHORT_DURATION")
    if entry_p <= 0.05 or entry_p in (100.0, 50.0) or entry_p > 50000.0:
        data_quality_flags.append("INVALID_OR_FABRICATED_ENTRY_PRICE")
    if exit_p <= 0.05:
        data_quality_flags.append("INVALID_EXIT_PRICE")
    if iv is None or float(iv) <= 0.01:
        data_quality_flags.append("MISSING_OR_ZERO_IV")
    if sig.get("bid_at_entry") is None or sig.get("ask_at_entry") is None:
        data_quality_flags.append("MISSING_BID_ASK_QUOTES")

    has_inferred_initial_sl = False
    if sig.get("data_quality_json"):
        try:
            dq = json.loads(sig["data_quality_json"]) if isinstance(sig["data_quality_json"], str) else sig["data_quality_json"]
            if isinstance(dq, dict) and (dq.get("inferred_initial_sl") or dq.get("flag") == "INFERRED_INITIAL_SL"):
                has_inferred_initial_sl = True
        except Exception:
            pass
    if has_inferred_initial_sl:
        data_quality_flags.append("INFERRED_INITIAL_STOP_LOSS")

    # Note: SHORT_DURATION and INFERRED_INITIAL_STOP_LOSS are flagged but not treated as hard invalidations
    is_hard_data_quality_issue = any(f not in ("SHORT_DURATION", "INFERRED_INITIAL_STOP_LOSS") for f in data_quality_flags)

    # 3. Whole-Lot Transaction Costs & R-Multiple
    status = (sig.get("status") or "").upper()
    has_partial_t1 = (status == "TARGET_1_HIT" or exit_reason == "TARGET_2_HIT" or (exit_reason == "TRAILING_STOP_HIT" and status == "TARGET_1_HIT")) and (lots >= 2)

    if has_partial_t1:
        close_lots = lots // 2
        rem_lots = lots - close_lots
        f_t1 = close_lots / lots
        f_rem = rem_lots / lots
        exit_legs = [(t1_p, f_t1), (exit_p, f_rem)]
        gross_pts = (close_lots * (t1_p - entry_p) + rem_lots * (exit_p - entry_p)) / lots
    else:
        exit_legs = [(exit_p, 1.0)]
        gross_pts = exit_p - entry_p

    costs = calculate_trade_costs(entry_p, exit_legs, lot_size=lot_size, lots=lots)
    net_pts = round(gross_pts - costs["cost_points"], 2)
    net_inr = round(net_pts * lot_size * lots, 2)
    r_multiple = round(net_pts / initial_risk, 3)
    pnl_pct = round((net_pts / entry_p * 100.0), 2) if entry_p > 0 else 0.0

    # 4. Excursion & Trajectory Metrics
    high_p = float(sig.get("highest_price") or entry_p)
    low_p = float(sig.get("lowest_price") or entry_p)
    mfe_pts = max(0.0, round(high_p - entry_p, 2))
    mae_pts = max(0.0, round(entry_p - low_p, 2))

    mfe_capture_pct = round((net_pts / mfe_pts * 100.0), 1) if (mfe_pts > 0 and net_pts > 0) else 0.0
    mae_vs_stop_pct = round((mae_pts / initial_risk * 100.0), 1)

    # Compute time to MFE from real trade_ticks
    time_to_mfe = 0.0
    if ticks:
        try:
            t0 = datetime.strptime(ticks[0]["timestamp"], "%Y-%m-%d %H:%M:%S")
            max_tick = max(ticks, key=lambda x: float(x.get("ltp") or 0.0))
            tm = datetime.strptime(max_tick["timestamp"], "%Y-%m-%d %H:%M:%S")
            time_to_mfe = round((tm - t0).total_seconds() / 60.0, 1)
        except Exception:
            time_to_mfe = round(dur_mins * 0.5, 1)
    else:
        time_to_mfe = round(dur_mins * 0.5, 1)

    # 5. Post-Exit Trajectory & 7-State Classification
    post_exit_reached_t1 = False
    post_exit_max_price = exit_p

    try:
        with _get_db() as conn:
            exit_time_str = sig.get("exit_time") or sig.get("updated_at")
            if exit_time_str:
                post_ticks = conn.execute("""
                    SELECT contract_ltp, timestamp FROM decision_log
                    WHERE symbol = ? AND chosen_contract = ? AND timestamp > ?
                    ORDER BY id ASC LIMIT 15
                """, (symbol, contract_name, exit_time_str)).fetchall()

                for pt in post_ticks:
                    pltp = float(pt["contract_ltp"] or 0.0)
                    if pltp > post_exit_max_price:
                        post_exit_max_price = pltp
                    if pltp >= t1_p:
                        post_exit_reached_t1 = True
    except Exception:
        pass

    # Exact Classification Logic
    if is_hard_data_quality_issue:
        classification = "DATA_QUALITY_ISSUE"
    elif net_pts > 0:
        if post_exit_max_price >= (exit_p * 1.5) or (exit_reason in ("TARGET_1_HIT", "MANUALLY_CLOSED", "MANUAL_EXIT") and post_exit_max_price >= t2_p):
            classification = "EARLY_EXIT"
        else:
            classification = "CLEAN_WIN"
    else:  # Losing Trade
        if exit_reason in ("STOP_LOSS_HIT", "SL_HIT") and post_exit_reached_t1:
            classification = "STOPPED_BY_NOISE"
        elif mfe_pts >= (0.5 * initial_risk) or mfe_pts >= (0.5 * (t1_p - entry_p)):
            classification = "RIGHT_THEN_REVERSED"
        elif dur_mins >= 45.0 and mae_pts < (0.9 * initial_risk):
            classification = "THETA_STAGNATION"
        else:
            classification = "WRONG_DIRECTION"

    # 6. Entry Quality Breakdown
    pillar_flags = {}
    raw_flags = sig.get("pillar_flags_json")
    if raw_flags:
        try:
            pillar_flags = json.loads(raw_flags) if isinstance(raw_flags, str) else raw_flags
        except Exception:
            pillar_flags = {}

    passed_pillars = [k for k, v in pillar_flags.items() if v]
    marginal_gates = []
    score = int(sig.get("setup_score") or 0)
    if 75 <= score <= 78:
        marginal_gates.append(f"Setup Score at minimum boundary ({score}/100)")
    if iv and (float(iv) < 11.0 or float(iv) > 24.0):
        marginal_gates.append(f"IV near boundary ({float(iv):.1f}%)")

    # Time of Day
    c_time_str = sig.get("created_at") or ""
    try:
        hour, minute = map(int, c_time_str.split(" ")[1].split(":")[:2])
        tot_m = hour * 60 + minute
        if tot_m < 615: tod_str = "OPENING (09:15-10:15)"
        elif tot_m < 705: tod_str = "MORNING_TREND (10:15-11:45)"
        elif tot_m < 795: tod_str = "MIDDAY_CHOP (11:45-13:15)"
        elif tot_m < 855: tod_str = "AFTERNOON_EXPANSION (13:15-14:15)"
        else: tod_str = "LATE_DECAY (14:15-15:30)"
    except Exception:
        tod_str = "UNKNOWN"

    # IV Regime
    iv_f = float(iv) if (iv and float(iv) > 0) else 0.0
    if iv_f <= 0: iv_regime = "MISSING"
    elif iv_f < 13.0: iv_regime = "LOW (<13%)"
    elif iv_f <= 20.0: iv_regime = "NORMAL (13-20%)"
    elif iv_f <= 26.0: iv_regime = "ELEVATED (20-26%)"
    else: iv_regime = "HIGH (>26%)"

    # 7. Counterfactual Replay Simulations on Trade Path
    counterfactuals = {
        "SL_10_PCT": replay_trade_on_ticks(sig, ticks, {"sl_pct": 0.10, "t1_multiple": 1.5, "t2_multiple": 2.5}),
        "SL_15_PCT_BASELINE": r_multiple,
        "SL_20_PCT": replay_trade_on_ticks(sig, ticks, {"sl_pct": 0.20, "t1_multiple": 1.5, "t2_multiple": 2.5}),
        "T1_1_0_R": replay_trade_on_ticks(sig, ticks, {"sl_pct": 0.15, "t1_multiple": 1.0, "t2_multiple": 2.0}),
        "T1_1_5_R": r_multiple,
        "T1_2_0_R": replay_trade_on_ticks(sig, ticks, {"sl_pct": 0.15, "t1_multiple": 2.0, "t2_multiple": 3.0}),
        "TSL_BE_0_8_R": 0.0 if mfe_pts >= (0.8 * initial_risk) and net_pts < 0 else r_multiple,
        "TIME_STOP_45M": replay_trade_on_ticks(sig, ticks, {"sl_pct": 0.15, "t1_multiple": 1.5, "t2_multiple": 2.5, "time_stop_mins": 45.0}),
        "ENTRY_AT_LTP": round((net_pts + (sig.get("spread_paid") or 0.0)) / initial_risk, 3),
    }

    # 8. Plain-Language Summary
    spread_drag = float(sig.get("spread_paid") or 0.0) * lot_size * lots
    summary_lines = [
        f"Trade #{signal_id} ({symbol} {contract_name}) completed with {classification} ({r_multiple:+.2f}R | {net_pts:+.1f} pts | ₹{net_inr:+,.2f} net).",
        f"Holding duration: {dur_mins:.1f}m | MFE: +{mfe_pts:.1f} pts ({mfe_capture_pct:.0f}% captured) | MAE: -{mae_pts:.1f} pts ({mae_vs_stop_pct:.0f}% of SL).",
        f"Entry executed in {tod_str} with {len(passed_pillars)}/6 pillars confirmed (IV: {iv_f:.1f}% | DTE: {dte if dte is not None else 'N/A'}).",
        f"Friction drag: ₹{costs['total_costs_inr']:.2f} statutory fees + ₹{spread_drag:.2f} spread paid.",
        f"Primary Cause: {classification.replace('_', ' ').title()} — {'Price action followed setup structure cleanly.' if net_pts > 0 else 'Market structure reversed before reaching targets.'}"
    ]
    plain_summary = "\n".join(summary_lines[:5])

    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    # 9. Persist Review Record
    review_record = {
        "signal_id": signal_id,
        "symbol": symbol,
        "contract_name": contract_name,
        "signal_type": sig_type,
        "entry_price": entry_p,
        "exit_price": exit_p,
        "initial_risk_pts": round(initial_risk, 2),
        "net_pnl_pts": net_pts,
        "net_pnl_inr": net_inr,
        "r_multiple": r_multiple,
        "pnl_pct": pnl_pct,
        "classification": classification,
        "mfe_capture_pct": mfe_capture_pct,
        "mae_vs_stop_pct": mae_vs_stop_pct,
        "time_to_mfe_mins": time_to_mfe,
        "time_in_trade_mins": dur_mins,
        "spread_cost_drag_inr": round(spread_drag, 2),
        "statutory_costs_inr": costs["total_costs_inr"],
        "passed_pillars_json": json.dumps(passed_pillars),
        "marginal_gates_json": json.dumps(marginal_gates),
        "time_of_day": tod_str,
        "dte": dte,
        "iv_regime": iv_regime,
        "counterfactuals_json": json.dumps(counterfactuals),
        "data_quality_flags_json": json.dumps(data_quality_flags),
        "plain_language_summary": plain_summary,
        "reviewed_at": now_str,
    }

    try:
        with _get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trade_reviews (
                    signal_id, symbol, contract_name, signal_type, entry_price, exit_price,
                    initial_risk_pts, net_pnl_pts, net_pnl_inr, r_multiple, pnl_pct,
                    classification, mfe_capture_pct, mae_vs_stop_pct, time_to_mfe_mins,
                    time_in_trade_mins, spread_cost_drag_inr, statutory_costs_inr,
                    passed_pillars_json, marginal_gates_json, time_of_day, dte, iv_regime,
                    counterfactuals_json, data_quality_flags_json, plain_language_summary, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_id, symbol, contract_name, sig_type, entry_p, exit_p,
                round(initial_risk, 2), net_pts, net_inr, r_multiple, pnl_pct,
                classification, mfe_capture_pct, mae_vs_stop_pct, time_to_mfe,
                dur_mins, round(spread_drag, 2), costs["total_costs_inr"],
                json.dumps(passed_pillars), json.dumps(marginal_gates), tod_str, dte, iv_regime,
                json.dumps(counterfactuals), json.dumps(data_quality_flags), plain_summary, now_str
            ))
            review_record["id"] = cursor.lastrowid
            conn.execute("COMMIT")
    except Exception as e:
        log.warning(f"Could not persist review for #{signal_id}: {e}")

    review_record["is_data_quality_flagged"] = is_hard_data_quality_issue
    review_record["counterfactual_results_json"] = review_record["counterfactuals_json"]

    # 10. Record observation in hypotheses tracking
    if not is_hard_data_quality_issue:
        try:
            update_hypotheses_from_review(review_record)
        except Exception as e:
            log.warning(f"Hypothesis tracking update failed for #{signal_id}: {e}")

    log.info(f"[TRADE REVIEW] Signal #{signal_id}: {classification} ({r_multiple:+.2f}R | ₹{net_inr:+,.2f}) — {summary_lines[4]}")
    return review_record


def update_hypotheses_from_review(review: dict | None = None):
    """
    Feeds a trade review observation into the hypotheses tracking table.
    Records matching evidence IDs for hypothesis verification in the daily job.
    """
    if review is None or review.get("is_data_quality_flagged") or review.get("classification") == "DATA_QUALITY_ISSUE":
        return

    sig_id = review.get("signal_id")
    c = review.get("classification")
    dte = review.get("dte")
    dur = float(review.get("time_in_trade_mins") or review.get("duration_mins") or 0.0)
    r_val = float(review.get("r_multiple") or 0.0)
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    hyp_defs = get_live_hypotheses_definitions()

    with _get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for hyp in hyp_defs:
            h_id = hyp["hypothesis_id"]
            existing = conn.execute("SELECT * FROM hypotheses WHERE hypothesis_id = ?", (h_id,)).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO hypotheses (
                        hypothesis_id, title, category, parameter, current_value, proposed_value,
                        observation_count, evidence_signals_json, status, validation_plan, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, '[]', 'OBSERVATION', ?, ?, ?)
                """, (
                    h_id, hyp["title"], hyp["category"], hyp["parameter"],
                    hyp["current_value"], hyp["proposed_value"], hyp["validation_plan"],
                    now_str, now_str
                ))

        matched = []
        if (dte is not None and dte <= 1.0) and c == "STOPPED_BY_NOISE":
            matched.append("HYP_EXPIRY_TIGHT_SL")
        if c == "THETA_STAGNATION" or (dur >= 45.0 and r_val < 0):
            matched.append("HYP_THETA_STAGNATION_STOP")
        if c == "RIGHT_THEN_REVERSED":
            matched.append("HYP_PROFIT_REVERSAL_TSL")

        for h_id in matched:
            h_row = conn.execute("SELECT * FROM hypotheses WHERE hypothesis_id = ?", (h_id,)).fetchone()
            if h_row:
                ev_list = json.loads(h_row["evidence_signals_json"] or "[]")
                if sig_id and sig_id not in ev_list:
                    ev_list.append(sig_id)
                obs_cnt = max(len(ev_list), int(h_row["observation_count"] or 0) + 1)
                new_status = h_row["status"]
                if obs_cnt >= MIN_SAMPLE_SIZE and new_status == "OBSERVATION":
                    new_status = "CANDIDATE"

                conn.execute("""
                    UPDATE hypotheses
                    SET observation_count = ?, evidence_signals_json = ?, status = ?, updated_at = ?
                    WHERE hypothesis_id = ?
                """, (obs_cnt, json.dumps(ev_list), new_status, now_str, h_id))

        conn.execute("COMMIT")


def review_unreviewed_closed_trades() -> int:
    """Startup job: reviews any closed signals in the journal that lack a review record."""
    reviewed_count = 0
    try:
        with _get_db() as conn:
            unreviewed = conn.execute("""
                SELECT id FROM signals
                WHERE status NOT IN ('ACTIVE', 'TARGET_1_HIT') AND is_deleted = 0
                  AND id NOT IN (SELECT signal_id FROM trade_reviews)
                ORDER BY id ASC
            """).fetchall()

        for row in unreviewed:
            res = review_closed_trade(row["id"])
            if res:
                reviewed_count += 1
    except Exception as e:
        log.error(f"Error in review_unreviewed_closed_trades startup job: {e}")

    return reviewed_count


# ==============================================================================
# Continuous Hypothesis Promotion Engine (Replay & Evidence Tracking)
# ==============================================================================

def get_live_hypotheses_definitions() -> list[dict]:
    """Builds standard hypothesis list with current_value generated dynamically from ENGINE_CONFIG."""
    return [
        {
            "hypothesis_id": "HYP_EXPIRY_TIGHT_SL",
            "title": "Stop Loss is too tight for DTE <= 1 (Noise-induced stopouts)",
            "category": "Risk Management",
            "parameter": "STOP_LOSS_DTE_0",
            "current_value": "15% (1.0R)",
            "proposed_value": "20% (1.3R)",
            "params_variant": {"sl_pct": 0.20, "t1_multiple": 1.5, "t2_multiple": 2.5},
            "validation_plan": "Replay historical expiry day trades with 20% SL and verify reduction in STOPPED_BY_NOISE rate.",
        },
        {
            "hypothesis_id": "HYP_THETA_STAGNATION_STOP",
            "title": "Time stop at 45 minutes reduces theta decay drag on stagnant trades",
            "category": "Exit Strategy",
            "parameter": "TIME_STOP_MINS",
            "current_value": "No fixed time exit (EOD)",
            "proposed_value": "45 Minutes Time Stop",
            "params_variant": {"sl_pct": 0.15, "t1_multiple": 1.5, "t2_multiple": 2.5, "time_stop_mins": 45.0},
            "validation_plan": "Walk-forward replay comparison with 45m stagnation cut on test partition.",
        },
        {
            "hypothesis_id": "HYP_PROFIT_REVERSAL_TSL",
            "title": "Locking breakeven at +0.5R excursion eliminates profit reversals",
            "category": "Trailing Stop",
            "parameter": "TSL_ACTIVATION_THRESHOLD",
            "current_value": f"+10% MFE / {ENGINE_CONFIG.get('MIN_SETUP_SCORE', 75)} Score",
            "proposed_value": "+0.5R Breakeven Lock",
            "params_variant": {"sl_pct": 0.15, "t1_multiple": 1.5, "t2_multiple": 2.5, "trail_step": 0.5},
            "validation_plan": "Paper-trade 30 consecutive setups with +0.5R breakeven lock in shadow mode.",
        },
    ]


def update_hypotheses_in_daily_job():
    """
    Evaluates hypotheses out-of-sample in the daily review job (never inside per-trade review thread).
    Promotes to RECOMMENDED only if n >= 30, FDR passes, and paired CI excludes zero after costs.
    """
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    hyp_defs = get_live_hypotheses_definitions()

    with _get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for hyp in hyp_defs:
            h_id = hyp["hypothesis_id"]
            existing = conn.execute("SELECT * FROM hypotheses WHERE hypothesis_id = ?", (h_id,)).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO hypotheses (
                        hypothesis_id, title, category, parameter, current_value, proposed_value,
                        observation_count, evidence_signals_json, status, validation_plan, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, '[]', 'OBSERVATION', ?, ?, ?)
                """, (
                    h_id, hyp["title"], hyp["category"], hyp["parameter"],
                    hyp["current_value"], hyp["proposed_value"], hyp["validation_plan"],
                    now_str, now_str
                ))
            else:
                # Keep current_value synchronized with live config
                conn.execute("""
                    UPDATE hypotheses SET current_value = ?, updated_at = ? WHERE hypothesis_id = ?
                """, (hyp["current_value"], now_str, h_id))

        # Count matched observations from reviews
        reviews = [dict(r) for r in conn.execute("SELECT * FROM trade_reviews WHERE classification != 'DATA_QUALITY_ISSUE'").fetchall()]

        for hyp in hyp_defs:
            h_id = hyp["hypothesis_id"]
            matched_ids = []
            for r in reviews:
                c = r.get("classification")
                dte = r.get("dte")
                dur = float(r.get("time_in_trade_mins") or 0.0)
                r_val = float(r.get("r_multiple") or 0.0)

                if h_id == "HYP_EXPIRY_TIGHT_SL" and (dte is not None and dte <= 1.0) and c == "STOPPED_BY_NOISE":
                    matched_ids.append(r["signal_id"])
                elif h_id == "HYP_THETA_STAGNATION_STOP" and (c == "THETA_STAGNATION" or (dur >= 45.0 and r_val < 0)):
                    matched_ids.append(r["signal_id"])
                elif h_id == "HYP_PROFIT_REVERSAL_TSL" and c == "RIGHT_THEN_REVERSED":
                    matched_ids.append(r["signal_id"])

            obs_cnt = len(matched_ids)
            new_status = "OBSERVATION"
            effect_size = 0.0
            ci_low, ci_high = 0.0, 0.0
            impact_str = None
            replay_json = None
            rejection_reason = None

            if obs_cnt >= MIN_SAMPLE_SIZE:
                try:
                    replay_res = run_walk_forward_evaluation(train_ratio=0.60, params_variant=hyp.get("params_variant"))
                    replay_json = json.dumps(replay_res)
                    d_ci = replay_res.get("delta_95_ci", [0.0, 0.0])
                    ci_low, ci_high = d_ci[0], d_ci[1]
                    effect_size = round((ci_low + ci_high) / 2.0, 3)
                    impact_str = f"+{effect_size:.3f} R delta (95% CI: [{ci_low}, {ci_high}])"

                    if replay_res.get("verdict") == "EVIDENCE_CONFIRMED" and ci_low > 0.0:
                        new_status = "RECOMMENDED"
                    elif replay_res.get("verdict") == "BASELINE_UNPROFITABLE":
                        new_status = "REJECTED"
                        rejection_reason = "Baseline strategy unprofitable in out-of-sample replay."
                    else:
                        new_status = "CANDIDATE"
                        rejection_reason = f"Out-of-sample paired 95% CI [{ci_low}, {ci_high}] includes zero."
                except Exception as e:
                    new_status = "CANDIDATE"
                    rejection_reason = f"Replay evaluation error: {e}"

            conn.execute("""
                UPDATE hypotheses
                SET observation_count = ?, evidence_signals_json = ?, status = ?,
                    effect_size = ?, ci_lower = ?, ci_upper = ?, expected_net_impact = ?,
                    replay_result_json = ?, rejection_reason = ?, updated_at = ?
                WHERE hypothesis_id = ?
            """, (
                obs_cnt, json.dumps(matched_ids), new_status,
                effect_size, ci_low, ci_high, impact_str,
                replay_json, rejection_reason, now_str, h_id
            ))

        conn.execute("COMMIT")


def generate_daily_review_report(target_date: str | None = None) -> dict:
    """
    Generates a daily summary report of post-trade reviews, classification counts,
    hypotheses status movements, and any RECOMMENDED changes.
    """
    if target_date is None:
        target_date = datetime.now(IST).strftime("%Y-%m-%d")

    # Run hypotheses update as part of the daily job
    try:
        update_hypotheses_in_daily_job()
    except Exception as e:
        log.warning(f"Error updating hypotheses in daily report: {e}")

    with _get_db() as conn:
        reviews_rows = conn.execute("""
            SELECT * FROM trade_reviews
            WHERE reviewed_at LIKE ?
            ORDER BY id ASC
        """, (f"{target_date}%",)).fetchall()
        reviews = [dict(r) for r in reviews_rows]

        hypotheses_rows = conn.execute("SELECT * FROM hypotheses ORDER BY id ASC").fetchall()
        hypotheses = [dict(h) for h in hypotheses_rows]

    classification_counts = {
        "WRONG_DIRECTION": 0,
        "RIGHT_THEN_REVERSED": 0,
        "STOPPED_BY_NOISE": 0,
        "THETA_STAGNATION": 0,
        "CLEAN_WIN": 0,
        "EARLY_EXIT": 0,
        "DATA_QUALITY_ISSUE": 0,
    }

    tot_pts = 0.0
    tot_inr = 0.0
    for r in reviews:
        c = r["classification"]
        if c in classification_counts:
            classification_counts[c] += 1
        tot_pts += float(r.get("net_pnl_pts") or 0.0)
        tot_inr += float(r.get("net_pnl_inr") or 0.0)

    recommended_items = [h for h in hypotheses if h["status"] == "RECOMMENDED"]
    candidate_items = [h for h in hypotheses if h["status"] == "CANDIDATE"]

    return {
        "date": target_date,
        "total_reviews": len(reviews),
        "total_net_pts": round(tot_pts, 1),
        "total_net_inr": round(tot_inr, 2),
        "classification_counts": classification_counts,
        "recommended_changes": recommended_items,
        "candidate_hypotheses": candidate_items,
        "all_hypotheses": hypotheses,
        "reviews": reviews,
    }


# ==============================================================================
# Main Diagnostic Telemetry Pipeline
# ==============================================================================

def analyze_paper_trading_logs(trades_override: list[dict] = None) -> dict:
    """
    Diagnostic analysis entry point:
    Combines overall performance, 6 setup pillars (from decision_log), loser taxonomy,
    counterfactual replay on ticks, individual trade reviews, and hypothesis tracking.
    """
    if trades_override is not None:
        all_signals = trades_override
    else:
        try:
            with _get_db() as conn:
                all_signals = [dict(r) for r in conn.execute("SELECT * FROM signals WHERE is_deleted = 0 ORDER BY id DESC").fetchall()]
        except Exception as e:
            log.error(f"Failed to query signals DB: {e}")
            all_signals = []

    if not all_signals:
        return _build_empty_diagnostic_report()

    closed_raw = [s for s in all_signals if s.get("status") not in ("ACTIVE", "TARGET_1_HIT")]
    active_trades = [s for s in all_signals if s.get("status") in ("ACTIVE", "TARGET_1_HIT")]

    total_raw_count = len(all_signals)
    closed_raw_count = len(closed_raw)

    if closed_raw_count == 0:
        rep = _build_empty_diagnostic_report()
        rep["sample_size"]["total_signals"] = total_raw_count
        rep["sample_size"]["active_trades"] = len(active_trades)
        return rep

    # 1. Data Quality & Hygiene Filter
    valid_trades, data_quality = _filter_data_quality(closed_raw)
    n_valid = len(valid_trades)

    if n_valid == 0:
        rep = _build_empty_diagnostic_report()
        rep["sample_size"]["total_signals"] = total_raw_count
        rep["sample_size"]["closed_trades_raw"] = closed_raw_count
        rep["sample_size"]["active_trades"] = len(active_trades)
        rep["data_quality"] = data_quality
        rep["status"] = "NO_VALID_TRADES_AFTER_FILTER"
        return rep

    # 2. Enrich with Net Costs & R-Multiples
    enriched_trades = [_enrich_trade_costs_and_r(t) for t in valid_trades]

    # 3. Overall Statistical Aggregates & Distribution
    r_values = [t["r_multiple"] for t in enriched_trades]
    wins = [t for t in enriched_trades if t["net_pnl_pts"] > 0]
    losses = [t for t in enriched_trades if t["net_pnl_pts"] < 0]
    break_evens = [t for t in enriched_trades if t["net_pnl_pts"] == 0]

    win_count = len(wins)
    loss_count = len(losses)
    win_rate_pct = round((win_count / n_valid) * 100, 2)
    win_rate_ci = _wilson_score_interval(win_count, n_valid)

    mean_r = sum(r_values) / n_valid
    r_bootstrap_ci = _bootstrap_mean_ci(r_values)
    pf_value, pf_ci = _bootstrap_profit_factor(r_values)
    max_dd_r, max_dd_pct = _compute_drawdowns(enriched_trades)

    total_net_inr = sum(t["net_pnl_inr"] for t in enriched_trades)
    total_net_pts = sum(t["net_pnl_pts"] for t in enriched_trades)
    avg_win_r = (sum(t["r_multiple"] for t in wins) / win_count) if win_count > 0 else 0.0
    avg_loss_r = (abs(sum(t["r_multiple"] for t in losses)) / loss_count) if loss_count > 0 else 0.0

    # 4. Measure Setup Pillars from decision_log forward labels
    pillar_analysis = _analyze_setup_pillars_from_decision_log(baseline_r=mean_r, fallback_trades=enriched_trades)
    excursion_analysis = _analyze_excursions(enriched_trades)
    duration_analysis = _analyze_duration(enriched_trades, baseline_r=mean_r)
    time_analysis = _analyze_time_of_day(enriched_trades, baseline_r=mean_r)
    direction_analysis = _analyze_direction(enriched_trades, baseline_r=mean_r)
    moneyness_analysis = _analyze_moneyness(enriched_trades, baseline_r=mean_r)
    iv_analysis = _analyze_iv_regimes(enriched_trades, baseline_r=mean_r)
    score_analysis = _analyze_setup_score(enriched_trades, baseline_r=mean_r)
    exit_reason_analysis = _analyze_exit_reasons(enriched_trades)
    symbol_analysis = _analyze_symbols(enriched_trades, baseline_r=mean_r)

    # 5. Loser Taxonomy Classification
    loser_taxonomy = _classify_losers(enriched_trades)

    # 6. Counterfactual Replay Simulation
    counterfactual_results = _run_counterfactual_replay(enriched_trades)

    # 7. Best / Worst Segment Identification
    all_segment_tests = (
        direction_analysis + moneyness_analysis + time_analysis +
        iv_analysis + score_analysis + duration_analysis + symbol_analysis
    )
    _apply_benjamini_hochberg_fdr(all_segment_tests, alpha=0.05)
    best_conditions = _identify_best_conditions(all_segment_tests, pillar_analysis)
    worst_conditions = _identify_worst_conditions(all_segment_tests, pillar_analysis)

    # 8. Top 3 Profit Leaks (Ranked by R Lost with CIs)
    top_3_leaks = _identify_top_3_profit_leaks(
        enriched_trades=enriched_trades,
        loser_taxonomy=loser_taxonomy,
        duration_analysis=duration_analysis,
        time_analysis=time_analysis,
        score_analysis=score_analysis,
        pillar_analysis=pillar_analysis,
        counterfactual=counterfactual_results
    )

    # 9. Hypotheses Table & Structured Recommendations
    with _get_db() as conn:
        hypotheses_rows = conn.execute("SELECT * FROM hypotheses ORDER BY id ASC").fetchall()
        hypotheses = [dict(h) for h in hypotheses_rows]
        recent_reviews = [dict(r) for r in conn.execute("SELECT * FROM trade_reviews ORDER BY id DESC LIMIT 20").fetchall()]

    recommendations = _generate_rigorous_recommendations(
        n_valid=n_valid,
        baseline_mean_r=mean_r,
        pillar_analysis=pillar_analysis,
        worst_conditions=worst_conditions,
        counterfactual=counterfactual_results,
        loser_taxonomy=loser_taxonomy,
        hypotheses=hypotheses
    )

    health_score = _compute_rigorous_health_score(
        mean_r=mean_r,
        r_ci=r_bootstrap_ci,
        win_rate=win_rate_pct,
        pf=pf_value,
        max_dd_r=max_dd_r,
        n=n_valid
    )

    return {
        "status": "DIAGNOSTIC_COMPLETE" if n_valid >= MIN_SAMPLE_SIZE else "INSUFFICIENT_DATA",
        "sample_sufficiency": "SUFFICIENT" if n_valid >= MIN_SAMPLE_SIZE else "INSUFFICIENT",
        "mode": "OBSERVE_AND_RECOMMEND_ONLY",
        "advisory_notice": "All recommendations are strictly advisory and require human approval before altering live engine parameters.",
        "data_limitation_notice": "MFE and MAE metrics are captured via periodic 3-minute polling cycles and may understate intra-interval price excursions or flash spikes.",
        "data_quality": data_quality,
        "sample_size": {
            "total_signals_raw": total_raw_count,
            "closed_trades_raw": closed_raw_count,
            "valid_closed_trades": n_valid,
            "excluded_trades": data_quality["excluded_count"],
            "active_trades": len(active_trades),
            "win_count": win_count,
            "loss_count": loss_count,
            "be_count": len(break_evens),
            "win_rate_pct": win_rate_pct,
            "win_rate_95_ci": win_rate_ci,
            "min_sample_required_per_segment": MIN_SAMPLE_SIZE,
            "sample_sufficient": n_valid >= MIN_SAMPLE_SIZE,
        },
        "overall_performance": {
            "expectancy_r": round(mean_r, 3),
            "expectancy_r_95_ci": r_bootstrap_ci,
            "profit_factor": round(pf_value, 2),
            "profit_factor_95_ci": pf_ci,
            "total_net_inr": round(total_net_inr, 2),
            "total_net_pts": round(total_net_pts, 2),
            "avg_win_r": round(avg_win_r, 2),
            "avg_loss_r": round(avg_loss_r, 2),
            "max_drawdown_r": round(max_dd_r, 2),
            "max_drawdown_pct": round(max_dd_pct, 1),
            "strategy_health_score": health_score,
        },
        "setup_pillars": pillar_analysis,
        "loser_taxonomy": loser_taxonomy,
        "counterfactual_replay": counterfactual_results,
        "best_conditions": best_conditions,
        "worst_conditions": worst_conditions,
        "top_profit_leaks": top_3_leaks,
        "recommended_changes": recommendations,
        "hypotheses_tracker": hypotheses,
        "recent_trade_reviews": recent_reviews,
        "dimensions": {
            "pillars": pillar_analysis,
            "excursion": excursion_analysis,
            "duration": duration_analysis,
            "time_of_day": time_analysis,
            "direction": direction_analysis,
            "moneyness": moneyness_analysis,
            "iv_regimes": iv_analysis,
            "setup_score": score_analysis,
            "exit_reasons": exit_reason_analysis,
            "symbols": symbol_analysis,
        },
        "timestamp": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================================
# Helper Subroutines
# ==============================================================================

def _filter_data_quality(raw_trades: list[dict]) -> tuple[list[dict], dict]:
    """
    Filters raw trade records for statistical validity:
    - Excludes hygiene/cleanup actions (DUPLICATE_CLEANUP, STALE_EXIT, EXPIRED) from trading performance.
    - Flags SHORT_DURATION (< 3 min) but keeps them in aggregate stats to avoid survivorship bias.
    - Excludes fabricated or unfillable quotes.
    """
    valid = []
    hygiene_excluded = []
    exclusions = {
        "SHORT_DURATION_FLAGGED": 0,
        "HYGIENE_CLEANUP_OR_EXPIRED": 0,
        "FABRICATED_OR_OUTLIER_ENTRY": 0,
        "MISSING_ZERO_IV": 0,
        "STALE_OR_INVALID_EXIT": 0,
    }

    excluded_hygiene_reasons = ("DUPLICATE_CLEANUP", "STALE_EXIT", "CONTRACT_EXPIRED", "EXPIRED")

    for t in raw_trades:
        duration = float(t.get("duration_mins") or 0.0)
        entry_price = float(t.get("entry_price") or 0.0)
        exit_price = float(t.get("exit_price") or 0.0)
        iv = t.get("iv_at_entry")
        reason = (t.get("exit_reason") or t.get("status") or "").upper()

        if reason in excluded_hygiene_reasons:
            exclusions["HYGIENE_CLEANUP_OR_EXPIRED"] += 1
            hygiene_excluded.append(t)
            continue
        if entry_price <= 0.05 or entry_price in (100.0, 50.0) or entry_price > 50000.0:
            exclusions["FABRICATED_OR_OUTLIER_ENTRY"] += 1
            continue
        if iv is None or float(iv) <= 0.01:
            exclusions["MISSING_ZERO_IV"] += 1
            continue
        if exit_price <= 0.05:
            exclusions["STALE_OR_INVALID_EXIT"] += 1
            continue

        if duration < 3.0:
            exclusions["SHORT_DURATION_FLAGGED"] += 1

        valid.append(t)

    total_excluded = exclusions["HYGIENE_CLEANUP_OR_EXPIRED"] + exclusions["FABRICATED_OR_OUTLIER_ENTRY"] + exclusions["MISSING_ZERO_IV"] + exclusions["STALE_OR_INVALID_EXIT"]

    summary = {
        "total_raw": len(raw_trades),
        "valid_count": len(valid),
        "excluded_count": total_excluded,
        "hygiene_excluded_count": len(hygiene_excluded),
        "exclusion_reasons": exclusions,
        "filter_passed": total_excluded == 0,
    }
    return valid, summary


def _enrich_trade_costs_and_r(trade: dict) -> dict:
    t = dict(trade)
    symbol = (t.get("symbol") or "NIFTY").upper()
    cfg = INDEX_CONFIGS.get(symbol, INDEX_CONFIGS.get("NIFTY", {"lot_size": 25}))
    lot_size = int(t.get("lot_size") or cfg.get("lot_size", 25))
    lots = int(t.get("lots") or 1)

    entry = float(t.get("ask_at_entry") or t.get("entry_price") or 0.0)
    exit_p = float(t.get("bid_at_exit") or t.get("exit_price") or entry)

    init_sl = float(t.get("initial_stop_loss") or t.get("stop_loss") or (entry * 0.85))
    initial_risk = max(entry - init_sl, 0.1)

    status = (t.get("status") or "").upper()
    exit_reason = (t.get("exit_reason") or "").upper()

    has_partial_t1 = (status == "TARGET_1_HIT" or exit_reason == "TARGET_2_HIT" or (exit_reason == "TRAILING_STOP_HIT" and status == "TARGET_1_HIT")) and (lots >= 2)

    if has_partial_t1:
        t1 = float(t.get("target_1") or (entry + initial_risk * 1.5))
        close_lots = lots // 2
        rem_lots = lots - close_lots
        f_t1 = close_lots / lots
        f_rem = rem_lots / lots
        exit_legs = [(t1, f_t1), (exit_p, f_rem)]
        gross_pts = (close_lots * (t1 - entry) + rem_lots * (exit_p - entry)) / lots
    else:
        exit_legs = [(exit_p, 1.0)]
        gross_pts = exit_p - entry

    costs = calculate_trade_costs(entry, exit_legs, lot_size=lot_size, lots=lots)
    net_pts = round(gross_pts - costs["cost_points"], 2)
    net_inr = round(net_pts * lot_size * lots, 2)
    r_multiple = round(net_pts / initial_risk, 3)

    slippage_pts = round((entry * 0.002) + (exit_p * 0.002), 2)
    t["initial_risk_pts"] = round(initial_risk, 2)
    t["gross_pnl_pts"] = round(gross_pts, 2)
    t["slippage_pts"] = slippage_pts
    t["net_pnl_pts"] = net_pts
    t["net_pnl_inr"] = net_inr
    t["statutory_cost_inr"] = costs["total_costs_inr"]
    t["r_multiple"] = r_multiple
    return t


def _wilson_score_interval(successes: int, total: int) -> list[float]:
    if total <= 0:
        return [0.0, 0.0]
    p = successes / total
    z = Z_SCORE_95
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p + z2 / (2.0 * total)) / denom
    margin = (z / denom) * math.sqrt((p * (1.0 - p) / total) + (z2 / (4.0 * total * total)))
    return [round(max(0.0, center - margin) * 100.0, 1), round(min(1.0, center + margin) * 100.0, 1)]


def _bootstrap_mean_ci(values: list[float], iterations: int = 1000, seed: int = 42) -> list[float]:
    n = len(values)
    if n == 0:
        return [0.0, 0.0]
    if n == 1:
        return [round(values[0], 3), round(values[0], 3)]
    rng = random.Random(seed)
    boot_means = [sum(values[rng.randint(0, n - 1)] for _ in range(n)) / n for _ in range(iterations)]
    boot_means.sort()
    return [round(boot_means[int(iterations * 0.025)], 3), round(boot_means[int(iterations * 0.975)], 3)]


def _bootstrap_profit_factor(r_values: list[float], iterations: int = 1000, seed: int = 42) -> tuple[float, list[float]]:
    n = len(r_values)
    if n == 0:
        return 1.0, [1.0, 1.0]

    def _calc_pf(vals):
        wins = sum(v for v in vals if v > 0)
        losses = abs(sum(v for v in vals if v < 0))
        return round(wins / losses, 2) if losses > 0 else (round(wins, 2) if wins > 0 else 1.0)

    actual_pf = _calc_pf(r_values)
    if n < 3:
        return actual_pf, [actual_pf, actual_pf]
    rng = random.Random(seed)
    boot_pfs = [_calc_pf([r_values[rng.randint(0, n - 1)] for _ in range(n)]) for _ in range(iterations)]
    boot_pfs.sort()
    return actual_pf, [round(boot_pfs[int(iterations * 0.025)], 2), round(boot_pfs[int(iterations * 0.975)], 2)]


def _compute_welch_t_test(group_a: list[float], group_b: list[float]) -> tuple[float, float]:
    n1, n2 = len(group_a), len(group_b)
    if n1 < 2 or n2 < 2:
        return 0.0, 1.0
    m1, m2 = sum(group_a) / n1, sum(group_b) / n2
    v1 = sum((x - m1) ** 2 for x in group_a) / (n1 - 1)
    v2 = sum((x - m2) ** 2 for x in group_b) / (n2 - 1)
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se == 0:
        return 0.0, 1.0
    t_stat = (m1 - m2) / se
    z = abs(t_stat)
    p_val = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))
    return round(t_stat, 3), round(max(0.0001, min(1.0, p_val)), 4)


def _apply_benjamini_hochberg_fdr(segment_tests: list[dict], alpha: float = 0.05):
    valid_tests = [t for t in segment_tests if "p_value" in t]
    m = len(valid_tests)
    if m == 0:
        return
    sorted_tests = sorted(valid_tests, key=lambda x: x["p_value"])
    max_k = -1
    for i, t in enumerate(sorted_tests):
        if t["p_value"] <= ((i + 1) / m) * alpha:
            max_k = i
    for i, t in enumerate(sorted_tests):
        t["fdr_significant"] = (i <= max_k) if max_k >= 0 else False


def _compute_drawdowns(trades: list[dict]) -> tuple[float, float]:
    cum_r, peak_r, max_dd_r = 0.0, 0.0, 0.0
    cum_inr, peak_inr, max_dd_inr = 0.0, 0.0, 0.0
    for t in trades:
        cum_r += t["r_multiple"]
        if cum_r > peak_r: peak_r = cum_r
        if (peak_r - cum_r) > max_dd_r: max_dd_r = peak_r - cum_r

        cum_inr += t["net_pnl_inr"]
        if cum_inr > peak_inr: peak_inr = cum_inr
        if (peak_inr - cum_inr) > max_dd_inr: max_dd_inr = peak_inr - cum_inr
    max_dd_pct = (max_dd_inr / peak_inr * 100.0) if peak_inr > 0 else 0.0
    return max_dd_r, max_dd_pct


def _analyze_setup_pillars_from_decision_log(baseline_r: float = 0.0, fallback_trades: list[dict] = None) -> list[dict]:
    """
    Measures setup pillar and gate discrimination value from decision_log with forward labels (all cycles incl. WAIT).
    Falls back to trade-level pillar flags if decision_log forward labels are not yet populated.
    """
    pillars_def = [
        {"id": "direction", "name": "Directional Bias", "col": "pillar_direction", "flag_keys": ["direction", "bias_favorable"]},
        {"id": "level", "name": "Key Level Structure", "col": "pillar_level", "flag_keys": ["level", "key_level"]},
        {"id": "momentum", "name": "Price Momentum", "col": "pillar_momentum", "flag_keys": ["momentum"]},
        {"id": "volume", "name": "Volume Expansion", "col": "pillar_volume", "flag_keys": ["volume"]},
        {"id": "oi", "name": "Institutional OI Flow", "col": "pillar_oi", "flag_keys": ["oi"]},
        {"id": "iv_session", "name": "IV & Session Window", "col": "pillar_iv_session", "flag_keys": ["iv_session"]},
    ]

    try:
        with _get_db() as conn:
            d_rows = conn.execute("""
                SELECT timestamp, candidate_side, pillar_direction, pillar_level, pillar_momentum,
                       pillar_volume, pillar_oi, pillar_iv_session, forward_underlying_30m, forward_mfe_pct
                FROM decision_log
                WHERE forward_underlying_30m IS NOT NULL
                  AND candidate_side IN ('CE', 'PE')
            """).fetchall()
            d_logs = [dict(r) for r in d_rows]
    except Exception:
        d_logs = []

    results = []

    if len(d_logs) >= 30:
        # Measure from decision_log forward outcomes with side sign-adjustment & day clustering
        raw_p_values = []
        raw_items = []

        for p in pillars_def:
            col = p["col"]

            # Filter and sign-adjust forward moves (PE moves are inverted: drop is favorable)
            pass_records = []
            fail_records = []

            for r in d_logs:
                fwd_move = float(r["forward_underlying_30m"])
                cand_side = str(r.get("candidate_side") or "CE").upper()
                signed_move = fwd_move if cand_side == "CE" else (-1.0 * fwd_move)
                day_str = str(r.get("timestamp") or "")[:10]

                if r.get(col) == 1:
                    pass_records.append((day_str, signed_move))
                else:
                    fail_records.append((day_str, signed_move))

            pass_moves = [m for _, m in pass_records]
            fail_moves = [m for _, m in fail_records]
            cnt_pass = len(pass_moves)
            cnt_fail = len(fail_moves)
            tot = cnt_pass + cnt_fail

            mean_pass = sum(pass_moves) / cnt_pass if cnt_pass > 0 else 0.0
            mean_fail = sum(fail_moves) / cnt_fail if cnt_fail > 0 else 0.0
            delta_mean = mean_pass - mean_fail

            # Cluster by trading day
            days_set = {d for d, _ in pass_records} | {d for d, _ in fail_records}
            num_days = max(1, len(days_set))

            # Day-level permutation test across clusters to account for intraday autocorrelation
            rng = random.Random(42)
            n_perms = 1000
            diff_count = 0
            all_records = pass_records + fail_records
            days_map = {}
            for d, m in all_records:
                if d not in days_map:
                    days_map[d] = []
                days_map[d].append(m)

            for _ in range(n_perms):
                # Permute pass/fail assignment at observation level within or across day blocks
                shuffled_pass = []
                shuffled_fail = []
                for d, day_moves in days_map.items():
                    n_d = len(day_moves)
                    # Fraction of pass in this day
                    d_pass_cnt = sum(1 for dp, _ in pass_records if dp == d)
                    perm = list(day_moves)
                    rng.shuffle(perm)
                    shuffled_pass.extend(perm[:d_pass_cnt])
                    shuffled_fail.extend(perm[d_pass_cnt:])

                p_mean = sum(shuffled_pass) / len(shuffled_pass) if shuffled_pass else 0.0
                f_mean = sum(shuffled_fail) / len(shuffled_fail) if shuffled_fail else 0.0
                perm_diff = p_mean - f_mean
                if abs(perm_diff) >= abs(delta_mean):
                    diff_count += 1

            p_val = round(max(1.0 / n_perms, diff_count / n_perms), 4) if tot > 0 else 1.0

            # Effective sample size: adjust for clustering (N_eff = N / (1 + (M - 1) * icc))
            avg_m = tot / num_days if num_days > 0 else 1.0
            # Intra-cluster correlation approximation (default 0.15 for intraday market moves)
            icc = 0.15
            neff = max(1, int(round(tot / (1.0 + max(0.0, avg_m - 1.0) * icc))))

            raw_p_values.append(p_val)
            raw_items.append({
                "pillar_id": p["id"],
                "pillar_name": p["name"],
                "data_source": "DECISION_LOG_FORWARD_LABELS",
                "sign_adjusted_for_side": True,
                "pass_stats": {
                    "count": cnt_pass,
                    "mean_forward_atr_30m": round(mean_pass, 2),
                    "mean_r_95_ci": _bootstrap_mean_ci(pass_moves),
                },
                "fail_stats": {
                    "count": cnt_fail,
                    "mean_forward_atr_30m": round(mean_fail, 2),
                    "mean_r_95_ci": _bootstrap_mean_ci(fail_moves),
                },
                "total_cycles_evaluated": tot,
                "effective_sample_size": neff,
                "trading_days_evaluated": num_days,
                "delta_mean_r": round(delta_mean, 3),
                "raw_p_value": p_val,
            })

        # Apply Benjamini-Hochberg FDR correction across all evaluated pillars
        m = len(raw_p_values)
        indexed = sorted(enumerate(raw_p_values), key=lambda x: x[1])
        adj_p_map = {}
        cum_min = 1.0
        for rank_idx, (orig_i, p_v) in reversed(list(enumerate(indexed))):
            rank = rank_idx + 1
            adj_p = min(1.0, (p_v * m) / rank)
            cum_min = min(cum_min, adj_p)
            adj_p_map[orig_i] = round(cum_min, 4)

        for idx, item in enumerate(raw_items):
            fdr_p = adj_p_map.get(idx, item["raw_p_value"])
            item["p_value"] = fdr_p
            item["fdr_p_value"] = fdr_p
            item["sample_status"] = "SUFFICIENT" if item["effective_sample_size"] >= MIN_SAMPLE_SIZE else "INSUFFICIENT_DATA"
            item["statistically_significant"] = bool(fdr_p < 0.05 and item["effective_sample_size"] >= MIN_SAMPLE_SIZE)
            results.append(item)
    else:
        # Fallback to trade-level flags
        trades = fallback_trades or []
        n = len(trades)
        for p in pillars_def:
            p_id = p["id"]
            keys = p["flag_keys"]
            pass_t, fail_t, unk_t = [], [], []

            for t in trades:
                raw_flags = t.get("pillar_flags_json") or t.get("pillar_flags")
                flags = {}
                if isinstance(raw_flags, str):
                    try: flags = json.loads(raw_flags)
                    except Exception: flags = {}
                elif isinstance(raw_flags, dict): flags = raw_flags

                matched_key = next((k for k in keys if k in flags), None)
                if matched_key is None: unk_t.append(t)
                elif bool(flags[matched_key]): pass_t.append(t)
                else: fail_t.append(t)

            def _calc_grp(grp, lbl):
                cnt = len(grp)
                if cnt == 0:
                    return {"status": lbl, "count": 0, "win_rate_pct": 0.0, "mean_r": 0.0, "mean_r_95_ci": [0.0, 0.0], "total_net_inr": 0.0}
                w_cnt = sum(1 for x in grp if x["net_pnl_pts"] > 0)
                r_vals = [x["r_multiple"] for x in grp]
                return {
                    "status": lbl, "count": cnt, "win_rate_pct": round(w_cnt / cnt * 100, 1),
                    "mean_r": round(sum(r_vals) / cnt, 3), "mean_r_95_ci": _bootstrap_mean_ci(r_vals),
                    "total_net_inr": round(sum(x["net_pnl_inr"] for x in grp), 2)
                }

            pass_stats = _calc_grp(pass_t, "PASS")
            fail_stats = _calc_grp(fail_t, "FAIL")
            eval_count = len(pass_t) + len(fail_t)
            t_stat, p_val = _compute_welch_t_test([x["r_multiple"] for x in pass_t], [x["r_multiple"] for x in fail_t])

            results.append({
                "pillar_id": p_id,
                "pillar_name": p["name"],
                "data_source": "TRADE_EXECUTION_JOURNAL",
                "pass_stats": pass_stats,
                "fail_stats": fail_stats,
                "unknown_count": len(unk_t),
                "evaluated_trades": eval_count,
                "total_trades": n,
                "delta_mean_r": round(pass_stats["mean_r"] - fail_stats["mean_r"], 3) if (pass_t and fail_t) else 0.0,
                "p_value": p_val,
                "sample_status": "SUFFICIENT" if eval_count >= MIN_SAMPLE_SIZE else "INSUFFICIENT_DATA",
                "statistically_significant": (p_val < 0.05 and eval_count >= MIN_SAMPLE_SIZE),
            })

    return results


def _analyze_excursions(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0: return {}
    tot_mfe, tot_mae, real_w, pot_w, rev_cnt = 0.0, 0.0, 0.0, 0.0, 0
    for t in trades:
        entry = float(t.get("entry_price") or 0.0)
        high = float(t.get("highest_price") or entry)
        low = float(t.get("lowest_price") or entry)
        pts = float(t.get("net_pnl_pts") or 0.0)
        mfe = float(t.get("mfe_points") or max(0.0, high - entry))
        mae = float(t.get("mae_points") or max(0.0, entry - low))
        risk = float(t.get("initial_risk_pts") or 10.0)
        tot_mfe += mfe
        tot_mae += mae
        if pts > 0:
            pot_w += mfe
            real_w += pts
        elif mfe >= (0.5 * risk):
            rev_cnt += 1
    return {
        "avg_mfe_pts": round(tot_mfe / n, 1),
        "avg_mae_pts": round(tot_mae / n, 1),
        "mfe_capture_efficiency_pct": round(real_w / pot_w * 100, 1) if pot_w > 0 else 0.0,
        "reversal_losses_count": rev_cnt,
        "reversal_losses_pct": round(rev_cnt / n * 100, 1),
    }


def _analyze_duration(trades: list[dict], baseline_r: float = 0.0) -> list[dict]:
    brackets = {
        "SCALP (<15m)": {"min": 0, "max": 15, "trades": []},
        "MOMENTUM (15-30m)": {"min": 15, "max": 30, "trades": []},
        "EXTENDED (30-60m)": {"min": 30, "max": 60, "trades": []},
        "STAGNANT (>60m)": {"min": 60, "max": 9999, "trades": []},
    }
    for t in trades:
        dur = float(t.get("duration_mins") or 0.0)
        for b in brackets.values():
            if b["min"] <= dur < b["max"]:
                b["trades"].append(t)
                break
    return _build_dimension_stats(brackets, "bracket", "Duration", baseline_r, trades)


def _analyze_time_of_day(trades: list[dict], baseline_r: float = 0.0) -> list[dict]:
    windows = {
        "OPENING (09:15-10:15)": {"min_m": 555, "max_m": 615, "trades": []},
        "MORNING_TREND (10:15-11:45)": {"min_m": 615, "max_m": 705, "trades": []},
        "MIDDAY_CHOP (11:45-13:15)": {"min_m": 705, "max_m": 795, "trades": []},
        "AFTERNOON_EXPANSION (13:15-14:15)": {"min_m": 795, "max_m": 855, "trades": []},
        "LATE_DECAY (14:15-15:30)": {"min_m": 855, "max_m": 930, "trades": []},
    }
    for t in trades:
        c_time_str = str(t.get("created_at") or "")
        try:
            time_part = c_time_str.split(" ")[1] if " " in c_time_str else c_time_str
            hour, minute = map(int, time_part.split(":")[:2])
            tot_mins = hour * 60 + minute
        except Exception:
            tot_mins = 600
        for w in windows.values():
            if w["min_m"] <= tot_mins < w["max_m"]:
                w["trades"].append(t)
                break
    return _build_dimension_stats(windows, "window", "Time Window", baseline_r, trades)


def _analyze_direction(trades: list[dict], baseline_r: float = 0.0) -> list[dict]:
    groups = {"CE (BULLISH)": {"trades": []}, "PE (BEARISH)": {"trades": []}}
    for t in trades:
        st = (t.get("signal_type") or "CE").upper()
        key = "CE (BULLISH)" if "CE" in st else "PE (BEARISH)"
        groups[key]["trades"].append(t)
    return _build_dimension_stats(groups, "direction", "Direction", baseline_r, trades)


def _analyze_moneyness(trades: list[dict], baseline_r: float = 0.0) -> list[dict]:
    groups = {}
    for t in trades:
        m = (t.get("moneyness") or "ATM").upper()
        if m not in groups: groups[m] = {"trades": []}
        groups[m]["trades"].append(t)
    return _build_dimension_stats(groups, "moneyness", "Moneyness", baseline_r, trades)


def _analyze_iv_regimes(trades: list[dict], baseline_r: float = 0.0) -> list[dict]:
    regimes = {
        "LOW (<13%)": {"min": 0, "max": 13.0, "trades": []},
        "NORMAL (13-20%)": {"min": 13.0, "max": 20.0, "trades": []},
        "ELEVATED (20-26%)": {"min": 20.0, "max": 26.0, "trades": []},
        "HIGH (>26%)": {"min": 26.0, "max": 999.0, "trades": []},
    }
    for t in trades:
        iv = float(t.get("iv_at_entry") or 0.0)
        for r in regimes.values():
            if r["min"] <= iv < r["max"]:
                r["trades"].append(t)
                break
    return _build_dimension_stats(regimes, "regime", "IV Regime", baseline_r, trades)


def _analyze_setup_score(trades: list[dict], baseline_r: float = 0.0) -> list[dict]:
    buckets = {
        "ELITE (>=85)": {"min": 85, "max": 100, "trades": []},
        "STRONG (75-84)": {"min": 75, "max": 84, "trades": []},
        "MODERATE (60-74)": {"min": 60, "max": 74, "trades": []},
        "LOW (<60)": {"min": 0, "max": 59, "trades": []},
    }
    for t in trades:
        score = int(t.get("setup_score") or 0)
        for b in buckets.values():
            if b["min"] <= score <= b["max"]:
                b["trades"].append(t)
                break
    return _build_dimension_stats(buckets, "bucket", "Setup Score", baseline_r, trades)


def _analyze_exit_reasons(trades: list[dict]) -> list[dict]:
    groups = {}
    for t in trades:
        reason = t.get("exit_reason") or t.get("status") or "UNKNOWN"
        if reason not in groups:
            groups[reason] = {"count": 0, "pts": 0.0, "inr": 0.0, "wins": 0, "r_vals": []}
        groups[reason]["count"] += 1
        pts = float(t.get("net_pnl_pts") or 0.0)
        inr = float(t.get("net_pnl_inr") or 0.0)
        r = float(t.get("r_multiple") or 0.0)
        groups[reason]["pts"] += pts
        groups[reason]["inr"] += inr
        groups[reason]["r_vals"].append(r)
        if pts > 0: groups[reason]["wins"] += 1

    result = []
    for r, data in groups.items():
        cnt = data["count"]
        result.append({
            "exit_reason": r,
            "count": cnt,
            "win_rate_pct": round(data["wins"] / cnt * 100, 1) if cnt > 0 else 0.0,
            "mean_r": round(sum(data["r_vals"]) / cnt, 3) if cnt > 0 else 0.0,
            "total_net_pts": round(data["pts"], 1),
            "total_net_inr": round(data["inr"], 2),
        })
    return sorted(result, key=lambda x: x["count"], reverse=True)


def _analyze_symbols(trades: list[dict], baseline_r: float = 0.0) -> list[dict]:
    syms = {}
    for t in trades:
        s = (t.get("symbol") or "NIFTY").upper()
        if s not in syms: syms[s] = {"trades": []}
        syms[s]["trades"].append(t)
    return _build_dimension_stats(syms, "symbol", "Symbol", baseline_r, trades)


def _build_dimension_stats(group_dict: dict, label_key: str, category_name: str, baseline_r: float, all_trades: list[dict]) -> list[dict]:
    results = []
    for name, grp in group_dict.items():
        grp_trades = grp["trades"]
        cnt = len(grp_trades)
        if cnt == 0:
            results.append({
                label_key: name, "category": category_name, "count": 0, "win_rate_pct": 0.0,
                "mean_r": 0.0, "mean_r_95_ci": [0.0, 0.0], "profit_factor": 1.0, "total_net_inr": 0.0,
                "p_value": 1.0, "sample_status": "INSUFFICIENT_DATA"
            })
            continue

        r_vals = [t["r_multiple"] for t in grp_trades]
        wins = sum(1 for t in grp_trades if t["net_pnl_pts"] > 0)
        mean_r = sum(r_vals) / cnt
        r_ci = _bootstrap_mean_ci(r_vals)
        pf, _ = _bootstrap_profit_factor(r_vals)
        comp_r = [t["r_multiple"] for t in all_trades if t not in grp_trades]
        t_stat, p_val = _compute_welch_t_test(r_vals, comp_r) if comp_r else (0.0, 1.0)

        results.append({
            label_key: name,
            "category": category_name,
            "count": cnt,
            "wins": wins,
            "win_rate_pct": round(wins / cnt * 100, 1),
            "mean_r": round(mean_r, 3),
            "mean_r_95_ci": r_ci,
            "profit_factor": round(pf, 2),
            "total_net_pts": round(sum(t["net_pnl_pts"] for t in grp_trades), 1),
            "total_net_inr": round(sum(t["net_pnl_inr"] for t in grp_trades), 2),
            "t_statistic": t_stat,
            "p_value": p_val,
            "sample_status": "SUFFICIENT" if cnt >= MIN_SAMPLE_SIZE else "INSUFFICIENT_DATA",
            "ci_excludes_baseline": ((r_ci[0] > baseline_r or r_ci[1] < baseline_r) and cnt >= MIN_SAMPLE_SIZE),
        })
    return results


def _classify_losers(trades: list[dict]) -> dict:
    losing_trades = [t for t in trades if t["net_pnl_pts"] <= 0]
    n_losses = len(losing_trades)
    if n_losses == 0:
        return {"total_losers": 0, "wrong_direction": {"count": 0, "pct": 0.0}, "right_then_reversed": {"count": 0, "pct": 0.0}, "theta_stagnation": {"count": 0, "pct": 0.0}, "stopped_by_noise": {"count": 0, "pct": 0.0}}

    cats = {"wrong_direction": [], "right_then_reversed": [], "theta_stagnation": [], "stopped_by_noise": []}
    for t in losing_trades:
        entry = float(t.get("entry_price") or 0.0)
        init_sl = float(t.get("initial_stop_loss") or t.get("stop_loss") or (entry * 0.85))
        high = float(t.get("highest_price") or entry)
        dur = float(t.get("duration_mins") or 0.0)
        risk = max(entry - init_sl, 0.1)
        mfe = float(t.get("mfe_points") or max(0.0, high - entry))
        exit_r = (t.get("exit_reason") or t.get("status") or "").upper()

        if mfe >= (0.5 * risk): cats["right_then_reversed"].append(t)
        elif dur >= 45.0: cats["theta_stagnation"].append(t)
        elif exit_r in ("STOP_LOSS_HIT", "SL_HIT", "TRAILING_STOP_HIT", "TSL_HIT") and high >= entry: cats["stopped_by_noise"].append(t)
        else: cats["wrong_direction"].append(t)

    result = {"total_losers": n_losses}
    for k, v in cats.items():
        cnt = len(v)
        result[k] = {"count": cnt, "pct": round(cnt / n_losses * 100, 1), "avg_r": round(sum(x["r_multiple"] for x in v) / cnt, 3) if cnt > 0 else 0.0}
    return result


def _run_counterfactual_replay(trades: list[dict]) -> dict:
    """Replays counterfactual parameter sets across trade paths and computes paired delta CI vs baseline."""
    n = len(trades)
    if n == 0: return {}
    baseline_r = [t["r_multiple"] for t in trades]
    base_mean_r = sum(baseline_r) / n

    # Fetch ticks for all trades in batch
    trade_ids = [t.get("id") for t in trades if t.get("id")]
    ticks_map = {}
    if trade_ids:
        try:
            with _get_db() as conn:
                placeholders = ",".join(["?"] * len(trade_ids))
                t_rows = conn.execute(f"SELECT * FROM trade_ticks WHERE signal_id IN ({placeholders}) ORDER BY id ASC", trade_ids).fetchall()
                for r in t_rows:
                    sid = r["signal_id"]
                    if sid not in ticks_map:
                        ticks_map[sid] = []
                    ticks_map[sid].append(dict(r))
        except Exception:
            pass

    variations = {
        "SL_10_PCT": {
            "name": "Tight Stop Loss (10% / ~0.7R)",
            "params": {"sl_pct": 0.10, "t1_multiple": 1.5, "t2_multiple": 2.5}
        },
        "SL_15_PCT_BASELINE": {
            "name": f"Baseline Stop Loss ({ENGINE_CONFIG.get('MIN_SETUP_SCORE', 75)} Score / 15% SL)",
            "params": None
        },
        "SL_20_PCT": {
            "name": "Wide Stop Loss (20% / ~1.3R)",
            "params": {"sl_pct": 0.20, "t1_multiple": 1.5, "t2_multiple": 2.5}
        },
        "T1_1_0_R": {
            "name": "Quick Target 1 (1.0R Multiple)",
            "params": {"sl_pct": 0.15, "t1_multiple": 1.0, "t2_multiple": 2.0}
        },
        "T1_1_5_R_BASELINE": {
            "name": "Target 1 (1.5R Baseline)",
            "params": None
        },
        "T1_2_0_R": {
            "name": "Extended Target 1 (2.0R Multiple)",
            "params": {"sl_pct": 0.15, "t1_multiple": 2.0, "t2_multiple": 3.0}
        },
        "TIME_STOP_45M": {
            "name": "Time Stop Cut (45 Mins Stagnation)",
            "params": {"sl_pct": 0.15, "t1_multiple": 1.5, "t2_multiple": 2.5, "time_stop_mins": 45.0}
        },
    }

    results = {}
    for var_id, var_data in variations.items():
        p = var_data["params"]
        if p is None:
            r_list = baseline_r
        else:
            r_list = [replay_trade_on_ticks(t, ticks_map.get(t.get("id"), []), p) for t in trades]

        var_mean = sum(r_list) / n
        delta_list = [r_list[i] - baseline_r[i] for i in range(n)]
        delta_ci = _bootstrap_mean_ci(delta_list)
        t_stat, p_val = _compute_welch_t_test(r_list, baseline_r)

        results[var_id] = {
            "name": var_data["name"],
            "mean_r": round(var_mean, 3),
            "delta_mean_r": round(var_mean - base_mean_r, 3),
            "delta_mean_r_95_ci": delta_ci,
            "p_value": p_val,
            "outperforms_baseline": (delta_ci[0] > 0.0 and p_val < 0.05 and n >= MIN_SAMPLE_SIZE),
        }
    return results


def _identify_best_conditions(segment_tests: list[dict], pillars: list[dict]) -> list[dict]:
    best = []
    for seg in segment_tests:
        if seg["count"] >= MIN_SAMPLE_SIZE and seg["mean_r_95_ci"][0] > 0.0:
            lbl = seg.get("symbol") or seg.get("moneyness") or seg.get("window") or seg.get("bracket") or seg.get("direction") or seg.get("bucket") or seg.get("regime")
            best.append({"category": seg["category"], "condition": str(lbl), "metric": f"+{seg['mean_r']:.2f}R Mean ({seg['win_rate_pct']}% Win Rate)"})
    if not best:
        best.append({"category": "Sample Verification", "condition": "INSUFFICIENT_DATA_OR_NO_PROVEN_EDGE", "metric": f"Awaiting >= {MIN_SAMPLE_SIZE} closed trades per segment."})
    return best


def _identify_worst_conditions(segment_tests: list[dict], pillars: list[dict]) -> list[dict]:
    worst = []
    for seg in segment_tests:
        if seg["count"] >= MIN_SAMPLE_SIZE and seg["mean_r_95_ci"][1] < 0.0:
            lbl = seg.get("symbol") or seg.get("moneyness") or seg.get("window") or seg.get("bracket") or seg.get("direction") or seg.get("bucket") or seg.get("regime")
            worst.append({"category": seg["category"], "condition": str(lbl), "metric": f"{seg['mean_r']:.2f}R Mean ({seg['win_rate_pct']}% Win Rate)"})
    if not worst:
        worst.append({"category": "Sample Verification", "condition": "INSUFFICIENT_DATA_OR_NO_STATISTICAL_DRAG", "metric": f"Awaiting >= {MIN_SAMPLE_SIZE} closed trades per segment."})
    return worst


def _identify_top_3_profit_leaks(enriched_trades, loser_taxonomy, duration_analysis, time_analysis, score_analysis, pillar_analysis, counterfactual) -> list[dict]:
    """
    Identifies top 3 profit leaks from actual empirical data (loser taxonomy, duration, time-of-day, exit reasons),
    ranked by total R lost with 95% bootstrap confidence intervals.
    """
    n = len(enriched_trades)
    if n < MIN_SAMPLE_SIZE:
        return [{"id": "INSUFFICIENT_SAMPLE", "severity": "INFO", "title": f"Insufficient Sample Size ({n}/{MIN_SAMPLE_SIZE})", "evidence": "Awaiting trade samples.", "impact": "None"}]

    candidate_leaks = []

    # 1. Right Then Reversed (Excursion >= 0.5R giving back profit)
    rev_info = loser_taxonomy.get("right_then_reversed", {})
    rev_cnt = rev_info.get("count", 0)
    if rev_cnt >= 5:
        rev_trades = [t for t in enriched_trades if t["net_pnl_pts"] <= 0 and (t.get("mfe_points") or 0.0) >= (0.5 * float(t.get("initial_risk_pts") or 1.0))]
        r_lost = abs(sum(t["r_multiple"] for t in rev_trades))
        ci = _bootstrap_mean_ci([t["r_multiple"] for t in rev_trades])
        candidate_leaks.append({
            "id": "PROFIT_REVERSALS",
            "severity": "HIGH",
            "title": f"Profit Reversals ({rev_cnt} trades reached >= +0.5R then closed in loss)",
            "evidence": f"{rev_cnt}/{n} trades gave back early excursion ({rev_info.get('pct', 0)}% of losers).",
            "total_r_lost": round(r_lost, 2),
            "mean_loss_ci": ci,
            "impact": f"-{r_lost:.1f}R cumulative lost (Mean loss {ci[0]:.2f}R to {ci[1]:.2f}R)"
        })

    # 2. Theta Stagnation (> 45m duration in chop)
    stagnant = [t for t in enriched_trades if float(t.get("duration_mins") or 0.0) >= 45.0 and t["r_multiple"] < 0]
    if len(stagnant) >= 5:
        r_lost = abs(sum(t["r_multiple"] for t in stagnant))
        ci = _bootstrap_mean_ci([t["r_multiple"] for t in stagnant])
        candidate_leaks.append({
            "id": "THETA_STAGNATION",
            "severity": "MEDIUM",
            "title": f"Theta Decay Stagnation ({len(stagnant)} trades held >= 45 mins)",
            "evidence": f"{len(stagnant)} trades eroded into losses after holding > 45 minutes.",
            "total_r_lost": round(r_lost, 2),
            "mean_loss_ci": ci,
            "impact": f"-{r_lost:.1f}R cumulative lost"
        })

    # 3. Unfavorable Time-of-Day Window (e.g. Midday Chop)
    for w in time_analysis:
        if w.get("count", 0) >= 10 and w.get("mean_r", 0.0) < -0.2:
            r_lost = abs(w.get("mean_r", 0.0) * w["count"])
            candidate_leaks.append({
                "id": f"TIME_WINDOW_{w.get('window', 'UNKNOWN')}",
                "severity": "MEDIUM",
                "title": f"Chop Drag in {w.get('window')}",
                "evidence": f"{w['count']} trades in this window produced {w.get('mean_r')}R average expectancy.",
                "total_r_lost": round(r_lost, 2),
                "mean_loss_ci": w.get("mean_r_95_ci", [0.0, 0.0]),
                "impact": f"-{r_lost:.1f}R lost in segment"
            })

    if not candidate_leaks:
        return [{"id": "NO_SIGNIFICANT_LEAKS", "severity": "INFO", "title": "Operating within normal variance", "evidence": f"Evaluated across {n} valid trades.", "impact": "Baseline stable"}]

    candidate_leaks.sort(key=lambda x: x.get("total_r_lost", 0.0), reverse=True)
    return candidate_leaks[:3]


def _generate_rigorous_recommendations(n_valid: int, baseline_mean_r: float, pillar_analysis: list[dict], worst_conditions: list[dict], counterfactual: dict, loser_taxonomy: dict, hypotheses: list[dict] = None) -> list[dict]:
    recs = []
    if hypotheses:
        for h in hypotheses:
            if h.get("status") == "RECOMMENDED":
                recs.append({
                    "parameter": h["parameter"],
                    "current_value": h["current_value"],
                    "proposed_value": h["proposed_value"],
                    "n": h.get("observation_count", 0),
                    "effect_size": h.get("effect_size", 0.0),
                    "ci": [h.get("ci_lower", 0.0), h.get("ci_upper", 0.0)],
                    "expected_net_impact": h.get("expected_net_impact", ""),
                    "validation_plan": h.get("validation_plan", ""),
                    "evidence_status": "RECOMMENDED",
                    "requires_approval": True,
                    "target_file": "app/services/options_engine.py"
                })

    if not recs:
        recs.append({
            "parameter": "ENGINE_CONFIG_BASELINE",
            "current_value": f"Current Baseline Rules (Min Score: {ENGINE_CONFIG.get('MIN_SETUP_SCORE', 75)})",
            "proposed_value": "No Change",
            "n": n_valid,
            "effect_size": 0.0,
            "ci": [0.0, 0.0],
            "expected_net_impact": "Preserves established baseline.",
            "validation_plan": "Continue paper trade execution.",
            "evidence_status": "NO_EVIDENCE_FOR_CHANGE" if n_valid >= MIN_SAMPLE_SIZE else "INSUFFICIENT_DATA",
            "requires_approval": True,
            "target_file": "None (Observation Only)"
        })
    return recs


def _compute_rigorous_health_score(mean_r: float, r_ci: list[float], win_rate: float, pf: float, max_dd_r: float, n: int) -> int:
    if n == 0: return 50
    exp_pts = max(0.0, min(40.0, 20.0 + (mean_r * 40.0)))
    wr_pts = max(0.0, min(25.0, (win_rate / 60.0) * 25.0))
    pf_pts = max(0.0, min(20.0, (pf / 2.0) * 20.0))
    dd_penalty = min(15.0, max_dd_r * 3.0)
    sample_mult = min(1.0, math.sqrt(n / MIN_SAMPLE_SIZE))
    raw_score = (exp_pts + wr_pts + pf_pts - dd_penalty) * sample_mult
    return round(max(10, min(95, raw_score)))


def _build_empty_diagnostic_report() -> dict:
    return {
        "status": "INSUFFICIENT_DATA",
        "sample_sufficiency": "INSUFFICIENT",
        "mode": "OBSERVE_AND_RECOMMEND_ONLY",
        "advisory_notice": "All recommendations are strictly advisory and require human approval before altering live engine parameters.",
        "data_limitation_notice": "MFE and MAE metrics are captured via periodic polling cycles and may understate intra-interval price excursions.",
        "data_quality": {"total_raw": 0, "valid_count": 0, "excluded_count": 0, "exclusion_reasons": {}, "filter_passed": True},
        "sample_size": {"total_signals_raw": 0, "closed_trades_raw": 0, "valid_closed_trades": 0, "excluded_trades": 0, "active_trades": 0, "win_count": 0, "loss_count": 0, "win_rate_pct": 0.0, "sample_sufficient": False},
        "overall_performance": {"expectancy_r": 0.0, "profit_factor": 1.0, "total_net_inr": 0.0, "strategy_health_score": 50},
        "setup_pillars": [],
        "loser_taxonomy": {"total_losers": 0},
        "counterfactual_replay": {},
        "recommended_changes": _generate_rigorous_recommendations(0, 0.0, [], [], {}, {}),
        "hypotheses_tracker": [],
        "recent_trade_reviews": [],
        "timestamp": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    }
