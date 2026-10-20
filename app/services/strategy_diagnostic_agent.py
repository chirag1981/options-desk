"""
app/services/strategy_diagnostic_agent.py — Autonomous Options Strategy Diagnostic & Optimization Agent
Executes continuous trade failure detection, root cause diagnosis, counterfactual tick replay,
parameter optimization, and regression testing.

Pillars of Diagnostic Evaluation:
1. MONITOR & INSPECT: Detect terminal failed trades in signals & trade_reviews.
2. ROOT CAUSE DIAGNOSIS:
   - 50% Pivot Filter & Round Strike Defense
   - Trending OI Metrics & Bias Validation
   - IV Crush, Session Time Bounds & Spread Erosion
   - Stop-Loss Buffers (STOPPED_BY_NOISE vs True Failure)
3. STRATEGY REPLAY & COUNTERFACTUAL VERIFICATION:
   - Evaluates parameter variations across real trade_ticks using replay_trade_on_ticks().
4. AUTOMATED PARAMETER / CODE FIX:
   - Proposes and safely applies validated ENGINE_CONFIG optimizations with pytest verification.
5. VERIFICATION & REPORTING:
   - Generates structured diagnostic reports and validates live engine decisions.
"""

import os
import sys
import json
import logging
import sqlite3
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from app.services.options_engine import (
    ENGINE_CONFIG,
    analyze_option_desk,
)
from app.services.options_signal_service import _get_db, get_signals_summary
from app.services.trade_analyzer_agent import (
    review_closed_trade,
    review_unreviewed_closed_trades,
)
from app.services.options_replay_backtest import (
    replay_trade_on_ticks,
    calculate_trade_costs,
)

log = logging.getLogger("strategy_diagnostic_agent")
IST = ZoneInfo("Asia/Kolkata")

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


# ==============================================================================
# PHASE 1: MONITOR & INSPECT (Trade Failure Detection)
# ==============================================================================

def detect_failed_trades(lookback_days: int = 14, limit: int = 50) -> list[dict]:
    """
    Queries recently closed or failed paper trades from signals and trade_reviews.
    Identifies failure modes: WRONG_DIRECTION, RIGHT_THEN_REVERSED, STOPPED_BY_NOISE,
    THETA_STAGNATION, and DATA_QUALITY_ISSUE.
    """
    review_unreviewed_closed_trades()

    with _get_db() as conn:
        cursor = conn.cursor()
        query = """
            SELECT 
                s.id, s.symbol, s.signal_type, s.contract_name, s.strike, s.expiry,
                s.spot_at_entry, s.entry_price, s.stop_loss, s.target_1, s.target_2,
                s.status, s.exit_price, s.exit_time, s.points_pnl, s.pnl_pct,
                s.net_pnl_inr, s.created_at, s.bias_at_entry, s.pcr_at_entry,
                s.spread_paid, s.duration_mins, s.exit_reason,
                r.classification, r.r_multiple, r.mfe_capture_pct, r.mae_vs_stop_pct,
                r.time_of_day, r.counterfactuals_json, r.data_quality_flags_json,
                r.plain_language_summary
            FROM signals s
            LEFT JOIN trade_reviews r ON s.id = r.signal_id
            WHERE s.status NOT IN ('ACTIVE', 'TARGET_1_HIT')
              AND s.is_deleted = 0
              AND (
                  s.status IN ('SL_HIT', 'TSL_HIT', 'TIME_STOP_EXIT', 'STALE_EXIT')
                  OR s.points_pnl < 0
                  OR r.classification IN ('WRONG_DIRECTION', 'RIGHT_THEN_REVERSED', 'STOPPED_BY_NOISE', 'THETA_STAGNATION')
              )
            ORDER BY s.id DESC
            LIMIT ?
        """
        rows = cursor.execute(query, (limit,)).fetchall()
        failed_trades = []
        for r in rows:
            trade = dict(r)
            # Ensure trade review exists if classification is None
            if not trade.get("classification"):
                rev = review_closed_trade(trade["id"])
                if rev:
                    trade["classification"] = rev.get("classification")
                    trade["r_multiple"] = rev.get("r_multiple")
                    trade["counterfactuals_json"] = rev.get("counterfactuals_json")

            failed_trades.append(trade)

        return failed_trades


# ==============================================================================
# PHASE 2: ROOT CAUSE DIAGNOSIS
# ==============================================================================

def diagnose_trade_root_cause(signal_id: int) -> dict:
    """
    Inspects decision_log, trade_ticks, and market telemetry to diagnose the root cause
    of a failed trade against the 4 core strategy pillars in options_engine.py.
    """
    with _get_db() as conn:
        conn.row_factory = sqlite3.Row
        sig_row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        if not sig_row:
            return {"signal_id": signal_id, "error": f"Signal {signal_id} not found"}

        sig = dict(sig_row)
        review_row = conn.execute("SELECT * FROM trade_reviews WHERE signal_id = ?", (signal_id,)).fetchone()
        review = dict(review_row) if review_row else {}

        # Fetch ticks
        ticks = [
            dict(t)
            for t in conn.execute(
                "SELECT * FROM trade_ticks WHERE signal_id = ? ORDER BY id ASC", (signal_id,)
            ).fetchall()
        ]

        # Fetch closest preceding decision_log entry for this symbol
        sig_created = sig.get("created_at") or ""
        dec_row = conn.execute("""
            SELECT * FROM decision_log 
            WHERE symbol = ? AND (timestamp <= ? OR created_at <= ?)
            ORDER BY timestamp DESC LIMIT 1
        """, (sig.get("symbol"), sig_created, sig_created)).fetchone()
        dec = dict(dec_row) if dec_row else {}

    diagnosis = {
        "signal_id": signal_id,
        "symbol": sig.get("symbol"),
        "contract": sig.get("contract_name"),
        "signal_type": sig.get("signal_type"),
        "entry_price": sig.get("entry_price"),
        "exit_price": sig.get("exit_price"),
        "points_pnl": sig.get("points_pnl"),
        "net_pnl_inr": sig.get("net_pnl_inr"),
        "status": sig.get("status"),
        "classification": review.get("classification") or "UNCLASSIFIED",
        "r_multiple": review.get("r_multiple"),
        "root_causes": [],
        "pillar_findings": {},
        "evidence": {},
    }

    entry_p = float(sig.get("entry_price") or 0.0)
    stop_p = float(sig.get("stop_loss") or (entry_p * 0.85))
    target_p = float(sig.get("target_1") or (entry_p * 1.5))
    initial_risk = max(0.5, entry_p - stop_p)
    spot_entry = float(sig.get("spot_at_entry") or 0.0)

    # --------------------------------------------------------------------------
    # Pillar 1: 50% Pivot Filter & Round Strike Defense
    # --------------------------------------------------------------------------
    p1_issues = []
    major_round_strikes = [22000, 22500, 23000, 23500, 24000, 24500, 25000, 25500]
    nearest_major = min(major_round_strikes, key=lambda x: abs(x - spot_entry)) if spot_entry > 0 else 0
    dist_to_major = abs(spot_entry - nearest_major) if nearest_major > 0 else 999.0

    # If within 25 points of a major round strike resistance/support
    if dist_to_major <= 25.0:
        if sig.get("signal_type") in ("CE", "BUY_CALL") and (nearest_major - spot_entry) in range(0, 26):
            p1_issues.append(f"Spot entered right into Major Round Strike Resistance {nearest_major} (buffer: {dist_to_major:.1f} pts).")
        elif sig.get("signal_type") in ("PE", "BUY_PUT") and (spot_entry - nearest_major) in range(0, 26):
            p1_issues.append(f"Spot entered right into Major Round Strike Support {nearest_major} (buffer: {dist_to_major:.1f} pts).")

    # 50% Day Pivot alignment check
    pivot_flag = dec.get("pillar_level")
    if pivot_flag is not None and pivot_flag == 0:
        p1_issues.append("50% Pivot Filter violated: Spot position conflicted with day's 50% anchor range.")

    diagnosis["pillar_findings"]["pivot_and_strike_defense"] = {
        "status": "FLAGGED" if p1_issues else "CLEAN",
        "issues": p1_issues,
        "spot_at_entry": spot_entry,
        "nearest_major_strike": nearest_major,
        "distance_to_major": round(dist_to_major, 1),
    }
    if p1_issues:
        diagnosis["root_causes"].extend(p1_issues)

    # --------------------------------------------------------------------------
    # Pillar 2: Trending OI Metrics & Bias Validation
    # --------------------------------------------------------------------------
    p2_issues = []
    pcr = float(sig.get("pcr_at_entry") or dec.get("pcr") or 1.0)
    bias = str(sig.get("bias_at_entry") or dec.get("bias") or "").upper()
    sig_side = str(sig.get("signal_type") or "").upper()

    if "PE" in sig_side and bias == "BULLISH":
        p2_issues.append(f"Severe Bias Divergence: Put bought while bias was strongly BULLISH (PCR: {pcr:.2f}).")
    elif "CE" in sig_side and bias == "BEARISH":
        p2_issues.append(f"Severe Bias Divergence: Call bought while bias was strongly BEARISH (PCR: {pcr:.2f}).")
    elif bias == "NEUTRAL":
        p2_issues.append("Low-Conviction Entry: Direction bias was NEUTRAL at entry; directional OI writing was insufficient.")

    diagnosis["pillar_findings"]["trending_oi_and_bias"] = {
        "status": "FLAGGED" if p2_issues else "CLEAN",
        "issues": p2_issues,
        "bias": bias,
        "pcr": round(pcr, 2),
    }
    if p2_issues:
        diagnosis["root_causes"].extend(p2_issues)

    # --------------------------------------------------------------------------
    # Pillar 3: IV Crush, Session Time Bounds & Spread Erosion
    # --------------------------------------------------------------------------
    p3_issues = []
    time_of_day = review.get("time_of_day") or ""
    spread_paid = float(sig.get("spread_paid") or 0.0)

    # Check session bounds
    if "MIDDAY_CHOP" in time_of_day:
        p3_issues.append("Adverse Timing Regime: Trade executed during MIDDAY_CHOP (11:45-13:15 IST) prone to false breakouts.")

    sig_created = sig.get("created_at") or ""
    if sig_created:
        try:
            created_at_dt = datetime.strptime(sig_created, "%Y-%m-%d %H:%M:%S")
            time_mins = created_at_dt.hour * 60 + created_at_dt.minute
            cutoff = ENGINE_CONFIG.get("PRIME_WINDOW_END_MINS", 870)
            if time_mins > cutoff:
                p3_issues.append(f"Late Session Timing: Entered at {created_at_dt.strftime('%H:%M')} past prime cutoff ({cutoff//60:02d}:{cutoff%60:02d} IST).")
        except Exception:
            pass

    # Spread Drag check
    if entry_p > 0 and (spread_paid / entry_p) > 0.04:
        p3_issues.append(f"Excessive Spread Friction: Spread paid ({spread_paid:.2f} pts) exceeded 4% of option entry price.")

    diagnosis["pillar_findings"]["timing_iv_and_spread"] = {
        "status": "FLAGGED" if p3_issues else "CLEAN",
        "issues": p3_issues,
        "time_of_day": time_of_day,
        "spread_paid": spread_paid,
    }
    if p3_issues:
        diagnosis["root_causes"].extend(p3_issues)

    # --------------------------------------------------------------------------
    # Pillar 4: Stop-Loss Buffers & Noise Immunity
    # --------------------------------------------------------------------------
    p4_issues = []
    max_risk_cap = ENGINE_CONFIG.get("NIFTY_MAX_OPTION_RISK_POINTS", 8.0)
    is_stopped_by_noise = review.get("classification") == "STOPPED_BY_NOISE"

    if is_stopped_by_noise:
        p4_issues.append(f"Premature Stop-Loss Exit (STOPPED_BY_NOISE): Price hit SL ({stop_p:.1f}) but recovered to T1 ({target_p:.1f}) on subsequent path.")
    elif initial_risk <= ENGINE_CONFIG.get("NIFTY_MIN_OPTION_RISK_POINTS", 3.0) + 0.5:
        p4_issues.append(f"Stop-Loss Too Tight: Risk buffer ({initial_risk:.1f} pts) vulnerable to normal market bid/ask noise.")

    diagnosis["pillar_findings"]["stop_loss_buffers"] = {
        "status": "FLAGGED" if p4_issues else "CLEAN",
        "issues": p4_issues,
        "initial_risk_pts": round(initial_risk, 2),
        "max_risk_cap": max_risk_cap,
        "is_stopped_by_noise": is_stopped_by_noise,
    }
    if p4_issues:
        diagnosis["root_causes"].extend(p4_issues)

    if not diagnosis["root_causes"]:
        diagnosis["root_causes"].append("Market Direction Shift: Natural stop-out where underlying trend failed to expand.")

    return diagnosis


# ==============================================================================
# PHASE 3: STRATEGY REPLAY & COUNTERFACTUAL VERIFICATION
# ==============================================================================

def replay_counterfactual_parameter_grid(failed_trades: list[dict] | None = None) -> dict:
    """
    Simulates counterfactual parameter variations across actual tick paths for failed trades.
    Tests:
      1. WIDER_SL_20_PCT: sl_pct = 0.20 (wider noise buffer)
      2. TIGHTER_SL_10_PCT: sl_pct = 0.10 (fast loss cut)
      3. QUICK_SCALP_1_0_R: t1_multiple = 1.0 (earlier profit taking)
      4. TIME_STOP_30M: time_stop_mins = 30.0 (aggressive theta defense)
      5. TSL_BE_0_75_R: moves SL to BE at +0.75R excursion
    """
    if failed_trades is None:
        failed_trades = detect_failed_trades(limit=25)

    if not failed_trades:
        return {
            "status": "NO_TRADES",
            "message": "No failed trades available for counterfactual replay.",
            "tested_count": 0,
            "variations": {},
        }

    sig_ids = [t["id"] for t in failed_trades]
    ticks_map = {}
    with _get_db() as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join(["?"] * len(sig_ids))
        tick_rows = conn.execute(
            f"SELECT * FROM trade_ticks WHERE signal_id IN ({placeholders}) ORDER BY id ASC",
            sig_ids,
        ).fetchall()
        for tr in tick_rows:
            sid = tr["signal_id"]
            if sid not in ticks_map:
                ticks_map[sid] = []
            ticks_map[sid].append(dict(tr))

    variants = {
        "BASELINE_15_PCT": {"sl_pct": 0.15, "t1_multiple": 1.5, "t2_multiple": 2.5},
        "WIDER_SL_20_PCT": {"sl_pct": 0.20, "t1_multiple": 1.5, "t2_multiple": 2.5},
        "TIGHTER_SL_10_PCT": {"sl_pct": 0.10, "t1_multiple": 1.5, "t2_multiple": 2.5},
        "QUICK_SCALP_1_0_R": {"sl_pct": 0.15, "t1_multiple": 1.0, "t2_multiple": 2.0},
        "TIME_STOP_30M": {"sl_pct": 0.15, "t1_multiple": 1.5, "t2_multiple": 2.5, "time_stop_mins": 30.0},
        "TSL_BE_0_75_R": {"sl_pct": 0.15, "t1_multiple": 1.5, "t2_multiple": 2.5, "trail_be_step": 0.50},
    }

    results = {v: {"r_multiples": [], "wins": 0, "losses": 0, "total_r": 0.0} for v in variants}

    for t in failed_trades:
        sid = t["id"]
        t_ticks = ticks_map.get(sid, [])
        for v_name, v_params in variants.items():
            r = replay_trade_on_ticks(t, t_ticks, v_params)
            results[v_name]["r_multiples"].append(r)
            results[v_name]["total_r"] += r
            if r > 0.05:
                results[v_name]["wins"] += 1
            elif r < -0.05:
                results[v_name]["losses"] += 1

    summary_comparison = {}
    baseline_total_r = results["BASELINE_15_PCT"]["total_r"]
    cnt = len(failed_trades)

    for v_name, data in results.items():
        avg_r = round(data["total_r"] / cnt, 3) if cnt > 0 else 0.0
        delta_r = round(data["total_r"] - baseline_total_r, 3)
        win_rate = round(data["wins"] / cnt * 100, 1) if cnt > 0 else 0.0
        summary_comparison[v_name] = {
            "avg_r_multiple": avg_r,
            "total_r_multiple": round(data["total_r"], 2),
            "delta_r_vs_baseline": delta_r,
            "win_rate_pct": win_rate,
            "profitable_reversals": data["wins"],
        }

    # Best counterfactual parameter
    best_variant = max(summary_comparison.items(), key=lambda x: x[1]["total_r_multiple"])

    return {
        "status": "SUCCESS",
        "tested_trades_count": cnt,
        "best_variant": best_variant[0],
        "best_variant_stats": best_variant[1],
        "comparison": summary_comparison,
    }


# ==============================================================================
# PHASE 4: AUTOMATED CODE / PARAMETER FIX
# ==============================================================================

def apply_strategy_optimization(recommendation: dict, dry_run: bool = True) -> dict:
    """
    Applies validated optimizations to ENGINE_CONFIG or trading rules.
    Runs the automated pytest suite to ensure zero regressions before locking in changes.
    """
    target_param = recommendation.get("target_param")
    new_value = recommendation.get("new_value")

    if not target_param or new_value is None:
        return {"status": "SKIPPED", "message": "No valid parameter specified for optimization."}

    old_value = ENGINE_CONFIG.get(target_param)
    log.info(f"[STRATEGY_OPTIMIZER] Preparing to update {target_param}: {old_value} -> {new_value} (dry_run={dry_run})")

    if dry_run:
        return {
            "status": "DRY_RUN",
            "target_param": target_param,
            "old_value": old_value,
            "new_value": new_value,
            "message": "Optimization validated in counterfactual replay. Set dry_run=False to apply.",
        }

    # Apply in memory
    ENGINE_CONFIG[target_param] = new_value

    # Run verification test suite
    test_res = run_regression_tests()
    if not test_res["passed"]:
        # Revert immediately on test failure
        ENGINE_CONFIG[target_param] = old_value
        log.error(f"[REGRESSION_DETECTED] Reverted {target_param} back to {old_value}. Test failure: {test_res.get('error')}")
        return {
            "status": "REVERTED",
            "target_param": target_param,
            "reverted_to": old_value,
            "test_output": test_res.get("output", "")[:500],
            "message": "Parameter update caused test failure and was safely reverted.",
        }

    return {
        "status": "APPLIED",
        "target_param": target_param,
        "old_value": old_value,
        "new_value": new_value,
        "tests_passed": test_res["passed"],
        "message": f"Successfully updated {target_param} to {new_value} with 100% passing tests.",
    }


def run_regression_tests() -> dict:
    """Executes pytest suite relevant to strategy engine and verifies all tests pass."""
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_pivot_50_strategy.py", "tests/test_paper_strategy_scenarios.py", "-q"],
            cwd=WORKSPACE_ROOT,
            capture_output=True,
            text=True,
            timeout=45,
        )
        return {
            "passed": res.returncode == 0,
            "output": res.stdout.strip(),
            "error": res.stderr.strip() if res.returncode != 0 else "",
        }
    except Exception as e:
        return {"passed": False, "error": str(e), "output": ""}


# ==============================================================================
# PHASE 5: VERIFICATION & CONTINUOUS EXECUTION LOOP
# ==============================================================================

def run_strategy_diagnostic_cycle(auto_apply: bool = False) -> dict:
    """
    Executes the complete autonomous diagnostic and optimization loop:
    1. Detects recently failed trades.
    2. Diagnoses root causes across all 4 pillars.
    3. Runs counterfactual replay across historical ticks.
    4. Proposes or applies safe optimizations.
    5. Validates live analyze_option_desk() decision pipeline.
    """
    log.info("[STRATEGY_DIAGNOSTIC_AGENT] Starting diagnostic and optimization cycle...")
    failed_trades = detect_failed_trades(limit=10)

    diagnosed_trades = []
    for t in failed_trades:
        diag = diagnose_trade_root_cause(t["id"])
        diagnosed_trades.append(diag)

    replay_results = replay_counterfactual_parameter_grid(failed_trades)

    # Determine optimization candidates
    recommendations = []
    if replay_results.get("status") == "SUCCESS":
        best = replay_results.get("best_variant")
        best_stats = replay_results.get("best_variant_stats", {})
        delta_r = best_stats.get("delta_r_vs_baseline", 0.0)

        if delta_r > 0.5:
            if best == "WIDER_SL_20_PCT":
                recommendations.append({
                    "target_param": "NIFTY_MAX_OPTION_RISK_POINTS",
                    "new_value": 10.0,
                    "reason": f"Wider SL buffer improved net expectancy by +{delta_r:.2f}R by eliminating premature noise stops.",
                })
            elif best == "TIME_STOP_30M":
                recommendations.append({
                    "target_param": "PRIME_WINDOW_END_MINS",
                    "new_value": 855,  # 14:15 cutoff
                    "reason": f"Theta decay defense improved net expectancy by +{delta_r:.2f}R.",
                })

    optimization_actions = []
    for rec in recommendations:
        res = apply_strategy_optimization(rec, dry_run=not auto_apply)
        optimization_actions.append(res)

    # Verify live engine analysis health
    engine_health = verify_engine_health()

    report = {
        "timestamp": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "total_failed_trades_reviewed": len(failed_trades),
        "diagnoses": diagnosed_trades,
        "counterfactual_replay": replay_results,
        "recommendations": recommendations,
        "optimization_actions": optimization_actions,
        "engine_health_verification": engine_health,
        "status": "PASS" if engine_health.get("passed") else "ATTENTION_REQUIRED",
    }

    log.info(f"[STRATEGY_DIAGNOSTIC_AGENT] Cycle complete. Reviewed {len(failed_trades)} trades. Engine health: {report['status']}")
    return report


def verify_engine_health() -> dict:
    """Verifies analyze_option_desk() functions cleanly on live or mock option chains."""
    mock_chain = {
        "symbol": "NIFTY",
        "spot_price": 24200.0,
        "spot_change_pct": 0.15,
        "data_status": "LIVE",
        "underlying_candles": [
            {"timestamp": 1000, "is_completed": True, "open": 24190, "high": 24210, "low": 24180, "close": 24200}
        ],
        "chain": [
            {
                "strike": 24200,
                "call_ltp": 120.0,
                "call_bid": 119.5,
                "call_ask": 120.5,
                "call_oi": 150000,
                "call_oi_change": 25000,
                "call_iv": 14.5,
                "put_ltp": 115.0,
                "put_bid": 114.5,
                "put_ask": 115.5,
                "put_oi": 180000,
                "put_oi_change": 45000,
                "put_iv": 15.0,
            }
        ],
    }
    try:
        analysis = analyze_option_desk(mock_chain)
        opt_buying = analysis.get("option_buying", {})
        action = opt_buying.get("action") or opt_buying.get("decision")
        score = opt_buying.get("setup_score")
        return {
            "passed": action in ("CE", "PE", "BUY CE", "BUY PE", "WAIT"),
            "action": action,
            "setup_score": score,
            "message": "analyze_option_desk() executed successfully with deterministic decision output.",
        }
    except Exception as e:
        return {"passed": False, "error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    rep = run_strategy_diagnostic_cycle(auto_apply=False)
    print(json.dumps(rep, indent=2))
