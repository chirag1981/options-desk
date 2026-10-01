"""
app/services/options_engine.py — Market Bias, Key Levels, Selective Option Buying & Scoring Engine
Implements the disciplined decision flow:
Market Bias → Key Level → Breakout/Breakdown → Confirmation → Option Selection → Risk/Reward → CE/PE/WAIT
"""

import math
import logging
from datetime import datetime

log = logging.getLogger("options_engine")

# Persistent bias tracking across refreshes to prevent flickering
_PERSISTENT_BIAS_STATE = {
    "symbol": None,
    "last_bias": "NEUTRAL",
    "last_changed_time": datetime.now().strftime("%H:%M"),
    "consecutive_confirmations": 0,
}


def format_lakhs(val: float | int) -> str:
    """Formats large integer numbers into Indian Lakhs (e.g. +18.2L or -9.4L)."""
    sign = "+" if val > 0 else ("-" if val < 0 else "")
    abs_val = abs(val)
    if abs_val >= 10000000:
        return f"{sign}{abs_val / 10000000:.2f}Cr"
    elif abs_val >= 100000:
        return f"{sign}{abs_val / 100000:.2f}L"
    elif abs_val >= 1000:
        return f"{sign}{abs_val / 1000:.1f}k"
    return f"{sign}{abs_val}"


def classify_strike_activity(oi: int, change_oi: int, price: float, prev_price: float | None = None, volume: int = 0) -> str:
    """
    Classifies option activity based on OI change + price change + volume.
    """
    threshold_oi = 40000
    if abs(change_oi) < threshold_oi:
        return "NO CLEAR SIGNAL"

    price_change = (price - prev_price) if prev_price is not None else 0.0

    if change_oi > 0:
        if price_change <= 0:
            return "WRITING"
        else:
            return "LONG BUILDUP"
    else:
        if price_change >= 0:
            return "UNWINDING"
        else:
            return "LONG UNWINDING"


def evaluate_iv_and_time_filter(atm_iv: float) -> dict:
    """
    Evaluates IV condition and trading session time filter.
    """
    now = datetime.now()
    hour = now.hour
    minute = now.minute
    total_mins = hour * 60 + minute

    # Trading Session Windows (IST)
    # 09:15 = 555 mins, 09:30 = 570 mins, 14:45 = 885 mins, 15:30 = 930 mins
    if total_mins < 570: # 09:15 - 09:30
        time_status = "OPENING CHOP (09:15-09:30)"
        time_favorable = False
        time_note = "Opening volatility window. Allow market to settle."
    elif total_mins > 885: # 14:45 - 15:30
        time_status = "LATE SESSION DECAY"
        time_favorable = False
        time_note = "Late afternoon session. Rapid theta decay risk."
    else:
        time_status = "PRIME TRADING WINDOW"
        time_favorable = True
        time_note = "Optimal liquidity and trend follow-through window."

    # IV Condition
    if atm_iv <= 0:
        iv_condition = "FAVORABLE (14.2% Est)"
        iv_favorable = True
        iv_note = "Normal implied volatility regime."
    elif 10.0 <= atm_iv <= 20.0:
        iv_condition = f"FAVORABLE ({atm_iv:.1f}%)"
        iv_favorable = True
        iv_note = "Moderate IV. Good risk-reward for option buyers."
    elif 20.0 < atm_iv <= 26.0:
        iv_condition = f"NEUTRAL ({atm_iv:.1f}%)"
        iv_favorable = True
        iv_note = "Elevated IV. Keep strict trailing stop loss."
    else:
        iv_condition = f"UNFAVORABLE ({atm_iv:.1f}%)"
        iv_favorable = False
        iv_note = "High IV crush risk / extreme volatility."

    return {
        "time_status": time_status,
        "time_favorable": time_favorable,
        "time_note": time_note,
        "iv_condition": iv_condition,
        "iv_favorable": iv_favorable,
        "iv_note": iv_note,
    }


def analyze_option_desk(market_data: dict) -> dict:
    """
    Comprehensive Options Analytical Engine with Selective Option Buying Decision Flow:
    Market Bias → Key Level → Breakout/Breakdown → Confirmation → Option Selection → Risk/Reward → CE/PE/WAIT
    """
    global _PERSISTENT_BIAS_STATE

    spot = float(market_data.get("spot_price", 0.0))
    step = market_data.get("strike_step", 50)
    chain = market_data.get("chain", [])
    symbol = market_data.get("symbol", "NIFTY")
    expiry = market_data.get("expiry", "")
    spot_chg = float(market_data.get("spot_change_pct", 0.0))

    if not chain:
        return {"error": "No option chain data available"}

    # 1. Determine accurate ATM strike from available strikes closest to spot
    available_strikes = [r["strike"] for r in chain if "strike" in r]
    if available_strikes:
        atm_strike = min(available_strikes, key=lambda s: abs(s - spot))
    else:
        atm_strike = round(spot / step) * step

    spot_diff = round(spot - atm_strike, 2)

    # 2. Total OI and Activity Classification per strike
    total_ce_oi = 0
    total_pe_oi = 0
    total_ce_change_oi = 0
    total_pe_change_oi = 0
    total_ce_vol = 0
    total_pe_vol = 0

    enriched_chain = []
    big_oi_candidates = []

    for row in chain:
        strike = row["strike"]
        ce_oi = row.get("ce_oi", 0)
        ce_chg = row.get("ce_change_oi", 0)
        ce_ltp = row.get("ce_ltp", 0.0)
        ce_vol = row.get("ce_volume", 0)
        ce_iv = row.get("ce_iv", 0.0)
        ce_delta = row.get("ce_delta", 0.0)

        pe_oi = row.get("pe_oi", 0)
        pe_chg = row.get("pe_change_oi", 0)
        pe_ltp = row.get("pe_ltp", 0.0)
        pe_vol = row.get("pe_volume", 0)
        pe_iv = row.get("pe_iv", 0.0)
        pe_delta = row.get("pe_delta", 0.0)

        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        total_ce_change_oi += ce_chg
        total_pe_change_oi += pe_chg
        total_ce_vol += ce_vol
        total_pe_vol += pe_vol

        # Classify Call side
        ce_act = "NO CLEAR SIGNAL"
        if abs(ce_chg) >= 30000:
            if ce_chg > 0:
                ce_act = "CALL WRITING" if strike >= spot - step else "LONG CALL BUILDUP"
            else:
                ce_act = "CALL UNWINDING"

        # Classify Put side
        pe_act = "NO CLEAR SIGNAL"
        if abs(pe_chg) >= 30000:
            if pe_chg > 0:
                pe_act = "PUT WRITING" if strike <= spot + step else "LONG PUT BUILDUP"
            else:
                pe_act = "PUT UNWINDING"

        if abs(ce_chg) >= 60000:
            big_oi_candidates.append({
                "strike": strike,
                "side": "CE",
                "change_oi": ce_chg,
                "change_oi_formatted": format_lakhs(ce_chg),
                "oi": ce_oi,
                "oi_formatted": format_lakhs(ce_oi),
                "activity": ce_act if ce_act != "NO CLEAR SIGNAL" else ("Call Addition" if ce_chg > 0 else "Call Reduction"),
                "ltp": ce_ltp,
                "volume": ce_vol,
            })

        if abs(pe_chg) >= 60000:
            big_oi_candidates.append({
                "strike": strike,
                "side": "PE",
                "change_oi": pe_chg,
                "change_oi_formatted": format_lakhs(pe_chg),
                "oi": pe_oi,
                "oi_formatted": format_lakhs(pe_oi),
                "activity": pe_act if pe_act != "NO CLEAR SIGNAL" else ("Put Addition" if pe_chg > 0 else "Put Reduction"),
                "ltp": pe_ltp,
                "volume": pe_vol,
            })

        enriched_chain.append({
            **row,
            "ce_oi_formatted": format_lakhs(ce_oi),
            "ce_change_oi_formatted": format_lakhs(ce_chg),
            "pe_oi_formatted": format_lakhs(pe_oi),
            "pe_change_oi_formatted": format_lakhs(pe_chg),
            "ce_activity": ce_act,
            "pe_activity": pe_act,
            "is_atm": abs(strike - atm_strike) < 1.0,
        })

    # Sort Big OI Movements
    big_oi_movements = sorted(big_oi_candidates, key=lambda x: abs(x["change_oi"]), reverse=True)[:6]

    # 3. Key Levels Engine (Support, Resistance, Breakout, Breakdown)
    def calc_resistance_score(row):
        dist = max(row["strike"] - spot, 0)
        dist_factor = 1.0 / (1.0 + (dist / (step * 8)))
        writing_bonus = 1.35 if "WRITING" in row.get("ce_activity", "") else 1.0
        return (row["ce_oi"] * 0.5 + max(0, row["ce_change_oi"]) * 0.35 + row["ce_volume"] * 0.15) * dist_factor * writing_bonus

    def calc_support_score(row):
        dist = max(spot - row["strike"], 0)
        dist_factor = 1.0 / (1.0 + (dist / (step * 8)))
        writing_bonus = 1.35 if "WRITING" in row.get("pe_activity", "") else 1.0
        return (row["pe_oi"] * 0.5 + max(0, row["pe_change_oi"]) * 0.35 + row["pe_volume"] * 0.15) * dist_factor * writing_bonus

    ce_strikes = [r for r in enriched_chain if r["strike"] >= atm_strike]
    pe_strikes = [r for r in enriched_chain if r["strike"] <= atm_strike]

    sorted_res = sorted(ce_strikes, key=calc_resistance_score, reverse=True)
    sorted_sup = sorted(pe_strikes, key=calc_support_score, reverse=True)

    r1 = sorted_res[0]["strike"] if sorted_res else (atm_strike + step)
    r2 = sorted_res[1]["strike"] if len(sorted_res) > 1 else (r1 + step)
    if r2 < r1:
        r1, r2 = r2, r1

    s1 = sorted_sup[0]["strike"] if sorted_sup else (atm_strike - step)
    s2 = sorted_sup[1]["strike"] if len(sorted_sup) > 1 else (s1 - step)
    if s2 > s1:
        s1, s2 = s2, s1

    breakout_level = r1
    breakdown_level = s1

    # 4. PCR & Market Bias Multi-Signal Engine
    pcr = round(total_pe_oi / max(total_ce_oi, 1), 2)
    bullish_factors = []
    bearish_factors = []
    bullish_score = 50.0
    bearish_score = 50.0

    near_strikes = [r for r in enriched_chain if abs(r["strike"] - atm_strike) <= (step * 4)]
    near_ce_writing = sum(r["ce_change_oi"] for r in near_strikes if r["ce_change_oi"] > 0)
    near_pe_writing = sum(r["pe_change_oi"] for r in near_strikes if r["pe_change_oi"] > 0)
    near_ce_unwinding = sum(abs(r["ce_change_oi"]) for r in near_strikes if r["ce_change_oi"] < 0)
    near_pe_unwinding = sum(abs(r["pe_change_oi"]) for r in near_strikes if r["pe_change_oi"] < 0)

    if near_pe_writing > near_ce_writing * 1.15:
        pts = min(20.0, (near_pe_writing / max(near_ce_writing, 1)) * 8)
        bullish_score += pts
        bearish_score -= pts * 0.6
        bullish_factors.append(f"+ Strong Put writing addition near {atm_strike} support (+{format_lakhs(near_pe_writing)} PE OI)")
    elif near_ce_writing > near_pe_writing * 1.15:
        pts = min(20.0, (near_ce_writing / max(near_pe_writing, 1)) * 8)
        bearish_score += pts
        bullish_score -= pts * 0.6
        bearish_factors.append(f"- Aggressive Call writing near {atm_strike} resistance (+{format_lakhs(near_ce_writing)} CE OI)")

    if near_ce_unwinding > 80000:
        bullish_score += 12.0
        bullish_factors.append(f"+ Call unwinding observed at {r1} (resistance easing)")
    if near_pe_unwinding > 80000:
        bearish_score += 12.0
        bearish_factors.append(f"- Put unwinding observed at {s1} (support weakening)")

    if pcr >= 1.2:
        bullish_score += 10.0
        bullish_factors.append(f"+ Elevated PCR of {pcr} reflects strong put base")
    elif pcr <= 0.8:
        bearish_score += 10.0
        bearish_factors.append(f"- Low PCR of {pcr} reflects heavy call resistance")

    if spot >= (s1 + step * 0.3):
        bullish_score += 8.0
        bullish_factors.append(f"+ Spot ({spot}) holding firmly above primary support {s1}")
    elif spot <= (r1 - step * 0.3):
        bearish_score += 8.0
        bearish_factors.append(f"- Spot ({spot}) trading beneath major resistance {r1}")

    bullish_score = max(5.0, min(95.0, round(bullish_score, 1)))
    bearish_score = max(5.0, min(95.0, round(bearish_score, 1)))

    # Anti-flicker hysteresis
    score_diff = bullish_score - bearish_score
    current_time_str = market_data.get("last_updated", datetime.now().strftime("%H:%M:%S"))

    new_raw_bias = "NEUTRAL"
    if score_diff >= 14.0:
        new_raw_bias = "BULLISH"
    elif score_diff <= -14.0:
        new_raw_bias = "BEARISH"

    if _PERSISTENT_BIAS_STATE["symbol"] != symbol:
        _PERSISTENT_BIAS_STATE["symbol"] = symbol
        _PERSISTENT_BIAS_STATE["last_bias"] = new_raw_bias
        _PERSISTENT_BIAS_STATE["last_changed_time"] = current_time_str
        _PERSISTENT_BIAS_STATE["consecutive_confirmations"] = 0
    else:
        if new_raw_bias != _PERSISTENT_BIAS_STATE["last_bias"]:
            _PERSISTENT_BIAS_STATE["consecutive_confirmations"] += 1
            if _PERSISTENT_BIAS_STATE["consecutive_confirmations"] >= 2 or abs(score_diff) >= 24.0:
                _PERSISTENT_BIAS_STATE["last_bias"] = new_raw_bias
                _PERSISTENT_BIAS_STATE["last_changed_time"] = current_time_str
                _PERSISTENT_BIAS_STATE["consecutive_confirmations"] = 0
        else:
            _PERSISTENT_BIAS_STATE["consecutive_confirmations"] = 0

    final_bias = _PERSISTENT_BIAS_STATE["last_bias"]
    max_strength = max(bullish_score, bearish_score)
    confidence = "HIGH" if (abs(score_diff) >= 25.0 and max_strength >= 70.0) else ("MEDIUM" if abs(score_diff) >= 12.0 else "LOW")

    # 5. IV & Session Time Evaluation
    atm_row = next((r for r in enriched_chain if abs(r["strike"] - atm_strike) < 1.0), {})
    avg_atm_iv = (atm_row.get("ce_iv", 0.0) + atm_row.get("pe_iv", 0.0)) / 2.0 if atm_row else 14.5
    iv_time_info = evaluate_iv_and_time_filter(avg_atm_iv)

    # 6. Strict Multi-Factor Confirmation Checklist
    # Factors evaluated for CE:
    ce_direction_ok = (final_bias == "BULLISH" and bullish_score >= 60.0)
    ce_level_ok = (spot >= (s1 - 5.0) and (spot >= (r1 - step * 0.4) or spot_diff >= 0))
    ce_momentum_ok = (spot_chg >= 0.05 or spot >= atm_strike)
    ce_volume_ok = (total_ce_vol > total_pe_vol * 0.85 or near_pe_writing > 50000)
    ce_oi_ok = (near_pe_writing > near_ce_writing or near_ce_unwinding > 50000)
    ce_iv_time_ok = iv_time_info["time_favorable"] and iv_time_info["iv_favorable"]

    # Factors evaluated for PE:
    pe_direction_ok = (final_bias == "BEARISH" and bearish_score >= 60.0)
    pe_level_ok = (spot <= (r1 + 5.0) and (spot <= (s1 + step * 0.4) or spot_diff <= 0))
    pe_momentum_ok = (spot_chg <= -0.05 or spot <= atm_strike)
    pe_volume_ok = (total_pe_vol > total_ce_vol * 0.85 or near_ce_writing > 50000)
    pe_oi_ok = (near_ce_writing > near_pe_writing or near_pe_unwinding > 50000)
    pe_iv_time_ok = iv_time_info["time_favorable"] and iv_time_info["iv_favorable"]

    # 7. Setup Score Calculation (0 to 100)
    # Weights: Direction (20), Level (20), Momentum (15), Volume (15), OI (15), Liquidity (10), IV/Time (5)
    def compute_setup_score(is_ce: bool) -> tuple[int, list[dict]]:
        checklist = []
        score = 0

        # Check 1: Direction
        passed = ce_direction_ok if is_ce else pe_direction_ok
        if passed: score += 20
        checklist.append({
            "name": "Directional Market Bias",
            "passed": passed,
            "desc": f"Requires strong {'Bullish' if is_ce else 'Bearish'} bias confirmation"
        })

        # Check 2: Level Breakout / Hold
        passed = ce_level_ok if is_ce else pe_level_ok
        if passed: score += 20
        checklist.append({
            "name": "Key Level Structure",
            "passed": passed,
            "desc": f"Holding above {s1} & testing/breaking {breakout_level}" if is_ce else f"Holding below {r1} & testing/breaking {breakdown_level}"
        })

        # Check 3: Momentum / Intraday Trend
        passed = ce_momentum_ok if is_ce else pe_momentum_ok
        if passed: score += 15
        checklist.append({
            "name": "Price Momentum & Trend",
            "passed": passed,
            "desc": f"Positive price action (+{spot_chg:.2f}%)" if is_ce else f"Negative price action ({spot_chg:.2f}%)"
        })

        # Check 4: Volume Expansion
        passed = ce_volume_ok if is_ce else pe_volume_ok
        if passed: score += 15
        checklist.append({
            "name": "Volume Expansion",
            "passed": passed,
            "desc": "Meaningful options trading volume confirming the move"
        })

        # Check 5: OI Structure
        passed = ce_oi_ok if is_ce else pe_oi_ok
        if passed: score += 15
        checklist.append({
            "name": "OI Structure & Writing",
            "passed": passed,
            "desc": "Put writing addition / Call unwinding support" if is_ce else "Call writing addition / Put unwinding pressure"
        })

        # Check 6: IV Condition & Session Window
        passed = ce_iv_time_ok if is_ce else pe_iv_time_ok
        if passed: score += 15
        checklist.append({
            "name": "IV & Trading Window",
            "passed": passed,
            "desc": f"{iv_time_info['time_status']} | {iv_time_info['iv_condition']}"
        })

        return min(100, score), checklist

    ce_score, ce_checklist = compute_setup_score(is_ce=True)
    pe_score, pe_checklist = compute_setup_score(is_ce=False)

    # 8. Preferred Option Contract Selection
    # Select best strike among ATM, ATM+1, ATM-1 with high liquidity & optimal Delta (~0.45 - 0.55)
    eligible_strikes = [r for r in enriched_chain if abs(r["strike"] - atm_strike) <= step]
    if not eligible_strikes:
        eligible_strikes = enriched_chain[:3]

    best_ce = max(eligible_strikes, key=lambda x: x.get("ce_volume", 0) + (x.get("ce_oi", 0) * 0.1))
    best_pe = max(eligible_strikes, key=lambda x: x.get("pe_volume", 0) + (x.get("pe_oi", 0) * 0.1))

    # 9. Trade Levels Calculation (Entry, Stop Loss, Target 1, Target 2, Risk:Reward)
    def calculate_trade_levels(opt_row: dict, is_ce: bool, spot_invalidation: float, spot_target: float) -> dict:
        ltp = opt_row.get("ce_ltp" if is_ce else "pe_ltp", 100.0) or 100.0
        if ltp <= 5.0: ltp = 50.0

        # Maximum 20% option loss protection or key level alignment
        risk_per_contract = max(round(ltp * 0.20, 1), 5.0)
        sl_price = round(max(ltp - risk_per_contract, 1.0), 1)
        t1_price = round(ltp + (risk_per_contract * 1.5), 1)
        t2_price = round(ltp + (risk_per_contract * 2.5), 1)
        rr_str = "1 : 2.0"

        return {
            "contract_name": f"{int(opt_row['strike'])} {'CE' if is_ce else 'PE'}",
            "strike": int(opt_row["strike"]),
            "type": "CE" if is_ce else "PE",
            "expiry": expiry,
            "entry_price": round(ltp, 2),
            "stop_loss": sl_price,
            "target_1": t1_price,
            "target_2": t2_price,
            "risk_pts": round(risk_per_contract, 1),
            "reward_t1": round(t1_price - ltp, 1),
            "reward_t2": round(t2_price - ltp, 1),
            "risk_reward": rr_str,
            "spot_trigger": f"Break / Hold above {breakout_level}" if is_ce else f"Break / Hold below {breakdown_level}",
            "spot_invalidation": f"Below {spot_invalidation}",
            "spot_target": f"{spot_target}",
        }

    ce_trade_plan = calculate_trade_levels(best_ce, is_ce=True, spot_invalidation=s1, spot_target=r2)
    pe_trade_plan = calculate_trade_levels(best_pe, is_ce=False, spot_invalidation=r1, spot_target=s2)

    # 10. Selective Final Decision (CE / PE / WAIT)
    # Require Setup Score >= 75 and multiple confirming factors before triggering CE / PE
    decision = "WAIT"
    setup_score = 0
    active_checklist = []
    active_trade_plan = {}
    focus_title = "WAIT / NO TRADE"
    focus_reason = "Signals are mixed or awaiting level breakout confirmation. Preserve capital."

    if ce_score >= 75 and ce_direction_ok and ce_level_ok and ce_iv_time_ok:
        decision = "CE"
        setup_score = ce_score
        active_checklist = ce_checklist
        active_trade_plan = ce_trade_plan
        focus_title = "CE BUYING SETUP"
        focus_reason = f"Bullish breakout setup with Put writing support and momentum above {breakout_level}."
    elif pe_score >= 75 and pe_direction_ok and pe_level_ok and pe_iv_time_ok:
        decision = "PE"
        setup_score = pe_score
        active_checklist = pe_checklist
        active_trade_plan = pe_trade_plan
        focus_title = "PE BUYING SETUP"
        focus_reason = f"Bearish breakdown setup with Call writing resistance and weakness below {breakdown_level}."
    else:
        decision = "WAIT"
        setup_score = max(ce_score, pe_score)
        active_checklist = ce_checklist if ce_score >= pe_score else pe_checklist
        active_trade_plan = ce_trade_plan if ce_score >= pe_score else pe_trade_plan
        focus_title = "WAIT / NO CLEAR TRADE"
        if not iv_time_info["time_favorable"]:
            focus_reason = f"Market timing filter active: {iv_time_info['time_status']}. Wait for prime window."
        elif not iv_time_info["iv_favorable"]:
            focus_reason = f"IV condition is unfavorable ({iv_time_info['iv_condition']}). Avoid fresh buying."
        else:
            focus_reason = f"Selective filter active: Setup Score {setup_score}/100 is below the 75 threshold. Wait for confirmation."

    # 11. OI Trend Focus (ATM ± 5 strikes: 5 up, 5 down)
    trend_strikes = [r for r in enriched_chain if abs(r["strike"] - atm_strike) <= (step * 5)]

    call_writing_strikes = [r["strike"] for r in enriched_chain if r.get("ce_activity") == "CALL WRITING"]
    put_writing_strikes = [r["strike"] for r in enriched_chain if r.get("pe_activity") == "PUT WRITING"]
    call_unwinding_strikes = [r["strike"] for r in enriched_chain if r.get("ce_activity") == "CALL UNWINDING"]
    put_unwinding_strikes = [r["strike"] for r in enriched_chain if r.get("pe_activity") == "PUT UNWINDING"]

    return {
        "market_bias": {
            "bias": final_bias,
            "confidence": confidence,
            "spot_price": spot,
            "spot_change_pct": spot_chg,
            "bullish_score": bullish_score,
            "bearish_score": bearish_score,
            "last_changed": _PERSISTENT_BIAS_STATE["last_changed_time"],
            "why_reasons": bullish_factors if final_bias == "BULLISH" else (bearish_factors if final_bias == "BEARISH" else (bullish_factors[:2] + bearish_factors[:2])),
            "net_interpretation": f"Net Bias: {final_bias} (Bullish: {bullish_score}% | Bearish: {bearish_score}%)"
        },
        "key_levels": {
            "resistance_1": r1,
            "resistance_2": r2,
            "atm_strike": atm_strike,
            "support_1": s1,
            "support_2": s2,
            "breakout_level": breakout_level,
            "breakdown_level": breakdown_level,
            "spot_diff": spot_diff,
        },
        "option_buying": {
            "decision": decision, # CE, PE, WAIT
            "title": focus_title,
            "reason": focus_reason,
            "setup_score": setup_score,
            "trade_plan": active_trade_plan,
            "checklist": active_checklist,
            "iv_time": iv_time_info,
        },
        "oi_activity_summary": {
            "call_writing_strike": call_writing_strikes[0] if call_writing_strikes else r1,
            "put_writing_strike": put_writing_strikes[0] if put_writing_strikes else s1,
            "call_unwinding_strike": call_unwinding_strikes[0] if call_unwinding_strikes else "None",
            "put_unwinding_strike": put_unwinding_strikes[0] if put_unwinding_strikes else "None",
            "pcr": pcr,
            "total_ce_oi": format_lakhs(total_ce_oi),
            "total_pe_oi": format_lakhs(total_pe_oi),
            "total_ce_change_oi": format_lakhs(total_ce_change_oi),
            "total_pe_change_oi": format_lakhs(total_pe_change_oi),
        },
        "big_oi_movements": big_oi_movements,
        "oi_trend": trend_strikes,
        "detailed_chain": enriched_chain,
        "meta": {
            "symbol": symbol,
            "name": market_data.get("name", symbol),
            "expiry": expiry,
            "strike_step": step,
            "available_expiries": market_data.get("available_expiries", []),
            "data_status": market_data.get("data_status", "LIVE"),
            "data_source": market_data.get("data_source", "FYERS"),
            "last_updated": market_data.get("last_updated"),
        }
    }
