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

import os
import math
import logging
import threading
from datetime import datetime
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
    "PRIME_WINDOW_END_MINS": int(os.environ.get("PRIME_WINDOW_END_MINS", 870)),     # 14:30 IST (02:30 PM default cutoff, extended from 14:00)
    "BREAKOUT_WINDOW_END_MINS": int(os.environ.get("BREAKOUT_WINDOW_END_MINS", 885)), # 14:45 IST (02:45 PM on valid high-conviction breakouts)
    "MARKET_SESSION_START_MINS": int(os.environ.get("MARKET_SESSION_START_MINS", 550)), # 09:10 IST
    "MARKET_SESSION_END_MINS": int(os.environ.get("MARKET_SESSION_END_MINS", 930)),   # 15:30 IST
    "CONSECUTIVE_CONFIRMATIONS_REQUIRED": int(os.environ.get("CONSECUTIVE_CONFIRMATIONS_REQUIRED", 1)),
    "SCHEDULER_ENTRY_CONFIRMATIONS": int(os.environ.get("SCHEDULER_ENTRY_CONFIRMATIONS", 2)),
    "ENABLE_50_PIVOT_FILTER": True,           # If current price > 50% pivot of day's High-Low, look for CE; if < 50% pivot, look for PE
    "NIFTY_MAX_OPTION_RISK_POINTS": 8.0, # Configurable maximum option premium risk cap (in points)
    "NIFTY_MIN_OPTION_RISK_POINTS": 3.0, # Minimum option SL buffer floor (in points)
    "POST_EXPANSION_EXHAUSTION_FILTER": True, # Blocks buying options that have expanded > 70% and pulled back > 18% into chop
    "POST_EXPANSION_MIN_GAIN_PCT": 70.0,      # Minimum % expansion required to flag post-expansion regime
    "POST_EXPANSION_DROP_FROM_HIGH_PCT": 18.0,# % drop from intraday high indicating exhaustion / distribution
    "MAX_DAILY_TRADES": int(os.environ.get("MAX_DAILY_TRADES", 25)), # Maximum completed/entered trades allowed per trading day (raised for testing)
    "KEY_LEVELS_NEAR_SUPPORT_THRESHOLD": float(os.environ.get("KEY_LEVELS_NEAR_SUPPORT_THRESHOLD", 0.25)), # <= 25% of S1-R1 range
    "KEY_LEVELS_NEAR_RESISTANCE_THRESHOLD": float(os.environ.get("KEY_LEVELS_NEAR_RESISTANCE_THRESHOLD", 0.75)), # >= 75% of S1-R1 range
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
    }
}


log = logging.getLogger("options_engine")

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
        req_confirmations = ENGINE_CONFIG.get("CONSECUTIVE_CONFIRMATIONS_REQUIRED", 1)

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
                    if state["consecutive_confirmations"] >= req_confirmations:
                        state["last_bias"] = raw_bias
                        state["last_changed_time"] = datetime.now(IST).strftime("%H:%M")

        return state["last_bias"]


def clear_symbol_price_history(symbol: str | None = None) -> None:
    """Safe no-op / backward-compatibility reset hook for tests and session cleanup."""
    pass


def evaluate_market_expansion_and_chop(
    spot: float,
    spot_chg: float,
    strike_step: float,
    price_history: list[float] | None = None,
) -> tuple[bool, bool]:
    """
    Evaluates whether the market is actively expanding or trapped in low-expansion / sideways chop.
    Returns: (expansion_confirmed: bool, is_choppy: bool)
    """
    mom_threshold = float(ENGINE_CONFIG.get("MOMENTUM_CHANGE_THRESHOLD", 0.05))
    if not price_history or len(price_history) < 3:
        is_expanding = abs(spot_chg) >= mom_threshold
        return is_expanding, not is_expanding

    hist = list(price_history)
    range_pts = max(hist) - min(hist)
    avg_spot = sum(hist) / len(hist) if hist else spot
    range_pct = (range_pts / avg_spot * 100) if avg_spot > 0 else 0.0

    deltas = [hist[i] - hist[i - 1] for i in range(1, len(hist))]
    non_zero_deltas = [d for d in deltas if abs(d) >= 1e-4]
    sign_flips = sum(1 for i in range(1, len(non_zero_deltas)) if (non_zero_deltas[i] > 0) != (non_zero_deltas[i - 1] > 0))

    is_choppy = False
    expansion_confirmed = True

    # Compressed price action with weak momentum
    if (range_pct < 0.04 or range_pts < min(1.5, strike_step * 0.03)) and abs(spot_chg) < mom_threshold:
        is_choppy = True
        expansion_confirmed = False

    # Frequent sign flips / alternating chop
    if len(non_zero_deltas) >= 3:
        if sign_flips >= len(non_zero_deltas) - 1 and (abs(spot_chg) < mom_threshold * 2 or range_pct < 0.08):
            is_choppy = True
            expansion_confirmed = False
        elif sign_flips >= 3 and abs(hist[-1] - hist[0]) < (range_pts * 0.3):
            is_choppy = True
            expansion_confirmed = False

    if abs(spot_chg) < mom_threshold:
        expansion_confirmed = False

    return expansion_confirmed, is_choppy


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

    end_mins = ENGINE_CONFIG.get("PRIME_WINDOW_END_MINS", 870)
    end_h = end_mins // 60
    end_m = end_mins % 60
    end_str = f"{end_h:02d}:{end_m:02d}"

    # Trading Session Windows (IST)
    if total_mins < ENGINE_CONFIG["PRIME_WINDOW_START_MINS"]:  # 09:10 - 09:30
        time_status = "OPENING CHOP (09:10-09:30)"
        time_favorable = False
        time_note = "Opening volatility window. Allow market structure to settle."
    elif total_mins >= end_mins:  # Post cutoff
        time_status = f"LATE SESSION THETA DECAY (Post {end_str})"
        time_favorable = False
        time_note = f"Late afternoon session (post {end_str} IST). Exponential Theta decay and squaring-off chop. No fresh buying."
    else:
        time_status = f"PRIME TRADING WINDOW (09:30-{end_str})"
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
        "total_mins": total_mins,
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
    Computes Trending OI metrics over ATM ± num_strikes_each_side strikes (default 5 up, 5 down = 11 strikes)
    following Oi Pulse methodology with comprehensive support for positive, negative, zero, and mixed OI changes:

      - Diff. in OI = Chng. in Put OI - Chng. in Call OI
      - Formulas:
          * Positive Changes (Both PE_chg >= 0 and CE_chg >= 0):
              If PE_chg >= CE_chg: diff_pct = + (PE_chg - CE_chg) / max(PE_chg, 1) * 100, Bullish
              Else:                diff_pct = - (CE_chg - PE_chg) / max(CE_chg, 1) * 100, Bearish
          * Negative Changes (Both unwinding: PE_chg <= 0 and CE_chg <= 0):
              Diff_oi = PE_chg - CE_chg.
              If Diff_oi > 0 (CE unwound more): raw_pct = + (Diff_oi / max(|CE_chg|, |PE_chg|, 1)) * 100, Bullish
              If Diff_oi < 0 (PE unwound more): raw_pct = - (|Diff_oi| / max(|PE_chg|, |CE_chg|, 1)) * 100, Bearish
              If Diff_oi == 0:                  diff_pct = 0, Neutral
          * Mixed Changes (One positive, one negative):
              If PE_chg >= 0 and CE_chg < 0 (Put writing + Call unwinding):
                  raw_pct = + (Diff_oi / max(PE_chg + |CE_chg|, 1)) * 100, Bullish
              If PE_chg < 0 and CE_chg >= 0 (Put unwinding + Call writing):
                  raw_pct = - (|Diff_oi| / max(|PE_chg| + CE_chg, 1)) * 100, Bearish
          * Zero / Inactive (Both == 0):
              diff_pct = 0, sentiment = "Neutral", dots = 0, status = "NEUTRAL"
      - Strength Dots:
          < 30%: 0 dots, gray capsule (weak sentiment)
          30% - 39%: 1 dot, gray capsule
          40% - 59%: 2 dots, active color (>= 40% threshold for conviction)
          >= 60%: 3 dots, active color (strong conviction)
      - Net PCR = Chng. in Put OI / max(Chng. in Call OI, 1) if positive, else ratio of magnitudes.
    """
    if step <= 0:
        step = 50.0

    target_strikes = [round(atm_strike + i * step, 2) for i in range(-num_strikes_each_side, num_strikes_each_side + 1)]

    # Deduplicate and sort rows matching target strikes
    seen_strikes = set()
    selected_rows = []
    for r in enriched_chain:
        stk = r.get("strike")
        if stk is not None and any(abs(float(stk) - s) < 1.0 for s in target_strikes):
            stk_round = round(float(stk), 2)
            if stk_round not in seen_strikes:
                seen_strikes.add(stk_round)
                selected_rows.append(r)
    selected_rows.sort(key=lambda x: float(x["strike"]))

    for r in selected_rows:
        if "ce_activity" not in r:
            ce_c = int(r.get("ce_change_oi") if r.get("ce_change_oi") is not None else (r.get("ce_oich") or 0))
            ce_p = r.get("ce_chp")
            if ce_c > 0:
                r["ce_activity"] = "Long Buildup" if (ce_p is not None and ce_p > 0.05) else ("Call Writing" if (ce_p is not None and ce_p < -0.05) else "Call Buildup")
            elif ce_c < 0:
                r["ce_activity"] = "Short Covering" if (ce_p is not None and ce_p > 0.05) else ("Long Unwinding" if (ce_p is not None and ce_p < -0.05) else "Call Unwinding")
            else:
                r["ce_activity"] = "Neutral"
        if "pe_activity" not in r:
            pe_c = int(r.get("pe_change_oi") if r.get("pe_change_oi") is not None else (r.get("pe_oich") or 0))
            pe_p = r.get("pe_chp")
            if pe_c > 0:
                r["pe_activity"] = "Short Buildup (Writing)" if (pe_p is not None and pe_p < -0.05) else ("Long Buildup" if (pe_p is not None and pe_p > 0.05) else "Put Buildup")
            elif pe_c < 0:
                r["pe_activity"] = "Long Unwinding" if (pe_p is not None and pe_p < -0.05) else ("Short Covering" if (pe_p is not None and pe_p > 0.05) else "Put Unwinding")
            else:
                r["pe_activity"] = "Neutral"

    total_ce_change_oi = sum(int(r.get("ce_change_oi") if r.get("ce_change_oi") is not None else (r.get("ce_oich") or 0)) for r in selected_rows)
    total_pe_change_oi = sum(int(r.get("pe_change_oi") if r.get("pe_change_oi") is not None else (r.get("pe_oich") or 0)) for r in selected_rows)
    total_ce_oi = sum(int(r.get("ce_oi", 0) or 0) for r in selected_rows)
    total_pe_oi = sum(int(r.get("pe_oi", 0) or 0) for r in selected_rows)

    diff_oi = total_pe_change_oi - total_ce_change_oi

    # Calculate difference percentage and sentiment
    if total_pe_change_oi == 0 and total_ce_change_oi == 0:
        diff_pct = 0
        sentiment = "Neutral"
    elif total_pe_change_oi >= 0 and total_ce_change_oi >= 0:
        # Standard Oi Pulse positive buildup regime
        if total_pe_change_oi >= total_ce_change_oi:
            denom = max(total_pe_change_oi, 1)
            raw_pct = ((total_pe_change_oi - total_ce_change_oi) / denom) * 100.0
            diff_pct = min(100, max(0, round(raw_pct)))
            sentiment = "Bullish"
        else:
            denom = max(total_ce_change_oi, 1)
            raw_pct = -((total_ce_change_oi - total_pe_change_oi) / denom) * 100.0
            diff_pct = max(-100, min(0, round(raw_pct)))
            sentiment = "Bearish"
    elif total_pe_change_oi <= 0 and total_ce_change_oi <= 0:
        # Both sides unwinding
        if diff_oi > 0:
            denom = max(abs(total_ce_change_oi), abs(total_pe_change_oi), 1)
            raw_pct = (diff_oi / denom) * 100.0
            diff_pct = min(100, max(0, round(raw_pct)))
            sentiment = "Bullish"
        elif diff_oi < 0:
            denom = max(abs(total_pe_change_oi), abs(total_ce_change_oi), 1)
            raw_pct = (diff_oi / denom) * 100.0
            diff_pct = max(-100, min(0, round(raw_pct)))
            sentiment = "Bearish"
        else:
            diff_pct = 0
            sentiment = "Neutral"
    elif total_pe_change_oi >= 0 and total_ce_change_oi < 0:
        # PE writing + CE unwinding (Clear Bullish)
        activity = total_pe_change_oi + abs(total_ce_change_oi)
        denom = max(activity, 1)
        raw_pct = (diff_oi / denom) * 100.0
        diff_pct = min(100, max(0, round(raw_pct)))
        sentiment = "Bullish"
    else:
        # PE unwinding + CE writing (Clear Bearish)
        activity = abs(total_pe_change_oi) + total_ce_change_oi
        denom = max(activity, 1)
        raw_pct = (diff_oi / denom) * 100.0
        diff_pct = max(-100, min(0, round(raw_pct)))
        sentiment = "Bearish"

    abs_pct = abs(diff_pct)
    if sentiment == "Neutral" or abs_pct < 30:
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
        strength_status = "STRONG"
        sentiment = "Strong Bullish" if diff_pct > 0 else "Strong Bearish"

    if total_ce_change_oi > 0:
        net_pcr = round(total_pe_change_oi / total_ce_change_oi, 2)
    elif total_ce_change_oi < 0:
        if total_pe_change_oi < 0:
            net_pcr = round(abs(total_pe_change_oi) / abs(total_ce_change_oi), 2)
        else:
            net_pcr = 9.99
    else:
        net_pcr = 1.0 if total_pe_change_oi == 0 else (9.99 if total_pe_change_oi > 0 else 0.01)

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
        "strength_pct": diff_pct,
        "dots": dots,
        "strength_dots": dots,
        "conviction_confirmed": bool(abs(diff_pct) >= 40 and dots >= 2),
        "is_grayed_out": bool(abs(diff_pct) < 30),
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
            trigger_str = f"Buy CE on sustained move above CPR Top (₹{cpr_top:.1f}) with bullish volume"
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
            trigger_str = f"Buy PE on sustained move below CPR Bottom (₹{cpr_bottom:.1f}) with bearish volume"
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
            "risk_reward": f"1:{rr_ratio:.1f}",
        },
        "why_reasons": why_reasons,
    }


def compute_tomorrow_trade_bias(
    market_data: dict,
    spot: float,
    step: float,
    pcr: float,
    bullish_score: float,
    bearish_score: float,
    r1: float,
    s1: float,
    trending_oi: dict | None = None,
) -> dict:
    """
    Computes deterministic Tomorrow's Bias / Next Day Outlook based on:
    1. Today's OHLC & Next-Day Central Pivot Range (Pivot, TC, BC, R1, S1)
    2. EOD Open Interest accumulation, PCR, and Trending OI dominance
    3. Overhead Call walls and downside Put bases
    """
    today_high = float(market_data.get("high_price") or market_data.get("high") or market_data.get("prev_high") or (spot + step))
    today_low = float(market_data.get("low_price") or market_data.get("low") or market_data.get("prev_low") or (spot - step))
    today_close = spot

    if today_high <= today_low:
        today_high = max(today_high, today_low + step)

    pivot = round((today_high + today_low + today_close) / 3.0, 1)
    bc = round((today_high + today_low) / 2.0, 1)
    tc = round((pivot - bc) + pivot, 1)
    cpr_width = round(abs(tc - bc), 1)
    cpr_type = "NARROW (TRENDING)" if (cpr_width / pivot * 100 <= 0.25) else ("WIDE (RANGEBOUND)" if (cpr_width / pivot * 100 >= 0.55) else "AVERAGE")

    tomorrow_r1 = round(2 * pivot - today_low, 1)
    tomorrow_s1 = round(2 * pivot - today_high, 1)

    diff_pct = float(trending_oi.get("diff_pct", 0.0)) if trending_oi else 0.0
    sentiment = (trending_oi.get("sentiment") or "").upper() if trending_oi else ""

    call_wall = f"{r1:,.0f} CE"
    put_base = f"{s1:,.0f} PE"

    if bullish_score > bearish_score and (bullish_score >= 55 or diff_pct >= 30 or "BULLISH" in sentiment):
        direction = "BULLISH"
        badge = "BULLISH (BUY-ON-DIPS)"
        badge_class = "bullish"
        detail = f"Next Pivot: {pivot:,.0f} | CPR: {cpr_type} | Range: {tomorrow_s1:,.0f} - {tomorrow_r1:,.0f} | Resist: {call_wall} | Base: {put_base}"
    elif bearish_score > bullish_score and (bearish_score >= 55 or diff_pct <= -30 or "BEARISH" in sentiment):
        direction = "BEARISH"
        badge = "BEARISH (SELL-ON-RISE)"
        badge_class = "bearish"
        detail = f"Next Pivot: {pivot:,.0f} | CPR: {cpr_type} | Range: {tomorrow_s1:,.0f} - {tomorrow_r1:,.0f} | Resist: {call_wall} | Base: {put_base}"
    elif bearish_score >= 60 or diff_pct <= -30 or "BEARISH" in sentiment:
        direction = "BEARISH"
        badge = "BEARISH (SELL-ON-RISE)"
        badge_class = "bearish"
        detail = f"Next Pivot: {pivot:,.0f} | CPR: {cpr_type} | Range: {tomorrow_s1:,.0f} - {tomorrow_r1:,.0f} | Resist: {call_wall} | Base: {put_base}"
    elif bullish_score >= 60 or diff_pct >= 30 or "BULLISH" in sentiment:
        direction = "BULLISH"
        badge = "BULLISH (BUY-ON-DIPS)"
        badge_class = "bullish"
        detail = f"Next Pivot: {pivot:,.0f} | CPR: {cpr_type} | Range: {tomorrow_s1:,.0f} - {tomorrow_r1:,.0f} | Resist: {call_wall} | Base: {put_base}"
    else:
        direction = "RANGEBOUND"
        badge = "RANGEBOUND (WAIT FOR BREAKOUT)"
        badge_class = "neutral"
        detail = f"Expected Range: {tomorrow_s1:,.0f} - {tomorrow_r1:,.0f} | Pivot: {pivot:,.0f} | CPR: {cpr_type}"

    return {
        "direction": direction,
        "badge": badge,
        "badge_class": badge_class,
        "pivot": pivot,
        "r1": tomorrow_r1,
        "s1": tomorrow_s1,
        "key_wall": call_wall,
        "key_support": put_base,
        "cpr_type": cpr_type,
        "detail": detail,
    }


def compute_oi_wall_metrics(
    spot: float | None,
    s1: float | None,
    r1: float | None,
    s2: float | None = None,
    r2: float | None = None,
    atm: float | None = None,
    strike_step: float = 50.0,
) -> dict:
    """
    Computes deterministic Key Levels (OI walls) display metrics:
    - position_pct: proportional position of spot between S1 (0%) and R1 (100%), clamped [0.0, 100.0]
    - room_ratio: (R1 - spot) / max(spot - S1, strike_step * 0.1), rounded to 1 decimal
    - status_chip: BREAKOUT, BREAKDOWN, NEAR SUPPORT, NEAR RESISTANCE, MID-RANGE, or UNAVAILABLE
    - status_text: human-readable status explanation
    - dist_to_s1: round(spot - S1, 1) in points
    - dist_to_r1: round(R1 - spot, 1) in points
    - atm_tick_pct: proportional position of ATM if it differs from S1 and R1, else None
    - is_available: True if S1 and R1 are valid numbers with R1 > S1, else False
    """
    near_sup_thresh = float(ENGINE_CONFIG.get("KEY_LEVELS_NEAR_SUPPORT_THRESHOLD", 0.25))
    near_res_thresh = float(ENGINE_CONFIG.get("KEY_LEVELS_NEAR_RESISTANCE_THRESHOLD", 0.75))

    # Edge cases: missing S1/R1 or non-positive / invalid ordering
    if (
        spot is None
        or s1 is None
        or r1 is None
        or s1 <= 0
        or r1 <= 0
        or r1 <= s1
    ):
        return {
            "is_available": False,
            "position_pct": None,
            "room_ratio": None,
            "status_chip": "UNAVAILABLE",
            "status_text": "OI walls unavailable",
            "dist_to_s1": None,
            "dist_to_r1": None,
            "atm_tick_pct": None,
            "s1": s1,
            "r1": r1,
            "s2": s2,
            "r2": r2,
            "atm": atm,
        }

    range_pts = r1 - s1
    dist_s1 = round(spot - s1, 1)
    dist_r1 = round(r1 - spot, 1)

    raw_pos = (spot - s1) / range_pts
    clamped_pos = max(0.0, min(1.0, raw_pos))
    pos_pct = round(clamped_pos * 100.0, 1)

    # Status chip calculation
    if spot > r1:
        status_chip = "BREAKOUT"
        status_text = f"Spot is trading above R1 ({r1:,.0f}) in breakout territory"
    elif spot < s1:
        status_chip = "BREAKDOWN"
        status_text = f"Spot is trading below S1 ({s1:,.0f}) in breakdown territory"
    elif raw_pos <= near_sup_thresh:
        status_chip = "NEAR SUPPORT"
        status_text = f"Spot is within {int(near_sup_thresh * 100)}% of S1 Support ({s1:,.0f})"
    elif raw_pos >= near_res_thresh:
        status_chip = "NEAR RESISTANCE"
        status_text = f"Spot is within {int((1.0 - near_res_thresh) * 100)}% of R1 Resistance ({r1:,.0f})"
    else:
        status_chip = "MID-RANGE"
        status_text = f"Spot is trading in the mid-range channel between S1 ({s1:,.0f}) and R1 ({r1:,.0f})"

    # Room/risk metric: (R1 - spot) / max(spot - S1, strike_step * 0.1); 1 decimal
    step_floor = max(0.1, strike_step * 0.1)
    denom = max(spot - s1, step_floor)
    room_ratio = round((r1 - spot) / denom, 1)

    # ATM tick on bar only when it differs from S1 and R1
    atm_tick_pct = None
    if atm is not None and atm > 0 and abs(atm - s1) > 0.01 and abs(atm - r1) > 0.01:
        atm_pos = max(0.0, min(1.0, (atm - s1) / range_pts))
        atm_tick_pct = round(atm_pos * 100.0, 1)

    return {
        "is_available": True,
        "position_pct": pos_pct,
        "room_ratio": room_ratio,
        "status_chip": status_chip,
        "status_text": status_text,
        "dist_to_s1": dist_s1,
        "dist_to_r1": dist_r1,
        "atm_tick_pct": atm_tick_pct,
        "s1": s1,
        "r1": r1,
        "s2": s2,
        "r2": r2,
        "atm": atm,
    }


def compute_market_vector(
    bias: str,
    bullish_score: float,
    bearish_score: float,
    pcr: float,
    spot: float,
    atm: float,
    r1: float,
    r2: float,
    s1: float,
    s2: float,
    trending_oi: dict | None = None,
) -> dict:
    """
    Computes unified, deterministic Market Vector (BULLISH / BEARISH / RANGEBOUND)
    and plain-English Seller Actions + Actionable Playbook.
    Guaranteed to stay perfectly aligned with Market Bias, Trending OI, and PCR.
    """
    diff_pct = float(trending_oi.get("diff_pct", 0.0)) if trending_oi else 0.0
    sentiment = (trending_oi.get("sentiment") or "").upper() if trending_oi else ""

    is_bearish = (
        bias == "BEARISH"
        or diff_pct <= -35.0
        or (pcr < 0.85 and bearish_score > bullish_score)
        or "BEARISH" in sentiment
    )
    is_bullish = (
        not is_bearish
        and (
            bias == "BULLISH"
            or diff_pct >= 35.0
            or (pcr > 1.15 and bullish_score > bearish_score)
            or "BULLISH" in sentiment
        )
    )

    call_wall = f"{r1:,.0f} CE"
    put_base = f"{s1:,.0f} PE"

    if is_bearish:
        conviction = int(min(98, max(70, round(max(bearish_score, abs(diff_pct))))))
        mode = "PE"
        mode_pill = "PURE PE PLAY (SELL-ON-RISE)"
        mode_class = "bearish"
        title = f"BEARISH VECTOR ({conviction}% Conviction)"
        seller_action = f"Institutional Call Writing dominant at {call_wall}. Put writers unwinding support floor."
        playbook = f"Sell-on-Rise: Rallies towards R1 ({r1:,.0f}) face heavy institutional supply. Downside target: S1 ({s1:,.0f}) / S2 ({s2:,.0f})."
    elif is_bullish:
        conviction = int(min(98, max(70, round(max(bullish_score, abs(diff_pct))))))
        mode = "CE"
        mode_pill = "PURE CE PLAY (BUY-ON-DIPS)"
        mode_class = "bullish"
        title = f"BULLISH VECTOR ({conviction}% Conviction)"
        seller_action = f"Institutional Put Writing dominant at {put_base}. Call writers trapped and short-covering."
        playbook = f"Buy-on-Dip: Dips towards S1 ({s1:,.0f}) offer high probability support. Upside target: R1 ({r1:,.0f}) / R2 ({r2:,.0f})."
    else:
        conviction = 50
        mode = "WAIT"
        mode_pill = "AVOID NAKED BUYING (CHOP TRAP)"
        mode_class = "neutral"
        title = "RANGEBOUND VECTOR (50% Conviction)"
        seller_action = f"Two-way writing active around ATM ({atm:,.0f}). Diff OI under 30L; sellers eating premium from buyers."
        playbook = f"Preserve Capital: Avoid buying in middle of channel ({s1:,.0f} - {r1:,.0f}). Wait for confirmed breakout above R1 or breakdown below S1."

    return {
        "title": title,
        "mode": mode,
        "mode_pill": mode_pill,
        "mode_class": mode_class,
        "conviction_pct": conviction,
        "seller_action": seller_action,
        "playbook": playbook,
    }


def analyze_option_desk(market_data: dict, update_state: bool = False, has_active_trade: bool | None = None) -> dict:
    """
    Comprehensive Options Analytical Engine with Selective Option Buying Decision Flow:
    5 Core Signals (Price, Momentum, OI, Volume, PCR) -> 2/3 Confirmations -> Breakout / Market Expansion -> Active Trade Check -> BUY CE/BUY PE/WAIT
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
                "resistance_1": None,
                "resistance_2": None,
                "atm_strike": round(spot / step) * step if step else spot,
                "support_1": None,
                "support_2": None,
                "breakout_level": None,
                "breakdown_level": None,
                "spot_diff": 0.0,
                "is_available": False,
                "position_pct": None,
                "room_ratio": None,
                "status_chip": "UNAVAILABLE",
                "status_text": "OI walls unavailable",
                "dist_to_s1": None,
                "dist_to_r1": None,
                "atm_tick_pct": None,
                "nearest_barriers": {
                    "support": {"strike": None, "type": "PE", "oi": 0, "oi_formatted": "--", "change_oi": 0, "change_formatted": "--", "arrow": "―", "arrow_direction": "neutral", "arrow_class": "arrow-flat", "trend": "FLAT", "status": "Unavailable", "ltp": 0.0, "dist_pts": 0.0},
                    "resistance": {"strike": None, "type": "CE", "oi": 0, "oi_formatted": "--", "change_oi": 0, "change_formatted": "--", "arrow": "―", "arrow_direction": "neutral", "arrow_class": "arrow-flat", "trend": "FLAT", "status": "Unavailable", "ltp": 0.0, "dist_pts": 0.0},
                    "spot_price": spot,
                },
            },
            "nearest_barriers": {
                "support": {"strike": None, "type": "PE", "oi": 0, "oi_formatted": "--", "change_oi": 0, "change_formatted": "--", "arrow": "―", "arrow_direction": "neutral", "arrow_class": "arrow-flat", "trend": "FLAT", "status": "Unavailable", "ltp": 0.0, "dist_pts": 0.0},
                "resistance": {"strike": None, "type": "CE", "oi": 0, "oi_formatted": "--", "change_oi": 0, "change_formatted": "--", "arrow": "―", "arrow_direction": "neutral", "arrow_class": "arrow-flat", "trend": "FLAT", "status": "Unavailable", "ltp": 0.0, "dist_pts": 0.0},
                "spot_price": spot,
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
                    "risk_reward": "1:2.0",
                },
                "why_reasons": ["Market data currently unavailable from FYERS. Fail-closed discipline active."],
            },
            "tomorrow_bias": {
                "direction": "NEUTRAL",
                "badge": "ANALYZING...",
                "badge_class": "neutral",
                "pivot": spot,
                "r1": spot + step,
                "s1": spot - step,
                "key_wall": f"{spot + step:,.0f} CE",
                "key_support": f"{spot - step:,.0f} PE",
                "cpr_type": "AVERAGE",
                "detail": "Data unavailable. Evaluating EOD market structure...",
            },
            "market_vector": {
                "title": "RANGEBOUND VECTOR (Awaiting Live Flow)",
                "mode": "WAIT",
                "mode_pill": "⏸ AWAITING DATA",
                "mode_class": "neutral",
                "conviction_pct": 50,
                "seller_action": "Market data currently unavailable from broker.",
                "playbook": "Preserve Capital: Do not place orders until live data feeds are active.",
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
        ce_chg = int(row.get("ce_change_oi") if row.get("ce_change_oi") is not None else (row.get("ce_oich") or 0))
        ce_ltp = float(row.get("ce_ltp", 0.0) or 0.0)
        ce_vol = int(row.get("ce_volume", 0) or 0)
        ce_iv = float(row.get("ce_iv", 0.0) or 0.0)
        ce_delta = float(row.get("ce_delta", 0.0) or 0.0)
        ce_bid = float(row.get("ce_bid", 0.0) or 0.0)
        ce_ask = float(row.get("ce_ask", 0.0) or 0.0)

        pe_oi = int(row.get("pe_oi", 0) or 0)
        pe_chg = int(row.get("pe_change_oi") if row.get("pe_change_oi") is not None else (row.get("pe_oich") or 0))
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

        # Classify Call side using symbol threshold and premium change heuristic
        ce_chp = row.get("ce_chp")
        if ce_chp is not None:
            try:
                ce_chp = float(ce_chp)
            except (ValueError, TypeError):
                ce_chp = None

        pe_chp = row.get("pe_chp")
        if pe_chp is not None:
            try:
                pe_chp = float(pe_chp)
            except (ValueError, TypeError):
                pe_chp = None

        ce_act = "NO CLEAR SIGNAL"
        if abs(ce_chg) >= oi_thresholds["WRITING"]:
            if ce_chg > 0:
                if ce_chp is not None and ce_chp > 0.05:
                    ce_act = "LONG CALL BUILDUP"  # OI rising + premium rising
                elif ce_chp is not None and ce_chp < -0.05:
                    ce_act = "CALL WRITING"       # OI rising + premium falling
                else:
                    ce_act = "CALL WRITING" if strike >= spot - step else "LONG CALL BUILDUP"
            else:
                if ce_chp is not None and ce_chp > 0.05:
                    ce_act = "CALL SHORT COVERING" # OI falling + premium rising
                elif ce_chp is not None and ce_chp < -0.05:
                    ce_act = "CALL UNWINDING"      # OI falling + premium falling
                else:
                    ce_act = "CALL UNWINDING"

        # Classify Put side using symbol threshold and premium change heuristic
        pe_act = "NO CLEAR SIGNAL"
        if abs(pe_chg) >= oi_thresholds["WRITING"]:
            if pe_chg > 0:
                if pe_chp is not None and pe_chp > 0.05:
                    pe_act = "LONG PUT BUILDUP"   # OI rising + premium rising
                elif pe_chp is not None and pe_chp < -0.05:
                    pe_act = "PUT WRITING"        # OI rising + premium falling
                else:
                    pe_act = "PUT WRITING" if strike <= spot + step else "LONG PUT BUILDUP"
            else:
                if pe_chp is not None and pe_chp > 0.05:
                    pe_act = "PUT SHORT COVERING" # OI falling + premium rising
                elif pe_chp is not None and pe_chp < -0.05:
                    pe_act = "PUT UNWINDING"      # OI falling + premium falling
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

    # Dynamic Nearest Support & Resistance Barriers relative to live spot price
    res_candidates = [r for r in enriched_chain if r["strike"] > (spot + 0.5)]
    sup_candidates = [r for r in enriched_chain if r["strike"] < (spot - 0.5)]

    def _bar_res_score(r):
        dist = max(0.0, r["strike"] - spot)
        proximity = 1.0 / (1.0 + (dist / (step * 2.5)))
        writing_bonus = 1.3 if "WRITING" in r.get("ce_activity", "") else 1.0
        return (r.get("ce_oi", 0) * 0.5 + max(0, r.get("ce_change_oi", 0)) * 0.35 + r.get("ce_volume", 0) * 0.15) * proximity * writing_bonus

    def _bar_sup_score(r):
        dist = max(0.0, spot - r["strike"])
        proximity = 1.0 / (1.0 + (dist / (step * 2.5)))
        writing_bonus = 1.3 if "WRITING" in r.get("pe_activity", "") else 1.0
        return (r.get("pe_oi", 0) * 0.5 + max(0, r.get("pe_change_oi", 0)) * 0.35 + r.get("pe_volume", 0) * 0.15) * proximity * writing_bonus

    bar_res_row = max(res_candidates, key=_bar_res_score) if res_candidates else next((r for r in enriched_chain if abs(r["strike"] - r1) < 1.0), {})
    bar_sup_row = max(sup_candidates, key=_bar_sup_score) if sup_candidates else next((r for r in enriched_chain if abs(r["strike"] - s1) < 1.0), {})

    res_bar_strike = bar_res_row.get("strike", r1)
    sup_bar_strike = bar_sup_row.get("strike", s1)

    s1_pe_chg = int(bar_sup_row.get("pe_change_oi", 0) or 0)
    s1_pe_oi = int(bar_sup_row.get("pe_oi", 0) or 0)
    s1_pe_ltp = float(bar_sup_row.get("pe_ltp", 0.0) or 0.0)

    r1_ce_chg = int(bar_res_row.get("ce_change_oi", 0) or 0)
    r1_ce_oi = int(bar_res_row.get("ce_oi", 0) or 0)
    r1_ce_ltp = float(bar_res_row.get("ce_ltp", 0.0) or 0.0)

    nearest_barriers = {
        "support": {
            "strike": sup_bar_strike,
            "type": "PE",
            "oi": s1_pe_oi,
            "oi_formatted": format_lakhs(s1_pe_oi),
            "change_oi": s1_pe_chg,
            "change_formatted": format_lakhs(s1_pe_chg),
            "arrow": "▲" if s1_pe_chg > 0 else ("▼" if s1_pe_chg < 0 else "―"),
            "arrow_direction": "up" if s1_pe_chg > 0 else ("down" if s1_pe_chg < 0 else "neutral"),
            "arrow_class": "arrow-up-green" if s1_pe_chg > 0 else ("arrow-down-red" if s1_pe_chg < 0 else "arrow-neutral"),
            "trend": "ADDING" if s1_pe_chg > 0 else ("EXITING" if s1_pe_chg < 0 else "FLAT"),
            "status": "Put Writers Adding" if s1_pe_chg > 0 else ("Put Writers Exiting" if s1_pe_chg < 0 else "Neutral Flow"),
            "ltp": s1_pe_ltp,
            "dist_pts": max(0.0, round(spot - sup_bar_strike, 1)),
        },
        "resistance": {
            "strike": res_bar_strike,
            "type": "CE",
            "oi": r1_ce_oi,
            "oi_formatted": format_lakhs(r1_ce_oi),
            "change_oi": r1_ce_chg,
            "change_formatted": format_lakhs(r1_ce_chg),
            "arrow": "▲" if r1_ce_chg > 0 else ("▼" if r1_ce_chg < 0 else "―"),
            "arrow_direction": "up" if r1_ce_chg > 0 else ("down" if r1_ce_chg < 0 else "neutral"),
            "arrow_class": "arrow-up-amber" if r1_ce_chg > 0 else ("arrow-down-green" if r1_ce_chg < 0 else "arrow-neutral"),
            "trend": "ADDING" if r1_ce_chg > 0 else ("EXITING" if r1_ce_chg < 0 else "FLAT"),
            "status": "Call Writers Adding" if r1_ce_chg > 0 else ("Call Writers Exiting" if r1_ce_chg < 0 else "Neutral Flow"),
            "ltp": r1_ce_ltp,
            "dist_pts": max(0.0, round(res_bar_strike - spot, 1)),
        },
        "spot_price": spot,
    }

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
    iv_time_info = evaluate_iv_and_time_filter(avg_atm_iv, now_dt=market_data.get("now_dt"))

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

    # Signal 3: OI Structure (OI-Led Directional Evaluation)
    writing_threshold = oi_thresholds.get("WRITING", 30000)
    unwind_threshold = oi_thresholds.get("UNWINDING", 50000)
    activity_threshold = oi_thresholds.get("ACTIVITY", 40000)

    has_pe_writing_dominance = (near_pe_writing >= writing_threshold and near_pe_writing > near_ce_writing * 1.15)
    has_ce_unwind_squeeze = (near_ce_unwinding >= unwind_threshold and near_ce_unwinding > near_pe_unwinding * 1.15)
    has_net_bullish_flow = (total_pe_change_oi - total_ce_change_oi) >= activity_threshold

    has_ce_writing_dominance = (near_ce_writing >= writing_threshold and near_ce_writing > near_pe_writing * 1.15)
    has_pe_unwind_panic = (near_pe_unwinding >= unwind_threshold and near_pe_unwinding > near_ce_unwinding * 1.15)
    has_net_bearish_flow = (total_ce_change_oi - total_pe_change_oi) >= activity_threshold

    is_oi_bullish = (has_pe_writing_dominance or has_ce_unwind_squeeze or has_net_bullish_flow) and not (has_ce_writing_dominance or has_pe_unwind_panic)
    is_oi_bearish = (has_ce_writing_dominance or has_pe_unwind_panic or has_net_bearish_flow) and not (has_pe_writing_dominance or has_ce_unwind_squeeze)

    if is_oi_bullish:
        oi_signal = "BULLISH"
    elif is_oi_bearish:
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

    # Confirmations from (OI, Volume, PCR)
    bull_confirm_count = sum(1 for s in (oi_signal, volume_signal, pcr_signal) if s == "BULLISH")
    bear_confirm_count = sum(1 for s in (oi_signal, volume_signal, pcr_signal) if s == "BEARISH")

    # 50% Intraday Range Pivot Gate (High & Low of today):
    # If current price is above 50% pivot level of low and high: Look for CE (Bullish half)
    # If current price is below 50% pivot level of low and high: Look for PE (Bearish half)
    today_high = float(market_data.get("high_price") or market_data.get("high") or market_data.get("today_high") or 0.0)
    today_low = float(market_data.get("low_price") or market_data.get("low") or market_data.get("today_low") or 0.0)
    has_valid_day_range = (today_high > 0.0 and today_low > 0.0 and today_high > today_low)
    pivot_50 = round((today_high + today_low) / 2.0, 2) if has_valid_day_range else spot
    pivot_filter_enabled = ENGINE_CONFIG.get("ENABLE_50_PIVOT_FILTER", True)

    pivot_50_ce_blocked = pivot_filter_enabled and has_valid_day_range and (spot < pivot_50)
    pivot_50_pe_blocked = pivot_filter_enabled and has_valid_day_range and (spot > pivot_50)
    pivot_50_msg = (
        f"50%_PIVOT_BLOCK (Spot {spot:,.1f} is above 50% Pivot {pivot_50:,.1f} [Day L: {today_low:,.1f} | H: {today_high:,.1f}]. Only CE allowed in upper half of day range)."
        if pivot_50_pe_blocked else
        f"50%_PIVOT_BLOCK (Spot {spot:,.1f} is below 50% Pivot {pivot_50:,.1f} [Day L: {today_low:,.1f} | H: {today_high:,.1f}]. Only PE allowed in lower half of day range)."
    )

    # OI-Led directional gate: OI MUST agree with direction; price context must not conflict.
    # Price is a sanity check (spot_chg not contradicting); momentum is not counted as an independent vote.
    is_bullish_dir = (oi_signal == "BULLISH" and spot_chg >= 0.0 and bull_confirm_count >= 2)
    is_bearish_dir = (oi_signal == "BEARISH" and spot_chg <= 0.0 and bear_confirm_count >= 2)


    # Market Expansion and Chop Evaluation
    custom_price_hist = market_data.get("price_history")
    expansion_confirmed, is_choppy = evaluate_market_expansion_and_chop(
        spot=spot, spot_chg=spot_chg, strike_step=step, price_history=custom_price_hist
    )
    setup_type = "TRENDING_BREAKOUT" if is_bullish_dir else ("TRENDING_BREAKDOWN" if is_bearish_dir else "NEUTRAL")

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

        # Technical Invalidation & Maximum Option Premium Risk Management
        delta = abs(float(opt_row.get("ce_delta" if is_ce else "pe_delta", 0.5) or 0.5))
        if delta < 0.15 or delta > 0.95:
            delta = 0.50

        if is_ce:
            spot_risk_pts = max(spot - spot_invalidation, 0.0) if spot and spot_invalidation else 0.0
        else:
            spot_risk_pts = max(spot_invalidation - spot, 0.0) if spot and spot_invalidation else 0.0

        tech_option_risk = round(spot_risk_pts * delta, 1) if spot_risk_pts > 0 else None
        max_risk_cap = float(ENGINE_CONFIG.get("NIFTY_MAX_OPTION_RISK_POINTS", 8.0))
        min_risk_floor = float(ENGINE_CONFIG.get("NIFTY_MIN_OPTION_RISK_POINTS", 3.0))

        if tech_option_risk is not None and tech_option_risk > 0:
            risk_per_contract = min(tech_option_risk, max_risk_cap)
        else:
            risk_per_contract = max_risk_cap

        risk_per_contract = max(risk_per_contract, min_risk_floor)
        risk_per_contract = min(risk_per_contract, round(fill_entry_price * 0.5, 1))
        risk_per_contract = round(risk_per_contract, 1)

        sl_price = round(max(fill_entry_price - risk_per_contract, 0.05), 2)
        t1_price = round(fill_entry_price + (risk_per_contract * 1.5), 2)
        t2_price = round(fill_entry_price + (risk_per_contract * 2.5), 2)
        rr_str = "1:2.0"

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

    # 10. Check Daily Trade Limit, Consecutive Loss Cooldown & Active Trade Constraint
    if has_active_trade is None:
        try:
            from app.services.options_signal_service import has_active_signal_for_symbol
            has_active_trade = has_active_signal_for_symbol(symbol)
        except Exception:
            has_active_trade = False

    daily_trade_count = market_data.get("daily_trade_count")
    if daily_trade_count is None:
        try:
            from app.services.options_signal_service import get_daily_trade_count
            daily_trade_count = get_daily_trade_count(symbol)
        except Exception:
            daily_trade_count = 0

    max_daily_trades = ENGINE_CONFIG.get("MAX_DAILY_TRADES", 3)

    in_loss_cooldown = False
    loss_cooldown_reason = ""
    try:
        from app.services.options_signal_service import check_consecutive_loss_cooldown
        in_loss_cooldown, loss_cooldown_reason, _ = check_consecutive_loss_cooldown(symbol)
    except Exception:
        in_loss_cooldown = False

    active_trade_str = "YES" if has_active_trade else "NO"

    # Major Round Strike Defense (Psychological Wall & Century Barrier Protection)
    major_round_step = 500.0
    century_step = 100.0

    nearest_round_ceil = math.ceil(spot / major_round_step) * major_round_step
    nearest_round_floor = math.floor(spot / major_round_step) * major_round_step
    nearest_century_ceil = math.ceil(spot / century_step) * century_step
    nearest_century_floor = math.floor(spot / century_step) * century_step

    round_ce_blocked = False
    round_ce_msg = ""

    # Check 1: Major Round Level (500/1000) within tighter 0.60*step buffer (30 pts on NIFTY)
    if 0 < (nearest_round_ceil - spot) <= (step * 0.60):
        round_strike_rows = [r for r in enriched_chain if abs(r["strike"] - nearest_round_ceil) < 1.0]
        if round_strike_rows:
            round_ce_chg = int(round_strike_rows[0].get("ce_change_oi", 0) or 0)
            round_ce_oi = int(round_strike_rows[0].get("ce_oi", 0) or 0)
            round_pe_oi = int(round_strike_rows[0].get("pe_oi", 0) or 0)
            if round_ce_chg > 0 or (round_ce_oi > 0 and round_ce_oi >= round_pe_oi * 1.2):
                round_ce_blocked = True
                round_ce_msg = f"MAJOR_ROUND_RESISTANCE_BLOCK (Directly under {int(nearest_round_ceil)} CE Wall with active Call writing/dominant OI)"

    # Check 2: Round Century Level (100-multiple) with active Call writing within 0.50*step buffer
    if not round_ce_blocked and 0 < (nearest_century_ceil - spot) <= (step * 0.50):
        century_strike_rows = [r for r in enriched_chain if abs(r["strike"] - nearest_century_ceil) < 1.0]
        if century_strike_rows:
            century_ce_chg = int(century_strike_rows[0].get("ce_change_oi", 0) or 0)
            if century_ce_chg > 0:
                round_ce_blocked = True
                round_ce_msg = f"MAJOR_ROUND_RESISTANCE_BLOCK (Directly under {int(nearest_century_ceil)} CE Wall with active Call writing)"

    round_pe_blocked = False
    round_pe_msg = ""

    # Check 1: Major Round Level (500/1000) within tighter 0.60*step buffer (30 pts on NIFTY)
    if 0 < (spot - nearest_round_floor) <= (step * 0.60):
        round_strike_rows = [r for r in enriched_chain if abs(r["strike"] - nearest_round_floor) < 1.0]
        if round_strike_rows:
            round_pe_chg = int(round_strike_rows[0].get("pe_change_oi", 0) or 0)
            round_pe_oi = int(round_strike_rows[0].get("pe_oi", 0) or 0)
            round_ce_oi = int(round_strike_rows[0].get("ce_oi", 0) or 0)
            if round_pe_chg > 0 or (round_pe_oi > 0 and round_pe_oi >= round_ce_oi * 1.2):
                round_pe_blocked = True
                round_pe_msg = f"MAJOR_ROUND_SUPPORT_BLOCK (Directly above {int(nearest_round_floor)} PE Wall with active Put writing/dominant OI)"

    # Check 2: Round Century Level (100-multiple) with active Put writing within 0.50*step buffer
    if not round_pe_blocked and 0 < (spot - nearest_century_floor) <= (step * 0.50):
        century_strike_rows = [r for r in enriched_chain if abs(r["strike"] - nearest_century_floor) < 1.0]
        if century_strike_rows:
            century_pe_chg = int(century_strike_rows[0].get("pe_change_oi", 0) or 0)
            if century_pe_chg > 0:
                round_pe_blocked = True
                round_pe_msg = f"MAJOR_ROUND_SUPPORT_BLOCK (Directly above {int(nearest_century_floor)} PE Wall with active Put writing)"

    # Post-Expansion Exhaustion Protection (Anti-Chop / Anti-Late-Entry Protection)
    ce_exhausted = False
    ce_exhaustion_msg = ""
    pe_exhausted = False
    pe_exhaustion_msg = ""

    if ENGINE_CONFIG.get("POST_EXPANSION_EXHAUSTION_FILTER", True):
        from app.services.fyers_options_service import get_option_contract_stats
        min_gain_pct = float(ENGINE_CONFIG.get("POST_EXPANSION_MIN_GAIN_PCT", 70.0))
        drop_threshold_pct = float(ENGINE_CONFIG.get("POST_EXPANSION_DROP_FROM_HIGH_PCT", 18.0))

        if best_ce:
            ce_name = f"{int(best_ce.get('strike', 0))} CE"
            ce_ltp = float(best_ce.get("ce_ltp", 0.0) or 0.0)
            ce_chp = float(best_ce.get("ce_chp", 0.0) or 0.0)
            ce_high = float(best_ce.get("ce_high") or best_ce.get("high_price") or 0.0)
            ce_low = float(best_ce.get("ce_low") or best_ce.get("low_price") or 0.0)
            ce_sym = best_ce.get("ce_symbol")

            if (ce_high <= 0.0 or ce_chp == 0.0) and ce_sym:
                stats = get_option_contract_stats(ce_sym)
                if stats:
                    ce_high = float(stats.get("high_price", 0.0) or 0.0)
                    ce_low = float(stats.get("low_price", 0.0) or 0.0)
                    ce_chp = float(stats.get("chp", ce_chp) or ce_chp)

            ce_expansion_pct = ((ce_high - ce_low) / ce_low * 100) if (ce_high > 0 and ce_low > 0) else ce_chp
            ce_drop_pct = ((ce_high - ce_ltp) / ce_high * 100) if (ce_high > 0 and ce_ltp > 0) else 0.0

            if (ce_chp >= min_gain_pct or ce_expansion_pct >= min_gain_pct) and ce_drop_pct >= drop_threshold_pct:
                ce_exhausted = True
                ce_exhaustion_msg = f"POST_EXPANSION_EXHAUSTION_BLOCK ({ce_name} gained +{int(max(ce_chp, ce_expansion_pct))}% to high {round(ce_high, 1)}, now dragging {round(ce_drop_pct, 1)}% below peak). Avoid buying post-expansion chop."

        if best_pe:
            pe_name = f"{int(best_pe.get('strike', 0))} PE"
            pe_ltp = float(best_pe.get("pe_ltp", 0.0) or 0.0)
            pe_chp = float(best_pe.get("pe_chp", 0.0) or 0.0)
            pe_high = float(best_pe.get("pe_high") or best_pe.get("high_price") or 0.0)
            pe_low = float(best_pe.get("pe_low") or best_pe.get("low_price") or 0.0)
            pe_sym = best_pe.get("pe_symbol")

            if (pe_high <= 0.0 or pe_chp == 0.0) and pe_sym:
                stats = get_option_contract_stats(pe_sym)
                if stats:
                    pe_high = float(stats.get("high_price", 0.0) or 0.0)
                    pe_low = float(stats.get("low_price", 0.0) or 0.0)
                    pe_chp = float(stats.get("chp", pe_chp) or pe_chp)

            pe_expansion_pct = ((pe_high - pe_low) / pe_low * 100) if (pe_high > 0 and pe_low > 0) else pe_chp
            pe_drop_pct = ((pe_high - pe_ltp) / pe_high * 100) if (pe_high > 0 and pe_ltp > 0) else 0.0

            if (pe_chp >= min_gain_pct or pe_expansion_pct >= min_gain_pct) and pe_drop_pct >= drop_threshold_pct:
                pe_exhausted = True
                pe_exhaustion_msg = f"POST_EXPANSION_EXHAUSTION_BLOCK ({pe_name} gained +{int(max(pe_chp, pe_expansion_pct))}% to high {round(pe_high, 1)}, now dragging {round(pe_drop_pct, 1)}% below peak). Avoid buying post-expansion chop."

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

    # 11. Trending OI (ATM +/- 5 strikes: 5 up, 5 down = 11 strikes total) matching Oi Pulse specification
    trending_oi = compute_trending_oi_metrics(enriched_chain, spot, step, atm_strike, num_strikes_each_side=5)

    # Build checklist & pillar flags
    def build_checklist_and_flags(is_ce: bool):
        is_dir = is_bullish_dir if is_ce else is_bearish_dir
        p_sig = price_signal == ("BULLISH" if is_ce else "BEARISH")
        m_sig = momentum_signal == ("BULLISH" if is_ce else "BEARISH")
        o_sig = oi_signal == ("BULLISH" if is_ce else "BEARISH")
        v_sig = volume_signal == ("BULLISH" if is_ce else "BEARISH")
        pcr_sig = pcr_signal == ("BULLISH" if is_ce else "BEARISH")
        iv_ok = bool(iv_safe and in_trade_window)

        # Oi Pulse Trending OI Conviction Rule: >= 40% diff with >= 2 strength dots aligned with bias
        t_oi_pct = trending_oi.get("diff_pct", 0)
        t_oi_dots = trending_oi.get("dots", 0)
        t_oi_confirmed = (t_oi_pct >= 40 and t_oi_dots >= 2) if is_ce else (t_oi_pct <= -40 and t_oi_dots >= 2)
        if t_oi_confirmed:
            t_oi_desc = f"{'+' if t_oi_pct > 0 else ''}{t_oi_pct}% ({t_oi_dots} dots) — Confirmed Trend (&ge;40%, 2+ dots)"
        elif abs(t_oi_pct) < 30:
            t_oi_desc = f"{'+' if t_oi_pct > 0 else ''}{t_oi_pct}% ({t_oi_dots} dots) — Grayed out / Noise zone (&lt;30%)"
        elif abs(t_oi_pct) < 40:
            t_oi_desc = f"{'+' if t_oi_pct > 0 else ''}{t_oi_pct}% ({t_oi_dots} dots) — Weak trend (&lt;40% threshold)"
        else:
            t_oi_desc = f"{'+' if t_oi_pct > 0 else ''}{t_oi_pct}% ({t_oi_dots} dots) — Opposing OI flow"

        pivot_50_ok = (spot >= pivot_50) if is_ce else (spot <= pivot_50) if has_valid_day_range else True
        pivot_50_desc = (
            f"Spot {spot:,.1f} {'>=' if is_ce else '<='} 50% Pivot {pivot_50:,.1f} [L: {today_low:,.0f} | H: {today_high:,.0f}]"
            if has_valid_day_range else "Day range unavailable"
        )

        chk = [
            {"name": "Price Direction", "passed": p_sig, "desc": f"Price is {price_signal}"},
            {"name": "Momentum", "passed": m_sig, "desc": f"Momentum is {momentum_signal}"},
            {"name": "OI Structure", "passed": o_sig, "desc": f"OI is {oi_signal}"},
            {"name": "50% Range Pivot", "passed": pivot_50_ok, "desc": pivot_50_desc},
            {"name": "Trending OI (&ge;40%, 2+ Dots)", "passed": t_oi_confirmed, "desc": t_oi_desc},
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
            "pivot_50": pivot_50_ok,
            "trending_oi": t_oi_confirmed,
            "iv_session": iv_ok,
        }
        score = 20 if p_sig else 0
        if m_sig:
            score += 20
        if o_sig:
            score += 20
        if v_sig:
            score += 20
        if pcr_sig:
            score += 20
        return score, chk, flags

    ce_score, ce_checklist, ce_pillar_flags = build_checklist_and_flags(is_ce=True)
    pe_score, pe_checklist, pe_pillar_flags = build_checklist_and_flags(is_ce=False)

    # Smart Breakout Detection (Score >= 80 and confirmed directional move)
    is_breakout_expansion = (
        (is_bullish_dir and ce_score >= 80 and spot_chg > 0.05) or
        (is_bearish_dir and pe_score >= 80 and spot_chg < -0.05)
    )

    # Breakout Extension: Extend cutoff up to 14:45 IST (02:45 PM) on valid high-conviction breakout
    # Cannot extend before 09:30 (opening chop)
    iv_mins = iv_time_info.get("total_mins")
    if iv_mins is None:
        now_dt = datetime.now(IST)
        iv_mins = now_dt.hour * 60 + now_dt.minute
    prime_start_mins = ENGINE_CONFIG.get("PRIME_WINDOW_START_MINS", 570)
    breakout_end_mins = ENGINE_CONFIG.get("BREAKOUT_WINDOW_END_MINS", 885)
    can_extend_breakout = is_breakout_expansion and (prime_start_mins <= iv_mins < breakout_end_mins)
    effective_trade_window = in_trade_window or can_extend_breakout


    # Smart Cooldown Reset on Valid Breakout:
    effective_loss_cooldown = in_loss_cooldown and not is_breakout_expansion

    if not is_live_data:
        decision = "WAIT"
        setup_score = max(ce_score, pe_score)
        active_checklist = ce_checklist if is_bullish_dir else pe_checklist
        active_trade_plan = (ce_trade_plan if is_bullish_dir else pe_trade_plan) or {}
        active_pillar_flags = ce_pillar_flags if is_bullish_dir else pe_pillar_flags
        focus_title = "WAIT"
        focus_reason = f"Data quality gate failed: FYERS LIVE market data required (current status: {data_status})."
    elif not effective_trade_window:
        decision = "WAIT"
        setup_score = max(ce_score, pe_score)
        active_checklist = ce_checklist if is_bullish_dir else pe_checklist
        active_trade_plan = (ce_trade_plan if is_bullish_dir else pe_trade_plan) or {}
        active_pillar_flags = ce_pillar_flags if is_bullish_dir else pe_pillar_flags
        focus_title = "WAIT"
        focus_reason = f"Outside entry window: {iv_time_info.get('time_status', '')}. No fresh buying."
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
    elif effective_loss_cooldown:
        decision = "WAIT"
        setup_score = max(ce_score, pe_score)
        active_checklist = ce_checklist if (is_bullish_dir or ce_score >= pe_score) else pe_checklist
        active_trade_plan = (ce_trade_plan if (is_bullish_dir or ce_score >= pe_score) else pe_trade_plan) or {}
        active_pillar_flags = ce_pillar_flags if (is_bullish_dir or ce_score >= pe_score) else pe_pillar_flags
        focus_title = "WAIT"
        focus_reason = loss_cooldown_reason or "CONSECUTIVE_LOSS_COOLDOWN"
    elif daily_trade_count >= max_daily_trades:
        decision = "WAIT"
        setup_score = max(ce_score, pe_score)
        active_checklist = ce_checklist if (is_bullish_dir or ce_score >= pe_score) else pe_checklist
        active_trade_plan = (ce_trade_plan if (is_bullish_dir or ce_score >= pe_score) else pe_trade_plan) or {}
        active_pillar_flags = ce_pillar_flags if (is_bullish_dir or ce_score >= pe_score) else pe_pillar_flags
        focus_title = "WAIT"
        focus_reason = "MAX_DAILY_TRADES_REACHED"
    elif is_bullish_dir:
        if ce_trade_plan is None:
            decision = "WAIT"
            setup_score = ce_score
            active_checklist = ce_checklist
            active_trade_plan = {}
            active_pillar_flags = ce_pillar_flags
            focus_title = "WAIT"
            focus_reason = "Option quality gate failed: CE contract quote missing, zero LTP, or bid/ask spread invalid."
        elif pivot_50_ce_blocked:
            decision = "WAIT"
            setup_score = ce_score
            active_checklist = ce_checklist
            active_trade_plan = ce_trade_plan or {}
            active_pillar_flags = ce_pillar_flags
            focus_title = "WAIT"
            focus_reason = pivot_50_msg
        elif round_ce_blocked:
            decision = "WAIT"
            setup_score = ce_score
            active_checklist = ce_checklist
            active_trade_plan = ce_trade_plan or {}
            active_pillar_flags = ce_pillar_flags
            focus_title = "WAIT"
            focus_reason = round_ce_msg
        elif ce_exhausted:
            decision = "WAIT"
            setup_score = ce_score
            active_checklist = ce_checklist
            active_trade_plan = ce_trade_plan or {}
            active_pillar_flags = ce_pillar_flags
            focus_title = "WAIT"
            focus_reason = ce_exhaustion_msg
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
            setup_type = "TRENDING_BREAKOUT"
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
        elif pivot_50_pe_blocked:
            decision = "WAIT"
            setup_score = pe_score
            active_checklist = pe_checklist
            active_trade_plan = pe_trade_plan or {}
            active_pillar_flags = pe_pillar_flags
            focus_title = "WAIT"
            focus_reason = pivot_50_msg
        elif round_pe_blocked:
            decision = "WAIT"
            setup_score = pe_score
            active_checklist = pe_checklist
            active_trade_plan = pe_trade_plan or {}
            active_pillar_flags = pe_pillar_flags
            focus_title = "WAIT"
            focus_reason = round_pe_msg
        elif pe_exhausted:
            decision = "WAIT"
            setup_score = pe_score
            active_checklist = pe_checklist
            active_trade_plan = pe_trade_plan or {}
            active_pillar_flags = pe_pillar_flags
            focus_title = "WAIT"
            focus_reason = pe_exhaustion_msg
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
            setup_type = "TRENDING_BREAKDOWN"
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
        if pivot_50_ce_blocked and (price_signal == "BULLISH" or ce_score >= pe_score):
            focus_reason = pivot_50_msg
        elif pivot_50_pe_blocked and (price_signal == "BEARISH" or pe_score > ce_score):
            focus_reason = pivot_50_msg
        elif is_choppy or not expansion_confirmed:
            focus_reason = "LOW_EXPANSION_OR_CHOP"
        elif price_signal == "BULLISH" and oi_signal != "BULLISH":
            if bull_confirm_count < 2:
                focus_reason = f"Bullish confirmation gate failed: Only {bull_confirm_count}/3 confirmations (OI, Volume, PCR) support Bullish direction."
            else:
                focus_reason = "Bullish confirmation gate failed: OI evidence is neutral, conflicting, or insufficient."
        elif price_signal == "BEARISH" and oi_signal != "BEARISH":
            if bear_confirm_count < 2:
                focus_reason = f"Bearish confirmation gate failed: Only {bear_confirm_count}/3 confirmations (OI, Volume, PCR) support Bearish direction."
            else:
                focus_reason = "Bearish confirmation gate failed: OI evidence is neutral, conflicting, or insufficient."
        elif price_signal == "BULLISH":
            focus_reason = f"Bullish confirmation gate failed: Only {bull_confirm_count}/3 confirmations (OI, Volume, PCR) support Bullish direction."
        elif price_signal == "BEARISH":
            focus_reason = f"Bearish confirmation gate failed: Only {bear_confirm_count}/3 confirmations (OI, Volume, PCR) support Bearish direction."
        else:
            focus_reason = "Market direction or momentum unclear. Preserve capital."


    # -------------------------------------------------------------
    # STRUCTURED STRATEGY LOGGING (Item 12: Explicit Strategy Telemetry)
    # -------------------------------------------------------------
    market_bias_str = "BULLISH" if is_bullish_dir else ("BEARISH" if is_bearish_dir else "NEUTRAL")
    confirm_display = f"{bull_confirm_count if is_bullish_dir else (bear_confirm_count if is_bearish_dir else max(bull_confirm_count, bear_confirm_count))}/3"

    expansion_str = "YES" if expansion_confirmed else "NO"
    window_str = "PASS" if (in_trade_window and iv_safe) else "FAIL"
    bidask_str = "VALID" if (active_trade_plan and active_trade_plan.get("bid_at_entry") is not None) else "INVALID/MISSING"
    spread_str = f"{active_trade_plan.get('spread_paid', 0.0)} pts ({active_trade_plan.get('bid_ask_spread_pct', 0.0)}%)" if active_trade_plan else "N/A"

    log.info(
        f"\n[OPTIONS DESK EVALUATION - {symbol}]\n"
        f"Price       : {price_signal}\n"
        f"Momentum    : {momentum_signal}\n"
        f"OI          : {oi_signal}\n"
        f"Volume      : {volume_signal}\n"
        f"PCR         : {pcr_signal}\n\n"
        f"Confirmation: {confirm_display}\n\n"
        f"Expansion   : {expansion_str}\n\n"
        f"Active Trade: {active_trade_str}\n"
        f"Window      : {window_str}\n"
        f"Bid/Ask     : {bidask_str}\n"
        f"Spread      : {spread_str}\n\n"
        f"Decision    : {decision}\n"
        f"Reason      : {focus_reason}\n"
    )

    # 11. OI Trend Focus & Trending OI (ATM +/- 5 strikes: 5 up, 5 down, 11 strikes total)
    trend_strikes = [r for r in enriched_chain if abs(r["strike"] - atm_strike) <= (step * 5)]
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
        "today_high": today_high,
        "today_low": today_low,
        "pivot_50": pivot_50,
        "spot_vs_pivot_50": "ABOVE" if spot >= pivot_50 else "BELOW",
        "range_half": "UPPER (CE Favored)" if spot >= pivot_50 else "LOWER (PE Favored)",

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

    tomorrow_bias = compute_tomorrow_trade_bias(
        market_data=market_data,
        spot=spot,
        step=step,
        pcr=pcr,
        bullish_score=bullish_score,
        bearish_score=bearish_score,
        r1=r1,
        s1=s1,
        trending_oi=trending_oi,
    )

    market_vector = compute_market_vector(
        bias=market_bias_str if market_bias_str in ("BULLISH", "BEARISH") else final_bias,
        bullish_score=bullish_score,
        bearish_score=bearish_score,
        pcr=pcr,
        spot=spot,
        atm=atm_strike,
        r1=r1,
        r2=r2,
        s1=s1,
        s2=s2,
        trending_oi=trending_oi,
    )

    oi_wall_metrics = compute_oi_wall_metrics(
        spot=spot,
        s1=s1,
        r1=r1,
        s2=s2,
        r2=r2,
        atm=atm_strike,
        strike_step=step,
    )

    return {
        "morning_bias": morning_bias,
        "tomorrow_bias": tomorrow_bias,
        "market_vector": market_vector,
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
            "pivot_50": pivot_50,
            "day_high": today_high,
            "day_low": today_low,
            "range_half": "UPPER (CE Favored)" if spot >= pivot_50 else "LOWER (PE Favored)",
            "spot_diff": spot_diff,
            "market_vector": market_vector,
            "is_available": oi_wall_metrics["is_available"],
            "position_pct": oi_wall_metrics["position_pct"],
            "room_ratio": oi_wall_metrics["room_ratio"],
            "status_chip": oi_wall_metrics["status_chip"],
            "status_text": oi_wall_metrics["status_text"],
            "dist_to_s1": oi_wall_metrics["dist_to_s1"],
            "dist_to_r1": oi_wall_metrics["dist_to_r1"],
            "atm_tick_pct": oi_wall_metrics["atm_tick_pct"],
            "nearest_barriers": nearest_barriers,
        },
        "nearest_barriers": nearest_barriers,
        "option_buying": {
            "decision": decision,  # BUY CE, BUY PE, WAIT
            "action": decision,
            "candidate_side": "CE" if "CE" in decision else ("PE" if "PE" in decision else None),
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
            "call_unwinding_strike": call_unwinding_strikes[0] if call_unwinding_strikes else None,
            "put_unwinding_strike": put_unwinding_strikes[0] if put_unwinding_strikes else None,
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
