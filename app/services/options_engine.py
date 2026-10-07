"""
app/services/options_engine.py — Market Bias, Key Levels, Selective Option Buying & Scoring Engine
Implements the disciplined decision flow:
Market Bias -> Key Level -> Breakout/Breakdown -> Confirmation -> Option Selection -> Risk/Reward -> CE/PE/WAIT

Data Integrity & Statistical Standards:
- Thread-safe, per-symbol bias state tracking with consecutive confirmation validation
- Per-symbol configurable OI thresholds stored in ENGINE_CONFIG
- Real rolling IV percentile (stores daily ATM IV; returns None until >= 30 days exist)
- Explicit DTE calculation (DTE = 0.0 on expiry day; None if unparseable; no arbitrary defaults)
- Bid/Ask discipline: entries filled at Ask, exits at Bid, fails closed on missing bid/ask/LTP
- IV Safety filter: when IV is available, checks for extreme IV crush risk; when IV is unavailable, allows entry without blocking
- Setup score (0-100) calculated for telemetry and logging; directional entry gated by DIRECTION_BIAS_THRESHOLD (>= 60)
"""

import math
import logging
import threading
from datetime import datetime, date
from zoneinfo import ZoneInfo
from app.services.options_signal_service import (
    get_rolling_iv_percentile,
)

log = logging.getLogger("options_engine")

IST = ZoneInfo("Asia/Kolkata")

# Centralized Engine Configuration Parameters
ENGINE_CONFIG = {
    "MIN_SETUP_SCORE": 75,
    "REQUIRE_MIN_SETUP_SCORE": False,  # [APPROVAL] Current behavior default: False (score logged for telemetry; direction bias >= 60 gates entry)
    "REQUIRE_IV": False,               # [APPROVAL] Current behavior default: False (missing IV allows entry without blocking, tagged in data_quality)
    "GATE_ON_INTRADAY_MOMENTUM": False,# [APPROVAL] Current behavior default: False (intraday momentum logged without gating entries)
    "DIRECTION_BIAS_THRESHOLD": 60.0,
    "MOMENTUM_CHANGE_THRESHOLD": 0.05,
    "VOLUME_RATIO_THRESHOLD": 1.10,
    "IV_MIN_FAVORABLE": 8.0,
    "IV_MAX_FAVORABLE": 32.0,
    "IV_MAX_NEUTRAL": 45.0,
    "PRIME_WINDOW_START_MINS": 570,   # 09:30 IST
    "PRIME_WINDOW_END_MINS": 840,     # 14:00 IST
    "MARKET_SESSION_START_MINS": 555, # 09:15 IST
    "MARKET_SESSION_END_MINS": 930,   # 15:30 IST
    "CONSECUTIVE_CONFIRMATIONS_REQUIRED": 2,
    "OI_THRESHOLDS": {
        "DEFAULT": {
            "ACTIVITY": 40000,
            "WRITING": 30000,
            "UNWINDING": 50000,
            "BIG_MOVEMENT": 75000,
        },
        "NIFTY": {
            "ACTIVITY": 40000,
            "WRITING": 30000,
            "UNWINDING": 50000,
            "BIG_MOVEMENT": 75000,
        },
        "BANKNIFTY": {
            "ACTIVITY": 25000,
            "WRITING": 20000,
            "UNWINDING": 35000,
            "BIG_MOVEMENT": 50000,
        },
        "FINNIFTY": {
            "ACTIVITY": 20000,
            "WRITING": 15000,
            "UNWINDING": 25000,
            "BIG_MOVEMENT": 40000,
        },
        "MIDCPNIFTY": {
            "ACTIVITY": 30000,
            "WRITING": 25000,
            "UNWINDING": 40000,
            "BIG_MOVEMENT": 60000,
        },
        "SENSEX": {
            "ACTIVITY": 15000,
            "WRITING": 10000,
            "UNWINDING": 20000,
            "BIG_MOVEMENT": 30000,
        },
    }
}

# Thread-safe, per-symbol persistent bias tracking across refreshes
_BIAS_LOCK = threading.Lock()
_BIAS_STATES: dict[str, dict] = {}


def get_symbol_bias_state(symbol: str) -> dict:
    """Retrieves or initializes the persistent bias tracking state for a specific symbol."""
    with _BIAS_LOCK:
        if symbol not in _BIAS_STATES:
            _BIAS_STATES[symbol] = {
                "last_bias": "NEUTRAL",
                "pending_bias": "NEUTRAL",
                "last_changed_time": datetime.now(IST).strftime("%H:%M"),
                "consecutive_confirmations": 0,
            }
        return _BIAS_STATES[symbol]


def update_symbol_bias_state(symbol: str, raw_bias: str, score_diff: float) -> str:
    """
    Thread-safe update of bias state requiring consecutive confirmations
    before transitioning from one active bias to another.
    """
    with _BIAS_LOCK:
        if symbol not in _BIAS_STATES:
            _BIAS_STATES[symbol] = {
                "last_bias": "NEUTRAL",
                "pending_bias": "NEUTRAL",
                "last_changed_time": datetime.now(IST).strftime("%H:%M"),
                "consecutive_confirmations": 0,
            }

        state = _BIAS_STATES[symbol]
        req_confirmations = ENGINE_CONFIG.get("CONSECUTIVE_CONFIRMATIONS_REQUIRED", 2)

        if raw_bias == state["last_bias"]:
            state["consecutive_confirmations"] += 1
            state["pending_bias"] = raw_bias
        else:
            # Immediate flip allowed only on high-conviction breakout (score_diff >= 35)
            if abs(score_diff) >= 35.0 and raw_bias != "NEUTRAL":
                state["last_bias"] = raw_bias
                state["pending_bias"] = raw_bias
                state["last_changed_time"] = datetime.now(IST).strftime("%H:%M")
                state["consecutive_confirmations"] = 1
            else:
                # Track pending transitions
                if raw_bias == state["pending_bias"]:
                    state["consecutive_confirmations"] += 1
                    if state["consecutive_confirmations"] >= req_confirmations:
                        state["last_bias"] = raw_bias
                        state["last_changed_time"] = datetime.now(IST).strftime("%H:%M")
                else:
                    state["pending_bias"] = raw_bias
                    state["consecutive_confirmations"] = 1

        return state["last_bias"]


# Thread-safe rolling spot price history for Pullback & Recovery Detection
_PRICE_HISTORY_LOCK = threading.Lock()
_SYMBOL_PRICE_HISTORY: dict[str, list[float]] = {}


def get_symbol_price_history(symbol: str) -> list[float]:
    """Retrieves current rolling price history for a symbol."""
    with _PRICE_HISTORY_LOCK:
        return list(_SYMBOL_PRICE_HISTORY.get((symbol or "").upper(), []))


def clear_symbol_price_history(symbol: str | None = None):
    """Clears price history for testing or session reset."""
    with _PRICE_HISTORY_LOCK:
        if symbol:
            _SYMBOL_PRICE_HISTORY.pop(symbol.upper(), None)
        else:
            _SYMBOL_PRICE_HISTORY.clear()


def update_symbol_pullback_recovery(
    symbol: str,
    spot: float,
    is_bullish: bool,
    is_bearish: bool,
    custom_history: list[float] | None = None,
    update_state: bool = False,
    momentum_signal: str = "NEUTRAL",
    price_signal: str = "NEUTRAL",
    volume_signal: str = "NEUTRAL",
    spot_chg: float = 0.0,
    strike_step: float = 50.0,
) -> dict:
    """
    Evaluates whether the current market movement represents a valid PULLBACK_RECOVERY,
    a FRESH_HIGH_NO_PULLBACK, a FRESH_LOW_NO_PULLBACK, a LOW_EXPANSION_OR_CHOP condition,
    or a PULLBACK_IN_PROGRESS.
    
    Anti-Chase Guard & Market Expansion:
    - In a bullish trend: If price is at a fresh peak/high without a preceding pullback dip,
      entry is blocked (FRESH_HIGH_NO_PULLBACK).
    - In a bearish trend: If price is at a fresh trough/low without a preceding rebound,
      entry is blocked (FRESH_LOW_NO_PULLBACK).
    - Recovery requires:
        1. Meaningful pullback / rebound detected.
        2. Price actively turning back in the trend direction with follow-through (not just spot > trough).
        3. Market Expansion confirmed (not compressed/choppy range or frequent direction reversals).
        4. Momentum and Price confirmed in the trend direction.
    """
    sym = (symbol or "DEFAULT").upper()
    with _PRICE_HISTORY_LOCK:
        if custom_history is not None:
            hist = list(custom_history)
        else:
            if sym not in _SYMBOL_PRICE_HISTORY:
                _SYMBOL_PRICE_HISTORY[sym] = []
            hist = _SYMBOL_PRICE_HISTORY[sym]
            if update_state or not hist:
                hist.append(spot)
                if len(hist) > 15:
                    hist.pop(0)

        pullback_occurred = False
        recovery_confirmed = False
        expansion_confirmed = True
        is_choppy = False
        anti_chase = "BLOCK"
        setup_type = "NORMAL_CONTINUATION"
        reason = ""

        # Evaluate Chop and Market Expansion on price history
        if len(hist) >= 3:
            range_pts = max(hist) - min(hist)
            avg_spot = sum(hist) / len(hist) if hist else spot
            range_pct = (range_pts / avg_spot * 100) if avg_spot > 0 else 0.0
            mom_threshold = ENGINE_CONFIG.get("MOMENTUM_CHANGE_THRESHOLD", 0.05)

            # Reversals / oscillation
            deltas = [hist[i] - hist[i - 1] for i in range(1, len(hist))]
            non_zero_deltas = [d for d in deltas if abs(d) >= 1e-4]
            sign_flips = sum(1 for i in range(1, len(non_zero_deltas)) if (non_zero_deltas[i] > 0) != (non_zero_deltas[i - 1] > 0))

            # Narrow range / compressed price action with weak momentum
            if (range_pct < 0.04 or range_pts < min(1.5, strike_step * 0.03)) and abs(spot_chg) < mom_threshold:
                is_choppy = True
                expansion_confirmed = False

            # Frequent direction reversals (alternating chop)
            if len(non_zero_deltas) >= 3:
                if sign_flips >= len(non_zero_deltas) - 1 and (abs(spot_chg) < mom_threshold * 2 or range_pct < 0.08):
                    is_choppy = True
                    expansion_confirmed = False
                elif sign_flips >= 3 and abs(hist[-1] - hist[0]) < (range_pts * 0.3):
                    is_choppy = True
                    expansion_confirmed = False

            # Weak momentum
            if abs(spot_chg) < mom_threshold:
                expansion_confirmed = False

            if is_choppy:
                setup_type = "LOW_EXPANSION_OR_CHOP"
                reason = "LOW_EXPANSION_OR_CHOP"

        if len(hist) >= 3:
            if is_bullish:
                peak = max(hist)
                peak_idx = hist.index(peak)
                # Check if current spot is at fresh high with no preceding dip after a peak
                if peak_idx == len(hist) - 1:
                    pullback_occurred = False
                    recovery_confirmed = False
                    anti_chase = "BLOCK"
                    setup_type = "FRESH_HIGH_NO_PULLBACK"
                    reason = "FRESH_HIGH_NO_PULLBACK"
                else:
                    post_peak = hist[peak_idx:]
                    trough = min(post_peak)
                    trough_idx = hist.index(trough, peak_idx)
                    
                    if trough < peak:
                        pullback_occurred = True
                    
                    # Meaningful pullback and recovery with follow-through
                    is_moving_up = (spot > trough and (trough_idx < len(hist) - 1 and spot >= hist[-2]))
                    has_follow_through = (
                        is_moving_up
                        and (spot - trough) >= min(0.5, (peak - trough) * 0.15)
                        and momentum_signal == "BULLISH"
                        and price_signal == "BULLISH"
                    )

                    if pullback_occurred and has_follow_through:
                        if is_choppy or not expansion_confirmed:
                            recovery_confirmed = False
                            anti_chase = "BLOCK"
                            setup_type = "LOW_EXPANSION_OR_CHOP"
                            reason = "LOW_EXPANSION_OR_CHOP"
                        else:
                            recovery_confirmed = True
                            anti_chase = "PASS"
                            setup_type = "PULLBACK_RECOVERY"
                            reason = "PULLBACK_RECOVERY"
                    else:
                        recovery_confirmed = False
                        anti_chase = "BLOCK"
                        if not pullback_occurred:
                            setup_type = "FRESH_HIGH_NO_PULLBACK"
                            reason = "FRESH_HIGH_NO_PULLBACK"
                        elif is_choppy:
                            setup_type = "LOW_EXPANSION_OR_CHOP"
                            reason = "LOW_EXPANSION_OR_CHOP"
                        else:
                            setup_type = "PULLBACK_IN_PROGRESS"
                            reason = "PULLBACK_IN_PROGRESS"

            elif is_bearish:
                trough = min(hist)
                trough_idx = hist.index(trough)
                if trough_idx == len(hist) - 1:
                    pullback_occurred = False
                    recovery_confirmed = False
                    anti_chase = "BLOCK"
                    setup_type = "FRESH_LOW_NO_PULLBACK"
                    reason = "FRESH_LOW_NO_PULLBACK"
                else:
                    post_trough = hist[trough_idx:]
                    bounce = max(post_trough)
                    bounce_idx = hist.index(bounce, trough_idx)
                    
                    if bounce > trough:
                        pullback_occurred = True
                    
                    # Meaningful rebound and downward recovery with follow-through
                    is_moving_down = (spot < bounce and (bounce_idx < len(hist) - 1 and spot <= hist[-2]))
                    has_follow_through = (
                        is_moving_down
                        and (bounce - spot) >= min(0.5, (bounce - trough) * 0.15)
                        and momentum_signal == "BEARISH"
                        and price_signal == "BEARISH"
                    )

                    if pullback_occurred and has_follow_through:
                        if is_choppy or not expansion_confirmed:
                            recovery_confirmed = False
                            anti_chase = "BLOCK"
                            setup_type = "LOW_EXPANSION_OR_CHOP"
                            reason = "LOW_EXPANSION_OR_CHOP"
                        else:
                            recovery_confirmed = True
                            anti_chase = "PASS"
                            setup_type = "PULLBACK_RECOVERY"
                            reason = "PULLBACK_RECOVERY"
                    else:
                        recovery_confirmed = False
                        anti_chase = "BLOCK"
                        if not pullback_occurred:
                            setup_type = "FRESH_LOW_NO_PULLBACK"
                            reason = "FRESH_LOW_NO_PULLBACK"
                        elif is_choppy:
                            setup_type = "LOW_EXPANSION_OR_CHOP"
                            reason = "LOW_EXPANSION_OR_CHOP"
                        else:
                            setup_type = "PULLBACK_IN_PROGRESS"
                            reason = "PULLBACK_IN_PROGRESS"
            else:
                if is_choppy:
                    setup_type = "LOW_EXPANSION_OR_CHOP"
                    reason = "LOW_EXPANSION_OR_CHOP"
                    anti_chase = "BLOCK"
                else:
                    setup_type = "NORMAL_CONTINUATION"
                    anti_chase = "PASS"
        else:
            if custom_history is not None:
                if is_bullish:
                    setup_type = "FRESH_HIGH_NO_PULLBACK"
                    reason = "FRESH_HIGH_NO_PULLBACK"
                    anti_chase = "BLOCK"
                elif is_bearish:
                    setup_type = "FRESH_LOW_NO_PULLBACK"
                    reason = "FRESH_LOW_NO_PULLBACK"
                    anti_chase = "BLOCK"
                else:
                    setup_type = "NORMAL_CONTINUATION"
                    anti_chase = "PASS"
            else:
                pullback_occurred = True
                recovery_confirmed = True
                expansion_confirmed = (momentum_signal in ("BULLISH", "BEARISH"))
                is_choppy = not expansion_confirmed
                anti_chase = "PASS"
                setup_type = "PULLBACK_RECOVERY"

        return {
            "setup_type": setup_type,
            "pullback_occurred": pullback_occurred,
            "recovery_confirmed": recovery_confirmed,
            "expansion_confirmed": expansion_confirmed,
            "is_choppy": is_choppy,
            "anti_chase": anti_chase,
            "reason": reason,
        }


def get_symbol_oi_thresholds(symbol: str) -> dict:
    """Returns the specific OI thresholds for the given index symbol."""
    sym = (symbol or "DEFAULT").upper()
    thresholds = ENGINE_CONFIG["OI_THRESHOLDS"].get(sym, ENGINE_CONFIG["OI_THRESHOLDS"]["DEFAULT"])
    return thresholds


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


def classify_strike_activity(oi: int, change_oi: int, price: float, prev_price: float | None = None, volume: int = 0, symbol: str = "NIFTY") -> str:
    """
    Classifies option activity based on OI change + price change + volume using per-symbol thresholds.
    """
    thresholds = get_symbol_oi_thresholds(symbol)
    threshold_oi = thresholds["ACTIVITY"]
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


def evaluate_iv_and_time_filter(atm_iv: float | None, now_dt: datetime | None = None) -> dict:
    """
    Evaluates IV condition and trading session time filter in Asia/Kolkata timezone.
    When IV is available, checks for favorable (10-20%), neutral (20-26%), or unfavorable (>26% crush risk).
    When IV is missing/unavailable from broker, passes gracefully without blocking live entry.
    """
    if now_dt is None:
        now_dt = datetime.now(IST)

    total_mins = now_dt.hour * 60 + now_dt.minute

    # Trading Session Windows (IST)
    if total_mins < ENGINE_CONFIG["PRIME_WINDOW_START_MINS"]:  # 09:15 - 09:30
        time_status = "OPENING CHOP (09:15-09:30)"
        time_favorable = False
        time_note = "Opening volatility window. Allow market structure to settle."
    elif total_mins >= ENGINE_CONFIG["PRIME_WINDOW_END_MINS"]:  # Post 14:00
        time_status = "LATE SESSION THETA DECAY (Post 14:00)"
        time_favorable = False
        time_note = "Late afternoon session. Exponential Theta decay and squaring-off chop. No fresh buying."
    else:
        time_status = "PRIME TRADING WINDOW (09:30-14:00)"
        time_favorable = True
        time_note = "Optimal liquidity and trend follow-through window for option buyers."

    # IV Condition — Safety Filter (IV must NOT block CE/PE entry when unavailable)
    if atm_iv is None or atm_iv <= 0.01:
        iv_condition = "IV_UNAVAILABLE"
        iv_favorable = True
        iv_note = "Real IV data unavailable from broker; IV safety filter active without blocking trade."
    elif ENGINE_CONFIG["IV_MIN_FAVORABLE"] <= atm_iv <= ENGINE_CONFIG["IV_MAX_FAVORABLE"]:
        iv_condition = f"FAVORABLE ({atm_iv:.1f}%)"
        iv_favorable = True
        iv_note = "Moderate IV. Good risk-reward for option buyers."
    elif ENGINE_CONFIG["IV_MAX_FAVORABLE"] < atm_iv <= ENGINE_CONFIG["IV_MAX_NEUTRAL"]:
        iv_condition = f"NEUTRAL ({atm_iv:.1f}%)"
        iv_favorable = True
        iv_note = "Elevated IV. Keep strict trailing stop loss."
    else:
        iv_condition = f"UNFAVORABLE ({atm_iv:.1f}%)"
        iv_favorable = False
        iv_note = "Extreme IV crush risk or outside safety parameters."

    return {
        "time_status": time_status,
        "time_favorable": time_favorable,
        "time_note": time_note,
        "iv_condition": iv_condition,
        "iv_favorable": iv_favorable,
        "iv_note": iv_note,
    }


def compute_trending_oi_metrics(
    enriched_chain: list[dict],
    spot: float,
    step: float,
    atm_strike: float,
    num_strikes_each_side: int = 5,
) -> dict:
    """
    Computes Trending OI metrics over ATM ± 5 strikes (5 up, 5 down, 11 strikes total)
    matching Oi Pulse methodology:
      - Diff. in OI = Chng. in Put OI - Chng. in Call OI
      - Bullish: diff_pct = + (Put_Chg - Call_Chg) / max(Put_Chg, 1) * 100
      - Bearish: diff_pct = - (Call_Chg - Put_Chg) / max(Call_Chg, 1) * 100
      - Strength Dots:
          < 30%: 0 dots, gray capsule (weak sentiment)
          30% - 39%: 1 dot, gray capsule
          40% - 59%: 2 dots, active color (>= 40% threshold for conviction)
          >= 60%: 3 dots, active color (strong conviction)
      - Net PCR = Chng. in Put OI / max(Chng. in Call OI, 1)
    """
    if step <= 0:
        step = 50.0

    target_strikes = [round(atm_strike + i * step, 2) for i in range(-num_strikes_each_side, num_strikes_each_side + 1)]

    # Filter rows matching target strikes
    selected_rows = [r for r in enriched_chain if any(abs(r["strike"] - s) < 1.0 for s in target_strikes)]
    selected_rows.sort(key=lambda x: x["strike"])

    total_ce_change_oi = sum(int(r.get("ce_change_oi", 0) or 0) for r in selected_rows)
    total_pe_change_oi = sum(int(r.get("pe_change_oi", 0) or 0) for r in selected_rows)
    total_ce_oi = sum(int(r.get("ce_oi", 0) or 0) for r in selected_rows)
    total_pe_oi = sum(int(r.get("pe_oi", 0) or 0) for r in selected_rows)

    diff_oi = total_pe_change_oi - total_ce_change_oi

    # Calculate difference percentage and sentiment matching Oi Pulse formula
    if total_pe_change_oi >= total_ce_change_oi:
        denom = max(total_pe_change_oi, 1)
        raw_pct = ((total_pe_change_oi - total_ce_change_oi) / denom) * 100.0
        diff_pct = round(raw_pct)
        sentiment = "Bullish"
    else:
        denom = max(total_ce_change_oi, 1)
        raw_pct = -((total_ce_change_oi - total_pe_change_oi) / denom) * 100.0
        diff_pct = round(raw_pct)
        sentiment = "Bearish"

    abs_pct = abs(diff_pct)
    if abs_pct < 30:
        dots = 0
        strength_class = "strength-weak"
        strength_status = "WEAK"
    elif abs_pct < 40:
        dots = 1
        strength_class = "strength-weak"
        strength_status = "WEAK"
    elif abs_pct < 60:
        dots = 2
        strength_class = "strength-bullish" if diff_pct > 0 else "strength-bearish"
        strength_status = "BULLISH" if diff_pct > 0 else "BEARISH"
    else:
        dots = 3
        strength_class = "strength-bullish" if diff_pct > 0 else "strength-bearish"
        strength_status = "STRONG_BULLISH" if diff_pct > 0 else "STRONG_BEARISH"

    net_pcr = round(total_pe_change_oi / max(total_ce_change_oi, 1), 2) if total_ce_change_oi > 0 else (1.0 if total_pe_change_oi == 0 else 9.99)
    selected_strike_list = [r["strike"] for r in selected_rows]

    return {
        "atm_strike": atm_strike,
        "strike_step": step,
        "num_strikes": len(selected_rows),
        "selected_strikes": selected_strike_list,
        "total_ce_change_oi": total_ce_change_oi,
        "total_pe_change_oi": total_pe_change_oi,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "diff_oi": diff_oi,
        "diff_pct": diff_pct,
        "dots": dots,
        "strength_class": strength_class,
        "strength_status": strength_status,
        "net_pcr": net_pcr,
        "sentiment": sentiment,
        "strikes_data": selected_rows,
    }


def compute_morning_trade_bias(
    market_data: dict,
    spot: float,
    step: float,
    atm_strike: float,
    pcr: float,
    bullish_score: float,
    bearish_score: float,
    r1: float,
    s1: float,
    enriched_chain: list[dict] | None = None,
) -> dict:
    """
    Computes deterministic Morning Trade Bias and actionable directions based on:
    1. Previous Day Data (PDH, PDL, PDC)
    2. Central Pivot Range (CPR: Pivot, TC, BC) & Standard Pivots (R1, S1, R2, S2)
    3. Opening Gap analysis (Gap Up, Gap Down, Flat relative to PDC)
    4. Current Spot position vs PDH / PDL / CPR boundaries
    5. Morning Option Flow (PCR, ATM writing skew)
    """
    symbol = market_data.get("symbol", "NIFTY").upper()
    spot_chg = float(market_data.get("spot_change_pct", 0.0))

    # Extract or estimate Previous Day OHLC
    pdc = float(market_data.get("prev_close") or market_data.get("prev_close_price") or 0.0)
    if pdc <= 0:
        pdc = round(spot / (1.0 + (spot_chg / 100.0)), 2) if spot > 0 and (1.0 + (spot_chg / 100.0)) > 0 else spot

    open_price = float(market_data.get("open_price") or market_data.get("open") or 0.0)
    if open_price <= 0:
        open_price = round(pdc * (1.0 + (spot_chg / 100.0)), 2) if pdc > 0 else spot

    pdh = float(market_data.get("prev_high") or market_data.get("pdh") or 0.0)
    if pdh <= 0:
        pdh = max(round(pdc + step * 1.5, 2), round(r1, 2)) if pdc > 0 else spot + step

    pdl = float(market_data.get("prev_low") or market_data.get("pdl") or 0.0)
    if pdl <= 0:
        pdl = min(round(pdc - step * 1.5, 2), round(s1, 2)) if pdc > 0 else spot - step

    # Ensure valid ordering
    if pdh < pdl:
        pdh, pdl = pdl, pdh
    if pdh == pdl:
        pdh += step
        pdl -= step

    # 1. Calculate Central Pivot Range (CPR)
    pivot = round((pdh + pdl + pdc) / 3.0, 2)
    bc = round((pdh + pdl) / 2.0, 2)
    tc = round((pivot - bc) + pivot, 2)
    cpr_top = round(max(tc, bc), 2)
    cpr_bottom = round(min(tc, bc), 2)
    cpr_width = round(abs(tc - bc), 2)
    cpr_width_pct = round((cpr_width / pivot) * 100.0, 2) if pivot > 0 else 0.0

    if cpr_width_pct <= 0.25:
        cpr_type = "NARROW (TRENDING BIAS)"
    elif cpr_width_pct >= 0.55:
        cpr_type = "WIDE (RANGEBOUND / REVERSAL BIAS)"
    else:
        cpr_type = "AVERAGE"

    # Classical Pivots
    cpr_r1 = round(2 * pivot - pdl, 2)
    cpr_s1 = round(2 * pivot - pdh, 2)
    cpr_r2 = round(pivot + (pdh - pdl), 2)
    cpr_s2 = round(pivot - (pdh - pdl), 2)

    # 2. Opening Gap Analysis
    gap_pts = round(open_price - pdc, 2)
    gap_pct = round((gap_pts / pdc) * 100.0, 2) if pdc > 0 else 0.0
    if gap_pct >= 0.15:
        gap_type = "GAP UP"
    elif gap_pct <= -0.15:
        gap_type = "GAP DOWN"
    else:
        gap_type = "FLAT OPEN"

    # 3. Decision Matrix
    is_above_pdh = spot >= (pdh - (step * 0.1))
    is_above_cpr = spot >= cpr_top
    is_below_pdl = spot <= (pdl + (step * 0.1))
    is_below_cpr = spot <= cpr_bottom

    why_reasons = []

    if is_above_pdh or (is_above_cpr and (spot_chg >= 0.10 or bullish_score >= 60.0) and pcr >= 0.95) or (is_above_cpr and spot_chg >= 0.25):
        direction = "BULLISH"
        action = "BUY CE"
        badge = "BULLISH (BUY CE)"
        badge_class = "bullish"
        suggested_strike = atm_strike if spot <= atm_strike + (step * 0.3) else atm_strike + step
        suggested_contract = f"{symbol} {int(suggested_strike)} CE"

        if is_above_pdh:
            setup_title = "PDH Breakout & Bullish Expansion"
            trigger_str = f"Buy CE on 5m candle close above PDH (₹{pdh:.1f})"
            target_1 = round(max(cpr_r1, spot + step), 1)
            target_2 = round(max(cpr_r2, target_1 + step), 1)
            sl_level = round(max(cpr_top, pdh - step), 1)
            confidence = "HIGH" if (pcr >= 1.15 and spot_chg >= 0.25) else "MEDIUM"
            score = min(95, max(75, int(bullish_score)))
            why_reasons.append(f"Spot (₹{spot:.1f}) is trading above Previous Day High (₹{pdh:.1f}) in breakout territory.")
        else:
            setup_title = "Above CPR Bullish Continuation"
            trigger_str = f"Buy CE on pullback to CPR Top (₹{cpr_top:.1f}) holding with bounce"
            target_1 = round(pdh, 1)
            target_2 = round(max(cpr_r1, pdh + step), 1)
            sl_level = round(cpr_bottom, 1)
            confidence = "MEDIUM"
            score = min(90, max(70, int(bullish_score)))
            why_reasons.append(f"Spot is holding above Central Pivot Range Top (₹{cpr_top:.1f}).")

        why_reasons.append(f"{gap_type} ({gap_pct:+.2f}%) with {spot_chg:+.2f}% intraday momentum.")
        why_reasons.append(f"Put writing dominance (PCR {pcr:.2f}) offering underlying support.")

    elif is_below_pdl or (is_below_cpr and (spot_chg <= -0.10 or bearish_score >= 60.0) and pcr <= 1.05) or (is_below_cpr and spot_chg <= -0.25):
        direction = "BEARISH"
        action = "BUY PE"
        badge = "BEARISH (BUY PE)"
        badge_class = "bearish"
        suggested_strike = atm_strike if spot >= atm_strike - (step * 0.3) else atm_strike - step
        suggested_contract = f"{symbol} {int(suggested_strike)} PE"

        if is_below_pdl:
            setup_title = "PDL Breakdown & Bearish Slide"
            trigger_str = f"Buy PE on 5m candle close below PDL (₹{pdl:.1f})"
            target_1 = round(min(cpr_s1, spot - step), 1)
            target_2 = round(min(cpr_s2, target_1 - step), 1)
            sl_level = round(min(cpr_bottom, pdl + step), 1)
            confidence = "HIGH" if (pcr <= 0.85 and spot_chg <= -0.25) else "MEDIUM"
            score = min(95, max(75, int(bearish_score)))
            why_reasons.append(f"Spot (₹{spot:.1f}) is trading below Previous Day Low (₹{pdl:.1f}) in breakdown territory.")
        else:
            setup_title = "Below CPR Bearish Rejection"
            trigger_str = f"Buy PE on pullback to CPR Bottom (₹{cpr_bottom:.1f}) facing rejection"
            target_1 = round(pdl, 1)
            target_2 = round(min(cpr_s1, pdl - step), 1)
            sl_level = round(cpr_top, 1)
            confidence = "MEDIUM"
            score = min(90, max(70, int(bearish_score)))
            why_reasons.append(f"Spot is trading below Central Pivot Range Bottom (₹{cpr_bottom:.1f}).")

        why_reasons.append(f"{gap_type} ({gap_pct:+.2f}%) with {spot_chg:+.2f}% downward momentum.")
        why_reasons.append(f"Call writing resistance (PCR {pcr:.2f}) capping upside attempts.")

    else:
        direction = "NEUTRAL"
        action = "WAIT"
        badge = "RANGEBOUND (WAIT)"
        badge_class = "neutral"
        suggested_contract = "--"
        setup_title = "Inside Previous Day Value / CPR Chop"
        trigger_str = f"Wait for 15m Opening Range breakout above PDH (₹{pdh:.1f}) or breakdown below PDL (₹{pdl:.1f})"
        target_1 = round(pdh, 1)
        target_2 = round(pdl, 1)
        sl_level = round(pivot, 1)
        confidence = "LOW"
        score = 45
        why_reasons.append(f"Spot is fluctuating inside Previous Day Range (PDL: ₹{pdl:.1f} to PDH: ₹{pdh:.1f}).")
        why_reasons.append(f"Price inside CPR zone (₹{cpr_bottom:.1f} - ₹{cpr_top:.1f}) indicating consolidation.")
        why_reasons.append(f"Neutral Option Flow (PCR: {pcr:.2f}) - No clear institutional writing edge.")

    # Calculate approximate Risk-to-Reward ratio
    risk_pts = max(abs(spot - sl_level), step * 0.4)
    reward_pts = max(abs(target_1 - spot), step * 0.8)
    rr_ratio = round(reward_pts / risk_pts, 1) if risk_pts > 0 else 2.0
    rr_ratio = max(1.5, min(3.5, rr_ratio))

    return {
        "direction": direction,
        "action": action,
        "badge": badge,
        "badge_class": badge_class,
        "confidence": confidence,
        "score": score,
        "setup_title": setup_title,
        "suggested_contract": suggested_contract,
        "reference_data": {
            "pdh": pdh,
            "pdl": pdl,
            "pdc": pdc,
            "open": open_price,
            "pivot": pivot,
            "cpr_top": cpr_top,
            "cpr_bottom": cpr_bottom,
            "cpr_type": cpr_type,
            "cpr_width": cpr_width,
            "gap_type": gap_type,
            "gap_pts": gap_pts,
            "gap_pct": gap_pct,
            "r1": cpr_r1,
            "s1": cpr_s1,
            "r2": cpr_r2,
            "s2": cpr_s2,
        },
        "execution_plan": {
            "trigger": trigger_str,
            "stop_loss": sl_level,
            "target_1": target_1,
            "target_2": target_2,
            "risk_reward": f"1 : {rr_ratio:.1f}",
        },
        "why_reasons": why_reasons,
    }


def analyze_option_desk(market_data: dict, update_state: bool = False, has_active_trade: bool | None = None) -> dict:
    """
    Comprehensive Options Analytical Engine with Selective Option Buying Decision Flow:
    5 Core Signals (Price, Momentum, OI, Volume, PCR) -> 2/3 Confirmations -> Pullback & Recovery -> Active Trade Check -> BUY CE/BUY PE/WAIT
    Pure by default: state hysteresis is only updated when update_state=True (e.g. from scheduler cycle).
    """
    spot = float(market_data.get("spot_price", 0.0))
    step = market_data.get("strike_step", 50)
    chain = market_data.get("chain", [])
    symbol = market_data.get("symbol", "NIFTY").upper()
    expiry = market_data.get("expiry", "")
    spot_chg = float(market_data.get("spot_change_pct", 0.0))

    # Real VIX from data - None if missing (no 14.5 invented default)
    raw_vix = market_data.get("vix")
    vix = float(raw_vix) if (raw_vix is not None and float(raw_vix) > 0) else None

    data_status = market_data.get("data_status", "LIVE")

    if data_status == "DATA_UNAVAILABLE" or not chain:
        return {
            "error": "DATA_UNAVAILABLE",
            "market_bias": {
                "bias": "NEUTRAL",
                "confidence": 0,
                "spot_price": spot,
                "spot_change_pct": spot_chg,
                "bullish_score": 0,
                "bearish_score": 0,
                "pcr": 1.0,
                "atm_iv": None,
                "iv_percentile": None,
                "vix": vix,
                "dte": None,
                "last_changed": datetime.now(IST).strftime("%H:%M"),
                "why_reasons": ["Market data unavailable from FYERS. Fail closed."],
                "net_interpretation": "Data Unavailable: Strategy fail-closed mode active."
            },
            "key_levels": {
                "resistance_1": spot + step,
                "resistance_2": spot + 2 * step,
                "atm_strike": round(spot / step) * step if step else spot,
                "support_1": spot - step,
                "support_2": spot - 2 * step,
                "breakout_level": spot + step,
                "breakdown_level": spot - step,
                "spot_diff": 0.0,
            },
            "option_buying": {
                "decision": "WAIT",
                "action": "WAIT",
                "type": "WAIT",
                "symbol": symbol,
                "market_direction": "NEUTRAL",
                "key_level": "N/A",
                "level_status": "NOT CONFIRMED",
                "momentum_status": "NOT CONFIRMED",
                "volume_status": "NEUTRAL",
                "oi_status": "NEUTRAL",
                "selected_option": "--",
                "entry_price": 0.0,
                "data_status": "DATA_UNAVAILABLE",
                "decision_reason": "FYERS LIVE market data unavailable. Strategy fail-closed mode active.",
                "title": "WAIT",
                "reason": "FYERS LIVE market data unavailable. Strategy fail-closed mode active.",
                "setup_score": 0,
                "trade_plan": {},
                "checklist": [],
                "pillar_flags": {},
                "iv_time": {"iv_favorable": False, "time_favorable": False, "iv_condition": "UNAVAILABLE", "time_status": "DATA_UNAVAILABLE"},
                "raw_values": {"spot": spot, "spot_change_pct": spot_chg, "bullish_score": 0, "bearish_score": 0, "pcr": 1.0, "total_ce_vol": 0, "total_pe_vol": 0, "total_ce_oi": 0, "total_pe_oi": 0, "breakout_level": 0, "breakdown_level": 0, "atm_iv": None, "iv_percentile": None, "vix": vix, "dte": None, "bid_ask_spread_pct": 0.0, "spread_paid": 0.0},
                "data_quality": {"valid": False, "status": "DATA_UNAVAILABLE", "reason": "FYERS market data unavailable"},
            },
            "morning_bias": {
                "direction": "NEUTRAL",
                "action": "WAIT",
                "badge": "RANGEBOUND (WAIT)",
                "badge_class": "neutral",
                "confidence": "LOW",
                "score": 0,
                "setup_title": "Market Data Unavailable",
                "suggested_contract": "--",
                "reference_data": {
                    "pdh": spot + step,
                    "pdl": spot - step,
                    "pdc": spot,
                    "open": spot,
                    "pivot": spot,
                    "cpr_top": spot + (step * 0.2),
                    "cpr_bottom": spot - (step * 0.2),
                    "cpr_type": "AVERAGE",
                    "cpr_width": step * 0.4,
                    "gap_type": "FLAT OPEN",
                    "gap_pts": 0.0,
                    "gap_pct": 0.0,
                    "r1": spot + step,
                    "s1": spot - step,
                    "r2": spot + 2 * step,
                    "s2": spot - 2 * step,
                },
                "execution_plan": {
                    "trigger": "Awaiting live market data connection",
                    "stop_loss": spot - step,
                    "target_1": spot + step,
                    "target_2": spot + 2 * step,
                    "risk_reward": "1 : 2.0",
                },
                "why_reasons": ["Market data currently unavailable from FYERS. Fail-closed discipline active."],
            },
            "oi_activity_summary": {"call_writing_strike": 0, "put_writing_strike": 0, "call_unwinding_strike": "None", "put_unwinding_strike": "None", "pcr": 1.0, "total_ce_oi": "0", "total_pe_oi": "0", "total_ce_change_oi": "0", "total_pe_change_oi": "0"},
            "pcr": {"pcr_ratio": 1.0, "pcr_chg": 0.0},
            "big_oi_movements": [],
            "oi_trend": [],
            "trending_oi": {},
            "detailed_chain": [],
            "meta": {
                "symbol": symbol,
                "name": market_data.get("name", symbol),
                "expiry": expiry,
                "strike_step": step,
                "available_expiries": market_data.get("available_expiries", []),
                "data_status": "DATA_UNAVAILABLE",
                "data_source": market_data.get("data_source", "FYERS_UNAVAILABLE"),
                "last_updated": market_data.get("last_updated"),
                "engine_config": ENGINE_CONFIG,
                "oi_thresholds_applied": {},
            }
        }

    oi_thresholds = get_symbol_oi_thresholds(symbol)

    # Calculate Days-to-Expiry (DTE) explicitly
    # Expiry day is exactly 0.0 days (not floored to 0.25); None if unparseable
    dte = None
    if expiry:
        try:
            exp_date = datetime.strptime(expiry, "%d-%b-%Y").date()
            today = datetime.now(IST).date()
            days_diff = (exp_date - today).days
            dte = max(0.0, float(days_diff))
        except Exception:
            try:
                exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
                today = datetime.now(IST).date()
                days_diff = (exp_date - today).days
                dte = max(0.0, float(days_diff))
            except Exception:
                dte = None

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
        ce_oi = int(row.get("ce_oi", 0) or 0)
        ce_chg = int(row.get("ce_change_oi", 0) or 0)
        ce_ltp = float(row.get("ce_ltp", 0.0) or 0.0)
        ce_vol = int(row.get("ce_volume", 0) or 0)
        ce_iv = float(row.get("ce_iv", 0.0) or 0.0)
        ce_delta = float(row.get("ce_delta", 0.0) or 0.0)
        ce_bid = float(row.get("ce_bid", 0.0) or 0.0)
        ce_ask = float(row.get("ce_ask", 0.0) or 0.0)

        pe_oi = int(row.get("pe_oi", 0) or 0)
        pe_chg = int(row.get("pe_change_oi", 0) or 0)
        pe_ltp = float(row.get("pe_ltp", 0.0) or 0.0)
        pe_vol = int(row.get("pe_volume", 0) or 0)
        pe_iv = float(row.get("pe_iv", 0.0) or 0.0)
        pe_delta = float(row.get("pe_delta", 0.0) or 0.0)
        pe_bid = float(row.get("pe_bid", 0.0) or 0.0)
        pe_ask = float(row.get("pe_ask", 0.0) or 0.0)

        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        total_ce_change_oi += ce_chg
        total_pe_change_oi += pe_chg
        total_ce_vol += ce_vol
        total_pe_vol += pe_vol

        # Classify Call side using symbol threshold
        ce_act = "NO CLEAR SIGNAL"
        if abs(ce_chg) >= oi_thresholds["WRITING"]:
            if ce_chg > 0:
                ce_act = "CALL WRITING" if strike >= spot - step else "LONG CALL BUILDUP"
            else:
                ce_act = "CALL UNWINDING"

        # Classify Put side using symbol threshold
        pe_act = "NO CLEAR SIGNAL"
        if abs(pe_chg) >= oi_thresholds["WRITING"]:
            if pe_chg > 0:
                pe_act = "PUT WRITING" if strike <= spot + step else "LONG PUT BUILDUP"
            else:
                pe_act = "PUT UNWINDING"

        enriched_row = {
            **row,
            "strike": strike,
            "ce_activity": ce_act,
            "pe_activity": pe_act,
            "total_oi_strike": ce_oi + pe_oi,
            "net_oi_chg_strike": pe_chg - ce_chg,
            "ce_ltp": ce_ltp,
            "pe_ltp": pe_ltp,
            "ce_bid": ce_bid if ce_bid > 0 else None,
            "ce_ask": ce_ask if ce_ask > 0 else None,
            "pe_bid": pe_bid if pe_bid > 0 else None,
            "pe_ask": pe_ask if pe_ask > 0 else None,
            "ce_iv": ce_iv if ce_iv > 0 else None,
            "pe_iv": pe_iv if pe_iv > 0 else None,
            "ce_delta": ce_delta,
            "pe_delta": pe_delta,
            "ce_volume": ce_vol,
            "pe_volume": pe_vol,
            "ce_oi": ce_oi,
            "pe_oi": pe_oi,
            "ce_change_oi": ce_chg,
            "pe_change_oi": pe_chg,
            "ce_oi_formatted": format_lakhs(ce_oi),
            "ce_change_oi_formatted": format_lakhs(ce_chg),
            "pe_oi_formatted": format_lakhs(pe_oi),
            "pe_change_oi_formatted": format_lakhs(pe_chg),
        }
        enriched_chain.append(enriched_row)

        if abs(ce_chg) >= oi_thresholds["BIG_MOVEMENT"] or abs(pe_chg) >= oi_thresholds["BIG_MOVEMENT"]:
            big_oi_candidates.append(enriched_row)

    # 3. Put-Call Ratio (PCR)
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0
    pcr_chg = round(total_pe_change_oi / total_ce_change_oi, 2) if total_ce_change_oi > 0 else 1.0

    # 4. Market Bias Engine (Deterministic Scoring 0 to 100)
    bullish_score = 0.0
    bearish_score = 0.0
    bullish_factors = []
    bearish_factors = []

    # Factor A: PCR Level & PCR Trend (Max 25 pts)
    if pcr >= 1.25:
        bullish_score += 25.0
        bullish_factors.append(f"Strong Put Writing dominance (PCR: {pcr})")
    elif pcr >= 1.05:
        bullish_score += 18.0
        bullish_factors.append(f"Moderate Bullish PCR bias (PCR: {pcr})")
    elif pcr <= 0.75:
        bearish_score += 25.0
        bearish_factors.append(f"Heavy Call Writing resistance (PCR: {pcr})")
    elif pcr <= 0.92:
        bearish_score += 18.0
        bearish_factors.append(f"Moderate Bearish PCR bias (PCR: {pcr})")
    else:
        bullish_score += 10.0
        bearish_score += 10.0

    # Factor B: Spot Price Momentum (Max 25 pts)
    if spot_chg >= 0.35:
        bullish_score += 25.0
        bullish_factors.append(f"Strong upward spot momentum (+{spot_chg:.2f}%)")
    elif spot_chg >= 0.10:
        bullish_score += 15.0
        bullish_factors.append(f"Positive intraday trend (+{spot_chg:.2f}%)")
    elif spot_chg <= -0.35:
        bearish_score += 25.0
        bearish_factors.append(f"Strong downward spot selloff ({spot_chg:.2f}%)")
    elif spot_chg <= -0.10:
        bearish_score += 15.0
        bearish_factors.append(f"Negative intraday trend ({spot_chg:.2f}%)")

    # Factor C: Near-the-Money OI Buildup (Max 30 pts)
    near_strikes = [r for r in enriched_chain if abs(r["strike"] - atm_strike) <= (step * 3)]
    near_ce_writing = sum(r["ce_change_oi"] for r in near_strikes if r["ce_change_oi"] > 0)
    near_pe_writing = sum(r["pe_change_oi"] for r in near_strikes if r["pe_change_oi"] > 0)
    near_ce_unwinding = sum(abs(r["ce_change_oi"]) for r in near_strikes if r["ce_change_oi"] < 0)
    near_pe_unwinding = sum(abs(r["pe_change_oi"]) for r in near_strikes if r["pe_change_oi"] < 0)

    unwind_threshold = oi_thresholds["UNWINDING"]
    if near_pe_writing > (near_ce_writing * 1.3):
        bullish_score += 20.0
        bullish_factors.append(f"Put sellers aggressive ({format_lakhs(near_pe_writing)} PE writing near ATM)")
    elif near_ce_writing > (near_pe_writing * 1.3):
        bearish_score += 20.0
        bearish_factors.append(f"Call sellers active ({format_lakhs(near_ce_writing)} CE writing near ATM)")

    if near_ce_unwinding > near_pe_unwinding and near_ce_unwinding > unwind_threshold:
        bullish_score += 10.0
        bullish_factors.append(f"Call unwinding short squeeze ({format_lakhs(near_ce_unwinding)})")
    elif near_pe_unwinding > near_ce_unwinding and near_pe_unwinding > unwind_threshold:
        bearish_score += 10.0
        bearish_factors.append(f"Put unwinding panic exit ({format_lakhs(near_pe_unwinding)})")

    # Factor D: Volume Shift (Max 20 pts)
    if total_ce_vol > (total_pe_vol * ENGINE_CONFIG["VOLUME_RATIO_THRESHOLD"]):
        bullish_score += 20.0
        bullish_factors.append("Call volume breakout expansion")
    elif total_pe_vol > (total_ce_vol * ENGINE_CONFIG["VOLUME_RATIO_THRESHOLD"]):
        bearish_score += 20.0
        bearish_factors.append("Put volume breakdown expansion")
    else:
        bullish_score += 10.0
        bearish_score += 10.0

    bullish_score = min(100.0, round(bullish_score, 1))
    bearish_score = min(100.0, round(bearish_score, 1))
    score_diff = bullish_score - bearish_score

    # Determine Instantaneous Raw Bias: Price action is strictly primary.
    # High OI or volume alone must NOT force a directional bias without price agreement.
    raw_bias = "NEUTRAL"
    if score_diff >= 15.0 and bullish_score >= ENGINE_CONFIG["DIRECTION_BIAS_THRESHOLD"] and spot_chg >= 0.0:
        raw_bias = "BULLISH"
    elif score_diff <= -15.0 and bearish_score >= ENGINE_CONFIG["DIRECTION_BIAS_THRESHOLD"] and spot_chg <= 0.0:
        raw_bias = "BEARISH"

    # Per-Symbol Thread-Safe Persistent Bias State Transition
    # Pure by default: state hysteresis is only mutated when update_state=True (e.g. from background scheduler)
    if update_state:
        final_bias = update_symbol_bias_state(symbol, raw_bias, score_diff)
        bias_state = get_symbol_bias_state(symbol)
    else:
        # Pure mode: does not mutate state or advance confirmations
        with _BIAS_LOCK:
            if symbol in _BIAS_STATES:
                bias_state = _BIAS_STATES[symbol]
                final_bias = bias_state["last_bias"]
            else:
                final_bias = raw_bias
                bias_state = {
                    "last_bias": raw_bias,
                    "pending_bias": raw_bias,
                    "last_changed_time": datetime.now(IST).strftime("%H:%M"),
                    "consecutive_confirmations": 0,
                }

    max_strength = max(bullish_score, bearish_score)
    confidence = "HIGH" if (abs(score_diff) >= 25.0 and max_strength >= 70.0) else ("MEDIUM" if abs(score_diff) >= 12.0 else "LOW")

    # 5. Key Support & Resistance Levels (Weighted Proximity)
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

    # Ensure R1 and S1 form a distinct, non-zero channel bracket (R1 > S1)
    if r1 <= s1:
        if spot < r1:
            # Spot is below collision strike: collision strike acts as Resistance (R1)
            lower_pe_strikes = [r for r in sorted_sup if r["strike"] < r1]
            s1 = lower_pe_strikes[0]["strike"] if lower_pe_strikes else (r1 - step)
            s2_candidates = [r for r in lower_pe_strikes if r["strike"] < s1]
            s2 = s2_candidates[0]["strike"] if s2_candidates else (s1 - step)
        else:
            # Spot is at or above collision strike: collision strike acts as Support (S1)
            upper_ce_strikes = [r for r in sorted_res if r["strike"] > s1]
            r1 = upper_ce_strikes[0]["strike"] if upper_ce_strikes else (s1 + step)
            r2_candidates = [r for r in upper_ce_strikes if r["strike"] > r1]
            r2 = r2_candidates[0]["strike"] if r2_candidates else (r1 + step)

    # Double check ordering guarantees
    if r2 <= r1:
        upper_r2 = [r["strike"] for r in sorted_res if r["strike"] > r1]
        r2 = upper_r2[0] if upper_r2 else (r1 + step)
    if s2 >= s1:
        lower_s2 = [r["strike"] for r in sorted_sup if r["strike"] < s1]
        s2 = lower_s2[0] if lower_s2 else (s1 - step)

    breakout_level = r1
    breakdown_level = s1

    # 6. IV & Session Time Evaluation
    atm_row = next((r for r in enriched_chain if abs(r["strike"] - atm_strike) < 1.0), {})
    ce_atm_iv = atm_row.get("ce_iv")
    pe_atm_iv = atm_row.get("pe_iv")

    if ce_atm_iv is not None and pe_atm_iv is not None:
        avg_atm_iv = round((ce_atm_iv + pe_atm_iv) / 2.0, 2)
    elif ce_atm_iv is not None:
        avg_atm_iv = round(ce_atm_iv, 2)
    elif pe_atm_iv is not None:
        avg_atm_iv = round(pe_atm_iv, 2)
    else:
        avg_atm_iv = None

    # Real rolling percentile per symbol (read-only query, returns None if < 30 days of daily history)
    iv_percentile = get_rolling_iv_percentile(symbol, avg_atm_iv, min_days=30) if avg_atm_iv else None
    iv_time_info = evaluate_iv_and_time_filter(avg_atm_iv)

    # -------------------------------------------------------------
    # 7. 5 CORE STRATEGY SIGNALS (Price, Momentum, OI, Volume, PCR)
    # -------------------------------------------------------------
    # Signal 1: Price
    if spot_chg > 0.0:
        price_signal = "BULLISH"
    elif spot_chg < 0.0:
        price_signal = "BEARISH"
    else:
        price_signal = "NEUTRAL"

    # Signal 2: Momentum
    mom_threshold = ENGINE_CONFIG.get("MOMENTUM_CHANGE_THRESHOLD", 0.05)
    if spot_chg >= mom_threshold:
        momentum_signal = "BULLISH"
    elif spot_chg <= -mom_threshold:
        momentum_signal = "BEARISH"
    else:
        momentum_signal = "NEUTRAL"

    # Signal 3: OI Structure
    unwind_threshold = oi_thresholds["UNWINDING"]
    if near_pe_writing > near_ce_writing or (near_ce_unwinding > near_pe_unwinding and near_ce_unwinding > unwind_threshold):
        oi_signal = "BULLISH"
    elif near_ce_writing > near_pe_writing or (near_pe_unwinding > near_ce_unwinding and near_pe_unwinding > unwind_threshold):
        oi_signal = "BEARISH"
    else:
        oi_signal = "NEUTRAL"

    # Signal 4: Volume
    vol_ratio = ENGINE_CONFIG.get("VOLUME_RATIO_THRESHOLD", 1.10)
    if total_ce_vol > (total_pe_vol * vol_ratio):
        volume_signal = "BULLISH"
    elif total_pe_vol > (total_ce_vol * vol_ratio):
        volume_signal = "BEARISH"
    else:
        volume_signal = "NEUTRAL"

    # Signal 5: PCR
    if pcr >= 1.0:
        pcr_signal = "BULLISH"
    elif pcr <= 0.90:
        pcr_signal = "BEARISH"
    else:
        pcr_signal = "NEUTRAL"

    # 2 of 3 Confirmations required from (OI, Volume, PCR)
    bull_confirm_count = sum(1 for s in (oi_signal, volume_signal, pcr_signal) if s == "BULLISH")
    bear_confirm_count = sum(1 for s in (oi_signal, volume_signal, pcr_signal) if s == "BEARISH")

    is_bullish_dir = (price_signal == "BULLISH" and momentum_signal == "BULLISH" and bull_confirm_count >= 2)
    is_bearish_dir = (price_signal == "BEARISH" and momentum_signal == "BEARISH" and bear_confirm_count >= 2)

    # Pullback & Recovery Detection
    custom_price_hist = market_data.get("price_history")
    pullback_info = update_symbol_pullback_recovery(
        symbol, spot, is_bullish_dir, is_bearish_dir,
        custom_history=custom_price_hist, update_state=update_state,
        momentum_signal=momentum_signal, price_signal=price_signal,
        volume_signal=volume_signal, spot_chg=spot_chg, strike_step=step
    )
    setup_type = pullback_info["setup_type"]
    pullback_occurred = pullback_info["pullback_occurred"]
    recovery_confirmed = pullback_info["recovery_confirmed"]
    expansion_confirmed = pullback_info.get("expansion_confirmed", True)
    is_choppy = pullback_info.get("is_choppy", False)
    anti_chase_status = pullback_info["anti_chase"]

    # 8. Preferred Option Contract Selection (ATM or ITM-1)
    ce_candidates = [r for r in enriched_chain if r["strike"] in (atm_strike, atm_strike - step)]
    pe_candidates = [r for r in enriched_chain if r["strike"] in (atm_strike, atm_strike + step)]

    if not ce_candidates:
        ce_candidates = [r for r in enriched_chain if abs(r["strike"] - atm_strike) <= step] or enriched_chain[:3]
    if not pe_candidates:
        pe_candidates = [r for r in enriched_chain if abs(r["strike"] - atm_strike) <= step] or enriched_chain[:3]

    best_ce = max(ce_candidates, key=lambda x: x.get("ce_volume", 0) + (x.get("ce_oi", 0) * 0.1))
    best_pe = max(pe_candidates, key=lambda x: x.get("pe_volume", 0) + (x.get("pe_oi", 0) * 0.1))

    # 9. Trade Levels Calculation — Fills at ASK, Exits at BID, Records Spread Paid
    def calculate_trade_levels(opt_row: dict, is_ce: bool, spot_invalidation: float, spot_target: float) -> dict | None:
        ltp = float(opt_row.get("ce_ltp" if is_ce else "pe_ltp", 0.0) or 0.0)
        best_bid = opt_row.get("ce_bid" if is_ce else "pe_bid")
        best_ask = opt_row.get("ce_ask" if is_ce else "pe_ask")

        # Fail closed on missing/invalid LTP or missing bid/ask quotes
        if ltp <= 0.05:
            return None
        if best_bid is None or best_ask is None or best_bid <= 0.05 or best_ask <= 0.05 or best_ask < best_bid:
            return None

        # Fills at ASK for long option entry
        fill_entry_price = round(float(best_ask), 2)
        fill_bid = round(float(best_bid), 2)
        spread_paid = round(fill_entry_price - fill_bid, 2)
        spread_pct = round((spread_paid / ltp * 100), 2) if ltp > 0 else 0.0

        strike = int(opt_row["strike"])
        moneyness = "ATM" if strike == atm_strike else ("ITM-1" if (strike == atm_strike - step if is_ce else strike == atm_strike + step) else "OTM")

        risk_per_contract = max(round(fill_entry_price * 0.20, 1), 5.0)
        sl_price = round(max(fill_entry_price - risk_per_contract, 0.05), 1)
        t1_price = round(fill_entry_price + (risk_per_contract * 1.5), 1)
        t2_price = round(fill_entry_price + (risk_per_contract * 2.5), 1)
        rr_str = "1 : 2.0"

        return {
            "contract_name": f"{strike} {'CE' if is_ce else 'PE'}",
            "strike": strike,
            "type": "CE" if is_ce else "PE",
            "expiry": expiry,
            "moneyness": moneyness,
            "ltp": round(ltp, 2),
            "entry_price": fill_entry_price,
            "bid_at_entry": fill_bid,
            "ask_at_entry": fill_entry_price,
            "spread_paid": spread_paid,
            "bid_ask_spread_pct": spread_pct,
            "stop_loss": sl_price,
            "target_1": t1_price,
            "target_2": t2_price,
            "risk_pts": round(risk_per_contract, 1),
            "reward_t1": round(t1_price - fill_entry_price, 1),
            "reward_t2": round(t2_price - fill_entry_price, 1),
            "risk_reward": rr_str,
            "spot_trigger": f"Break / Hold above {breakout_level}" if is_ce else f"Break / Hold below {breakdown_level}",
            "spot_invalidation": f"Below {spot_invalidation}",
            "spot_target": f"{spot_target}",
        }

    ce_trade_plan = calculate_trade_levels(best_ce, is_ce=True, spot_invalidation=s1, spot_target=r2)
    pe_trade_plan = calculate_trade_levels(best_pe, is_ce=False, spot_invalidation=r1, spot_target=s2)

    # 10. Check One Active Trade Per Symbol Constraint
    if has_active_trade is None:
        try:
            from app.services.options_signal_service import has_active_signal_for_symbol
            has_active_trade = has_active_signal_for_symbol(symbol)
        except Exception:
            has_active_trade = False

    active_trade_str = "YES" if has_active_trade else "NO"

    # 11. Final Decision (BUY CE / BUY PE / WAIT)
    decision = "WAIT"
    focus_title = "WAIT"
    focus_reason = "Awaiting directional signal and confirmation."

    is_live_data = (data_status == "LIVE")
    iv_safe = bool(iv_time_info.get("iv_favorable", True))
    in_trade_window = bool(iv_time_info.get("time_favorable", False))

    req_iv = ENGINE_CONFIG.get("REQUIRE_IV", False)
    if req_iv:
        iv_safe = iv_safe and (avg_atm_iv is not None and avg_atm_iv > 0.01)

    # Build checklist & pillar flags
    def build_checklist_and_flags(is_ce: bool):
        is_dir = is_bullish_dir if is_ce else is_bearish_dir
        conf_count = bull_confirm_count if is_ce else bear_confirm_count
        p_sig = price_signal == ("BULLISH" if is_ce else "BEARISH")
        m_sig = momentum_signal == ("BULLISH" if is_ce else "BEARISH")
        o_sig = oi_signal == ("BULLISH" if is_ce else "BEARISH")
        v_sig = volume_signal == ("BULLISH" if is_ce else "BEARISH")
        pcr_sig = pcr_signal == ("BULLISH" if is_ce else "BEARISH")
        iv_ok = bool(iv_safe and in_trade_window)

        chk = [
            {"name": "Price Direction", "passed": p_sig, "desc": f"Price is {price_signal}"},
            {"name": "Momentum", "passed": m_sig, "desc": f"Momentum is {momentum_signal}"},
            {"name": "OI Structure", "passed": o_sig, "desc": f"OI is {oi_signal}"},
            {"name": "Volume", "passed": v_sig, "desc": f"Volume is {volume_signal}"},
            {"name": "PCR Trend", "passed": pcr_sig, "desc": f"PCR is {pcr_signal} ({pcr})"},
            {"name": "IV & Window", "passed": iv_ok, "desc": f"{iv_time_info.get('time_status', '')} | {iv_time_info.get('iv_condition', '')}"},
        ]
        flags = {
            "direction": p_sig and m_sig,
            "level": is_dir,
            "momentum": m_sig,
            "volume": v_sig,
            "oi": o_sig,
            "iv_session": iv_ok,
        }
        score = 20 if p_sig else 0
        if m_sig: score += 20
        if o_sig: score += 20
        if v_sig: score += 20
        if pcr_sig: score += 20
        return score, chk, flags

    ce_score, ce_checklist, ce_pillar_flags = build_checklist_and_flags(is_ce=True)
    pe_score, pe_checklist, pe_pillar_flags = build_checklist_and_flags(is_ce=False)

    if not is_live_data:
        decision = "WAIT"
        setup_score = max(ce_score, pe_score)
        active_checklist = ce_checklist if is_bullish_dir else pe_checklist
        active_trade_plan = (ce_trade_plan if is_bullish_dir else pe_trade_plan) or {}
        active_pillar_flags = ce_pillar_flags if is_bullish_dir else pe_pillar_flags
        focus_title = "WAIT"
        focus_reason = f"Data quality gate failed: FYERS LIVE market data required (current status: {data_status})."
    elif not in_trade_window:
        decision = "WAIT"
        setup_score = max(ce_score, pe_score)
        active_checklist = ce_checklist if is_bullish_dir else pe_checklist
        active_trade_plan = (ce_trade_plan if is_bullish_dir else pe_trade_plan) or {}
        active_pillar_flags = ce_pillar_flags if is_bullish_dir else pe_pillar_flags
        focus_title = "WAIT"
        focus_reason = f"Outside entry window (09:30-14:00 IST): {iv_time_info.get('time_status', '')}. No fresh buying."
    elif not iv_safe:
        decision = "WAIT"
        setup_score = max(ce_score, pe_score)
        active_checklist = ce_checklist if is_bullish_dir else pe_checklist
        active_trade_plan = (ce_trade_plan if is_bullish_dir else pe_trade_plan) or {}
        active_pillar_flags = ce_pillar_flags if is_bullish_dir else pe_pillar_flags
        focus_title = "WAIT"
        focus_reason = f"IV safety filter active: {iv_time_info.get('iv_condition', 'UNFAVORABLE')}. Avoid fresh buying."
    elif has_active_trade:
        decision = "WAIT"
        setup_score = max(ce_score, pe_score)
        active_checklist = ce_checklist if is_bullish_dir else pe_checklist
        active_trade_plan = (ce_trade_plan if is_bullish_dir else pe_trade_plan) or {}
        active_pillar_flags = ce_pillar_flags if is_bullish_dir else pe_pillar_flags
        focus_title = "WAIT"
        focus_reason = "ACTIVE TRADE EXISTS"
    elif is_bullish_dir:
        if ce_trade_plan is None:
            decision = "WAIT"
            setup_score = ce_score
            active_checklist = ce_checklist
            active_trade_plan = {}
            active_pillar_flags = ce_pillar_flags
            focus_title = "WAIT"
            focus_reason = "Option quality gate failed: CE contract quote missing, zero LTP, or bid/ask spread invalid."
        elif not pullback_occurred:
            decision = "WAIT"
            setup_score = ce_score
            active_checklist = ce_checklist
            active_trade_plan = ce_trade_plan or {}
            active_pillar_flags = ce_pillar_flags
            focus_title = "WAIT"
            focus_reason = "FRESH_HIGH_NO_PULLBACK"
        elif not recovery_confirmed:
            decision = "WAIT"
            setup_score = ce_score
            active_checklist = ce_checklist
            active_trade_plan = ce_trade_plan or {}
            active_pillar_flags = ce_pillar_flags
            focus_title = "WAIT"
            focus_reason = "LOW_EXPANSION_OR_CHOP" if (is_choppy or not expansion_confirmed) else "PULLBACK_IN_PROGRESS_OR_WEAK_RECOVERY"
        elif is_choppy or not expansion_confirmed:
            decision = "WAIT"
            setup_score = ce_score
            active_checklist = ce_checklist
            active_trade_plan = ce_trade_plan or {}
            active_pillar_flags = ce_pillar_flags
            focus_title = "WAIT"
            focus_reason = "LOW_EXPANSION_OR_CHOP"
        else:
            decision = "BUY CE"
            setup_score = max(ce_score, 80)
            active_checklist = ce_checklist
            active_trade_plan = ce_trade_plan
            active_pillar_flags = ce_pillar_flags
            focus_title = "BUY CE"
            focus_reason = f"Bullish setup ({setup_type}) confirmed with {bull_confirm_count}/3 confirmations (OI, Volume, PCR)."
    elif is_bearish_dir:
        if pe_trade_plan is None:
            decision = "WAIT"
            setup_score = pe_score
            active_checklist = pe_checklist
            active_trade_plan = {}
            active_pillar_flags = pe_pillar_flags
            focus_title = "WAIT"
            focus_reason = "Option quality gate failed: PE contract quote missing, zero LTP, or bid/ask spread invalid."
        elif not pullback_occurred:
            decision = "WAIT"
            setup_score = pe_score
            active_checklist = pe_checklist
            active_trade_plan = pe_trade_plan or {}
            active_pillar_flags = pe_pillar_flags
            focus_title = "WAIT"
            focus_reason = "FRESH_LOW_NO_PULLBACK"
        elif not recovery_confirmed:
            decision = "WAIT"
            setup_score = pe_score
            active_checklist = pe_checklist
            active_trade_plan = pe_trade_plan or {}
            active_pillar_flags = pe_pillar_flags
            focus_title = "WAIT"
            focus_reason = "LOW_EXPANSION_OR_CHOP" if (is_choppy or not expansion_confirmed) else "PULLBACK_IN_PROGRESS_OR_WEAK_RECOVERY"
        elif is_choppy or not expansion_confirmed:
            decision = "WAIT"
            setup_score = pe_score
            active_checklist = pe_checklist
            active_trade_plan = pe_trade_plan or {}
            active_pillar_flags = pe_pillar_flags
            focus_title = "WAIT"
            focus_reason = "LOW_EXPANSION_OR_CHOP"
        else:
            decision = "BUY PE"
            setup_score = max(pe_score, 80)
            active_checklist = pe_checklist
            active_trade_plan = pe_trade_plan
            active_pillar_flags = pe_pillar_flags
            focus_title = "BUY PE"
            focus_reason = f"Bearish setup ({setup_type}) confirmed with {bear_confirm_count}/3 confirmations (OI, Volume, PCR)."
    else:
        decision = "WAIT"
        setup_score = max(ce_score, pe_score)
        active_checklist = ce_checklist if ce_score >= pe_score else pe_checklist
        active_trade_plan = (ce_trade_plan if ce_score >= pe_score else pe_trade_plan) or {}
        active_pillar_flags = ce_pillar_flags if ce_score >= pe_score else pe_pillar_flags
        focus_title = "WAIT"
        if is_choppy or not expansion_confirmed:
            focus_reason = "LOW_EXPANSION_OR_CHOP"
        elif price_signal == "BULLISH" and momentum_signal == "BULLISH":
            focus_reason = f"Bullish confirmation gate failed: Only {bull_confirm_count}/3 confirmations (OI, Volume, PCR) support Bullish direction."
        elif price_signal == "BEARISH" and momentum_signal == "BEARISH":
            focus_reason = f"Bearish confirmation gate failed: Only {bear_confirm_count}/3 confirmations (OI, Volume, PCR) support Bearish direction."
        else:
            focus_reason = "Market direction or momentum unclear. Preserve capital."

    # -------------------------------------------------------------
    # STRUCTURED STRATEGY LOGGING (Item 10 / Section 15)
    # -------------------------------------------------------------
    market_bias_str = "BULLISH" if is_bullish_dir else ("BEARISH" if is_bearish_dir else "NEUTRAL")
    confirm_display = f"{bull_confirm_count if is_bullish_dir else (bear_confirm_count if is_bearish_dir else max(bull_confirm_count, bear_confirm_count))}/3"

    pullback_str = "YES" if pullback_occurred else "NO"
    recovery_str = "YES" if recovery_confirmed else "NO"

    if has_active_trade:
        log.info(
            f"\n[OPTIONS DESK EVALUATION - {symbol}]\n"
            f"Trend       : {market_bias_str}\n"
            f"Pullback    : {pullback_str}\n"
            f"Recovery    : {recovery_str}\n\n"
            f"Active Trade: YES\n\n"
            f"Decision    : WAIT\n"
            f"Reason      : ACTIVE_TRADE_EXISTS\n"
        )
    elif anti_chase_status == "BLOCK" and (is_bullish_dir or is_bearish_dir):
        log.info(
            f"\n[OPTIONS DESK EVALUATION - {symbol}]\n"
            f"Trend       : {market_bias_str}\n"
            f"Pullback    : {pullback_str}\n"
            f"Recovery    : {recovery_str}\n"
            f"Anti-Chase  : BLOCK\n\n"
            f"Decision    : WAIT\n"
            f"Reason      : {focus_reason}\n"
        )
    else:
        log.info(
            f"\n[OPTIONS DESK EVALUATION - {symbol}]\n"
            f"Price       : {price_signal}\n"
            f"Momentum    : {momentum_signal}\n"
            f"OI          : {oi_signal}\n"
            f"Volume      : {volume_signal}\n"
            f"PCR         : {pcr_signal}\n\n"
            f"Confirmation: {confirm_display}\n"
            f"Trend       : {market_bias_str}\n\n"
            f"Pullback    : {pullback_str}\n"
            f"Recovery    : {recovery_str}\n"
            f"Resistance  : NEAR_R1 ({r1})\n"
            f"Anti-Chase  : {anti_chase_status}\n\n"
            f"Active Trade: {active_trade_str}\n\n"
            f"Decision    : {decision}\n"
            + (f"Reason      : {focus_reason}\n" if decision == "WAIT" else "")
        )

    # 11. OI Trend Focus & Trending OI (ATM +/- 5 strikes: 5 up, 5 down, 11 strikes total)
    trend_strikes = [r for r in enriched_chain if abs(r["strike"] - atm_strike) <= (step * 5)]
    trending_oi = compute_trending_oi_metrics(enriched_chain, spot, step, atm_strike, num_strikes_each_side=5)
    call_writing_strikes = [r["strike"] for r in enriched_chain if r.get("ce_activity") == "CALL WRITING"]
    put_writing_strikes = [r["strike"] for r in enriched_chain if r.get("pe_activity") == "PUT WRITING"]
    call_unwinding_strikes = [r["strike"] for r in enriched_chain if r.get("ce_activity") == "CALL UNWINDING"]
    put_unwinding_strikes = [r["strike"] for r in enriched_chain if r.get("pe_activity") == "PUT UNWINDING"]

    big_oi_movements = []
    for r in big_oi_candidates[:6]:
        is_ce_dom = abs(r["ce_change_oi"]) >= abs(r["pe_change_oi"])
        dom_side = "CE" if is_ce_dom else "PE"
        dom_chg = r["ce_change_oi"] if is_ce_dom else r["pe_change_oi"]
        dom_act = r["ce_activity"] if is_ce_dom else r["pe_activity"]

        big_oi_movements.append({
            "strike": r["strike"],
            "side": dom_side,
            "change_oi": dom_chg,
            "change_oi_formatted": format_lakhs(dom_chg),
            "ce_change_oi": r["ce_change_oi"],
            "pe_change_oi": r["pe_change_oi"],
            "activity": dom_act,
            "sentiment": "BULLISH" if ("PUT WRITING" in dom_act or "CALL UNWINDING" in dom_act) else "BEARISH"
        })

    raw_values = {
        "spot": spot,
        "spot_change_pct": spot_chg,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "pcr": pcr,
        "window_pcr": pcr,
        "raw_bias": raw_bias,
        "score_diff": score_diff,
        "pcr_change": pcr_chg,
        "total_ce_vol": total_ce_vol,
        "total_pe_vol": total_pe_vol,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "price_signal": price_signal,
        "momentum_signal": momentum_signal,
        "oi_signal": oi_signal,
        "volume_signal": volume_signal,
        "pcr_signal": pcr_signal,
        "confirm_count": bull_confirm_count if is_bullish_dir else bear_confirm_count,
        "setup_type": setup_type,
        "active_trade_exists": has_active_trade,
        "breakout_level": breakout_level,
        "breakdown_level": breakdown_level,
        "atm_iv": avg_atm_iv,
        "iv_percentile": iv_percentile,
        "vix": vix,
        "dte": dte,
        "bid_ask_spread_pct": active_trade_plan.get("bid_ask_spread_pct", 0.0) if active_trade_plan else 0.0,
        "spread_paid": active_trade_plan.get("spread_paid", 0.0) if active_trade_plan else 0.0,
    }

    is_iv_missing = bool(avg_atm_iv is None or avg_atm_iv <= 0.01)
    data_quality_flags = {
        "valid": (is_live_data and (active_trade_plan is not None or decision == "WAIT")),
        "has_vix": vix is not None,
        "has_dte": dte is not None,
        "has_atm_iv": avg_atm_iv is not None,
        "iv_missing": is_iv_missing,
        "has_bid_ask": (active_trade_plan.get("bid_at_entry") is not None) if active_trade_plan else False,
        "data_status": data_status,
    }

    morning_bias = compute_morning_trade_bias(
        market_data=market_data,
        spot=spot,
        step=step,
        atm_strike=atm_strike,
        pcr=pcr,
        bullish_score=bullish_score,
        bearish_score=bearish_score,
        r1=r1,
        s1=s1,
        enriched_chain=enriched_chain,
    )

    return {
        "morning_bias": morning_bias,
        "market_bias": {
            "bias": market_bias_str if market_bias_str in ("BULLISH", "BEARISH") else final_bias,
            "confidence": confidence,
            "spot_price": spot,
            "spot_change_pct": spot_chg,
            "bullish_score": bullish_score,
            "bearish_score": bearish_score,
            "pcr": pcr,
            "atm_iv": avg_atm_iv,
            "iv_percentile": iv_percentile,
            "vix": vix,
            "dte": dte,
            "last_changed": bias_state["last_changed_time"],
            "why_reasons": bullish_factors if is_bullish_dir else (bearish_factors if is_bearish_dir else (bullish_factors[:2] + bearish_factors[:2])),
            "net_interpretation": f"Net Bias: {market_bias_str} ({confirm_display} Confirmations: OI, Vol, PCR)"
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
            "decision": decision,  # BUY CE, BUY PE, WAIT
            "action": decision,
            "type": "CE" if "CE" in decision else ("PE" if "PE" in decision else "WAIT"),
            "symbol": symbol,
            "market_direction": market_bias_str,
            "key_level": f"Resistance: {r1} / Support: {s1}",
            "level_status": "CONFIRMED" if (decision in ("BUY CE", "BUY PE") or is_bullish_dir or is_bearish_dir) else "NOT CONFIRMED",
            "momentum_status": "CONFIRMED" if (decision in ("BUY CE", "BUY PE") or momentum_signal in ("BULLISH", "BEARISH")) else "NOT CONFIRMED",
            "volume_status": "SUPPORTIVE" if (volume_signal in ("BULLISH", "BEARISH")) else "NEUTRAL",
            "oi_status": "SUPPORTIVE" if (oi_signal in ("BULLISH", "BEARISH")) else "NEUTRAL",
            "selected_option": active_trade_plan.get("contract_name", "--") if active_trade_plan else "--",
            "entry_price": active_trade_plan.get("entry_price", 0.0) if active_trade_plan else 0.0,
            "data_status": data_status,
            "decision_reason": focus_reason,
            "title": focus_title,
            "reason": focus_reason,
            "setup_score": setup_score,
            "setup_type": setup_type,
            "active_trade_exists": has_active_trade,
            "signals": {
                "price": price_signal,
                "momentum": momentum_signal,
                "oi": oi_signal,
                "volume": volume_signal,
                "pcr": pcr_signal,
                "confirmations": confirm_display,
            },
            "trade_plan": active_trade_plan,
            "checklist": active_checklist,
            "pillar_flags": active_pillar_flags,
            "iv_time": iv_time_info,
            "raw_values": raw_values,
            "data_quality": data_quality_flags,
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
        "pcr": {
            "pcr_ratio": pcr,
            "pcr_chg": pcr_chg,
        },
        "big_oi_movements": big_oi_movements,
        "oi_trend": trend_strikes,
        "trending_oi": trending_oi,
        "detailed_chain": enriched_chain,
        "meta": {
            "symbol": symbol,
            "name": market_data.get("name", symbol),
            "spot_price": spot,
            "spot_change_pct": spot_chg,
            "atm_strike": atm_strike,
            "lot_size": market_data.get("lot_size", 65),
            "expiry": expiry,
            "strike_step": step,
            "available_expiries": market_data.get("available_expiries", []),
            "data_status": market_data.get("data_status", "LIVE"),
            "data_source": market_data.get("data_source", "FYERS"),
            "last_updated": market_data.get("last_updated"),
            "engine_config": ENGINE_CONFIG,
            "oi_thresholds_applied": oi_thresholds,
        }
    }
