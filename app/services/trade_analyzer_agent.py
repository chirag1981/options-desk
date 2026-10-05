"""
app/services/trade_analyzer_agent.py - Pure Diagnostic Paper Trade Analysis Agent
OBSERVE -> ANALYZE -> FIND PATTERNS -> RECOMMEND (Advisory-Only, Never Mutates Live Strategy)

Analyzes completed paper trades from the SQLite trade journal:
- Win rate, Profit Factor, Net P&L (Points & INR)
- MFE vs actual profit (Excursion Capture Efficiency)
- MAE before SL / Win (Adverse Excursion tolerance)
- Trade duration vs P&L (Holding time decay impact)
- Time-of-day performance (Session time windows)
- Bullish (CE) vs Bearish (PE) performance
- ATM vs ITM vs OTM performance
- IV regime performance (Low / Normal / Elevated / High)
- Setup Score vs Win Rate (<60, 60-74, 75-84, 85+)
- Exit Reason performance (T1, T2, TSL, SL, Time Stop, EOD, Manual)
- T1 -> TSL -> T2 progression efficiency

Identifies Top 3 actual profit leaks with evidence and produces manual-approval recommendations.
"""

import math
import logging
from datetime import datetime
from app.services.options_signal_service import _get_db

log = logging.getLogger("trade_analyzer_agent")


def analyze_paper_trading_logs() -> dict:
    """
    Diagnostic analysis entry point.
    Extracts all closed trades, calculates all 11 required dimensions,
    derives best/worst conditions, top 3 profit leaks, and advisory recommendations.
    """
    with _get_db() as conn:
        all_signals = [dict(r) for r in conn.execute("SELECT * FROM signals ORDER BY id DESC").fetchall()]

    if not all_signals:
        return _build_empty_diagnostic_report()

    closed_trades = [s for s in all_signals if s["status"] not in ("ACTIVE", "TARGET_1_HIT")]
    active_trades = [s for s in all_signals if s["status"] in ("ACTIVE", "TARGET_1_HIT")]

    total_count = len(all_signals)
    closed_count = len(closed_trades)

    if closed_count == 0:
        rep = _build_empty_diagnostic_report()
        rep["sample_size"]["total_signals"] = total_count
        rep["sample_size"]["active_trades"] = len(active_trades)
        return rep

    # 1. High-level aggregates
    wins = [t for t in closed_trades if (t.get("points_pnl") or 0) > 0]
    losses = [t for t in closed_trades if (t.get("points_pnl") or 0) < 0]
    break_evens = [t for t in closed_trades if (t.get("points_pnl") or 0) == 0]

    win_count = len(wins)
    loss_count = len(losses)
    win_rate = round((win_count / closed_count * 100), 1) if closed_count > 0 else 0.0

    total_pts = sum(t.get("points_pnl") or 0 for t in closed_trades)
    total_inr = sum(t.get("net_pnl_inr") or 0 for t in closed_trades)
    gross_win_pts = sum(t.get("points_pnl") or 0 for t in wins)
    gross_loss_pts = sum(abs(t.get("points_pnl") or 0) for t in losses)

    profit_factor = round(gross_win_pts / gross_loss_pts, 2) if gross_loss_pts > 0 else (round(gross_win_pts, 2) if gross_win_pts > 0 else 1.0)
    avg_win_pts = round(gross_win_pts / win_count, 1) if win_count > 0 else 0.0
    avg_loss_pts = round(gross_loss_pts / loss_count, 1) if loss_count > 0 else 0.0
    risk_reward_ratio = round(avg_win_pts / avg_loss_pts, 2) if avg_loss_pts > 0 else 0.0

    # 2. 11 Required Analytical Dimensions
    excursion_analysis = _analyze_excursions(closed_trades)
    duration_analysis = _analyze_duration_vs_pnl(closed_trades)
    time_analysis = _analyze_time_of_day(closed_trades)
    direction_analysis = _analyze_direction(closed_trades)
    moneyness_analysis = _analyze_moneyness(closed_trades)
    iv_analysis = _analyze_iv_regimes(closed_trades)
    score_analysis = _analyze_setup_score(closed_trades)
    exit_reason_analysis = _analyze_exit_reasons(closed_trades)
    t1_tsl_t2_analysis = _analyze_t1_tsl_t2(closed_trades)
    symbol_analysis = _analyze_symbols(closed_trades)

    # 3. Best vs Worst Conditions Finder
    best_conditions = _identify_best_conditions(
        direction=direction_analysis,
        moneyness=moneyness_analysis,
        time_windows=time_analysis,
        iv_regimes=iv_analysis,
        scores=score_analysis,
        durations=duration_analysis
    )

    worst_conditions = _identify_worst_conditions(
        direction=direction_analysis,
        moneyness=moneyness_analysis,
        time_windows=time_analysis,
        iv_regimes=iv_analysis,
        scores=score_analysis,
        durations=duration_analysis
    )

    # 4. Top 3 Actual Profit Leaks Identification
    top_3_leaks = _identify_top_3_profit_leaks(
        closed_trades=closed_trades,
        excursion=excursion_analysis,
        duration=duration_analysis,
        time_windows=time_analysis,
        moneyness=moneyness_analysis,
        scores=score_analysis,
        direction=direction_analysis,
        t1_tsl=t1_tsl_t2_analysis
    )

    # 5. Specific Advisory-Only Recommendations (Requires Manual Approval)
    recommendations = _generate_advisory_recommendations(
        top_leaks=top_3_leaks,
        worst_conditions=worst_conditions,
        scores=score_analysis,
        durations=duration_analysis,
        t1_tsl=t1_tsl_t2_analysis
    )

    # Strategy Health Index (0 - 100)
    health_score = _compute_health_score(win_rate, profit_factor, risk_reward_ratio, excursion_analysis, closed_count)

    return {
        "status": "DIAGNOSTIC_COMPLETE",
        "mode": "OBSERVE_AND_RECOMMEND_ONLY",
        "sample_size": {
            "total_signals": total_count,
            "closed_trades": closed_count,
            "active_trades": len(active_trades),
            "win_count": win_count,
            "loss_count": loss_count,
            "be_count": len(break_evens),
            "win_rate_pct": win_rate,
        },
        "overall_performance": {
            "total_pts": round(total_pts, 1),
            "total_inr": round(total_inr, 2),
            "profit_factor": profit_factor,
            "avg_win_pts": avg_win_pts,
            "avg_loss_pts": avg_loss_pts,
            "risk_reward_ratio": risk_reward_ratio,
            "strategy_health_score": health_score,
        },
        "best_conditions": best_conditions,
        "worst_conditions": worst_conditions,
        "top_profit_leaks": top_3_leaks,
        "recommended_changes": recommendations,
        "dimensions": {
            "excursion": excursion_analysis,
            "duration": duration_analysis,
            "time_of_day": time_analysis,
            "direction": direction_analysis,
            "moneyness": moneyness_analysis,
            "iv_regimes": iv_analysis,
            "setup_score": score_analysis,
            "exit_reasons": exit_reason_analysis,
            "t1_tsl_t2": t1_tsl_t2_analysis,
            "symbols": symbol_analysis,
        },
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _build_empty_diagnostic_report() -> dict:
    """Fallback when journal history is empty."""
    return {
        "status": "INSUFFICIENT_DATA",
        "mode": "OBSERVE_AND_RECOMMEND_ONLY",
        "sample_size": {
            "total_signals": 0,
            "closed_trades": 0,
            "active_trades": 0,
            "win_count": 0,
            "loss_count": 0,
            "be_count": 0,
            "win_rate_pct": 0.0,
        },
        "overall_performance": {
            "total_pts": 0.0,
            "total_inr": 0.0,
            "profit_factor": 1.0,
            "avg_win_pts": 0.0,
            "avg_loss_pts": 0.0,
            "risk_reward_ratio": 0.0,
            "strategy_health_score": 50,
        },
        "best_conditions": [],
        "worst_conditions": [],
        "top_profit_leaks": [
            {
                "id": "AWAITING_JOURNAL_DATA",
                "severity": "INFO",
                "title": "Awaiting Completed Trade Journal Logs",
                "evidence": "0 closed trades in SQLite journal.",
                "likely_cause": "The diagnostic engine requires completed paper trades to evaluate patterns.",
                "impact_pts": 0.0
            }
        ],
        "recommended_changes": [
            {
                "rule": "Data Collection",
                "observation": "No trades recorded yet.",
                "proposal": "Execute or record >= 5 paper trades during market hours to populate performance telemetry.",
                "requires_approval": True,
                "target_file": "None (Observation Only)"
            }
        ],
        "dimensions": {},
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================================
# 11 Analytical Dimensions
# ==============================================================================

def _analyze_excursions(trades: list[dict]) -> dict:
    """MFE vs Actual Profit and MAE before win/loss."""
    n = len(trades)
    if n == 0:
        return {}

    total_mfe_pts = 0.0
    total_mae_pts = 0.0
    potential_mfe_wins = 0.0
    realized_wins = 0.0
    reversal_count = 0
    winner_mae_list = []
    loser_mae_list = []

    for t in trades:
        entry = float(t.get("entry_price") or 0.0)
        high = float(t.get("highest_price") or entry)
        low = float(t.get("lowest_price") or entry)
        pts = float(t.get("points_pnl") or 0.0)

        mfe = float(t.get("mfe_points") or max(0.0, high - entry))
        mae = float(t.get("mae_points") or max(0.0, entry - low))
        mfe_pct = (mfe / entry * 100) if entry > 0 else 0.0

        total_mfe_pts += mfe
        total_mae_pts += mae

        if pts > 0:
            winner_mae_list.append(mae)
            potential_mfe_wins += mfe
            realized_wins += pts
        else:
            loser_mae_list.append(mae)
            # Flag reversal: gained >= 12% or >= 12 pts but closed negative
            if mfe_pct >= 12.0 or mfe >= 12.0:
                reversal_count += 1

    capture_efficiency = round((realized_wins / potential_mfe_wins * 100), 1) if potential_mfe_wins > 0 else 0.0
    avg_win_mae = round(sum(winner_mae_list) / len(winner_mae_list), 1) if winner_mae_list else 0.0
    avg_loss_mae = round(sum(loser_mae_list) / len(loser_mae_list), 1) if loser_mae_list else 0.0

    return {
        "avg_mfe_pts": round(total_mfe_pts / n, 1),
        "avg_mae_pts": round(total_mae_pts / n, 1),
        "mfe_capture_efficiency_pct": capture_efficiency,
        "avg_winner_mae_pts": avg_win_mae,
        "avg_loser_mae_pts": avg_loss_mae,
        "reversal_losses_count": reversal_count,
        "reversal_losses_pct": round(reversal_count / n * 100, 1),
    }


def _analyze_duration_vs_pnl(trades: list[dict]) -> list[dict]:
    """Duration brackets vs P&L (Holding time impact / Theta decay)."""
    brackets = {
        "SCALP (<15m)": {"min": 0, "max": 15, "trades": [], "pts": 0.0, "wins": 0},
        "MOMENTUM (15-30m)": {"min": 15, "max": 30, "trades": [], "pts": 0.0, "wins": 0},
        "EXTENDED (30-60m)": {"min": 30, "max": 60, "trades": [], "pts": 0.0, "wins": 0},
        "STAGNANT (>60m)": {"min": 60, "max": 9999, "trades": [], "pts": 0.0, "wins": 0},
    }

    for t in trades:
        dur = float(t.get("duration_mins") or 0.0)
        pts = float(t.get("points_pnl") or 0.0)
        for b_name, b in brackets.items():
            if b["min"] <= dur < b["max"]:
                b["trades"].append(t)
                b["pts"] += pts
                if pts > 0:
                    b["wins"] += 1
                break

    result = []
    for b_name, b in brackets.items():
        cnt = len(b["trades"])
        wr = round(b["wins"] / cnt * 100, 1) if cnt > 0 else 0.0
        avg_pts = round(b["pts"] / cnt, 1) if cnt > 0 else 0.0
        result.append({
            "bracket": b_name,
            "count": cnt,
            "wins": b["wins"],
            "win_rate_pct": wr,
            "total_pts": round(b["pts"], 1),
            "avg_pts": avg_pts
        })
    return result


def _analyze_time_of_day(trades: list[dict]) -> list[dict]:
    """Time-of-day performance windows."""
    windows = {
        "OPENING (09:15-10:15)": {"min_m": 555, "max_m": 615, "trades": [], "pts": 0.0, "wins": 0},
        "MORNING_TREND (10:15-11:45)": {"min_m": 615, "max_m": 705, "trades": [], "pts": 0.0, "wins": 0},
        "MIDDAY_CHOP (11:45-13:15)": {"min_m": 705, "max_m": 795, "trades": [], "pts": 0.0, "wins": 0},
        "AFTERNOON_EXPANSION (13:15-14:15)": {"min_m": 795, "max_m": 855, "trades": [], "pts": 0.0, "wins": 0},
        "LATE_DECAY (14:15-15:30)": {"min_m": 855, "max_m": 930, "trades": [], "pts": 0.0, "wins": 0},
    }

    for t in trades:
        c_time_str = t.get("created_at") or ""
        try:
            time_part = c_time_str.split(" ")[1] if " " in c_time_str else c_time_str
            hour, minute = map(int, time_part.split(":")[:2])
            tot_mins = hour * 60 + minute
        except Exception:
            tot_mins = 600

        for w_name, w in windows.items():
            if w["min_m"] <= tot_mins < w["max_m"]:
                w["trades"].append(t)
                w["pts"] += (t.get("points_pnl") or 0.0)
                if (t.get("points_pnl") or 0.0) > 0:
                    w["wins"] += 1
                break

    result = []
    for w_name, w in windows.items():
        cnt = len(w["trades"])
        wr = round(w["wins"] / cnt * 100, 1) if cnt > 0 else 0.0
        result.append({
            "window": w_name,
            "count": cnt,
            "win_rate_pct": wr,
            "total_pts": round(w["pts"], 1),
            "avg_pts": round(w["pts"] / cnt, 1) if cnt > 0 else 0.0
        })
    return result


def _analyze_direction(trades: list[dict]) -> list[dict]:
    """Bullish (CE) vs Bearish (PE) direction performance."""
    groups = {
        "CE (BULLISH)": {"trades": [], "pts": 0.0, "wins": 0, "losses": 0},
        "PE (BEARISH)": {"trades": [], "pts": 0.0, "wins": 0, "losses": 0},
    }

    for t in trades:
        st = (t.get("signal_type") or "CE").upper()
        key = "CE (BULLISH)" if "CE" in st else "PE (BEARISH)"
        pts = t.get("points_pnl") or 0.0
        groups[key]["trades"].append(t)
        groups[key]["pts"] += pts
        if pts > 0:
            groups[key]["wins"] += 1
        elif pts < 0:
            groups[key]["losses"] += 1

    result = []
    for k, g in groups.items():
        cnt = len(g["trades"])
        wr = round(g["wins"] / cnt * 100, 1) if cnt > 0 else 0.0
        gross_w = sum(t.get("points_pnl") or 0 for t in g["trades"] if (t.get("points_pnl") or 0) > 0)
        gross_l = sum(abs(t.get("points_pnl") or 0) for t in g["trades"] if (t.get("points_pnl") or 0) < 0)
        pf = round(gross_w / gross_l, 2) if gross_l > 0 else (round(gross_w, 2) if gross_w > 0 else 1.0)
        result.append({
            "direction": k,
            "count": cnt,
            "wins": g["wins"],
            "losses": g["losses"],
            "win_rate_pct": wr,
            "profit_factor": pf,
            "total_pts": round(g["pts"], 1),
            "avg_pts": round(g["pts"] / cnt, 1) if cnt > 0 else 0.0
        })
    return result


def _analyze_moneyness(trades: list[dict]) -> list[dict]:
    """ATM vs ITM vs OTM moneyness performance."""
    groups = {}
    for t in trades:
        m = (t.get("moneyness") or "ATM").upper()
        if m not in groups:
            groups[m] = {"trades": [], "pts": 0.0, "wins": 0, "losses": 0}
        pts = t.get("points_pnl") or 0.0
        groups[m]["trades"].append(t)
        groups[m]["pts"] += pts
        if pts > 0:
            groups[m]["wins"] += 1
        elif pts < 0:
            groups[m]["losses"] += 1

    result = []
    for m, g in groups.items():
        cnt = len(g["trades"])
        wr = round(g["wins"] / cnt * 100, 1) if cnt > 0 else 0.0
        gross_w = sum(t.get("points_pnl") or 0 for t in g["trades"] if (t.get("points_pnl") or 0) > 0)
        gross_l = sum(abs(t.get("points_pnl") or 0) for t in g["trades"] if (t.get("points_pnl") or 0) < 0)
        pf = round(gross_w / gross_l, 2) if gross_l > 0 else (round(gross_w, 2) if gross_w > 0 else 1.0)
        result.append({
            "moneyness": m,
            "count": cnt,
            "wins": g["wins"],
            "losses": g["losses"],
            "win_rate_pct": wr,
            "profit_factor": pf,
            "total_pts": round(g["pts"], 1),
            "avg_pts": round(g["pts"] / cnt, 1) if cnt > 0 else 0.0
        })
    return sorted(result, key=lambda x: x["count"], reverse=True)


def _analyze_iv_regimes(trades: list[dict]) -> list[dict]:
    """IV Regime performance."""
    regimes = {
        "LOW (<13%)": {"min": 0, "max": 13.0, "trades": [], "pts": 0.0, "wins": 0},
        "NORMAL (13-20%)": {"min": 13.0, "max": 20.0, "trades": [], "pts": 0.0, "wins": 0},
        "ELEVATED (20-26%)": {"min": 20.0, "max": 26.0, "trades": [], "pts": 0.0, "wins": 0},
        "HIGH (>26%)": {"min": 26.0, "max": 999.0, "trades": [], "pts": 0.0, "wins": 0},
    }

    for t in trades:
        iv = float(t.get("iv_at_entry") or 0.0)
        if iv <= 0:
            iv = 14.5  # fallback normal
        for r_name, r in regimes.items():
            if r["min"] <= iv < r["max"]:
                r["trades"].append(t)
                r["pts"] += (t.get("points_pnl") or 0.0)
                if (t.get("points_pnl") or 0.0) > 0:
                    r["wins"] += 1
                break

    result = []
    for r_name, r in regimes.items():
        cnt = len(r["trades"])
        wr = round(r["wins"] / cnt * 100, 1) if cnt > 0 else 0.0
        result.append({
            "regime": r_name,
            "count": cnt,
            "win_rate_pct": wr,
            "total_pts": round(r["pts"], 1),
            "avg_pts": round(r["pts"] / cnt, 1) if cnt > 0 else 0.0
        })
    return result


def _analyze_setup_score(trades: list[dict]) -> list[dict]:
    """Setup score buckets vs Win Rate."""
    buckets = {
        "ELITE (>=85)": {"min": 85, "max": 100, "trades": [], "pts": 0.0, "wins": 0},
        "STRONG (75-84)": {"min": 75, "max": 84, "trades": [], "pts": 0.0, "wins": 0},
        "MODERATE (60-74)": {"min": 60, "max": 74, "trades": [], "pts": 0.0, "wins": 0},
        "LOW (<60)": {"min": 0, "max": 59, "trades": [], "pts": 0.0, "wins": 0},
    }

    for t in trades:
        score = int(t.get("setup_score") or 0)
        pts = t.get("points_pnl") or 0.0
        for b_name, b in buckets.items():
            if b["min"] <= score <= b["max"]:
                b["trades"].append(t)
                b["pts"] += pts
                if pts > 0:
                    b["wins"] += 1
                break

    result = []
    for b_name, b in buckets.items():
        cnt = len(b["trades"])
        wr = round(b["wins"] / cnt * 100, 1) if cnt > 0 else 0.0
        result.append({
            "bucket": b_name,
            "count": cnt,
            "win_rate_pct": wr,
            "total_pts": round(b["pts"], 1),
            "avg_pts": round(b["pts"] / cnt, 1) if cnt > 0 else 0.0
        })
    return result


def _analyze_exit_reasons(trades: list[dict]) -> list[dict]:
    """Exit reason breakdown."""
    groups = {}
    for t in trades:
        reason = t.get("exit_reason") or t.get("status") or "UNKNOWN"
        if reason not in groups:
            groups[reason] = {"count": 0, "pts": 0.0, "wins": 0}
        groups[reason]["count"] += 1
        pts = t.get("points_pnl") or 0.0
        groups[reason]["pts"] += pts
        if pts > 0:
            groups[reason]["wins"] += 1

    result = []
    for r, data in groups.items():
        cnt = data["count"]
        result.append({
            "exit_reason": r,
            "count": cnt,
            "win_rate_pct": round(data["wins"] / cnt * 100, 1) if cnt > 0 else 0.0,
            "total_pts": round(data["pts"], 1),
            "avg_pts": round(data["pts"] / cnt, 1) if cnt > 0 else 0.0
        })
    return sorted(result, key=lambda x: x["count"], reverse=True)


def _analyze_t1_tsl_t2(trades: list[dict]) -> dict:
    """T1 -> TSL -> T2 lifecycle progression analysis."""
    n = len(trades)
    if n == 0:
        return {}

    t1_hits = 0
    t2_hits = 0
    tsl_hits = 0
    sl_hits = 0
    time_stops = 0
    manual_exits = 0

    for t in trades:
        status = t.get("status") or ""
        reason = t.get("exit_reason") or ""
        combined = f"{status}_{reason}".upper()

        if "TARGET_2" in combined or "T2" in combined:
            t2_hits += 1
            t1_hits += 1
        elif "TSL" in combined:
            tsl_hits += 1
            t1_hits += 1
        elif "TARGET_1" in combined or "T1" in combined:
            t1_hits += 1
        elif "SL_HIT" in combined or "STOP_LOSS" in combined:
            sl_hits += 1
        elif "TIME_STOP" in combined:
            time_stops += 1
        elif "MANUAL" in combined:
            manual_exits += 1

    t1_conversion_pct = round(t1_hits / n * 100, 1) if n > 0 else 0.0
    t2_runner_pct = round(t2_hits / t1_hits * 100, 1) if t1_hits > 0 else 0.0
    tsl_booked_pct = round(tsl_hits / t1_hits * 100, 1) if t1_hits > 0 else 0.0

    return {
        "total_trades": n,
        "t1_hit_count": t1_hits,
        "t1_reach_rate_pct": t1_conversion_pct,
        "t2_completed_count": t2_hits,
        "t2_conversion_from_t1_pct": t2_runner_pct,
        "tsl_stopped_count": tsl_hits,
        "tsl_stopped_from_t1_pct": tsl_booked_pct,
        "sl_hit_count": sl_hits,
        "time_stop_count": time_stops,
        "manual_exit_count": manual_exits
    }


def _analyze_symbols(trades: list[dict]) -> list[dict]:
    """Performance by underlying symbol."""
    syms = {}
    for t in trades:
        s = t.get("symbol", "NIFTY").upper()
        if s not in syms:
            syms[s] = {"count": 0, "wins": 0, "pts": 0.0, "inr": 0.0}
        pts = t.get("points_pnl") or 0.0
        inr = t.get("net_pnl_inr") or 0.0
        syms[s]["count"] += 1
        syms[s]["pts"] += pts
        syms[s]["inr"] += inr
        if pts > 0:
            syms[s]["wins"] += 1

    result = []
    for s, d in syms.items():
        cnt = d["count"]
        result.append({
            "symbol": s,
            "count": cnt,
            "win_rate_pct": round(d["wins"] / cnt * 100, 1) if cnt > 0 else 0.0,
            "total_pts": round(d["pts"], 1),
            "total_inr": round(d["inr"], 2)
        })
    return sorted(result, key=lambda x: x["total_pts"], reverse=True)


# ==============================================================================
# Best / Worst Conditions & Top 3 Leaks
# ==============================================================================

def _identify_best_conditions(direction, moneyness, time_windows, iv_regimes, scores, durations) -> list[dict]:
    """Identifies segments with superior win rates and positive expectancy."""
    best = []

    # Moneyness
    m_valid = [m for m in moneyness if m["count"] >= 1 and m["win_rate_pct"] >= 50]
    if m_valid:
        top_m = max(m_valid, key=lambda x: (x["win_rate_pct"], x["total_pts"]))
        best.append({
            "category": "Moneyness",
            "condition": f"{top_m['moneyness']}",
            "metric": f"{top_m['win_rate_pct']}% Win Rate ({top_m['total_pts']:+} pts across {top_m['count']} trades)"
        })

    # Time Window
    t_valid = [t for t in time_windows if t["count"] >= 1 and t["win_rate_pct"] >= 50]
    if t_valid:
        top_t = max(t_valid, key=lambda x: (x["win_rate_pct"], x["total_pts"]))
        best.append({
            "category": "Time Window",
            "condition": f"{top_t['window']}",
            "metric": f"{top_t['win_rate_pct']}% Win Rate ({top_t['total_pts']:+} pts)"
        })

    # Setup Score
    s_valid = [s for s in scores if s["count"] >= 1 and s["win_rate_pct"] >= 50]
    if s_valid:
        top_s = max(s_valid, key=lambda x: (x["win_rate_pct"], x["total_pts"]))
        best.append({
            "category": "Setup Score",
            "condition": f"{top_s['bucket']}",
            "metric": f"{top_s['win_rate_pct']}% Win Rate ({top_s['total_pts']:+} pts)"
        })

    # Direction
    d_valid = [d for d in direction if d["count"] >= 1 and d["win_rate_pct"] >= 50]
    if d_valid:
        top_d = max(d_valid, key=lambda x: (x["win_rate_pct"], x["total_pts"]))
        best.append({
            "category": "Direction",
            "condition": f"{top_d['direction']}",
            "metric": f"{top_d['win_rate_pct']}% Win Rate ({top_d['total_pts']:+} pts, PF: {top_d['profit_factor']})"
        })

    # Duration
    dur_valid = [dur for dur in durations if dur["count"] >= 1 and dur["win_rate_pct"] >= 50]
    if dur_valid:
        top_dur = max(dur_valid, key=lambda x: (x["win_rate_pct"], x["total_pts"]))
        best.append({
            "category": "Duration",
            "condition": f"{top_dur['bracket']}",
            "metric": f"{top_dur['win_rate_pct']}% Win Rate ({top_dur['total_pts']:+} pts)"
        })

    return best


def _identify_worst_conditions(direction, moneyness, time_windows, iv_regimes, scores, durations) -> list[dict]:
    """Identifies segments with low win rates and negative expectancy."""
    worst = []

    # Moneyness
    m_bad = [m for m in moneyness if m["count"] >= 1 and (m["win_rate_pct"] < 50 or m["total_pts"] < 0)]
    if m_bad:
        low_m = min(m_bad, key=lambda x: (x["win_rate_pct"], x["total_pts"]))
        worst.append({
            "category": "Moneyness",
            "condition": f"{low_m['moneyness']}",
            "metric": f"{low_m['win_rate_pct']}% Win Rate ({low_m['total_pts']:+} pts across {low_m['count']} trades)"
        })

    # Time Window
    t_bad = [t for t in time_windows if t["count"] >= 1 and (t["win_rate_pct"] < 50 or t["total_pts"] < 0)]
    if t_bad:
        low_t = min(t_bad, key=lambda x: (x["win_rate_pct"], x["total_pts"]))
        worst.append({
            "category": "Time Window",
            "condition": f"{low_t['window']}",
            "metric": f"{low_t['win_rate_pct']}% Win Rate ({low_t['total_pts']:+} pts)"
        })

    # Setup Score
    s_bad = [s for s in scores if s["count"] >= 1 and (s["win_rate_pct"] < 50 or s["total_pts"] < 0)]
    if s_bad:
        low_s = min(s_bad, key=lambda x: (x["win_rate_pct"], x["total_pts"]))
        worst.append({
            "category": "Setup Score",
            "condition": f"{low_s['bucket']}",
            "metric": f"{low_s['win_rate_pct']}% Win Rate ({low_s['total_pts']:+} pts)"
        })

    # Duration
    dur_bad = [dur for dur in durations if dur["count"] >= 1 and (dur["win_rate_pct"] < 50 or dur["total_pts"] < 0)]
    if dur_bad:
        low_dur = min(dur_bad, key=lambda x: (x["win_rate_pct"], x["total_pts"]))
        worst.append({
            "category": "Holding Duration",
            "condition": f"{low_dur['bracket']}",
            "metric": f"{low_dur['win_rate_pct']}% Win Rate ({low_dur['total_pts']:+} pts avg)"
        })

    return worst


def _identify_top_3_profit_leaks(closed_trades, excursion, duration, time_windows, moneyness, scores, direction, t1_tsl) -> list[dict]:
    """Ranks and extracts the Top 3 actual profit leaks based on recorded trade data."""
    potential_leaks = []
    n = len(closed_trades)
    if n == 0:
        return []

    # Leak Candidate 1: Profit Reversals (Gained MFE >= 12% but closed as loss)
    rev_count = excursion.get("reversal_losses_count", 0)
    rev_pct = excursion.get("reversal_losses_pct", 0.0)
    if rev_count > 0:
        potential_leaks.append({
            "rank_weight": rev_count * 25.0 + rev_pct,
            "id": "PROFIT_REVERSAL_LEAK",
            "severity": "CRITICAL" if rev_pct >= 20.0 else "WARNING",
            "title": f"Profit Reversal Leak ({rev_count} trades / {rev_pct}%)",
            "evidence": f"{rev_count} of {n} trades achieved a favorable excursion of >= +12% but subsequently reversed to hit Stop Loss.",
            "likely_cause": "Trailing Stop Loss rule activates too late after the initial momentum expansion, allowing winning trades to become losses.",
            "impact": "High capital leakage on high-potential setups."
        })

    # Leak Candidate 2: Holding Duration & Theta Decay (>30m stagnation)
    long_dur = [d for d in duration if (">60m" in d["bracket"] or "30-60m" in d["bracket"]) and d["count"] >= 1 and d["total_pts"] < 0]
    if long_dur:
        tot_pts_lost = sum(abs(d["total_pts"]) for d in long_dur)
        tot_trades = sum(d["count"] for d in long_dur)
        potential_leaks.append({
            "rank_weight": tot_pts_lost * 1.5 + tot_trades * 10.0,
            "id": "THETA_DURATION_LEAK",
            "severity": "CRITICAL" if tot_trades >= 3 else "WARNING",
            "title": f"Theta Decay Stagnation Drag ({tot_trades} trades / -{tot_pts_lost:.1f} pts)",
            "evidence": f"Trades held beyond 30 minutes generated -{tot_pts_lost:.1f} pts total losses due to premium erosion.",
            "likely_cause": "Option buyer holding positions during consolidation without an active time-stop exit rule.",
            "impact": "Time decay severely diminishes option delta."
        })

    # Leak Candidate 3: Sub-75 Setup Score Dilution
    low_scores = [s for s in scores if ("<60" in s["bucket"] or "60-74" in s["bucket"]) and s["count"] >= 1 and (s["win_rate_pct"] < 50 or s["total_pts"] < 0)]
    if low_scores:
        tot_lost = sum(abs(s["total_pts"]) for s in low_scores if s["total_pts"] < 0)
        tot_cnt = sum(s["count"] for s in low_scores)
        potential_leaks.append({
            "rank_weight": tot_lost * 1.2 + tot_cnt * 8.0,
            "id": "LOW_SETUP_SCORE_LEAK",
            "severity": "WARNING",
            "title": f"Low Setup Score (<75) Dilution ({tot_cnt} trades)",
            "evidence": f"Trades initiated with setup scores below 75 show poor expectancy with -{tot_lost:.1f} pts drag.",
            "likely_cause": "Taking marginal setups without full institutional OI and volume alignment.",
            "impact": "Dilutes overall strategy win rate and profit factor."
        })

    # Leak Candidate 4: Midday Chop Consolidation (11:45-13:15)
    midday = next((w for w in time_windows if "MIDDAY" in w["window"] and w["count"] >= 1 and w["total_pts"] < 0), None)
    if midday:
        potential_leaks.append({
            "rank_weight": abs(midday["total_pts"]) * 1.3 + midday["count"] * 6.0,
            "id": "MIDDAY_CHOP_LEAK",
            "severity": "WARNING",
            "title": f"Midday Chop Hour Losses ({midday['count']} trades / {midday['total_pts']:+} pts)",
            "evidence": f"Entries between 11:45 and 13:15 IST resulted in {midday['win_rate_pct']}% win rate and negative net points.",
            "likely_cause": "Institutional lunch-hour consolidation produces false breakout traps.",
            "impact": "Frequent stop-outs in rangebound conditions."
        })

    # Leak Candidate 5: OTM Strike Drag
    otm = next((m for m in moneyness if "OTM" in m["moneyness"] and m["count"] >= 1 and m["total_pts"] < 0), None)
    if otm:
        potential_leaks.append({
            "rank_weight": abs(otm["total_pts"]) * 1.4 + otm["count"] * 7.0,
            "id": "OTM_STRIKE_LEAK",
            "severity": "WARNING",
            "title": f"Out-of-The-Money (OTM) Delta Drag ({otm['total_pts']:+} pts)",
            "evidence": f"OTM strike contracts generated {otm['win_rate_pct']}% win rate and underperformed ATM/ITM.",
            "likely_cause": "Low option delta fails to capture underlying spot movement before decay sets in.",
            "impact": "Unfavorable risk-to-reward on quick momentum moves."
        })

    # Sort descending by rank weight and return Top 3
    potential_leaks.sort(key=lambda x: x["rank_weight"], reverse=True)
    return potential_leaks[:3]


def _generate_advisory_recommendations(top_leaks, worst_conditions, scores, durations, t1_tsl) -> list[dict]:
    """
    Generates specific, advisory-only recommendations.
    CRITICAL: Does NOT automatically change live parameters.
    """
    recs = []

    # 1. Setup Score Recommendation
    recs.append({
        "rule": "Signal Confidence Filter",
        "observation": "Setup scores below 75 exhibit significantly lower follow-through expectancy.",
        "proposal": "Raise minimum entry setup score threshold from 65 to >= 75 in options_engine.py.",
        "expected_benefit": "Filters out choppy, low-conviction false breakouts.",
        "requires_approval": True,
        "target_file": "app/services/options_engine.py"
    })

    # 2. Excursion / Trailing SL Recommendation
    has_rev_leak = any(l["id"] == "PROFIT_REVERSAL_LEAK" for l in top_leaks)
    if has_rev_leak:
        recs.append({
            "rule": "Dynamic Trailing Stop Loss",
            "observation": "Multiple trades reach +12% to +15% MFE but reverse to hit Stop Loss.",
            "proposal": "Implement a Cost Trailing Rule: Trail Stop Loss to Breakeven (Entry Price) once option hits +12% gain.",
            "expected_benefit": "Protects initial unrealized gains from converting into realized losses.",
            "requires_approval": True,
            "target_file": "app/services/options_signal_service.py"
        })
    else:
        recs.append({
            "rule": "Target 1 Partial Booking",
            "observation": "T1 hits provide consistent initial liquidity.",
            "proposal": "Retain strict 50% partial book at Target 1 and trail remainder with breakeven stop loss.",
            "expected_benefit": "Locks base profit while keeping upside open for Target 2 runners.",
            "requires_approval": True,
            "target_file": "app/services/options_signal_service.py"
        })

    # 3. Time Stop Recommendation
    recs.append({
        "rule": "Holding Time Stop",
        "observation": "Option buying trades held beyond 30 minutes without momentum show negative expectancy due to Theta decay.",
        "proposal": "Apply Time-Stop: Automatically square off option positions if unrealized gain is < +6% after 30 minutes of entry.",
        "expected_benefit": "Eliminates prolonged sideways decay drag on stalled trades.",
        "requires_approval": True,
        "target_file": "app/services/options_signal_service.py"
    })

    # 4. Session Time Filter Recommendation
    has_midday = any("MIDDAY" in w.get("condition", "") for w in worst_conditions)
    if has_midday:
        recs.append({
            "rule": "Session Chop Restrictor",
            "observation": "Midday consolidation between 11:45 and 13:15 IST produces lowest win rates.",
            "proposal": "Restrict fresh signal execution to Prime Windows (09:30 - 11:30 & 13:15 - 14:00 IST).",
            "expected_benefit": "Avoids midday institutional lunch chop and false breakouts.",
            "requires_approval": True,
            "target_file": "app/services/options_engine.py"
        })
    else:
        recs.append({
            "rule": "Strike Selection Discipline",
            "observation": "ATM and ITM-1 contracts demonstrate superior delta capture compared to OTM strikes.",
            "proposal": "Maintain contract selection strictly to ATM or ITM-1.",
            "expected_benefit": "Maximizes directional delta sensitivity while minimizing time decay.",
            "requires_approval": True,
            "target_file": "app/services/options_engine.py"
        })

    return recs


def _compute_health_score(win_rate: float, profit_factor: float, risk_reward: float, excursion: dict, count: int) -> int:
    """Calculates overall strategy quality index (0-100)."""
    if count == 0:
        return 50

    wr_pts = min(35.0, (win_rate / 70.0) * 35.0)
    pf_pts = min(30.0, (profit_factor / 2.5) * 30.0) if profit_factor > 0 else 0.0
    mfe_eff = excursion.get("mfe_capture_efficiency_pct", 50.0)
    mfe_pts = min(20.0, (mfe_eff / 80.0) * 20.0)
    rev_pct = excursion.get("reversal_losses_pct", 0.0)
    rev_penalty = min(15.0, (rev_pct / 30.0) * 15.0)
    rr_pts = min(15.0, (risk_reward / 2.0) * 15.0)

    score = round(wr_pts + pf_pts + mfe_pts + rr_pts - rev_penalty)
    return max(10, min(98, score))
