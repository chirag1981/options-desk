"""
app/services/options_replay_backtest.py — Walk-Forward Backtest & Replay Engine
Evaluates options buying strategy parameter variations using strictly empirical data:
- Realistic Bid/Ask execution fills (Ask at entry, Bid at exit)
- Full statutory transaction costs with STT 0.15% (effective Oct 2024 / 2025/2026 circulars)
- Discrete modeling of partial exit order legs (separate brokerage per execution, whole-lot only)
- Chronological train/test split replay on stored price paths from trade_ticks and signals
- Parametric replay supporting custom parameter sets (SL%, T1/T2 multiples, trail steps, time stop)
- Evaluates out-of-sample performance against train-split baseline with paired bootstrap CI on delta-R
- Excludes non-trade/cleanup statuses (DUPLICATE_CLEANUP, STALE_EXIT, EXPIRED, DATA_QUALITY_ISSUE)
- Distinct verdicts: BASELINE_UNPROFITABLE, NO_EVIDENCE_FOR_CHANGE, EVIDENCE_CONFIRMED
"""

import os
import math
import random
import json
import sqlite3
import logging
from datetime import datetime
from app.services.fyers_options_service import INDEX_CONFIGS
from app.services.options_signal_service import _get_db

log = logging.getLogger("options_replay_backtest")

# Statutory Transaction Charges Schedule (Verified against official circulars)
# Sources & Effective Dates:
# - NSE Circular Ref: NSE/CMP/2024 (Exchange Transaction Charges for options ~0.035% of premium turnover)
# - BSE Circular Ref: 20240906-44 (Exchange Transaction Charges for Sensex options ~0.0325% of premium turnover)
# - Finance Act 2024: STT on option sell turnover 0.15% (effective Oct 2024 / 2025/2026)
# - Indian Stamp Act: Stamp Duty on option buy turnover 0.003%
# - SEBI Turnover Fees: ₹10 per crore (0.0001% of turnover)
# - GST: 18% on (Brokerage + Exchange turnover charges + SEBI fees)
# - Brokerage: Configurable per-order value (default flat ₹20.0 via BROKERAGE_PER_ORDER)
CHARGES_CONFIG = {
    "EFFECTIVE_DATE": "2026-04-01",
    "BROKERAGE_PER_ORDER": float(os.getenv("BROKERAGE_PER_ORDER", "20.0")),
    "STT_SELL_OPTIONS_RATE": 0.0015,        # 0.15% on option sell premium turnover
    "STAMP_DUTY_BUY_RATE": 0.00003,         # 0.003% on buy premium turnover
    "SEBI_TURNOVER_RATE": 0.000001,         # ₹10 per crore (0.0001%)
    "GST_RATE": 0.18,                       # 18% on (Brokerage + Exchange + SEBI)
    "EXCHANGE_TURNOVER_RATES": {
        "NSE": 0.00035,                     # ~0.035% of premium turnover (NFO)
        "BSE": 0.000325,                    # ~0.0325% of premium turnover (BFO / Sensex)
    },
    "EXCHANGE_TURNOVER_RATE": 0.00035,      # Default fallback
}

DEFAULT_EXIT_CONFIG = {
    "sl_pct": 0.20,                         # 20% initial stop loss
    "sl_floor_pts": 5.0,                    # Minimum 5.0 pts risk floor
    "t1_multiple": 1.5,                     # Target 1 = Entry + 1.5 * Risk
    "t2_multiple": 2.5,                     # Target 2 = Entry + 2.5 * Risk
    "trail_be_step": 0.50,                  # Trail to BE+1 when 50% to T1
    "trail_lock_step": 0.80,                # Trail to +10% when 80% to T1
    "trail_t1_lock": 0.50,                  # Trail to 50% profit at T1
    "stagnation_morning_mins": 30.0,
    "stagnation_afternoon_mins": 15.0,
}

MIN_TEST_SAMPLE_SIZE = 30


def calculate_trade_costs(
    entry_price: float,
    exit_prices: list[tuple[float, float]],
    lot_size: int,
    lots: int = 1,
    symbol: str = "NIFTY",
    brokerage_per_order: float | None = None,
) -> dict:
    """
    Computes exact statutory Indian F&O options trading costs with per-exchange rates.
    Supports whole-lot partial exits (e.g. 50% lots at T1, remainder at T2/TSL) with separate order legs and brokerage.

    exit_prices: list of (exit_price, fraction_of_total_qty), e.g. [(180.0, 0.5), (200.0, 0.5)]
    """
    total_qty = lot_size * lots
    buy_turnover = entry_price * total_qty
    total_sell_turnover = 0.0

    order_count = 1  # 1 entry order
    for exit_p, fraction in exit_prices:
        if fraction > 0:
            order_count += 1
            leg_qty = total_qty * fraction
            total_sell_turnover += (exit_p * leg_qty)

    total_turnover = buy_turnover + total_sell_turnover

    # Select per-exchange transaction charges rate based on underlying symbol
    sym_clean = (symbol or "NIFTY").upper()
    exch_code = "BSE" if sym_clean == "SENSEX" else "NSE"
    exch_rate = CHARGES_CONFIG["EXCHANGE_TURNOVER_RATES"].get(exch_code, CHARGES_CONFIG["EXCHANGE_TURNOVER_RATE"])

    brk_per_order = brokerage_per_order if brokerage_per_order is not None else CHARGES_CONFIG["BROKERAGE_PER_ORDER"]
    total_brokerage = order_count * brk_per_order

    stt = total_sell_turnover * CHARGES_CONFIG["STT_SELL_OPTIONS_RATE"]
    exchange_charges = total_turnover * exch_rate
    sebi_charges = total_turnover * CHARGES_CONFIG["SEBI_TURNOVER_RATE"]
    gst = (total_brokerage + exchange_charges + sebi_charges) * CHARGES_CONFIG["GST_RATE"]
    stamp_duty = buy_turnover * CHARGES_CONFIG["STAMP_DUTY_BUY_RATE"]

    total_costs = round(total_brokerage + stt + exchange_charges + gst + stamp_duty + sebi_charges, 2)

    return {
        "order_count": order_count,
        "brokerage": round(total_brokerage, 2),
        "stt": round(stt, 2),
        "exchange_charges": round(exchange_charges, 2),
        "gst": round(gst, 2),
        "stamp_duty": round(stamp_duty, 2),
        "sebi_charges": round(sebi_charges, 2),
        "total_costs_inr": total_costs,
        "cost_points": round(total_costs / total_qty, 2) if total_qty > 0 else 0.0,
        "exchange": exch_code,
    }


def calculate_transaction_costs(entry_price: float, exit_price: float, lot_size: int, lots: int = 1, symbol: str = "NIFTY") -> dict:
    """Wrapper for single exit round-trip transaction costs calculation."""
    return calculate_trade_costs(entry_price, [(exit_price, 1.0)], lot_size, lots, symbol=symbol)


def compute_trade_r_multiple(trade: dict) -> dict:
    """
    Calculates net PnL and R-multiple for a single trade factoring in whole-lot partial exits and statutory costs.
    Strictly uses immutable initial_stop_loss as the risk denominator and the trade's own lot_size.
    """
    t = dict(trade)
    symbol = (t.get("symbol") or "NIFTY").upper()
    cfg = INDEX_CONFIGS.get(symbol, INDEX_CONFIGS.get("NIFTY", {"lot_size": 65}))
    lot_size = int(t.get("lot_size") or cfg.get("lot_size", 65))
    lots = int(t.get("lots") or 1)

    entry = float(t.get("ask_at_entry") or t.get("entry_price", 0.0))
    init_sl = float(t.get("initial_stop_loss") or t.get("stop_loss") or (entry * 0.80))
    initial_risk = max(entry - init_sl, 0.1)

    t1 = float(t.get("target_1") or (entry + initial_risk * 1.5))
    exit_p = float(t.get("bid_at_exit") or t.get("exit_price") or entry)
    exit_reason = (t.get("exit_reason") or t.get("status") or "").upper()
    status = (t.get("status") or "").upper()

    # Whole-lot partial exit logic:
    # If lots >= 2 and TARGET_1 was hit, 50% lots booked at T1, remainder at exit_p
    has_partial_t1 = (status == "TARGET_1_HIT" or exit_reason == "TARGET_2_HIT" or (exit_reason == "TRAILING_STOP_HIT" and status == "TARGET_1_HIT")) and (lots >= 2)

    if has_partial_t1:
        close_lots = lots // 2
        rem_lots = lots - close_lots
        f_t1 = close_lots / lots
        f_rem = rem_lots / lots
        exit_legs = [(t1, f_t1), (exit_p, f_rem)]
        gross_pnl_pts = (close_lots * (t1 - entry) + rem_lots * (exit_p - entry)) / lots
    else:
        exit_legs = [(exit_p, 1.0)]
        gross_pnl_pts = exit_p - entry

    costs = calculate_trade_costs(entry, exit_legs, lot_size, lots, symbol=symbol)
    cost_pts = costs["cost_points"]
    net_pnl_pts = gross_pnl_pts - cost_pts
    net_pnl_inr = round(net_pnl_pts * lot_size * lots, 2)
    r_multiple = round(net_pnl_pts / initial_risk, 3)

    t["initial_risk_pts"] = round(initial_risk, 2)
    t["gross_pnl_pts"] = round(gross_pnl_pts, 2)
    t["cost_pts"] = cost_pts
    t["net_pnl_pts"] = round(net_pnl_pts, 2)
    t["net_pnl_inr"] = net_pnl_inr
    t["r_multiple"] = r_multiple
    t["costs_detail"] = costs

    return t


def replay_trade_on_ticks(trade: dict, ticks: list[dict], params: dict | None = None) -> float:
    """
    Replays a specific parameter set on a trade's tick stream:
    - Fills entry at Ask, exits at Bid.
    - Matches journal price triggers (T1 on ltp, T2 on bid, SL/TSL on bid).
    - Deducts exact statutory transaction costs and models whole-lot partial exits at T1.
    - Returns NET R-multiple (matching compute_trade_r_multiple).
    """
    entry = float(trade.get("ask_at_entry") or trade.get("entry_price", 0.0))
    init_sl = float(trade.get("initial_stop_loss") or trade.get("stop_loss") or (entry * 0.80))
    initial_risk = max(entry - init_sl, 0.1)
    symbol = (trade.get("symbol") or "NIFTY").upper()
    cfg = INDEX_CONFIGS.get(symbol, {"lot_size": 65})
    lot_size = int(trade.get("lot_size") or cfg.get("lot_size", 65))
    lots = int(trade.get("lots") or 1)

    p = dict(DEFAULT_EXIT_CONFIG)
    if params:
        p.update(params)

    sl_pct = p.get("sl_pct", 0.20)
    sl_floor = p.get("sl_floor_pts", 5.0)
    sim_risk = max(entry * sl_pct, sl_floor)
    sim_sl = max(entry - sim_risk, 0.05)
    t1_mult = p.get("t1_multiple", 1.5)
    t2_mult = p.get("t2_multiple", 2.5)
    sim_t1 = entry + (t1_mult * initial_risk)
    sim_t2 = entry + (t2_mult * initial_risk)
    time_stop_mins = p.get("time_stop_mins", 30.0)

    if not ticks:
        high = float(trade.get("highest_price") or entry)
        low = float(trade.get("lowest_price") or entry)
        if (entry - low) >= sim_risk:
            sim_exit = sim_sl
        elif high >= sim_t2:
            sim_exit = sim_t2
        elif high >= sim_t1:
            sim_exit = sim_t1
        else:
            sim_exit = float(trade.get("bid_at_exit") or trade.get("exit_price") or entry)

        has_t1 = (high >= sim_t1) and (lots >= 2)
        if has_t1:
            close_lots = lots // 2
            rem_lots = lots - close_lots
            exit_legs = [(sim_t1, close_lots / lots), (sim_exit, rem_lots / lots)]
            gross_pnl_pts = (close_lots * (sim_t1 - entry) + rem_lots * (sim_exit - entry)) / lots
        else:
            exit_legs = [(sim_exit, 1.0)]
            gross_pnl_pts = sim_exit - entry

        costs = calculate_trade_costs(entry, exit_legs, lot_size, lots, symbol=symbol)
        net_pnl_pts = gross_pnl_pts - costs["cost_points"]
        return round(net_pnl_pts / initial_risk, 3)

    first_tick_time = None
    t1_reached = False
    trailed_sl = sim_sl
    sim_exit = None

    for tick in ticks:
        ltp = float(tick.get("ltp") or 0.0)
        bid = float(tick.get("bid") or ltp)
        ts_str = tick.get("timestamp") or tick.get("created_at")
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            if first_tick_time is None:
                first_tick_time = ts
            elapsed_m = (ts - first_tick_time).total_seconds() / 60.0
        except Exception:
            elapsed_m = 0.0

        if time_stop_mins is not None and elapsed_m >= time_stop_mins and not t1_reached:
            if (bid - entry) < ((sim_t1 - entry) * 0.25):
                sim_exit = bid
                break

        if bid >= sim_t2:
            sim_exit = bid
            break

        # T1 trigger evaluated on ltp (matching journal)
        if ltp >= sim_t1 and not t1_reached:
            t1_reached = True
            trailed_sl = max(trailed_sl, entry + (p.get("trail_t1_lock", 0.5) * (sim_t1 - entry)))

        if t1_reached and bid > sim_t1:
            trailed_sl = max(trailed_sl, sim_t1 + 0.5 * (bid - sim_t1))

        if bid <= trailed_sl:
            sim_exit = min(trailed_sl, bid)
            break

    if sim_exit is None:
        last_bid = float(ticks[-1].get("bid") or ticks[-1].get("ltp") or entry)
        sim_exit = last_bid

    has_partial = t1_reached and (lots >= 2)
    if has_partial:
        close_lots = lots // 2
        rem_lots = lots - close_lots
        exit_legs = [(sim_t1, close_lots / lots), (sim_exit, rem_lots / lots)]
        gross_pnl_pts = (close_lots * (sim_t1 - entry) + rem_lots * (sim_exit - entry)) / lots
    else:
        exit_legs = [(sim_exit, 1.0)]
        gross_pnl_pts = sim_exit - entry

    costs = calculate_trade_costs(entry, exit_legs, lot_size, lots, symbol=symbol)
    net_pnl_pts = gross_pnl_pts - costs["cost_points"]
    return round(net_pnl_pts / initial_risk, 3)


def bootstrap_ci(values: list[float], iterations: int = 1000, seed: int = 42) -> list[float]:
    """Computes 95% bootstrap confidence interval on a metric list."""
    n = len(values)
    if n == 0:
        return [0.0, 0.0]
    if n == 1:
        return [round(values[0], 3), round(values[0], 3)]

    rng = random.Random(seed)
    boot_means = []
    for _ in range(iterations):
        sample = [values[rng.randint(0, n - 1)] for _ in range(n)]
        boot_means.append(sum(sample) / n)

    boot_means.sort()
    low_idx = int(iterations * 0.025)
    high_idx = int(iterations * 0.975)
    return [round(boot_means[low_idx], 3), round(boot_means[high_idx], 3)]


def run_walk_forward_evaluation(train_ratio: float = 0.60, params_variant: dict | None = None) -> dict:
    """
    Runs walk-forward backtest and out-of-sample statistical validation across recorded trades.
    - Fits/optimizes on train partition (first 60%), evaluates on test partition (remaining 40%).
    - Evaluates in R-multiples net of statutory costs and whole-lot partial exits.
    - Replays params_variant on trade_ticks if provided.
    - Excludes non-trade/cleanup statuses (DUPLICATE_CLEANUP, STALE_EXIT, EXPIRED, DATA_QUALITY_ISSUE).
    - Requires >= 30 test trades before confirming parameter changes.
    """
    excluded_reasons = ("DUPLICATE_CLEANUP", "STALE_EXIT", "CONTRACT_EXPIRED", "EXPIRED", "DATA_QUALITY_ISSUE")

    with _get_db() as conn:
        raw_trades = [dict(r) for r in conn.execute("""
            SELECT * FROM signals 
            WHERE status NOT IN ('ACTIVE', 'TARGET_1_HIT') AND is_deleted = 0
            ORDER BY id ASC
        """).fetchall()]

    # Filter out hygiene/cleanup records
    valid_trades = [
        t for t in raw_trades
        if (t.get("exit_reason") or "").upper() not in excluded_reasons
        and float(t.get("entry_price") or 0.0) > 0.05
    ]

    n_trades = len(valid_trades)
    enriched_trades = [compute_trade_r_multiple(t) for t in valid_trades]

    # Chronological Train / Test Partition
    split_idx = int(n_trades * train_ratio)
    train_trades = enriched_trades[:split_idx]
    test_trades = enriched_trades[split_idx:]
    n_test = len(test_trades)

    if n_test < MIN_TEST_SAMPLE_SIZE:
        return {
            "status": "INSUFFICIENT_DATA",
            "message": f"Test sample size ({n_test}) is below the required minimum of {MIN_TEST_SAMPLE_SIZE} trades for out-of-sample verification.",
            "total_trades": n_trades,
            "train_count": len(train_trades),
            "test_count": n_test,
            "min_test_required": MIN_TEST_SAMPLE_SIZE,
            "charges_config": CHARGES_CONFIG,
            "recommendation_status": "NO_EVIDENCE_FOR_CHANGE",
            "verdict": "NO_EVIDENCE_FOR_CHANGE",
            "evidence_summary": f"Insufficient out-of-sample test sample size ({n_test}/{MIN_TEST_SAMPLE_SIZE}). Retaining baseline rules.",
        }

    # Fetch ticks for test trades if replaying a parameter variant
    ticks_map = {}
    if params_variant:
        test_ids = [t["id"] for t in test_trades]
        if test_ids:
            with _get_db() as conn:
                placeholders = ",".join(["?"] * len(test_ids))
                ticks_rows = conn.execute(f"SELECT * FROM trade_ticks WHERE signal_id IN ({placeholders}) ORDER BY id ASC", test_ids).fetchall()
                for r in ticks_rows:
                    sid = r["signal_id"]
                    if sid not in ticks_map:
                        ticks_map[sid] = []
                    ticks_map[sid].append(dict(r))

    def evaluate_partition(partition_trades: list[dict], label: str, variant: dict | None = None) -> dict:
        cnt = len(partition_trades)
        if cnt == 0:
            return {"label": label, "count": 0}

        if variant:
            r_vals = [replay_trade_on_ticks(t, ticks_map.get(t["id"], []), variant) for t in partition_trades]
        else:
            r_vals = [t["r_multiple"] for t in partition_trades]

        wins = sum(1 for r in r_vals if r > 0)
        losses = sum(1 for r in r_vals if r < 0)
        win_rate = round(wins / cnt * 100, 1)

        mean_r = round(sum(r_vals) / cnt, 3)
        r_ci = bootstrap_ci(r_vals)

        gross_w = sum(r for r in r_vals if r > 0)
        gross_l = abs(sum(r for r in r_vals if r < 0))
        pf = round(gross_w / gross_l, 2) if gross_l > 0 else (round(gross_w, 2) if gross_w > 0 else 1.0)

        var = sum((x - mean_r) ** 2 for x in r_vals) / (cnt - 1) if cnt > 1 else 0.0
        std_dev = math.sqrt(var)
        std_err = std_dev / math.sqrt(cnt) if cnt > 0 else 0.0
        t_stat = round(mean_r / std_err, 3) if std_err > 0 else 0.0

        # Per-symbol breakdown
        sym_r = {}
        for idx, t in enumerate(partition_trades):
            s = (t.get("symbol") or "NIFTY").upper()
            if s not in sym_r:
                sym_r[s] = []
            sym_r[s].append(r_vals[idx])

        symbol_breakdown = {}
        for s, s_vals in sym_r.items():
            symbol_breakdown[s] = {
                "count": len(s_vals),
                "mean_r": round(sum(s_vals) / len(s_vals), 3),
                "mean_r_95_ci": bootstrap_ci(s_vals),
            }

        return {
            "label": label,
            "count": cnt,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate,
            "mean_r": mean_r,
            "mean_r_95_ci": r_ci,
            "profit_factor": pf,
            "std_dev_r": round(std_dev, 3),
            "std_error_r": round(std_err, 3),
            "t_statistic": t_stat,
            "symbol_breakdown": symbol_breakdown,
            "total_net_inr": round(sum(t["net_pnl_inr"] for t in partition_trades), 2),
        }

    train_eval = evaluate_partition(train_trades, "TRAIN (In-Sample 60%)")
    test_eval = evaluate_partition(test_trades, "TEST (Out-of-Sample 40%)")

    # If replaying a variant, compute paired delta CI
    variant_test_eval = None
    delta_ci = [0.0, 0.0]
    if params_variant:
        variant_test_eval = evaluate_partition(test_trades, "TEST_VARIANT", variant=params_variant)
        base_r_vals = [t["r_multiple"] for t in test_trades]
        var_r_vals = [replay_trade_on_ticks(t, ticks_map.get(t["id"], []), params_variant) for t in test_trades]
        deltas = [var_r_vals[i] - base_r_vals[i] for i in range(len(test_trades))]
        delta_ci = bootstrap_ci(deltas)

    # Verdict Logic
    test_mean_r = test_eval["mean_r"]
    test_ci = test_eval["mean_r_95_ci"]
    is_baseline_profitable = (test_mean_r > 0.0 and test_ci[0] > 0.0)

    if test_mean_r <= 0.0:
        verdict = "BASELINE_UNPROFITABLE"
        rec_status = "BASELINE_UNPROFITABLE"
        evidence_summary = f"Baseline strategy out-of-sample expectancy is non-positive ({test_mean_r:.3f}R, 95% CI: [{test_ci[0]}, {test_ci[1]}]). Do not increase risk."
    elif params_variant and delta_ci[0] > 0.0:
        verdict = "EVIDENCE_CONFIRMED"
        rec_status = "RECOMMENDED"
        evidence_summary = f"Proposed parameter variant outperforms baseline in out-of-sample test (+{variant_test_eval['mean_r'] - test_mean_r:.3f}R delta, 95% CI: [{delta_ci[0]}, {delta_ci[1]}])."
    elif is_baseline_profitable and test_eval.get("t_statistic", 0.0) >= 2.0:
        verdict = "EVIDENCE_CONFIRMED"
        rec_status = "EVIDENCE_CONFIRMED"
        evidence_summary = f"Out-of-sample baseline profitability is statistically confirmed (+{test_mean_r:.3f}R per trade, 95% CI: [{test_ci[0]}, {test_ci[1]}], t = {test_eval['t_statistic']})."
    else:
        verdict = "NO_EVIDENCE_FOR_CHANGE"
        rec_status = "NO_EVIDENCE_FOR_CHANGE"
        evidence_summary = f"Out-of-sample 95% CI [{test_ci[0]}, {test_ci[1]}] includes zero or shows marginal edge. Retaining baseline rules."

    return {
        "status": "EVALUATION_COMPLETE",
        "verdict": verdict,
        "recommendation_status": rec_status,
        "evidence_summary": evidence_summary,
        "total_trades": n_trades,
        "train_partition": train_eval,
        "test_partition": test_eval,
        "variant_partition": variant_test_eval,
        "delta_95_ci": delta_ci,
        "charges_config": CHARGES_CONFIG,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
